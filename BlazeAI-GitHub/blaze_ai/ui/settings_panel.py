"""Slide-in settings drawer: local model, identity, resources and voice."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings, app_data_dir
from ..core.device import device_profile
from ..core.stt import voice_dependencies
from ..core.system import SystemSnapshot, format_bytes
from ..theme import THEMES, theme_manager
from ..version import VERSION
from .components import (
    SegmentMeter,
    ThemeCard,
    divider,
    section_label,
)


class SettingsPanel(QWidget):
    close_requested = pyqtSignal()
    model_load_requested = pyqtSignal(str, str)
    model_remove_requested = pyqtSignal(str)
    agent_mode_changed = pyqtSignal(bool)
    agent_confirm_changed = pyqtSignal(bool)
    ram_limit_changed = pyqtSignal(int)
    theme_changed = pyqtSignal(str)
    name_changed = pyqtSignal(str)
    stt_model_changed = pyqtSignal(str)
    clear_history_requested = pyqtSignal()
    reveal_data_requested = pyqtSignal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(0)
        self.setMaximumWidth(0)
        self._settings = settings
        self._theme_cards: dict[str, ThemeCard] = {}
        self._voice_ready = all(voice_dependencies().values())
        self._hf_asr_ready = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget(self)
        header.setFixedHeight(64)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 0, 14, 0)
        title = QLabel("SETTINGS", header)
        title.setObjectName("SectionLabel")
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        close = QPushButton("×", header)
        close.setObjectName("WindowButton")
        close.setFixedSize(32, 30)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close_requested)
        header_layout.addWidget(close)
        root.addWidget(header)
        root.addWidget(divider(self))

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 18, 16, 26)
        layout.setSpacing(12)

        layout.addWidget(section_label("IDENTITY", content))
        self._name_edit = QLineEdit(content)
        self._name_edit.setPlaceholderText("What should Blaze call you?")
        self._name_edit.setMaxLength(40)
        self._name_edit.returnPressed.connect(self._save_name)
        self._name_edit.editingFinished.connect(self._save_name)
        layout.addWidget(self._name_edit)

        theme_grid = QGridLayout()
        theme_grid.setSpacing(7)
        for index, (key, theme) in enumerate(THEMES.items()):
            card = ThemeCard(theme, content)
            card.clicked.connect(lambda _checked=False, k=key: self._choose_theme(k))
            self._theme_cards[key] = card
            theme_grid.addWidget(card, index // 2, index % 2)
        layout.addLayout(theme_grid)
        layout.addSpacing(8)
        layout.addWidget(divider(content))
        layout.addSpacing(6)

        layout.addWidget(section_label("LOCAL MODEL · ADD FROM DISK", content))
        self._profile = device_profile()
        self._device_label = QLabel(
            f"THIS DEVICE · {self._profile.summary}\n{self._profile.target_label}",
            content,
        )
        self._device_label.setObjectName("TinyLabel")
        self._device_label.setWordWrap(True)
        layout.addWidget(self._device_label)
        self._key_status = QLabel("LOCAL BUTLER · NO MODEL LOADED", content)
        self._key_status.setObjectName("KeyStatus")
        self._key_status.setWordWrap(True)
        layout.addWidget(self._key_status)
        self._add_model_file_button = QPushButton("CHOOSE MODEL FILE…", content)
        self._add_model_file_button.setProperty("variant", "primary")
        self._add_model_file_button.clicked.connect(self._pick_model_file)
        layout.addWidget(self._add_model_file_button)
        self._gguf_notice = QLabel(
            "<b><u>ONLY .GGUF AND .SAFETENSORS FILES ARE SUPPORTED HERE</u></b>", content
        )
        self._gguf_notice.setObjectName("ModelNotice")
        self._gguf_notice.setWordWrap(True)
        layout.addWidget(self._gguf_notice)
        self._add_model_button = QPushButton("CHOOSE MODEL FOLDER…", content)
        self._add_model_button.clicked.connect(self._pick_model_folder)
        layout.addWidget(self._add_model_button)
        task_row = QHBoxLayout()
        task_row.setSpacing(6)
        task_label = QLabel("USE AS", content)
        task_label.setObjectName("TinyLabel")
        task_row.addWidget(task_label)
        self._manual_task = QComboBox(content)
        self._manual_task.setMinimumWidth(0)
        self._manual_task.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._manual_task.addItem("CHAT", "text-generation")
        self._manual_task.addItem("VOICE-TO-TEXT", "automatic-speech-recognition")
        task_row.addWidget(self._manual_task, 1)
        layout.addLayout(task_row)
        self._remove_model_button = QPushButton("REMOVE CURRENT MODEL", content)
        self._remove_model_button.clicked.connect(self._remove_current_model)
        layout.addWidget(self._remove_model_button)

        self._key_error = QLabel("", content)
        self._key_error.setObjectName("ErrorLabel")
        self._key_error.setWordWrap(True)
        self._key_error.hide()
        layout.addWidget(self._key_error)
        hint = QLabel(
            "Blaze loads local models only. Pick a single .gguf file for "
            "quantized 4-bit chat models (7B–12B on 16 GB) or a .safetensors "
            "file whose folder also has config.json and tokenizer files, or "
            "choose a Transformers folder with config.json + "
            ".safetensors/.bin weights. Oversized or incomplete models are "
            "rejected before loading. Each chat can pick a different model "
            "from its header; models load on demand.",
            content,
        )
        hint.setObjectName("TinyLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(8)
        layout.addWidget(divider(content))
        layout.addSpacing(6)

        layout.addWidget(section_label("COMPUTER CONTROL · AGENT", content))
        self._agent_mode = QCheckBox("Enable agent mode", content)
        self._agent_mode.toggled.connect(self._agent_mode_toggled)
        layout.addWidget(self._agent_mode)
        self._agent_confirm = QCheckBox("Ask before each action", content)
        self._agent_confirm.toggled.connect(self._agent_confirm_toggled)
        layout.addWidget(self._agent_confirm)
        agent_note = QLabel(
            "With agent mode on, anything that is not a built-in butler command is "
            "handled by the model selected for this chat as a real computer task: shell, "
            "AppleScript, Mail, Messages, keyboard, mouse, clipboard, bundled ffmpeg "
            "video editing, and files. Risky actions are confirmed first; destructive "
            "system commands are always blocked. Press Esc to stop a running task.",
            content,
        )
        agent_note.setObjectName("TinyLabel")
        agent_note.setWordWrap(True)
        layout.addWidget(agent_note)
        layout.addSpacing(8)
        layout.addWidget(divider(content))
        layout.addSpacing(6)

        layout.addWidget(section_label("RESOURCES · LIVE PROBE", content))
        total_row = QHBoxLayout()
        total_label = QLabel("TOTAL SYSTEM RAM", content)
        total_label.setObjectName("TinyLabel")
        self._total_ram = QLabel("—", content)
        self._total_ram.setObjectName("TitleLabel")
        total_row.addWidget(total_label)
        total_row.addStretch(1)
        total_row.addWidget(self._total_ram)
        layout.addLayout(total_row)

        self._ram_meter = SegmentMeter(28, 9, content)
        layout.addWidget(self._ram_meter)
        self._process_ram = QLabel("BLAZE PROCESS  —", content)
        self._process_ram.setObjectName("MonoLabel")
        self._system_ram = QLabel("SYSTEM LOAD  —", content)
        self._system_ram.setObjectName("MonoLabel")
        layout.addWidget(self._process_ram)
        layout.addWidget(self._system_ram)

        limit_row = QHBoxLayout()
        limit_label = QLabel("RESOURCE LIMITER", content)
        limit_label.setObjectName("TinyLabel")
        self._limit_value = QLabel("65%", content)
        self._limit_value.setObjectName("MonoLabel")
        limit_row.addWidget(limit_label)
        limit_row.addStretch(1)
        limit_row.addWidget(self._limit_value)
        layout.addLayout(limit_row)
        self._limit_slider = QSlider(Qt.Orientation.Horizontal, content)
        self._limit_slider.setRange(20, 95)
        self._limit_slider.setSingleStep(1)
        self._limit_slider.setPageStep(5)
        self._limit_slider.valueChanged.connect(self._limit_dragged)
        self._limit_slider.sliderReleased.connect(self._commit_limit)
        layout.addWidget(self._limit_slider)
        self._budget_label = QLabel("SOFT CAP  —", content)
        self._budget_label.setObjectName("TinyLabel")
        layout.addWidget(self._budget_label)
        limiter_note = QLabel(
            "A soft cap releases caches and defers heavy local models when Blaze "
            "crosses the budget. It never hard-kills the UI.",
            content,
        )
        limiter_note.setObjectName("TinyLabel")
        limiter_note.setWordWrap(True)
        layout.addWidget(limiter_note)
        layout.addSpacing(8)
        layout.addWidget(divider(content))
        layout.addSpacing(6)

        layout.addWidget(section_label("VOICE · PUSH-TO-TALK", content))
        self._voice_status = QLabel("CHECKING DEPENDENCIES…", content)
        self._voice_status.setObjectName("KeyStatus")
        layout.addWidget(self._voice_status)
        self._stt_model = QComboBox(content)
        for label, value in (
            ("TINY.EN · fastest / lowest RAM", "tiny.en"),
            ("BASE.EN · balanced (default)", "base.en"),
            ("SMALL.EN · more accurate", "small.en"),
            ("MEDIUM.EN · highest accuracy", "medium.en"),
        ):
            self._stt_model.addItem(label, value)
        self._stt_model.currentIndexChanged.connect(self._stt_changed)
        layout.addWidget(self._stt_model)
        voice_note = QLabel(
            "Press the mic button or Ctrl+Shift+Space anywhere while Blaze runs to start listening; use the "
            "shortcut again to stop and send. No passive listening. faster-whisper runs locally.",
            content,
        )
        voice_note.setObjectName("TinyLabel")
        voice_note.setWordWrap(True)
        layout.addWidget(voice_note)
        if not self._voice_ready:
            missing = [
                name
                for name, ready in voice_dependencies().items()
                if not ready
            ]
            install = QLabel(
                "Missing: "
                + ", ".join(missing)
                + "\nInstall voice extras:\npip install faster-whisper sounddevice numpy",
                content,
            )
            install.setObjectName("ErrorLabel")
            install.setWordWrap(True)
            layout.addWidget(install)
        layout.addSpacing(8)
        layout.addWidget(divider(content))
        layout.addSpacing(6)

        layout.addWidget(section_label("DATA & ABOUT", content))
        data_row = QVBoxLayout()
        open_data = QPushButton("OPEN DATA FOLDER", content)
        open_data.setCursor(Qt.CursorShape.PointingHandCursor)
        open_data.clicked.connect(self.reveal_data_requested)
        data_row.addWidget(open_data, 1)
        clear = QPushButton("CLEAR HISTORY", content)
        clear.setProperty("variant", "danger")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(self.clear_history_requested)
        data_row.addWidget(clear)
        layout.addLayout(data_row)
        about = QLabel(
            f"BLAZE AI  ·  v{VERSION}\n100% Python · local-first · Qt 6\n"
            f"{app_data_dir()}",
            content,
        )
        about.setObjectName("TinyLabel")
        about.setWordWrap(True)
        layout.addWidget(about)
        layout.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self.refresh(settings)

    def refresh(self, settings: Settings) -> None:
        self._settings = settings
        blocked = [
            self._name_edit,
            self._limit_slider,
            self._stt_model,
            self._agent_mode,
            self._agent_confirm,
        ]
        for widget in blocked:
            widget.blockSignals(True)
        self._name_edit.setText(settings.get("name", ""))
        self._agent_mode.setChecked(bool(settings.get("agent_mode", True)))
        self._agent_confirm.setChecked(bool(settings.get("agent_confirm", True)))
        limit = int(settings.get("ram_limit_percent", 65))
        self._limit_slider.setValue(limit)
        self._limit_value.setText(f"{limit}%")
        stt_index = self._stt_model.findData(settings.get("stt_model", "base.en"))
        self._stt_model.setCurrentIndex(max(0, stt_index))
        for widget in blocked:
            widget.blockSignals(False)
        for key, card in self._theme_cards.items():
            card.setChecked(key == settings.get("theme", "onyx"))
        self._key_error.hide()
        self._update_voice_status()
        self._update_model_status()

    def set_model_status(self, label: str, *, active: bool, has_key: bool) -> None:
        self._key_status.setText(label.upper())
        self._key_status.setProperty("active", "true" if active else "false")
        self._key_status.style().unpolish(self._key_status)
        self._key_status.style().polish(self._key_status)
        self._remove_model_button.setEnabled(has_key)
        self._key_error.hide()

    def set_model_error(self, message: str) -> None:
        self._key_error.setText(message)
        self._key_error.setVisible(bool(message))

    def update_resources(self, snapshot: SystemSnapshot, limit_percent: int) -> None:
        self._last_snapshot = snapshot
        fraction = snapshot.process_ram / snapshot.total_ram if snapshot.total_ram else 0.0
        hot = snapshot.process_ram > snapshot.total_ram * (limit_percent / 100.0)
        self._total_ram.setText(format_bytes(snapshot.total_ram))
        self._process_ram.setText(
            f"BLAZE PROCESS  {format_bytes(snapshot.process_ram)}  ·  {fraction * 100:.1f}%"
        )
        self._system_ram.setText(
            f"SYSTEM LOAD  {snapshot.system_percent:.0f}%  ·  "
            f"{format_bytes(snapshot.used_ram)} USED"
        )
        self._ram_meter.set_value(fraction, hot=hot)
        self._budget_label.setText(
            f"SOFT CAP  {limit_percent}%  ·  {format_bytes(snapshot.total_ram * limit_percent / 100)}"
        )
        if self._limit_slider.value() != limit_percent:
            self._limit_slider.blockSignals(True)
            self._limit_slider.setValue(limit_percent)
            self._limit_value.setText(f"{limit_percent}%")
            self._limit_slider.blockSignals(False)
        self._process_ram.setProperty("style", "hot" if hot else "")
        self._process_ram.style().unpolish(self._process_ram)
        self._process_ram.style().polish(self._process_ram)

    def set_voice_state(self, state: str) -> None:
        if state in {"recording", "transcribing", "loading"}:
            label = {
                "recording": "● VOICE · LISTENING",
                "transcribing": "… VOICE · TRANSCRIBING",
                "loading": "… VOICE · LOADING MODEL",
            }[state]
            self._voice_status.setText(label)
            self._voice_status.setProperty("active", "true")
        else:
            self._update_voice_status()
            self._voice_status.setProperty("active", "false")
        self._voice_status.style().unpolish(self._voice_status)
        self._voice_status.style().polish(self._voice_status)

    def _update_voice_status(self) -> None:
        if self._hf_asr_ready:
            self._voice_status.setText("VOICE READY · HUGGING FACE SPEECH MODEL")
        elif self._voice_ready:
            self._voice_status.setText("VOICE READY · FASTER-WHISPER LOCAL")
        else:
            self._voice_status.setText("VOICE ENGINE OFFLINE · DEPENDENCIES MISSING")
        self._voice_status.setProperty("active", "true" if self._voice_ready or self._hf_asr_ready else "false")

    def set_voice_status(self, text: str) -> None:
        self._hf_asr_ready = text.startswith("VOICE READY · HUGGING FACE")
        self._voice_status.setText(text)
        self._voice_status.setProperty("active", "true" if self._hf_asr_ready else "false")
        self._voice_status.style().unpolish(self._voice_status)
        self._voice_status.style().polish(self._voice_status)

    def _update_model_status(self) -> None:
        repo = self._settings.get("hf_model", "") or self._settings.get("hf_voice_model", "")
        downloaded = self._settings.get("hf_downloaded_models", [])
        if not repo and downloaded:
            repo = downloaded[-1]
            label = f"LOCAL MODEL · {repo}"
        elif self._settings.get("hf_voice_model", ""):
            label = f"VOICE MODEL · {repo}"
        else:
            label = f"LOCAL MODEL · {repo}" if repo else "LOCAL BUTLER · NO MODEL LOADED"
        self.set_model_status(
            label,
            active=False,
            has_key=bool(repo or downloaded),
        )

    def _choose_theme(self, key: str) -> None:
        for card_key, card in self._theme_cards.items():
            card.setChecked(card_key == key)
            card.update()
        theme_manager.set_theme(key)
        self.theme_changed.emit(key)

    def _pick_model_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a .gguf or .safetensors model file",
            str(Path.home()),
            "Model files (*.gguf *.safetensors);;GGUF models (*.gguf);;"
            "Safetensors models (*.safetensors);;All files (*)",
        )
        if not file_path:
            return
        chosen = Path(file_path)
        if chosen.suffix.lower() not in {".gguf", ".safetensors"}:
            self.set_model_error(
                "Unsupported model file: choose a .gguf or .safetensors file."
            )
            return
        self._key_error.hide()
        self._key_status.setText(f"CHECKING · {chosen.name}")
        # Both formats are chat models; speech still needs a folder.
        self._manual_task.setCurrentIndex(0)
        self.model_load_requested.emit(str(chosen), "text-generation")

    def _pick_model_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose a local model folder",
            str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not folder:
            return
        chosen = Path(folder)
        self._key_error.hide()
        task = str(self._manual_task.currentData() or "text-generation")
        self._key_status.setText(f"CHECKING · {chosen.name}")
        self.model_load_requested.emit(str(chosen), task)

    def _remove_current_model(self) -> None:
        repo = (
            self._settings.get("hf_model", "")
            or self._settings.get("hf_voice_model", "")
        )
        if not repo:
            self.set_model_error("No local model is loaded.")
            return
        self.model_remove_requested.emit(str(repo))

    def _save_name(self) -> None:
        name = self._name_edit.text().strip()
        if name and name != self._settings.get("name", ""):
            self.name_changed.emit(name)

    def _agent_mode_toggled(self, enabled: bool) -> None:
        self._settings.set("agent_mode", bool(enabled))
        self.agent_mode_changed.emit(bool(enabled))

    def _agent_confirm_toggled(self, enabled: bool) -> None:
        self._settings.set("agent_confirm", bool(enabled))
        self.agent_confirm_changed.emit(bool(enabled))

    def _limit_dragged(self, value: int) -> None:
        self._limit_value.setText(f"{value}%")
        snapshot = getattr(self, "_last_snapshot", None)
        if snapshot:
            self._budget_label.setText(
                f"SOFT CAP  {value}%  ·  {format_bytes(snapshot.total_ram * value / 100)}"
            )

    def _commit_limit(self) -> None:
        self.ram_limit_changed.emit(self._limit_slider.value())

    def _stt_changed(self) -> None:
        value = str(self._stt_model.currentData() or "base.en")
        self.stt_model_changed.emit(value)
