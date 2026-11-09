"""Chat surface: threads, message bubbles, composer and push-to-talk UI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QKeyEvent, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.chat_store import Message
from ..theme import theme_manager
from .components import LogoMark
from .markdown import markdown_to_html


def _display_text(role: str, text: str) -> str:
    if role != "assistant":
        return text
    try:
        from ..core.agent import extract_final_text

        return extract_final_text(text)
    except Exception:
        return text


class MessageBubble(QWidget):
    def __init__(
        self,
        role: str,
        text: str,
        created_at: float | None = None,
        *,
        meta: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        is_user = role == "user"
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        frame = QFrame(self)
        frame.setObjectName("ChatBubbleUser" if is_user else "ChatBubbleAssistant")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        frame.setMaximumWidth(700)
        frame.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(15, 11, 15, 13)
        layout.setSpacing(6)

        stamp = datetime.fromtimestamp(created_at or datetime.now().timestamp()).strftime(
            "%H:%M"
        )
        role_text = "YOU" if is_user else ("BLAZE" + (f" · {meta}" if meta else ""))
        header = QLabel(f"{role_text}  ·  {stamp}", frame)
        header.setObjectName("BubbleRole")
        header.setProperty("role", "user" if is_user else "assistant")
        layout.addWidget(header)

        body = QLabel(frame)
        body.setObjectName("BubbleText")
        body.setProperty("role", "user" if is_user else "assistant")
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        body.setMaximumWidth(660)
        display = _display_text(role, text)
        try:
            body.setText(markdown_to_html(display, theme_manager.theme))
        except Exception:
            body.setTextFormat(Qt.TextFormat.PlainText)
            body.setText(str(display))
        body.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        layout.addWidget(body)

        self.text = display
        if is_user:
            outer.addStretch(1)
            outer.addWidget(frame)
        else:
            outer.addWidget(frame)
            outer.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)


class TypingBubble(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(self)
        frame.setObjectName("ChatBubbleAssistant")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        frame.setMaximumWidth(380)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(15, 11, 15, 13)
        layout.setSpacing(4)
        role = QLabel("BLAZE · THINKING", frame)
        role.setObjectName("BubbleRole")
        layout.addWidget(role)
        self._dots = QLabel("●", frame)
        self._dots.setObjectName("BubbleText")
        self._dots.setStyleSheet("font-size: 14px;")
        layout.addWidget(self._dots)
        outer.addWidget(frame)
        outer.addStretch(1)
        self._step = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(320)

    def _tick(self) -> None:
        self._step = (self._step + 1) % 4
        self._dots.setText("●" * (1 + self._step) + "○" * (3 - self._step))

    def stop(self) -> None:
        self._timer.stop()


class QuickAction(QPushButton):
    def __init__(self, label: str, prompt: str, parent: QWidget | None = None) -> None:
        super().__init__(label, parent)
        self.setObjectName("QuickAction")
        self.prompt = prompt
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(42)


class EmptyState(QWidget):
    quick_action = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 30, 32, 40)
        layout.setSpacing(10)
        layout.addStretch(1)
        logo = LogoMark(48)
        logo_row = QHBoxLayout()
        logo_row.addStretch(1)
        logo_row.addWidget(logo)
        logo_row.addStretch(1)
        layout.addLayout(logo_row)
        layout.addSpacing(10)
        self._title = QLabel("GOOD EVENING,\nFRIEND.", self)
        self._title.setObjectName("EmptyTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title)
        self._subtitle = QLabel(
            "Local butler online. Command the machine, or add a local model folder "
            "to unlock agent mode and full computer control.",
            self,
        )
        self._subtitle.setObjectName("EmptySubtitle")
        self._subtitle.setWordWrap(True)
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._subtitle)
        layout.addSpacing(18)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        actions = [
            ("↗  OPEN VS CODE", "open VS Code"),
            ("◉  SYSTEM STATUS", "system status"),
            ("▣  START RECORDING", "start recording"),
            ("✦  BRIEF ME", "brief me"),
        ]
        for index, (label, prompt) in enumerate(actions):
            button = QuickAction(label, prompt, self)
            button.clicked.connect(lambda _checked=False, p=prompt: self.quick_action.emit(p))
            grid.addWidget(button, index // 2, index % 2)
        wrapper = QHBoxLayout()
        wrapper.addStretch(1)
        action_widget = QWidget(self)
        action_widget.setLayout(grid)
        action_widget.setMaximumWidth(560)
        wrapper.addWidget(action_widget, 3)
        wrapper.addStretch(1)
        layout.addLayout(wrapper)
        layout.addStretch(2)

    def set_name(self, name: str) -> None:
        hour = datetime.now().hour
        greeting = (
            "GOOD MORNING" if hour < 12 else "GOOD AFTERNOON" if hour < 18 else "GOOD EVENING"
        )
        display = (name or "friend").strip().upper()
        self._title.setText(f"{greeting},\n{display}.")

    def set_online(self, online: bool) -> None:
        if online:
            self._subtitle.setText(
                "Agent online. Ask Blaze to edit videos, draft email, reply to "
                "texts, or take control of any app."
            )
        else:
            self._subtitle.setText(
                "Local butler online. Command the machine, or add a local model "
                "folder to unlock agent mode and full computer control."
            )


class ComposerEdit(QTextEdit):
    submit = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Composer")
        self.setPlaceholderText("Ask anything to Blaze AI: Your personal butler and AI companion…")
        self.setAcceptRichText(False)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(48)
        self.setMaximumHeight(150)
        self.textChanged.connect(self._schedule_adjust)
        self._adjust_height()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self.submit.emit()
                event.accept()
                return
        super().keyPressEvent(event)

    def _schedule_adjust(self) -> None:
        def adjust() -> None:
            try:
                self._adjust_height()
            except RuntimeError:
                return

        QTimer.singleShot(0, adjust)

    def _adjust_height(self) -> None:
        document_height = self.document().size().height()
        height = int(document_height + 24)
        self.setFixedHeight(max(48, min(150, height)))


class ChatView(QWidget):
    send_requested = pyqtSignal(str)
    stop_requested = pyqtSignal()
    mic_pressed = pyqtSignal()
    mic_released = pyqtSignal()
    quick_action = pyqtSignal(str)
    rename_thread_requested = pyqtSignal()
    delete_thread_requested = pyqtSignal()
    model_selected = pyqtSignal(str)

    def __init__(self, user_name: str = "friend", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ChatPane")
        self._user_name = user_name
        self._thread_id = ""
        self._mode_label = "LOCAL BUTLER · OFFLINE"
        self._online = False
        self._messages: list[MessageBubble] = []
        self._typing: TypingBubble | None = None
        self._busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("ChatHeader")
        header.setFixedHeight(76)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(26, 0, 18, 0)
        header_layout.setSpacing(12)
        title_column = QVBoxLayout()
        title_column.setSpacing(4)
        self._title = QLabel("NEW TRANSMISSION", header)
        self._title.setObjectName("TitleLabel")
        self._subtitle = QLabel(self._mode_label, header)
        self._subtitle.setObjectName("TinyLabel")
        title_column.addWidget(self._title)
        title_column.addWidget(self._subtitle)
        header_layout.addLayout(title_column, 1)
        self._model_combo = QComboBox(header)
        self._model_combo.setObjectName("ModelCombo")
        self._model_combo.setMinimumWidth(150)
        self._model_combo.setMaximumWidth(260)
        self._model_combo.setToolTip("Model used by this chat")
        self._model_combo.addItem("OFFLINE BUTLER", "")
        self._model_combo.currentIndexChanged.connect(self._emit_model_selected)
        header_layout.addWidget(self._model_combo, 0, Qt.AlignmentFlag.AlignVCenter)
        self._rec_badge = QLabel("● REC 00:00", header)
        self._rec_badge.setObjectName("RecDot")
        self._rec_badge.hide()
        header_layout.addWidget(self._rec_badge)
        self._menu_button = QPushButton("···", header)
        self._menu_button.setObjectName("ChatMenuButton")
        self._menu_button.setFixedSize(32, 30)
        self._menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._menu_button.setToolTip("Thread options")
        self._menu_button.clicked.connect(self._show_thread_menu)
        header_layout.addWidget(self._menu_button)
        root.addWidget(header)

        self._stack_host = QWidget(self)
        self._stack = QStackedLayout(self._stack_host)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._empty = EmptyState(self._stack_host)
        self._empty.set_name(user_name)
        self._empty.quick_action.connect(self.quick_action)
        self._stack.addWidget(self._empty)

        self._scroll = QScrollArea(self._stack_host)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._message_host = QWidget(self._scroll)
        self._message_layout = QVBoxLayout(self._message_host)
        self._message_layout.setContentsMargins(26, 24, 26, 26)
        self._message_layout.setSpacing(12)
        self._message_layout.addStretch(1)
        self._scroll.setWidget(self._message_host)
        self._stack.addWidget(self._scroll)
        self._stack.setCurrentWidget(self._empty)
        root.addWidget(self._stack_host, 1)

        composer_shell = QWidget(self)
        composer_shell.setObjectName("ComposerShell")
        composer_layout = QVBoxLayout(composer_shell)
        composer_layout.setContentsMargins(24, 14, 24, 12)
        composer_layout.setSpacing(7)
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.composer = ComposerEdit(composer_shell)
        self.composer.submit.connect(self._send)
        input_row.addWidget(self.composer, 1)
        self._mic = QPushButton("◉", composer_shell)
        self._mic.setObjectName("MicButton")
        self._mic.setFixedSize(36, 36)
        self._mic.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mic.setToolTip("Push and hold to speak; interrupts a reply")
        self._mic.pressed.connect(self.mic_pressed)
        self._mic.released.connect(self.mic_released)
        input_row.addWidget(self._mic, 0, Qt.AlignmentFlag.AlignBottom)
        self._send_button = QPushButton("↗", composer_shell)
        self._send_button.setObjectName("SendButton")
        self._send_button.setFixedSize(36, 36)
        self._send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_button.setToolTip("Send (Enter)")
        self._send_button.clicked.connect(self._send)
        input_row.addWidget(self._send_button, 0, Qt.AlignmentFlag.AlignBottom)
        self._stop_button = QPushButton("■", composer_shell)
        self._stop_button.setObjectName("SendButton")
        self._stop_button.setFixedSize(36, 36)
        self._stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_button.setToolTip("Stop response or speech")
        self._stop_button.clicked.connect(self.stop_requested)
        self._stop_button.hide()
        input_row.addWidget(self._stop_button, 0, Qt.AlignmentFlag.AlignBottom)
        composer_layout.addLayout(input_row)
        hint_row = QHBoxLayout()
        hint = QLabel("ENTER TO SEND  ·  SHIFT+ENTER NEW LINE", composer_shell)
        hint.setObjectName("ComposerHint")
        self._voice_hint = QLabel("MIC BUTTON OR CTRL+SHIFT+SPACE  ·  VOICE SENDS AUTOMATICALLY", composer_shell)
        self._voice_hint.setObjectName("ComposerHint")
        hint_row.addWidget(hint)
        hint_row.addStretch(1)
        hint_row.addWidget(self._voice_hint)
        composer_layout.addLayout(hint_row)
        root.addWidget(composer_shell)

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def set_user_name(self, name: str) -> None:
        self._user_name = name
        self._empty.set_name(name)

    def set_thread(
        self,
        thread_id: str,
        title: str,
        messages: list[Message],
        *,
        mode_label: str = "",
        online: bool = False,
        model_choices: list[tuple[str, str]] | None = None,
        model_path: str = "",
    ) -> None:
        self._thread_id = thread_id
        self._title.setText(title.upper() if title else "NEW TRANSMISSION")
        self._empty.set_name(self._user_name)
        self._empty.set_online(online)
        self._online = online
        if mode_label:
            self._mode_label = mode_label
            self._subtitle.setText(mode_label.upper())
        if model_choices is not None:
            self.set_model_choices(model_choices, model_path)
        self._clear_messages()
        for message in messages:
            if message.role in {"user", "assistant"}:
                self.add_message(
                    message.role, message.content, message.created_at, scroll=False
                )
        self._update_stack()
        QTimer.singleShot(30, self.scroll_to_bottom)

    def set_model_choices(
        self, choices: list[tuple[str, str]], current: str = ""
    ) -> None:
        """Fill the per-chat model selector without emitting a change."""
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for path, label in choices or [("", "OFFLINE BUTLER")]:
            self._model_combo.addItem(label.upper(), path)
        index = self._model_combo.findData(current or "")
        if index < 0 and current:
            # Keep a chat usable even if its model left the registry.
            self._model_combo.addItem(Path(current).name.upper(), current)
            index = self._model_combo.count() - 1
        self._model_combo.setCurrentIndex(index if index >= 0 else 0)
        self._model_combo.blockSignals(False)

    def set_model_busy(self, text: str) -> None:
        """Show a transient loading state in the selector."""
        self._model_combo.blockSignals(True)
        self._model_combo.setToolTip(text)
        self._model_combo.blockSignals(False)

    def _emit_model_selected(self, _index: int) -> None:
        self.model_selected.emit(str(self._model_combo.currentData() or ""))

    def update_thread_title(self, title: str) -> None:
        self._title.setText(title.upper() if title else "NEW TRANSMISSION")

    def set_mode(self, label: str, *, online: bool = False) -> None:
        self._mode_label = label.upper()
        self._subtitle.setText(self._mode_label)
        self._online = online
        self._empty.set_online(online)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_button.setVisible(not busy)
        self._send_button.setEnabled(not busy)
        self._stop_button.setVisible(busy)
        self._stop_button.setEnabled(busy)
        if busy:
            self.show_typing()
        else:
            self.hide_typing()

    def show_typing(self) -> None:
        if self._typing is not None:
            return
        self._typing = TypingBubble(self._message_host)
        self._message_layout.insertWidget(self._message_layout.count() - 1, self._typing)
        self.scroll_to_bottom()

    def hide_typing(self) -> None:
        if self._typing is None:
            return
        self._typing.stop()
        self._message_layout.removeWidget(self._typing)
        self._typing.deleteLater()
        self._typing = None

    def add_message(
        self,
        role: str,
        text: str,
        created_at: float | None = None,
        *,
        meta: str = "",
        scroll: bool = True,
    ) -> None:
        bubble = MessageBubble(role, text, created_at, meta=meta, parent=self._message_host)
        self._messages.append(bubble)
        self._message_layout.insertWidget(self._message_layout.count() - 1, bubble)
        self._update_stack()
        if scroll:
            self.scroll_to_bottom()

    def clear_thread(self) -> None:
        self._clear_messages()
        self._update_stack()

    def scroll_to_bottom(self) -> None:
        def scroll() -> None:
            # The widget can be destroyed between scheduling and firing.
            try:
                bar = self._scroll.verticalScrollBar()
            except RuntimeError:
                return
            bar.setValue(bar.maximum())

        QTimer.singleShot(20, scroll)

    def set_voice_state(self, state: str) -> None:
        if state == "recording":
            self._mic.setText("■")
            self._mic.setProperty("recording", "true")
            self._voice_hint.setText("LISTENING  ·  RELEASE TO TRANSCRIBE")
        elif state == "transcribing":
            self._mic.setText("…")
            self._mic.setProperty("recording", "true")
            self._voice_hint.setText("TRANSCRIBING  ·  LOCAL WHISPER")
        elif state == "loading":
            self._mic.setText("…")
            self._mic.setProperty("recording", "true")
            self._voice_hint.setText("LOADING WHISPER MODEL  ·  FIRST RUN")
        else:
            self._mic.setText("◉")
            self._mic.setProperty("recording", "false")
            self._voice_hint.setText("MIC BUTTON OR CTRL+SHIFT+SPACE  ·  VOICE SENDS AUTOMATICALLY")
        self._mic.style().unpolish(self._mic)
        self._mic.style().polish(self._mic)

    def set_voice_hint(self, text: str) -> None:
        self._voice_hint.setText(text.upper())

    def set_recording(self, recording: bool, elapsed: float = 0.0) -> None:
        self._rec_badge.setVisible(recording)
        if recording:
            minutes, seconds = divmod(int(elapsed), 60)
            self._rec_badge.setText(f"● REC {minutes:02d}:{seconds:02d}")

    def insert_text(self, text: str, *, send: bool = False) -> None:
        if send:
            self.composer.setPlainText(text)
            self._send()
        else:
            self.composer.setPlainText(text)
            cursor = self.composer.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.composer.setTextCursor(cursor)
            self.composer.setFocus()

    def focus_composer(self) -> None:
        self.composer.setFocus()

    def _send(self) -> None:
        text = self.composer.toPlainText().strip()
        if not text or self._busy:
            return
        self.composer.clear()
        self.send_requested.emit(text)

    def _clear_messages(self) -> None:
        self.hide_typing()
        for bubble in self._messages:
            self._message_layout.removeWidget(bubble)
            bubble.deleteLater()
        self._messages.clear()

    def _update_stack(self) -> None:
        self._stack.setCurrentWidget(self._scroll if self._messages else self._empty)

    def _show_thread_menu(self) -> None:
        menu = QMenu(self)
        rename = QAction("Rename thread", menu)
        rename.triggered.connect(self.rename_thread_requested)
        delete = QAction("Delete thread", menu)
        delete.triggered.connect(self.delete_thread_requested)
        menu.addAction(rename)
        menu.addAction(delete)
        point: QPoint = self._menu_button.mapToGlobal(
            self._menu_button.rect().bottomLeft()
        )
        menu.exec(point)
