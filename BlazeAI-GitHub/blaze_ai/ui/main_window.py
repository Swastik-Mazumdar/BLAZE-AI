"""Blaze AI main window, controller wiring and local/online routing."""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QCloseEvent, QIcon, QKeySequence, QMouseEvent, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings, app_data_dir, assets_dir
from ..core.agent import (
    CONFIRM_EACH_ACTION_TOOLS,
    AgentAction,
    AgentStep,
    ComputerAgent,
    extract_final_text,
)
from ..core.app_finder import AppFinder, clipboard_url, extract_first_url
from ..core.butler import ButlerReply, OfflineButler
from ..core.chat_store import MAX_MESSAGE_CHARS, ChatStore, Message
from ..core.command_filter import Command, match_command
from ..core.local_model import (
    CHAT_SYSTEM_PROMPT,
    HuggingFaceModel,
    clean_model_reply,
    model_path,
)
from ..core.screen_recorder import RecordingTicket, ScreenRecorder
from ..core.stt import Transcript, VoiceEngine, voice_capture_ready, voice_ready
from ..core.system import ResourceGovernor, SystemController, SystemSnapshot, format_bytes
from ..core.tts import speak_text
from ..core.typing_service import TypingService
from ..theme import theme_manager
from ..version import APP_NAME, TAGLINE, VERSION
from .chat import ChatView
from .components import LogoMark, Toast, VoiceOrb
from .onboarding import OnboardingDialog
from .settings_panel import SettingsPanel
from .sidebar import Sidebar


class LLMWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, model: HuggingFaceModel, history: list[dict[str, str]], name: str) -> None:
        super().__init__()
        self._model = model
        self._history = history
        self._name = name
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def run(self) -> None:
        try:
            text = self._model.respond(
                self._history, self._name, should_stop=lambda: self.cancelled
            )
            if self.cancelled:
                self.completed.emit(type("LocalResponse", (), {"text": "", "provider_label": "STOPPED"})())
                return
            self.completed.emit(type("LocalResponse", (), {"text": text, "provider_label": "HF LOCAL"})())
        except Exception as exc:
            if self.cancelled:
                self.completed.emit(type("LocalResponse", (), {"text": "", "provider_label": "STOPPED"})())
            else:
                self.failed.emit(str(exc))


class AgentConfirmation(QObject):
    """Blocking bridge between the agent thread and a Qt confirmation dialog.

    The agent thread calls the instance like a function; the dialog is shown on
    the main thread and the call returns once the user chooses.
    """

    requested = pyqtSignal(object)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self._event = threading.Event()
        self._allow = False
        self.always = False
        self.requested.connect(self._ask, Qt.ConnectionType.QueuedConnection)

    def __call__(self, action: AgentAction) -> bool:
        self._event.clear()
        self._allow = False
        self.requested.emit(action)
        if not self._event.wait(timeout=600):
            return False
        return self._allow

    def _ask(self, action: AgentAction) -> None:
        delay = False
        try:
            if self._parent.isMinimized():
                self._parent.showNormal()
            box = QMessageBox(self._parent)
            box.setWindowTitle("Blaze AI · agent action")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(f"Blaze wants to run: {action.tool.upper()}")
            box.setInformativeText(action.description)
            run_once = box.addButton("RUN ONCE", QMessageBox.ButtonRole.AcceptRole)
            allow_all = None
            if action.tool not in CONFIRM_EACH_ACTION_TOOLS:
                allow_all = box.addButton(
                    "ALLOW LOW-RISK ACTIONS", QMessageBox.ButtonRole.ActionRole
                )
            skip = box.addButton("SKIP", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(skip)
            box.exec()
            clicked = box.clickedButton()
            self._allow = clicked is run_once or clicked is allow_all
            if clicked is allow_all:
                self.always = True
            if self._allow and action.tool in {"type_text", "press_keys", "click"}:
                # The dialog took focus; get Blaze out of the way so the action
                # lands in the app the agent is controlling.
                self._parent.showMinimized()
                delay = True
        except Exception:
            self._allow = False
        finally:
            if delay:
                QTimer.singleShot(380, self._event.set)
            else:
                self._event.set()


class AgentWorker(QThread):
    step = pyqtSignal(object)
    input_started = pyqtSignal()
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        model: HuggingFaceModel,
        task: str,
        user_name: str,
        history: list[dict[str, str]],
        *,
        confirm: AgentConfirmation | None,
        typing,
        max_steps: int,
    ) -> None:
        super().__init__()
        self._task = task
        self._history = history
        self._agent = ComputerAgent(
            model,
            user_name=user_name,
            confirm=confirm,
            typing=typing,
            before_input=self._pause_for_input,
            max_steps=max_steps,
        )

    def _pause_for_input(self, _action: AgentAction) -> None:
        self.input_started.emit()
        time.sleep(0.6)

    def run(self) -> None:
        try:
            result = self._agent.run(self._task, context=self._history, on_step=self.step.emit)
            self.completed.emit(result)
        except Exception as exc:  # noqa: BLE001 - surfaced in the chat
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def cancel(self) -> None:
        self._agent.cancelled = True


class ModelLoadWorker(QThread):
    loaded = pyqtSignal(str, str, str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        model: HuggingFaceModel,
        folder: str,
        task: str,
        ram_limit_percent: int = 90,
    ) -> None:
        super().__init__()
        self.model = model
        self.folder = folder
        self.task = task
        self.ram_limit_percent = ram_limit_percent

    def run(self) -> None:
        try:
            mode = self.model.load_folder(
                self.folder,
                self.task,
                ram_limit_percent=self.ram_limit_percent,
            )
            self.loaded.emit(self.folder, self.task, mode)
        except Exception as exc:
            self.failed.emit(str(exc))


class HotkeyBridge(QObject):
    activated = pyqtSignal()


# Only multi-step markers are checked. Task verbs ("edit a video", "draft an
# email") never match the simple command grammar in the first place, so they
# already reach the agent. Limiting this list keeps deterministic butler
# commands like "open Messages" or "type this text" from being hijacked.
_AGENT_HINTS = (
    " and ",
    " then ",
    " afterwards ",
    " after that ",
    " followed by ",
    " for me ",
)


def _should_route_to_agent(text: str) -> bool:
    lowered = f" {text.strip().lower()} "
    if len(lowered) > 130:
        return True
    return any(hint in lowered for hint in _AGENT_HINTS)


def _model_display_name(path: str) -> str:
    """Readable label for a model folder or a single model file."""
    item = Path(str(path))
    if item.suffix.lower() in {".gguf", ".safetensors"}:
        return item.stem
    return item.name


_QUESTION_START = re.compile(
    r"^\s*(?:"
    r"what|who|whom|whose|when|where|why|how|which|"
    r"is|are|was|were|do|does|did|"
    r"tell\s+me|explain|define|describe|summari[sz]e|translate|"
    r"compare|meaning\s+of|difference\s+between"
    r")\b",
    re.IGNORECASE,
)
_INFORMATIONAL_INTENT = re.compile(
    r"^\s*(?:search|google|look\s+up|lookup|find\s+out|tell\s+me)\b",
    re.IGNORECASE,
)
_OPERATIONAL_TERMS = re.compile(
    r"\b(?:"
    r"file|files|folder|folders|directory|directories|"
    r"app|apps|application|applications|window|windows|screen|screenshot|"
    r"system|computer|machine|desktop|clipboard|"
    r"email|e-mail|mail|message|messages|text|video|audio|image|photo|"
    r"code|script|terminal|shell|battery|ram|memory|disk|storage|"
    r"download|install|open|close|launch|quit|edit|convert|extract|"
    r"export|import|rename|move|copy|delete|create|save|send|reply|"
    r"record|click|type|press|schedule|remind|trash|kill|process"
    r")\b",
    re.IGNORECASE,
)
_SELF_QUESTION = re.compile(
    r"^\s*(?:who\s+are\s+you|what\s+are\s+you|what\s+is\s+your\s+name|"
    r"who\s+is\s+blaze|what\s+can\s+you\s+do|how\s+are\s+you|"
    r"tell\s+me\s+about\s+yourself)\b",
    re.IGNORECASE,
)
_SMALL_TALK = re.compile(
    r"^\s*(?:"
    r"hi|hello|hey|yo|howdy|greetings|sup|"
    r"good\s+(?:morning|afternoon|evening)|"
    r"thanks|thank\s+you|cheers|"
    r"ok(?:ay)?|got\s+it|nice|cool"
    r")"
    r"(?:\s+(?:there|blaze|ai|friend))?"
    r"\s*[!.…]*$",
    re.IGNORECASE,
)


_CODING_TASK = re.compile(
    r"\b(?:"
    r"write|writ(?:e|ing)\s+code|code|coding|program|programming|script|scripts|"
    r"function|functions|class|classes|method|methods|bug|bugs|debug|debugging|"
    r"fix|fixing|refactor|refactoring|implement|implementation|compile|compiler|"
    r"algorithm|algorithms|variable|variables|loop|loops|api|apis|endpoint|"
    r"database|sql|query|queries|regex|regexp|python|javascript|typescript|"
    r"java\b|rust\b|golang|ruby|swift|kotlin|c\+\+|c#|php\b|html|css|json|yaml|"
    r"react|node|django|flask|express|next\.?js|vue|angular|"
    r"git\b|repo|repository|pull request|commit|syntax|stack\s*trace|exception|"
    r"unit test|pytest|unittest|npm|pip\b|module|package|library|framework|"
    r"web\s*app|website|frontend|backend|full\s*stack"
    r")\b",
    re.IGNORECASE,
)
_LITERAL_TYPE = re.compile(r"""^\s*(?:["'`]).*(?:["'`])\s*$""")
_CREATE_TASK = re.compile(
    r"\b(?:build|create|make|develop|design|generate|scaffold|spin\s+up)\b"
    r".*\b(?:app|application|website|site|page|script|program|function|class|"
    r"component|module|service|server|bot|tool|project|game|calculator|form|api)\b",
    re.IGNORECASE,
)


def _command_conflicts_with_ai_task(text: str, command: Command) -> bool:
    """True when a local command likely stole a coding/AI request."""
    raw = (text or "").strip()
    if not raw or command is None:
        return False
    lowered = raw.lower()

    if command.kind == "type_text":
        arg = (command.argument or "").strip()
        if not arg or _LITERAL_TYPE.match(arg):
            return False
        # Literal injection like "type hello" stays local; coding requests go to AI.
        if _CODING_TASK.search(arg) or _CODING_TASK.search(raw) or _CREATE_TASK.search(arg):
            return True
        if "?" in arg or len(arg.split()) > 8:
            return True
        return False

    if command.kind in {"open_app", "close_app"}:
        arg = (command.argument or "").strip().lower()
        # Protect real dev-tool app names, unless the user asked to do work in them.
        if re.search(
            r"\b(?:vs\s*code|visual\s+studio\s*code|xcode|android\s+studio|"
            r"pycharm|webstorm|intellij|idea|goland|rider|clion|datagrip|"
            r"sublime\s*text|textmate|nova|bbedit|"
            r"terminal|iterm|warp|kitty|alacritty|hyper|"
            r"github\s*desktop|docker\s*desktop|postman|insomnia|figma|cursor|zed)\b",
            lowered,
        ):
            if not re.search(
                r"\b(?:and\s+(?:fix|write|code|debug|create|build|edit|refactor|implement|make|run)|"
                r"to\s+(?:fix|write|code|debug|create|build|edit|refactor|implement|make|run)|"
                r"then\b)\b",
                lowered,
            ):
                return False
        if re.search(
            r"\b(?:coding|programming|scripting|debugging|developing|refactoring)\b", arg
        ):
            return True
        if _CREATE_TASK.search(raw) and not re.search(
            r"\b(?:settings|preferences|finder|safari|chrome|brave|firefox|mail|messages|notes|calendar|photos|music|spotify|slack|discord|zoom|teams)\b",
            lowered,
        ):
            return True
        if re.match(r"^\s*(?:start|run)\b", lowered) and (
            _CODING_TASK.search(raw) or _CREATE_TASK.search(raw)
        ):
            # "run Chrome" / "start Safari" are real launches; task phrasing means AI.
            if re.search(
                r"\b(?:a|an|the|my|this|some)\s+"
                r"(?:script|program|function|class|app|application|website|server|bot|project|code|test)",
                lowered,
            ) or re.search(r"\b(?:coding|programming|script|python|node|code)\b", lowered):
                return True
        if _CODING_TASK.search(arg):
            # A real app name rarely *is* "python script"/"function"; treat as task.
            if re.search(
                r"\b(?:script|function|class|program|code|bug|project|module|server|api|database)\b",
                arg,
            ):
                return True
        return False

    return False


def _is_conversational(text: str) -> bool:
    """True when a message is a question, greeting or small talk."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _SMALL_TALK.match(stripped):
        return True
    if _SELF_QUESTION.match(stripped):
        return True
    if _INFORMATIONAL_INTENT.match(stripped):
        rest = re.sub(
            r"^\s*(?:search(?:\s+for)?|google|look\s+up|lookup|find\s+out)\s+",
            "",
            stripped,
            flags=re.IGNORECASE,
        ).strip()
        if rest and not _OPERATIONAL_TERMS.search(rest):
            return True
    return bool(_QUESTION_START.match(stripped)) and not _OPERATIONAL_TERMS.search(
        stripped
    )


class VoiceReplyWorker(QThread):
    finished_speaking = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            speak_text(self.text, should_stop=self._cancelled.is_set)
            self.finished_speaking.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class TopBar(QWidget):
    minimize_requested = pyqtSignal()
    maximize_requested = pyqtSignal()
    close_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(66)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 10, 0)
        layout.setSpacing(10)
        layout.addWidget(LogoMark(28))
        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel(APP_NAME.upper(), self)
        name.setObjectName("TitleLabel")
        subtitle = QLabel(TAGLINE, self)
        subtitle.setObjectName("TinyLabel")
        brand.addWidget(name)
        brand.addWidget(subtitle)
        layout.addLayout(brand)
        layout.addSpacing(12)
        line = QFrame(self)
        line.setObjectName("VerticalDivider")
        line.setFixedWidth(1)
        line.setFixedHeight(24)
        layout.addWidget(line)
        layout.addSpacing(4)

        self._mode = QLabel("LOCAL BUTLER · OFFLINE", self)
        self._mode.setObjectName("StatusPill")
        self._mode.setProperty("active", "false")
        layout.addWidget(self._mode)
        layout.addStretch(1)
        self._ram = QLabel("RAM  —", self)
        self._ram.setObjectName("TinyLabel")
        layout.addWidget(self._ram)
        layout.addSpacing(12)

        self._min = QPushButton("−", self)
        self._min.setObjectName("WindowButton")
        self._min.setFixedSize(32, 30)
        self._min.setCursor(Qt.CursorShape.PointingHandCursor)
        self._min.clicked.connect(self.minimize_requested)
        layout.addWidget(self._min)
        self._max = QPushButton("□", self)
        self._max.setObjectName("WindowButton")
        self._max.setFixedSize(32, 30)
        self._max.setCursor(Qt.CursorShape.PointingHandCursor)
        self._max.clicked.connect(self.maximize_requested)
        layout.addWidget(self._max)
        self._close = QPushButton("×", self)
        self._close.setObjectName("CloseButton")
        self._close.setFixedSize(32, 30)
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.clicked.connect(self.close_requested)
        layout.addWidget(self._close)

    def set_mode(self, label: str, *, active: bool) -> None:
        self._mode.setText(label.upper())
        self._mode.setProperty("active", "true" if active else "false")
        self._mode.style().unpolish(self._mode)
        self._mode.style().polish(self._mode)

    def set_ram(self, process_ram: int, total_ram: int) -> None:
        fraction = (process_ram / total_ram * 100.0) if total_ram else 0.0
        self._ram.setText(f"RAM  {format_bytes(process_ram)}  ·  {fraction:.0f}%")

    def set_maximized(self, maximized: bool) -> None:
        self._max.setText("❐" if maximized else "□")
        self._max.setToolTip("Restore" if maximized else "Maximize")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.log = logging.getLogger("blaze.window")
        self.settings = settings
        self.store = ChatStore()
        self.local_model = HuggingFaceModel()
        self._model_worker: ModelLoadWorker | None = None
        self._voice_reply_worker: VoiceReplyWorker | None = None
        self._hotkey_listener = None
        self._hotkey_bridge = HotkeyBridge(self)
        self.voice_orb = VoiceOrb()
        self._speak_next_response = False
        self._queued_voice_transcript: Transcript | None = None
        from ..core.memory import LearningMemory

        self.memory = LearningMemory()
        self.finder = AppFinder(memory=self.memory)
        self.finder.start_warmup()
        self.governor = ResourceGovernor(int(settings.get("ram_limit_percent", 65)))
        self.system = SystemController()
        self.recorder = ScreenRecorder(int(settings.get("recording_fps", 8)), self)
        self.typing = TypingService()
        self.butler = OfflineButler(
            user_name=self._user_name(),
            finder=self.finder,
            system=self.system,
            recorder=self.recorder,
            governor=self.governor,
            typing=self.typing,
            confirm_destructive=bool(settings.get("confirm_destructive", True)),
            memory=self.memory,
        )
        self.voice = VoiceEngine(str(settings.get("stt_model", "base.en")), self)
        self._current_chat_id = ""
        self._llm_worker: LLMWorker | None = None
        self._llm_thread_id = ""
        self._agent_worker: AgentWorker | None = None
        self._agent_thread_id = ""
        self._pending_send: tuple[str, str] | None = None
        self._model_load_for_chat = False
        self._model_target_path = ""
        self._failed_model_path = ""
        self._settings_open = False
        self._last_snapshot: SystemSnapshot | None = None
        self._closing = False
        self._voice_missing_warned = False
        self._onboarding_dialog: OnboardingDialog | None = None

        self.setWindowTitle(f"{APP_NAME} — {TAGLINE.title()}")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(1040, 680)
        self.resize(1280, 820)
        self._apply_window_icon()

        self._outer = QWidget(self)
        self._outer_layout = QVBoxLayout(self._outer)
        self._outer_layout.setContentsMargins(10, 10, 10, 10)
        self._outer_layout.setSpacing(0)
        self._root = QFrame(self._outer)
        self._root.setObjectName("Root")
        self._root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._outer_layout.addWidget(self._root)
        self.setCentralWidget(self._outer)

        root_layout = QVBoxLayout(self._root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.topbar = TopBar(self._root)
        root_layout.addWidget(self.topbar)

        content = QWidget(self._root)
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.sidebar = Sidebar(content)
        self.chat = ChatView(self._user_name(), content)
        self.settings_panel = SettingsPanel(self.settings, content)
        self.settings_panel.setVisible(False)
        content_layout.addWidget(self.sidebar)
        content_layout.addWidget(self.chat, 1)
        content_layout.addWidget(self.settings_panel)
        root_layout.addWidget(content, 1)

        self._settings_anim = QPropertyAnimation(self.settings_panel, b"maximumWidth", self)
        self._settings_anim.setDuration(260)
        self._settings_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._size_grip = QSizeGrip(self._root)
        self._size_grip.setFixedSize(16, 16)
        self._toast = Toast(self._root)

        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(1000)
        self._rec_timer.timeout.connect(self._tick_recording)

        self._wire()
        self._apply_theme(theme_manager.theme.key)
        self._refresh_mode()
        self._load_start_state()
        self.governor.start()
        self.governor.updated.connect(self._on_resources)
        self.recorder.started.connect(self._on_recording_started)
        self.recorder.stopped.connect(self._on_recording_stopped)
        self.recorder.error.connect(self._toast.show_message)
        self.recorder.encoded.connect(self._on_recording_encoded)
        self.recorder.encode_failed.connect(self._on_recording_encode_failed)
        self.voice.state_changed.connect(self._on_voice_state)
        self.voice.transcript_ready.connect(self._on_transcript)
        self.voice.error.connect(self._on_voice_error)
        self.governor.pressure.connect(self._on_memory_pressure)
        QTimer.singleShot(80, self._bootstrap_chats)
        QTimer.singleShot(500, self._refresh_mode)

    # ---------------------------------------------------------------- setup
    def _wire(self) -> None:
        self.topbar.minimize_requested.connect(self.showMinimized)
        self.topbar.maximize_requested.connect(self._toggle_maximize)
        self.topbar.close_requested.connect(self.close)
        self.sidebar.new_chat_requested.connect(self._new_chat)
        self.sidebar.chat_selected.connect(self._select_chat)
        self.sidebar.rename_requested.connect(self._rename_chat)
        self.sidebar.delete_requested.connect(self._delete_chat)
        self.sidebar.settings_requested.connect(self.open_settings)
        self.chat.send_requested.connect(self._send_message)
        self.chat.stop_requested.connect(self._stop_response)
        self.chat.mic_pressed.connect(self._start_voice)
        self.chat.mic_released.connect(self._stop_voice)
        self.chat.quick_action.connect(self._send_message)
        self.chat.rename_thread_requested.connect(self._rename_current)
        self.chat.delete_thread_requested.connect(self._delete_current)
        self.chat.model_selected.connect(self._on_chat_model_selected)
        self.settings_panel.close_requested.connect(self.close_settings)
        self.settings_panel.model_load_requested.connect(self._load_local_model)
        self.settings_panel.model_remove_requested.connect(self._remove_local_model)
        self.settings_panel.agent_mode_changed.connect(self._on_agent_mode_changed)
        self.settings_panel.agent_confirm_changed.connect(self._on_agent_confirm_changed)
        self.settings_panel.ram_limit_changed.connect(self._set_ram_limit)
        self.settings_panel.theme_changed.connect(self._set_theme)
        self.settings_panel.name_changed.connect(self._set_name)
        self.settings_panel.stt_model_changed.connect(self._set_stt_model)
        self.settings_panel.clear_history_requested.connect(self._clear_history)
        self.settings_panel.reveal_data_requested.connect(
            lambda: self.system.reveal_in_file_manager(app_data_dir())
        )
        self._hotkey_bridge.activated.connect(self._toggle_voice)
        global_hotkey = False
        if sys.platform in {"darwin", "win32"}:
            try:
                from pynput import keyboard

                self._hotkey_listener = keyboard.GlobalHotKeys(
                    {"<ctrl>+<shift>+<space>": self._hotkey_bridge.activated.emit}
                )
                self._hotkey_listener.start()
                global_hotkey = True
            except Exception:
                self.log.warning("Global voice shortcut unavailable; using in-app shortcut", exc_info=True)
        shortcuts = [
            (QKeySequence("Ctrl+N"), self._new_chat),
            (QKeySequence("Ctrl+,"), self.open_settings),
            (QKeySequence("Escape"), self._escape),
        ]
        if sys.platform in {"darwin", "win32"} and not global_hotkey:
            shortcuts.append((QKeySequence("Ctrl+Shift+Space"), self._toggle_voice))
        for sequence, slot in shortcuts:
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(slot)

    def _apply_window_icon(self) -> None:
        icon_path = assets_dir() / "blaze_logo.svg"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            app = QApplication.instance()
            if app:
                app.setWindowIcon(QIcon(str(icon_path)))

    def _user_name(self) -> str:
        return (self.settings.get("name", "") or "friend").strip() or "friend"

    # ------------------------------------------------------ model registry
    def _registry(self) -> list[dict[str, str]]:
        """Normalized list of model folders the user has added."""
        raw = self.settings.get("model_registry", [])
        if not isinstance(raw, list) or (not raw and self.settings.get("hf_downloaded_models")):
            voice = self.settings.get("hf_voice_model", "")
            migrated: list[dict[str, str]] = []
            for value in self.settings.get("hf_downloaded_models", []) or []:
                path = str(value or "").strip()
                if not path:
                    continue
                task = "automatic-speech-recognition" if path == voice else "text-generation"
                migrated.append({"path": path, "task": task})
            if migrated or (isinstance(raw, list) and not raw):
                self.settings.set("model_registry", migrated)
            raw = migrated
        elif not isinstance(raw, list):
            raw = []
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if not path or path in seen:
                continue
            seen.add(path)
            entries.append(
                {
                    "path": path,
                    "task": str(item.get("task", "text-generation")),
                    "label": str(item.get("label", "") or _model_display_name(path)),
                    "params": str(item.get("params", "")),
                    "backend": str(item.get("backend", "")),
                }
            )
        return entries

    def _register_model(
        self,
        path: str,
        task: str,
        *,
        label: str = "",
        params: str = "",
        backend: str = "",
    ) -> None:
        path = str(path or "").strip()
        if not path:
            return
        entries = self._registry()
        for entry in entries:
            if entry["path"] == path:
                entry["task"] = task or entry["task"]
                entry["label"] = label or entry["label"]
                entry["params"] = params or entry["params"]
                entry["backend"] = backend or entry["backend"]
                break
        else:
            entries.append(
                {
                    "path": path,
                    "task": task or "text-generation",
                    "label": label or _model_display_name(path),
                    "params": params,
                    "backend": backend,
                }
            )
        self.settings.set("model_registry", entries)

    def _model_task(self, path: str) -> str:
        for entry in self._registry():
            if entry["path"] == path:
                return entry["task"]
        return "text-generation"

    def _chat_model_choices(self) -> list[tuple[str, str]]:
        choices: list[tuple[str, str]] = [("", "Offline butler")]
        for entry in self._registry():
            if entry["task"] == "automatic-speech-recognition":
                continue
            label = entry["label"] or _model_display_name(entry["path"])
            if entry.get("params"):
                label = f"{label} · {entry['params'].replace(' PARAMS', '')}"
            if entry.get("backend") == "gguf":
                label = f"{label} · GGUF"
            elif entry.get("backend") == "prism":
                label = f"{label} · BONSAI"
            choices.append((entry["path"], label))
        return choices

    def _chat_model_path(self, chat_id: str) -> str:
        if not chat_id:
            return ""
        summary = self.store.get_chat(chat_id)
        return summary.model_path if summary else ""

    def _is_active_model(self, path: str) -> bool:
        if not path or not self.local_model.loaded or self.local_model.local_path is None:
            return False
        try:
            loaded = Path(self.local_model.local_path).expanduser()
            target = Path(path).expanduser()
            if loaded == target:
                return True
            # A selected .safetensors file loads from its parent Transformers folder.
            if target.suffix.lower() == ".safetensors" and loaded == target.parent:
                return True
            try:
                return loaded.resolve() == target.resolve()
            except OSError:
                return False
        except OSError:
            return False

    def _chat_model_ready(self) -> bool:
        return self._is_active_model(self._chat_model_path(self._current_chat_id))

    def _default_model_path(self) -> str:
        if self._current_chat_id:
            summary = self.store.get_chat(self._current_chat_id)
            if summary is not None:
                return summary.model_path
        return str(self.settings.get("hf_model", "") or "")

    def _migrate_chat_models(self) -> None:
        """One-time: give existing chats the model that was globally loaded."""
        if self.settings.get("chat_models_initialized", False):
            return
        default = str(self.settings.get("hf_model", "") or "")
        if default:
            for chat in self.store.list_chats():
                if not chat.model_path:
                    self.store.set_chat_model(chat.id, default)
        self.settings.set("chat_models_initialized", True)

    def _sync_chat_model(self, chat_id: str) -> None:
        """Load the model assigned to ``chat_id`` if it is not active yet."""
        summary = self.store.get_chat(chat_id)
        if summary is None:
            return
        path = summary.model_path
        if not path or self._is_active_model(path):
            return
        if self._model_worker is not None and self._model_worker.isRunning():
            return
        if self._llm_worker is not None and self._llm_worker.isRunning():
            return
        if self._agent_worker is not None and self._agent_worker.isRunning():
            return
        display = _model_display_name(path)
        self.chat.set_model_busy(f"Loading {display}…")
        self._toast.show_message(f"Loading model for this chat · {display}", duration_ms=5000)
        self._load_local_model(path, self._model_task(path), for_chat=True)

    def _on_chat_model_selected(self, path: str) -> None:
        chat_id = self._current_chat_id
        if not chat_id:
            return
        summary = self.store.get_chat(chat_id)
        if summary is None or summary.model_path == path:
            return
        self.store.set_chat_model(chat_id, path)
        if not path:
            self._toast.show_message("This chat now uses the offline butler.")
            self._refresh_mode()
            return
        if self._is_active_model(path):
            self._refresh_mode()
            self._toast.show_message(f"This chat now uses {_model_display_name(path)}.")
            return
        display = _model_display_name(path)
        self.chat.set_model_busy(f"Loading {display}…")
        self._toast.show_message(f"Loading {display} for this chat…", duration_ms=5000)
        self._load_local_model(path, self._model_task(path), for_chat=True)

    def _bootstrap_chats(self) -> None:
        if self._closing or not self.settings.get("onboarding_complete", False):
            return
        self._migrate_chat_models()
        chats = self.store.list_chats()
        if not chats:
            self._create_welcome_thread()
        self._reload_chats(self.settings.get("last_chat_id", ""))

    def _create_welcome_thread(self) -> str:
        chat_id = self.store.create_chat(
            "First boot", model_path=self._default_model_path()
        )
        name = self._user_name()
        hour = datetime.now().hour
        greeting = (
            "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        )
        text = (
            f"{greeting}, {name}. Blaze is online.\n\n"
            "Local butler core: READY · Local model: optional\n\n"
            "Try “open VS Code”, “start recording”, “system status”, or "
            "“type your message”. Add a local chat model folder in Settings "
            "to unlock conversation and agent mode: then ask Blaze to edit videos, "
            "draft email, reply to texts, or control apps for you."
        )
        self.store.add_message(chat_id, "assistant", text)
        return chat_id

    def _load_start_state(self) -> None:
        self.sidebar.set_user(self._user_name())
        self.chat.set_user_name(self._user_name())
        self.settings_panel.refresh(self.settings)
        self.governor.refresh()

    # ------------------------------------------------------------- onboarding
    def show_onboarding(self) -> None:
        if self.settings.get("onboarding_complete", False):
            return
        dialog = OnboardingDialog(self.settings, self)
        dialog.completed.connect(self._onboarding_completed)
        self._onboarding_dialog = dialog
        dialog.exec()
        self._onboarding_dialog = None
        if not self.settings.get("onboarding_complete", False):
            self._onboarding_completed(
                {
                    "name": self.settings.get("name", "") or "Friend",
                    "theme": self.settings.get("theme", "onyx"),
                    "onboarding_complete": True,
                }
            )

    def _onboarding_completed(self, payload: dict) -> None:
        self.settings.update(payload)
        theme_key = str(payload.get("theme", self.settings.get("theme", "onyx")))
        theme_manager.set_theme(theme_key)
        self._apply_theme(theme_key)
        name = self._user_name()
        self.sidebar.set_user(name)
        self.chat.set_user_name(name)
        self.butler.set_user_name(name)
        self.settings_panel.refresh(self.settings)
        if not self.store.list_chats():
            self._create_welcome_thread()
        self._reload_chats(self.settings.get("last_chat_id", ""))
        self._refresh_mode()
        self._toast.show_message(f"Signal acquired. Welcome, {name}.")
        self.chat.focus_composer()

    # --------------------------------------------------------------- chat I/O
    def _ensure_chat(self) -> str:
        if self._current_chat_id and self.store.get_chat(self._current_chat_id):
            return self._current_chat_id
        chat_id = self.store.create_chat(model_path=self._default_model_path())
        self._current_chat_id = chat_id
        self._reload_chats(chat_id)
        return chat_id

    def _new_chat(self) -> None:
        chat_id = self.store.create_chat(model_path=self._default_model_path())
        self.settings.set("last_chat_id", chat_id)
        self._reload_chats(chat_id)
        self.chat.focus_composer()

    def _select_chat(self, chat_id: str) -> None:
        if chat_id == self._current_chat_id:
            return
        self._current_chat_id = chat_id
        self.settings.set("last_chat_id", chat_id)
        summary = self.store.get_chat(chat_id)
        messages = self.store.get_messages(chat_id)
        self.sidebar.set_selected(chat_id)
        self.chat.set_thread(
            chat_id,
            summary.title if summary else "New transmission",
            messages,
            mode_label=self._model_label(),
            online=self._chat_model_ready(),
            model_choices=self._chat_model_choices(),
            model_path=summary.model_path if summary else "",
        )
        self.chat.set_busy(False)
        self._sync_chat_model(chat_id)

    def _reload_chats(self, select_id: str = "") -> None:
        chats = self.store.list_chats()
        if not chats:
            self._current_chat_id = ""
            self.sidebar.set_chats([], "")
            self.chat.set_thread(
                "",
                "New transmission",
                [],
                mode_label=self._model_label(),
                online=False,
                model_choices=self._chat_model_choices(),
                model_path="",
            )
            return
        available = {chat.id for chat in chats}
        selected = select_id if select_id in available else ""
        if not selected and self._current_chat_id in available:
            selected = self._current_chat_id
        if not selected:
            last = self.settings.get("last_chat_id", "")
            selected = last if last in available else chats[0].id
        self._current_chat_id = selected
        self.settings.set("last_chat_id", selected)
        self.sidebar.set_chats(chats, selected)
        summary = self.store.get_chat(selected)
        self.chat.set_thread(
            selected,
            summary.title if summary else "New transmission",
            self.store.get_messages(selected),
            mode_label=self._model_label(),
            online=self._is_active_model(summary.model_path if summary else ""),
            model_choices=self._chat_model_choices(),
            model_path=summary.model_path if summary else "",
        )
        self._sync_chat_model(selected)

    def _add_assistant(
        self,
        text: str,
        *,
        meta: str = "LOCAL",
        thread_id: str | None = None,
        save: bool = True,
    ) -> None:
        chat_id = thread_id or self._current_chat_id
        if not chat_id or not self.store.get_chat(chat_id):
            return
        if save:
            self.store.add_message(chat_id, "assistant", text)
            self._reload_sidebar_only()
        if chat_id == self._current_chat_id:
            self.chat.add_message("assistant", text, meta=meta)
        if self._speak_next_response:
            self._speak_next_response = False
            self._speak_voice_reply(text)

    def _speak_voice_reply(self, text: str) -> None:
        if self._voice_reply_worker is not None and self._voice_reply_worker.isRunning():
            self._toast.show_message("Stop the current speech before starting another.")
            return
        self.voice_orb.show_state("speaking")
        self.chat.set_busy(True)
        self._voice_reply_worker = VoiceReplyWorker(text)
        self._voice_reply_worker.finished_speaking.connect(self._on_voice_reply_finished)
        self._voice_reply_worker.failed.connect(self._on_voice_reply_failed)
        self._voice_reply_worker.start()

    def _on_voice_reply_finished(self) -> None:
        self.chat.set_busy(False)
        if self.voice.is_recording:
            self.voice_orb.show_state("listening")
        else:
            self.voice_orb.show_state("hidden")

    def _on_voice_reply_failed(self, message: str) -> None:
        self.chat.set_busy(False)
        if not self.voice.is_recording:
            self.voice_orb.show_state("hidden")
        self._toast.show_message(f"Could not speak reply: {message}")

    def _stop_voice_reply(self) -> bool:
        worker = self._voice_reply_worker
        if worker is None or not worker.isRunning():
            return False
        worker.cancel()
        self.chat.set_busy(False)
        if not self.voice.is_recording:
            self.voice_orb.show_state("hidden")
        return True

    def _reload_sidebar_only(self) -> None:
        self.sidebar.set_chats(self.store.list_chats(), self._current_chat_id)

    def _send_message(self, text: str) -> None:
        if len(text) > MAX_MESSAGE_CHARS:
            self._toast.show_message(
                f"Message is too large. Limit: {MAX_MESSAGE_CHARS:,} characters."
            )
            return
        text = text.strip()
        if not text:
            return
        self._stop_voice_reply()
        if self._llm_worker is not None and self._llm_worker.isRunning():
            self._speak_next_response = False
            self._toast.show_message(
                "Blaze is still finishing a response in another thread."
            )
            return
        if self._agent_worker is not None and self._agent_worker.isRunning():
            self._speak_next_response = False
            self._toast.show_message("An agent task is running. Press Esc to stop it first.")
            return
        chat_id = self._ensure_chat()
        self.store.add_message(chat_id, "user", text)
        self.store.auto_title(chat_id, text)
        self.chat.add_message("user", text)
        summary = self.store.get_chat(chat_id)
        if summary:
            self.chat.update_thread_title(summary.title)
        self._reload_sidebar_only()
        self._route_message(chat_id, text)

    def _route_message(self, chat_id: str, text: str) -> None:
        chat_model = self._chat_model_path(chat_id)
        model_ready = bool(chat_model) and self._is_active_model(chat_model)
        command = match_command(text)
        agent_ready = model_ready and self.settings.get("agent_mode", True)
        # Deterministic offline-capable actions (media playback, open+URL, browser/site,
        # messaging, downloads, web search, volume, and local learning controls) should run locally.
        butler_preferred = command is not None and command.kind in {
            "play_media",
            "media_pause",
            "media_resume",
            "media_stop",
            "media_next",
            "media_prev",
            "volume_up",
            "volume_down",
            "volume_mute",
            "volume_set",
            "list_contacts",
            "save_contact",
            "delete_contact",
            "send_message",
            "web_search",
            "download_file",
            "open_url",
            "new_tab",
            "learn_alias",
            "forget_alias",
            "learning_status",
        }
        if command is not None and (
            butler_preferred or not (agent_ready and _should_route_to_agent(text))
        ):
            model_pending = bool(chat_model) and self._failed_model_path != chat_model
            if not ((model_ready or model_pending) and _command_conflicts_with_ai_task(text, command)):
                self._execute_command(command)
                return

        if model_ready:
            if self.settings.get("agent_mode", True) and not _is_conversational(text):
                self._dispatch_agent(chat_id, text)
            else:
                self._dispatch_llm(chat_id)
            return

        if chat_model and not model_ready:
            # Butler can still handle preferred deterministic commands offline.
            if butler_preferred and command is not None:
                self._execute_command(command)
                return
            self._pending_send = (chat_id, text)
            display = _model_display_name(chat_model)
            self.chat.set_model_busy(f"Loading {display}…")
            self._toast.show_message(
                f"Loading {display} for this chat…", duration_ms=5000
            )
            self._load_local_model(chat_model, self._model_task(chat_model), for_chat=True)
            return

        if command is not None and _command_conflicts_with_ai_task(text, command):
            # Avoid misfiring local app/type commands on coding requests when
            # no model is loaded; the fallback explains how to enable AI.
            reply = self.butler.fallback(text)
            self._handle_reply(reply, meta="LOCAL")
            return

        if command is not None:
            self._execute_command(command)
            return

        reply = self.butler.fallback(text)
        self._handle_reply(reply, meta="LOCAL")

    def _flush_pending_send(self) -> None:
        pending = self._pending_send
        self._pending_send = None
        if not pending:
            return
        chat_id, text = pending
        if not self.store.get_chat(chat_id):
            return
        if not self._is_active_model(self._chat_model_path(chat_id)):
            return
        self._route_message(chat_id, text)

    def _resume_pending_send(self) -> None:
        """Continue a message that was waiting for its chat model."""
        pending = self._pending_send
        if not pending or self._closing:
            return
        if self._model_worker is not None and self._model_worker.isRunning():
            return
        chat_id, text = pending
        if not self.store.get_chat(chat_id):
            self._pending_send = None
            return
        target = self._chat_model_path(chat_id)
        if not target:
            self._pending_send = None
            self._route_message(chat_id, text)
            return
        if self._is_active_model(target):
            self._flush_pending_send()
            return
        if target == self._failed_model_path:
            self._pending_send = None
            self._add_assistant(
                self.butler.fallback(text).text, meta="LOCAL", thread_id=chat_id
            )
            return
        self._sync_chat_model(chat_id)

    def _dispatch_llm(self, chat_id: str) -> None:
        # Keep prompt prefill short; old turns are already persisted and can
        # still be reopened, but are not all replayed through the model.
        messages = self.store.recent_context(chat_id, limit=8)
        history = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role in {"user", "assistant"}
        ]
        history.insert(0, {"role": "system", "content": CHAT_SYSTEM_PROMPT})
        self.chat.set_busy(True)
        worker = LLMWorker(self.local_model, history, self._user_name())
        self._llm_worker = worker
        self._llm_thread_id = chat_id
        worker.completed.connect(lambda response: self._on_llm_response(chat_id, response))
        worker.failed.connect(lambda error: self._on_llm_error(chat_id, error))
        worker.finished.connect(self._on_llm_thread_finished)
        worker.start()

    def _stop_response(self) -> None:
        if self._cancel_agent():
            return
        if self._llm_worker is not None and self._llm_worker.isRunning():
            self._llm_worker.cancel()
            self.chat.set_busy(False)
            self._toast.show_message("Stopping response…")
            return
        if self._stop_voice_reply():
            self._toast.show_message("Speech stopped.")

    def _on_llm_response(self, chat_id: str, response) -> None:  # noqa: ANN001
        self.chat.set_busy(False)
        if getattr(response, "provider_label", "") == "STOPPED" or (
            self._llm_worker is not None and getattr(self._llm_worker, "cancelled", False)
        ):
            if chat_id == self._current_chat_id:
                self._toast.show_message("Response stopped.")
            return
        self._add_assistant(
            extract_final_text(response.text),
            meta=response.provider_label.upper(),
            thread_id=chat_id,
        )
        if chat_id == self._current_chat_id:
            self.chat.set_mode(self._model_label(), online=self._chat_model_ready())
        if chat_id != self._current_chat_id:
            self._toast.show_message("Response ready in another thread.")

    def _on_llm_error(self, chat_id: str, message: str) -> None:
        self.chat.set_busy(False)
        if self._llm_worker is not None and getattr(self._llm_worker, "cancelled", False):
            if chat_id == self._current_chat_id:
                self._toast.show_message("Response stopped.")
            return
        text = (
            f"{message}\n\nUnload the local model in Settings to return to the offline butler."
        )
        self._add_assistant(text, meta="LOCAL MODEL", thread_id=chat_id)
        if chat_id == self._current_chat_id:
            self._toast.show_message("The local model response failed. Butler remains ready.")

    def _on_llm_thread_finished(self) -> None:
        if self._llm_worker is not None:
            self._llm_worker.deleteLater()
            self._llm_worker = None
            self._llm_thread_id = ""
        if not self._closing:
            if self._queued_voice_transcript is not None:
                transcript = self._queued_voice_transcript
                self._queued_voice_transcript = None
                self._on_transcript(transcript)
            else:
                self._resume_pending_send()

    # ------------------------------------------------------------------ agent
    def _dispatch_agent(self, chat_id: str, task: str) -> None:
        if self._agent_worker is not None and self._agent_worker.isRunning():
            self._toast.show_message("An agent task is already running.")
            return
        messages = self.store.recent_context(chat_id, limit=8)
        context = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role in {"user", "assistant"}
        ]
        confirm = (
            AgentConfirmation(self)
            if self.settings.get("agent_confirm", True)
            else None
        )
        worker = AgentWorker(
            self.local_model,
            task,
            self._user_name(),
            context,
            confirm=confirm,
            typing=self.typing,
            max_steps=int(self.settings.get("agent_max_steps", 8) or 8),
        )
        self._agent_worker = worker
        self._agent_thread_id = chat_id
        worker.input_started.connect(self._hide_for_agent_input)
        worker.completed.connect(
            lambda text, cid=chat_id: self._on_agent_complete(cid, text)
        )
        worker.failed.connect(
            lambda message, cid=chat_id: self._on_agent_error(cid, message)
        )
        worker.finished.connect(self._on_agent_finished)
        self.chat.set_busy(True)
        if chat_id == self._current_chat_id:
            self.chat.set_mode("AGENT · COMPUTER CONTROL", online=True)
        worker.start()

    def _hide_for_agent_input(self) -> None:
        if not self._closing:
            self.showMinimized()

    def _on_agent_step(self, chat_id: str, step: AgentStep) -> None:
        """Keep tool logs out of the chat; only dots and the final answer show."""
        _ = (chat_id, step)

    def _on_agent_complete(self, chat_id: str, text: str) -> None:
        self.chat.set_busy(False)
        answer = clean_model_reply(extract_final_text(text)) or "Done."
        self._add_assistant(answer, meta="AGENT", thread_id=chat_id)
        if chat_id == self._current_chat_id:
            self.chat.set_mode(self._model_label(), online=True)
        else:
            self._toast.show_message("Agent finished in another thread.")
        self._restore_after_agent()

    def _on_agent_error(self, chat_id: str, message: str) -> None:
        self.chat.set_busy(False)
        self._add_assistant(
            f"Agent stopped: {message}\n\nCheck the loaded model and macOS "
            "Automation, Accessibility and Screen Recording permissions.",
            meta="AGENT ERROR",
            thread_id=chat_id,
        )
        if chat_id == self._current_chat_id:
            self._toast.show_message("Agent task failed.")
        self._restore_after_agent()

    def _on_agent_finished(self) -> None:
        if self._agent_worker is not None:
            self._agent_worker.deleteLater()
            self._agent_worker = None
            self._agent_thread_id = ""
        self.chat.set_busy(False)
        if not self._closing:
            if self._queued_voice_transcript is not None:
                transcript = self._queued_voice_transcript
                self._queued_voice_transcript = None
                self._on_transcript(transcript)
            else:
                self._resume_pending_send()

    def _restore_after_agent(self) -> None:
        if not self._closing and self.isMinimized():
            self.showNormal()
            self.activateWindow()

    def _cancel_agent(self) -> bool:
        if self._agent_worker is not None and self._agent_worker.isRunning():
            self._agent_worker.cancel()
            self._toast.show_message("Stopping the agent task…")
            return True
        return False

    # --------------------------------------------------------------- commands
    def _execute_command(self, command: Command) -> None:
        if command.kind == "open_url" and not command.argument:
            # "open this website" refers to the link in the conversation unless
            # the user has just copied a different one.
            if not clipboard_url():
                command.argument = self._recent_chat_url(self._current_chat_id)
        if command.kind == "quit":
            self._add_assistant("Closing Blaze.")
            QTimer.singleShot(250, self.close)
            return
        if command.kind == "ui_settings":
            self._add_assistant("Opening Settings.")
            self.open_settings()
            return
        if command.kind == "type_text":
            QTimer.singleShot(120, lambda: self._type_command(command.argument))
            return
        reply = self.butler.run_command(command)
        self._handle_reply(reply)

    def _run_tool_command(self, kind: str) -> None:
        command = Command(kind)
        reply = self.butler.run_command(command)
        self._handle_reply(reply)

    def _recent_chat_url(self, chat_id: str) -> str:
        if not chat_id:
            return ""
        try:
            messages = self.store.get_messages(chat_id)
        except Exception:
            self.log.debug("Could not read chat messages for a URL", exc_info=True)
            return ""
        for message in reversed(messages):
            url = extract_first_url(message.content)
            if url:
                return url
        return ""

    def _handle_reply(self, reply: ButlerReply, *, meta: str = "LOCAL") -> None:
        self._add_assistant(reply.text, meta=meta)
        if reply.ui_action == "open_settings":
            self.open_settings()
        elif reply.ui_action == "quit":
            QTimer.singleShot(250, self.close)

    def _type_command(self, text: str) -> None:
        self.showMinimized()

        def type_it() -> None:
            reply = self.butler.type_text(text)
            self._handle_reply(reply)
            QTimer.singleShot(900, self._restore_after_typing)

        QTimer.singleShot(620, type_it)

    def _restore_after_typing(self) -> None:
        if not self._closing:
            self.showNormal()
            self.activateWindow()

    # ------------------------------------------------------------------ voice
    def _start_voice(self) -> None:
        # Pressing the mic during a spoken or generated reply is a barge-in:
        # stop the reply and immediately open the microphone for the new turn.
        self._stop_voice_reply()
        if self._llm_worker is not None and self._llm_worker.isRunning():
            self._llm_worker.cancel()
        if self._agent_worker is not None and self._agent_worker.isRunning():
            self._agent_worker.cancel()
        has_hf_speech = self.local_model.asr_pipeline is not None
        if not voice_capture_ready() or (not voice_ready() and not has_hf_speech):
            if not self._voice_missing_warned:
                self._voice_missing_warned = True
                self._add_assistant(
                    "Voice input needs the local STT dependencies. Install them with:\n\n"
                    "`pip install faster-whisper sounddevice numpy`\n\n"
                    "The rest of Blaze works without them.",
                    meta="SYSTEM",
                )
            else:
                self._toast.show_message("Voice engine dependencies are missing. See Settings.")
            self.chat.set_voice_state("idle")
            return
        if not self.settings.get("voice_enabled", True):
            self._toast.show_message("Voice input is disabled in Settings.")
            return
        self.chat.set_busy(False)
        self.voice.set_asr_pipeline(self.local_model.asr_pipeline)
        self.voice.start_recording()
        if self.voice.is_recording:
            self.voice_orb.show_state("listening")

    def _stop_voice(self) -> None:
        if self.voice.is_recording:
            self.voice.stop_recording()
            self.voice_orb.show_state("thinking")

    def _toggle_voice(self) -> None:
        """Mac-friendly in-app shortcut: press once to listen, again to send."""
        if self.voice.is_recording:
            self._stop_voice()
        else:
            self._start_voice()

    def _on_voice_state(self, state: str) -> None:
        self.chat.set_voice_state(state)
        self.settings_panel.set_voice_state(state)
        if state == "recording":
            self.voice_orb.show_state("listening")
        elif state in {"loading", "transcribing"}:
            self.voice_orb.show_state("thinking")
        elif state == "idle" and self.voice_orb.isVisible():
            self.voice_orb.show_state("idle")

    def _on_transcript(self, transcript: Transcript) -> None:
        text = transcript.text.strip()
        if not text:
            self._hide_voice_orb_if_idle()
            return
        if len(text) > MAX_MESSAGE_CHARS:
            self.voice_orb.show_state("idle")
            self._toast.show_message(
                f"Voice message is too large. Limit: {MAX_MESSAGE_CHARS:,} characters."
            )
            return
        if (
            self._llm_worker is not None and self._llm_worker.isRunning()
        ) or (
            self._agent_worker is not None and self._agent_worker.isRunning()
        ):
            # The model may need a moment to notice cancellation. Preserve the
            # user's interruption and route it as soon as that worker exits.
            self._queued_voice_transcript = transcript
            self._toast.show_message("Got it. Finishing the interrupted turn…")
            return
        self.voice_orb.show_state("thinking")
        # Spoken replies for voice interaction (Siri / Google Assistant experience)
        self._speak_next_response = True
        command = match_command(text, voice=True)
        confidence = f"{transcript.confidence * 100:.0f}%"
        if command is None:
            self._toast.show_message(f"Voice message sent · {confidence}")
            self._send_message(text)
            return
        chat_id = self._ensure_chat()
        self.store.add_message(chat_id, "user", text)
        self.store.auto_title(chat_id, text)
        self.chat.add_message("user", text)
        summary = self.store.get_chat(chat_id)
        if summary:
            self.chat.update_thread_title(summary.title)
        self._reload_sidebar_only()
        self._toast.show_message(f"Command recognised · {confidence}")
        self._execute_command(command)

    def _hide_voice_orb_if_idle(self) -> None:
        speaking = (
            self._voice_reply_worker is not None
            and self._voice_reply_worker.isRunning()
        )
        if not self.voice.is_recording and not speaking:
            self.voice_orb.show_state("hidden")

    def _on_voice_error(self, message: str) -> None:
        self._speak_next_response = False
        self.voice_orb.show_state("hidden")
        self.chat.set_voice_state("idle")
        self.settings_panel.set_voice_state("idle")
        self._toast.show_message(message, duration_ms=6500)

    # --------------------------------------------------------------- settings
    def open_settings(self) -> None:
        if self._settings_open:
            return
        self._settings_open = True
        self.settings_panel.refresh(self.settings)
        self.settings_panel.setVisible(True)
        if self._last_snapshot:
            self.settings_panel.update_resources(
                self._last_snapshot, self.governor.limit_percent
            )
        self._animate_settings(0, 420, hide_after=False)

    def close_settings(self) -> None:
        if not self._settings_open:
            return
        self._settings_open = False
        self._animate_settings(self.settings_panel.width(), 0, hide_after=True)

    def _animate_settings(self, start: int, end: int, *, hide_after: bool) -> None:
        self._settings_anim.stop()
        try:
            self._settings_anim.finished.disconnect()
        except TypeError:
            pass
        self._settings_anim.setStartValue(start)
        self._settings_anim.setEndValue(end)
        if hide_after:
            self._settings_anim.finished.connect(self._hide_settings_panel)
        self._settings_anim.start()

    def _hide_settings_panel(self) -> None:
        if not self._settings_open:
            self.settings_panel.setVisible(False)

    def _escape(self) -> None:
        if self._settings_open:
            self.close_settings()
        elif self._cancel_agent():
            return
        elif self._llm_worker is not None and self._llm_worker.isRunning():
            self._llm_worker.cancel()
            self.chat.set_busy(False)
            self._toast.show_message("Stopping response…")
        elif self._stop_voice_reply():
            self._toast.show_message("Speech stopped.")
        elif self.voice.is_recording:
            self.voice.cancel()
            self.chat.set_voice_state("idle")
            self.voice_orb.show_state("hidden")

    def _model_label(self) -> str:
        chat_model = self._chat_model_path(self._current_chat_id)
        if chat_model:
            label = _model_display_name(chat_model)
            if self._is_active_model(chat_model):
                if self.settings.get("agent_mode", True):
                    return f"AGENT · {label}"
                return f"MODEL · {label}"
            if self._model_worker is not None and self._model_worker.isRunning():
                return f"LOADING · {label}"
            return f"BUTLER · {label} NOT LOADED"
        if self.local_model.asr_pipeline is not None:
            return f"LOCAL BUTLER + HF SPEECH · {self.local_model.repo_id}"
        return "LOCAL BUTLER · OFFLINE"

    def _load_local_model(
        self, value: str, task: str = "", *, for_chat: bool = False
    ) -> None:
        if self._model_worker is not None and self._model_worker.isRunning():
            self._toast.show_message("A model is already loading.")
            return
        if self._llm_worker is not None and self._llm_worker.isRunning():
            self._toast.show_message("Wait for the current reply before switching models.")
            return
        if self._agent_worker is not None and self._agent_worker.isRunning():
            self._toast.show_message("Wait for the agent task to finish before switching models.")
            return
        path = Path(value).expanduser()
        is_model_file = path.is_file() and path.suffix.lower() in {
            ".gguf",
            ".safetensors",
        }
        if not path.is_dir() and not is_model_file and "/" in value:
            # Migrate a model downloaded by an older Blaze build.
            legacy = model_path(value)
            if legacy.is_dir():
                path = legacy
        if not path.is_dir() and not is_model_file:
            message = (
                "Unsupported model location: choose a .gguf or .safetensors "
                "file, or a Transformers folder with config.json + "
                ".safetensors/.bin weights."
            )
            self.settings_panel.set_model_status(
                "LOCAL BUTLER · NO MODEL LOADED", active=False, has_key=True
            )
            self.settings_panel.set_model_error(message)
            self._toast.show_message(
                "Choose a .gguf file or a local model folder. See Settings for details.",
                duration_ms=6500,
            )
            self._resume_pending_send()
            return
        self._model_load_for_chat = bool(for_chat)
        self._model_target_path = str(path)
        self._failed_model_path = ""
        display_name = path.stem if path.is_file() else path.name
        self.settings_panel.set_model_status(
            f"CHECKING MODEL · {display_name}", active=False, has_key=True
        )
        self._toast.show_message(f"Checking model {display_name}…", duration_ms=5000)
        self._model_worker = ModelLoadWorker(
            self.local_model, str(path), task, ram_limit_percent=90
        )
        self._model_worker.loaded.connect(self._on_local_model_loaded)
        self._model_worker.failed.connect(self._on_local_model_error)
        self._model_worker.finished.connect(self._on_model_worker_finished)
        self._model_worker.start()

    def _on_model_worker_finished(self) -> None:
        if self._model_worker is not None:
            self._model_worker.deleteLater()
            self._model_worker = None
        if not self._closing:
            self._resume_pending_send()

    def _on_local_model_loaded(self, repo: str, task: str, mode: str) -> None:
        saved = list(self.settings.get("hf_downloaded_models", []))
        if repo not in saved:
            saved.append(repo)
        self.settings.set("hf_downloaded_models", saved)
        self._register_model(
            repo,
            task,
            label=_model_display_name(repo),
            params=self.local_model.param_label,
            backend=self.local_model.backend,
        )
        if mode == "chat":
            if not self._model_load_for_chat:
                self.settings.update(
                    {"hf_model": repo, "hf_model_task": task, "hf_voice_model": ""}
                )
                # A newly added model becomes this chat's model when the chat
                # has not chosen one yet.
                if self._current_chat_id:
                    summary = self.store.get_chat(self._current_chat_id)
                    if summary is not None and not summary.model_path:
                        self.store.set_chat_model(self._current_chat_id, repo)
            self.voice.set_asr_pipeline(None)
            self.settings_panel.set_voice_status(
                "VOICE READY · FASTER-WHISPER LOCAL"
                if voice_ready()
                else "VOICE ENGINE OFFLINE · DEPENDENCIES MISSING"
            )
            detail = f" · {self.local_model.param_label}" if self.local_model.param_label else ""
            if self.local_model.backend == "gguf":
                backend = " · GGUF"
            elif self.local_model.backend == "prism":
                backend = " · BONSAI PRISM"
            else:
                backend = ""
            status = f"CHAT MODEL READY · {repo}{detail}{backend}"
        elif mode == "speech":
            self.settings.update({"hf_model": "", "hf_voice_model": repo})
            self.voice.set_asr_pipeline(self.local_model.asr_pipeline)
            detail = f" · {self.local_model.param_label}" if self.local_model.param_label else ""
            status = f"SPEECH-TO-TEXT MODEL READY · {repo}{detail}"
        else:
            if self.local_model.loaded:
                status = f"CHAT MODEL READY · {self.local_model.repo_id}  ·  ALSO ADDED {repo}"
            elif self.local_model.asr_pipeline is not None:
                status = f"SPEECH MODEL READY · {self.local_model.repo_id}  ·  ALSO ADDED {repo}"
            else:
                status = f"LOCAL MODEL READY · {repo}"
        self.settings_panel.set_model_status(status, active=mode == "chat", has_key=True)
        if self.local_model.last_download_note:
            self.settings_panel.set_model_error(self.local_model.last_download_note)
        if mode == "speech":
            self.settings_panel.set_voice_status("VOICE READY · HUGGING FACE SPEECH MODEL")
        self._model_load_for_chat = False
        status_choices = self._chat_model_choices()
        if self._current_chat_id:
            self.chat.set_model_choices(
                status_choices, self._chat_model_path(self._current_chat_id)
            )
        self._refresh_mode()
        self._flush_pending_send()
        self._toast.show_message(f"Local model ready · {_model_display_name(repo)}")

    def _on_local_model_error(self, message: str) -> None:
        self._failed_model_path = self._model_target_path
        self.settings_panel.set_model_status("LOCAL BUTLER · NO MODEL LOADED", active=False, has_key=True)
        self.settings_panel.set_model_error(message)
        self._toast.show_message("Model rejected or failed to load. See Settings for details.", duration_ms=6500)
        self._model_load_for_chat = False
        self._refresh_mode()

    def _remove_local_model(self, repo: str) -> None:
        if self._model_worker is not None and self._model_worker.isRunning():
            self._toast.show_message("Wait for the model operation to finish first.")
            return
        if self._llm_worker is not None and self._llm_worker.isRunning():
            self._toast.show_message("Wait for the current reply before removing a model.")
            return
        if self._agent_worker is not None and self._agent_worker.isRunning():
            self._toast.show_message("Wait for the agent task before removing a model.")
            return
        repo = (
            repo
            or self.local_model.repo_id
            or self.settings.get("hf_model", "")
            or self.settings.get("hf_voice_model", "")
        )
        active_chat = self.settings.get("hf_model", "") == repo
        active_speech = self.settings.get("hf_voice_model", "") == repo
        if repo:
            try:
                self.local_model.remove(repo)
            except Exception as exc:
                self.settings_panel.set_model_error(str(exc))
                return
        saved = [name for name in self.settings.get("hf_downloaded_models", []) if name != repo]
        registry = [entry for entry in self._registry() if entry["path"] != repo]
        for chat in self.store.list_chats():
            if chat.model_path == repo:
                self.store.set_chat_model(chat.id, "")
        self.settings.update({
            "hf_model": "" if active_chat else self.settings.get("hf_model", ""),
            "hf_voice_model": "" if active_speech else self.settings.get("hf_voice_model", ""),
            "hf_downloaded_models": saved,
            "model_registry": registry,
        })
        if active_speech:
            self.voice.set_asr_pipeline(None)
            self.settings_panel.set_voice_status(
                "VOICE READY · FASTER-WHISPER LOCAL"
                if voice_ready()
                else "VOICE ENGINE OFFLINE · DEPENDENCIES MISSING"
            )
        self.settings_panel.refresh(self.settings)
        self._refresh_mode()
        self._reload_chats(self._current_chat_id)
        self._toast.show_message("Model removed. Offline butler restored.")

    def _set_ram_limit(self, value: int) -> None:
        self.settings.set("ram_limit_percent", int(value))
        self.governor.set_limit_percent(int(value))
        if self._last_snapshot:
            self.settings_panel.update_resources(self._last_snapshot, int(value))
            self.sidebar.set_ram(self._last_snapshot, int(value))
        self._toast.show_message(f"Soft memory cap set to {value}%.")

    def _set_theme(self, key: str) -> None:
        self.settings.set("theme", key)
        self._apply_theme(key)
        self._refresh_mode()
        QTimer.singleShot(30, self._deferred_chat_reload)

    def _deferred_chat_reload(self) -> None:
        if self._closing or not self._current_chat_id:
            return
        self._reload_chats(self._current_chat_id)

    def _apply_theme(self, key: str) -> None:
        theme_manager.set_theme(key)
        self._toast.setStyleSheet("")

    def _set_name(self, name: str) -> None:
        self.settings.set("name", name.strip())
        self.sidebar.set_user(name)
        self.chat.set_user_name(name)
        self.butler.set_user_name(name)
        self._toast.show_message(f"Identity updated · {name.strip()}")

    def _set_stt_model(self, model: str) -> None:
        self.settings.set("stt_model", model)
        self.voice.set_model_size(model)
        self._toast.show_message(f"Whisper model set to {model}.")

    def _on_agent_mode_changed(self, enabled: bool) -> None:
        self.settings.set("agent_mode", bool(enabled))
        self._refresh_mode()
        self._toast.show_message(
            "Agent mode enabled. Loaded models can control the computer."
            if enabled
            else "Agent mode off. Responses stay in the chat."
        )

    def _on_agent_confirm_changed(self, enabled: bool) -> None:
        self.settings.set("agent_confirm", bool(enabled))
        self._toast.show_message(
            "Agent will ask before each action."
            if enabled
            else "Agent will run actions without asking. Destructive commands stay blocked."
        )

    def _refresh_mode(self) -> None:
        if self._closing:
            return
        label = self._model_label()
        online = self._chat_model_ready()
        self.topbar.set_mode(label, active=online)
        self.sidebar.set_mode(label, online=online)
        self.chat.set_mode(label, online=online)
        registry = self._registry()
        if online:
            status = label
        elif self.local_model.asr_pipeline is not None:
            status = f"VOICE MODEL READY · {self.local_model.repo_id}"
        elif registry:
            status = f"LOCAL BUTLER · {len(registry)} MODEL(S) AVAILABLE"
        else:
            status = "LOCAL BUTLER · NO MODEL LOADED"
        self.settings_panel.set_model_status(
            status,
            active=online,
            has_key=bool(
                self.settings.get("hf_model", "")
                or self.settings.get("hf_voice_model", "")
                or registry
            ),
        )

    # ----------------------------------------------------------------- system
    def _on_resources(self, snapshot: SystemSnapshot) -> None:
        self._last_snapshot = snapshot
        self.topbar.set_ram(snapshot.process_ram, snapshot.total_ram)
        self.sidebar.set_ram(snapshot, self.governor.limit_percent)
        self.settings_panel.update_resources(snapshot, self.governor.limit_percent)

    def _on_memory_pressure(self, over_bytes: int) -> None:
        self._toast.show_message(
            f"Soft memory cap reached — caches released ({format_bytes(over_bytes)} over)."
        )

    def _on_recording_started(self) -> None:
        self._rec_timer.start()
        self.chat.set_recording(True, 0)
        self._toast.show_message("Screen recording started. Say “stop recording” to save.")

    def _tick_recording(self) -> None:
        if self.recorder.is_recording:
            self.chat.set_recording(True, self.recorder.elapsed_seconds)
        else:
            self._rec_timer.stop()

    def _on_recording_stopped(self, ticket: RecordingTicket) -> None:
        self._rec_timer.stop()
        self.chat.set_recording(False)
        self._toast.show_message(ticket.message, duration_ms=6000)

    def _on_recording_encoded(self, path: str) -> None:
        self._toast.show_message(f"Recording saved · {Path(path).name}", duration_ms=7000)
        self._add_assistant(
            f"Screen recording saved to:\n`{path}`",
            meta="CAPTURE",
        )

    def _on_recording_encode_failed(self, message: str) -> None:
        self._toast.show_message(message, duration_ms=9000)
        self._add_assistant(message, meta="CAPTURE")

    # ----------------------------------------------------------- thread admin
    def _rename_current(self) -> None:
        if self._current_chat_id:
            self._rename_chat(self._current_chat_id)

    def _delete_current(self) -> None:
        if self._current_chat_id:
            self._delete_chat(self._current_chat_id)

    def _rename_chat(self, chat_id: str) -> None:
        summary = self.store.get_chat(chat_id)
        if not summary:
            return
        title, ok = QInputDialog.getText(
            self, "Rename transmission", "Thread name", text=summary.title
        )
        if not ok or not title.strip():
            return
        self.store.rename_chat(chat_id, title.strip())
        self._reload_sidebar_only()
        if chat_id == self._current_chat_id:
            self.chat.update_thread_title(title.strip())
        self._toast.show_message("Transmission renamed.")

    def _delete_chat(self, chat_id: str) -> None:
        if (
            QMessageBox.question(
                self,
                "Delete transmission",
                "Delete this thread and its messages? This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        was_current = chat_id == self._current_chat_id
        self.store.delete_chat(chat_id)
        chats = self.store.list_chats()
        if not chats:
            new_id = self.store.create_chat()
            self._reload_chats(new_id)
        elif was_current:
            self._current_chat_id = ""
            self._reload_chats(chats[0].id)
        else:
            self._reload_sidebar_only()
        self._toast.show_message("Transmission deleted.")

    def _clear_history(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Clear all history",
                "Delete every Blaze thread from this machine? This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.store.clear_all_chats()
        new_id = self.store.create_chat()
        self._reload_chats(new_id)
        self._toast.show_message("All chat history cleared.")

    # -------------------------------------------------------------- window UX
    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            maximized = self.isMaximized()
            self.topbar.set_maximized(maximized)
            if maximized:
                self._outer_layout.setContentsMargins(0, 0, 0, 0)
            else:
                self._outer_layout.setContentsMargins(10, 10, 10, 10)
            self._root.setProperty("maximized", "true" if maximized else "false")
            self._root.style().unpolish(self._root)
            self._root.style().polish(self._root)
            self._size_grip.setVisible(not maximized)
            if maximized:
                self._size_grip.hide()
            else:
                self._position_overlays()
        super().changeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_overlays()

    def _position_overlays(self) -> None:
        if hasattr(self, "_size_grip"):
            self._size_grip.move(
                self._root.width() - self._size_grip.width() - 2,
                self._root.height() - self._size_grip.height() - 2,
            )
        if hasattr(self, "_toast"):
            self._toast.position()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._closing = True
        if self._agent_worker is not None and self._agent_worker.isRunning():
            self._agent_worker.cancel()
            if not self._agent_worker.wait(5000):
                self.log.warning("Terminating an in-flight agent task")
                self._agent_worker.terminate()
                self._agent_worker.wait(1500)
        if self._llm_worker is not None and self._llm_worker.isRunning():
            self._llm_worker.requestInterruption()
            if not self._llm_worker.wait(5000):
                self.log.warning("Terminating an in-flight local model request")
                self._llm_worker.terminate()
                self._llm_worker.wait(1500)
        if self._voice_reply_worker is not None and self._voice_reply_worker.isRunning():
            self._voice_reply_worker.cancel()
            self._voice_reply_worker.wait(2000)
        if self.recorder.is_recording:
            try:
                self.recorder.stop_and_wait(120_000)
            except Exception:
                self.log.exception("Could not finalize recording during shutdown")
        self.voice.shutdown()
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
        self.governor.stop()
        self.local_model.unload()
        if self._onboarding_dialog is not None:
            self._onboarding_dialog.close()
        super().closeEvent(event)


def ensure_application_icon() -> QIcon:
    path = assets_dir() / "blaze_logo.svg"
    return QIcon(str(path)) if path.exists() else QIcon()
