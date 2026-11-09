"""Thread history sidebar with search, management and live system status."""

from __future__ import annotations

from datetime import datetime, timedelta

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.chat_store import ChatSummary
from ..core.system import SystemSnapshot, format_bytes
from ..theme import theme_manager
from .components import (
    Avatar,
    ElidedLabel,
    SegmentMeter,
    divider,
    section_label,
)


class ChatRow(QFrame):
    selected = pyqtSignal(str)
    rename_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)

    def __init__(self, chat: ChatSummary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chat = chat
        self.setObjectName("ChatRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(56)
        self._selected = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 6, 4, 6)
        layout.setSpacing(7)
        self._accent = QFrame(self)
        self._accent.setFixedWidth(2)
        self._accent.setStyleSheet("background: transparent; border-radius: 1px;")
        layout.addWidget(self._accent)

        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        self._title = ElidedLabel(chat.title, self)
        self._title.setObjectName("ChatRowTitle")
        self._title.setProperty("selected", "false")
        self._title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text_column.addWidget(self._title)
        preview = ElidedLabel(chat.preview or "No messages yet", self)
        preview.setObjectName("ChatRowPreview")
        preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text_column.addWidget(preview)
        layout.addLayout(text_column, 1)

        right_column = QVBoxLayout()
        right_column.setSpacing(1)
        time_label = QLabel(_format_time(chat.updated_at), self)
        time_label.setObjectName("ChatRowTime")
        time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        time_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        right_column.addWidget(time_label)
        self._menu_button = QPushButton("⋯", self)
        self._menu_button.setObjectName("ChatMenuButton")
        self._menu_button.setFixedSize(24, 22)
        self._menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._menu_button.setToolTip("Manage thread")
        self._menu_button.clicked.connect(self._open_menu)
        right_column.addWidget(self._menu_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(right_column)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        self._title.setProperty("selected", "true" if selected else "false")
        self._accent.setStyleSheet(
            "background: "
            + (theme_manager.theme.accent if selected else "transparent")
            + "; border-radius: 1px;"
        )
        for widget in (self, self._title):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.chat.id)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = self._make_menu()
        menu.exec(event.globalPos())

    def _open_menu(self) -> None:
        menu = self._make_menu()
        point: QPoint = self._menu_button.mapToGlobal(self._menu_button.rect().bottomLeft())
        menu.exec(point)

    def _make_menu(self) -> QMenu:
        menu = QMenu(self)
        rename = QAction("Rename thread", menu)
        rename.triggered.connect(lambda: self.rename_requested.emit(self.chat.id))
        delete = QAction("Delete thread", menu)
        delete.triggered.connect(lambda: self.delete_requested.emit(self.chat.id))
        menu.addAction(rename)
        menu.addAction(delete)
        return menu


class Sidebar(QWidget):
    new_chat_requested = pyqtSignal()
    chat_selected = pyqtSignal(str)
    rename_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    settings_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(276)
        self._chats: list[ChatSummary] = []
        self._selected_id = ""
        self._rows: list[ChatRow] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 18, 16, 16)
        root.setSpacing(10)

        profile = QHBoxLayout()
        profile.setSpacing(10)
        self._avatar = Avatar(36, self)
        profile.addWidget(self._avatar)
        profile_text = QVBoxLayout()
        profile_text.setSpacing(1)
        self._name = ElidedLabel("FRIEND", self)
        self._name.setObjectName("TitleLabel")
        self._mode = ElidedLabel("LOCAL BUTLER · OFFLINE", self)
        self._mode.setObjectName("TinyLabel")
        profile_text.addWidget(self._name)
        profile_text.addWidget(self._mode)
        profile.addLayout(profile_text, 1)
        root.addLayout(profile)
        root.addSpacing(8)

        self._new_button = QPushButton("＋   NEW TRANSMISSION", self)
        self._new_button.setProperty("variant", "primary")
        self._new_button.setMinimumHeight(40)
        self._new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_button.clicked.connect(self.new_chat_requested)
        root.addWidget(self._new_button)

        header_row = QHBoxLayout()
        header_row.addWidget(section_label("HISTORY", self))
        header_row.addStretch(1)
        self._count = QLabel("00", self)
        self._count.setObjectName("TinyLabel")
        header_row.addWidget(self._count)
        root.addLayout(header_row)

        self._search = QLineEdit(self)
        self._search.setObjectName("SearchField")
        self._search.setPlaceholderText("Search transmissions…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._render)
        root.addWidget(self._search)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list_host = QWidget(self._scroll)
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 2, 0, 2)
        self._list_layout.setSpacing(3)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll, 1)

        root.addWidget(divider(self))
        status_card = QFrame(self)
        status_card.setObjectName("Card")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(11, 9, 11, 10)
        status_layout.setSpacing(7)
        mode_row = QHBoxLayout()
        self._status_dot = QLabel("●", status_card)
        self._status_dot.setStyleSheet(f"color: {theme_manager.theme.muted}; font-size: 9px;")
        self._status_label = ElidedLabel("LOCAL BUTLER", status_card)
        self._status_label.setObjectName("MonoLabel")
        mode_row.addWidget(self._status_dot)
        mode_row.addWidget(self._status_label, 1)
        status_layout.addLayout(mode_row)
        ram_row = QHBoxLayout()
        ram_title = QLabel("RAM", status_card)
        ram_title.setObjectName("TinyLabel")
        self._ram_label = QLabel("— / —", status_card)
        self._ram_label.setObjectName("TinyLabel")
        ram_row.addWidget(ram_title)
        ram_row.addStretch(1)
        ram_row.addWidget(self._ram_label)
        status_layout.addLayout(ram_row)
        self._ram_meter = SegmentMeter(18, 6, status_card)
        status_layout.addWidget(self._ram_meter)
        root.addWidget(status_card)

        self._settings_button = QPushButton("⌘   SETTINGS", self)
        self._settings_button.setMinimumHeight(38)
        self._settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_button.clicked.connect(self.settings_requested)
        root.addWidget(self._settings_button)

    def set_user(self, name: str) -> None:
        display = (name or "friend").strip()
        self._name.setText(display.upper())
        self._avatar.set_text(display)

    def set_mode(self, label: str, *, online: bool = False) -> None:
        self._mode.setText(label.upper())
        status = "MODEL READY" if online else "LOCAL BUTLER"
        self._status_label.setText(status)
        color = theme_manager.theme.accent if online else theme_manager.theme.muted
        self._status_dot.setStyleSheet(f"color: {color}; font-size: 9px;")

    def set_ram(self, snapshot: SystemSnapshot, limit_percent: int) -> None:
        fraction = snapshot.process_ram / snapshot.total_ram if snapshot.total_ram else 0.0
        hot = snapshot.process_ram > snapshot.total_ram * (limit_percent / 100.0)
        self._ram_label.setText(
            f"{format_bytes(snapshot.process_ram)} / {format_bytes(snapshot.total_ram)}"
        )
        self._ram_meter.set_value(fraction, hot=hot)

    def set_chats(self, chats: list[ChatSummary], selected_id: str) -> None:
        self._chats = chats
        self._selected_id = selected_id
        self._count.setText(f"{len(chats):02d}")
        self._render()

    def set_selected(self, chat_id: str) -> None:
        self._selected_id = chat_id
        for row in self._rows:
            row.set_selected(row.chat.id == chat_id)

    def _render(self) -> None:
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._rows.clear()
        query = self._search.text().strip().lower()
        chats = [
            chat
            for chat in self._chats
            if not query or query in chat.title.lower() or query in chat.preview.lower()
        ]
        if not chats:
            label = QLabel(
                "No transmissions found." if query else "No transmissions yet.\nStart a new one.",
                self._list_host,
            )
            label.setObjectName("MutedLabel")
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setContentsMargins(8, 26, 8, 26)
            self._list_layout.insertWidget(0, label)
            return

        if query:
            groups: list[tuple[str, list[ChatSummary]]] = [("RESULTS", chats)]
        else:
            groups = _group_chats(chats)
        index = 0
        for group_name, group_chats in groups:
            header = section_label(group_name, self._list_host)
            header.setContentsMargins(4, 9 if index else 2, 0, 2)
            self._list_layout.insertWidget(index, header)
            index += 1
            for chat in group_chats:
                row = ChatRow(chat, self._list_host)
                row.set_selected(chat.id == self._selected_id)
                row.selected.connect(self.chat_selected)
                row.rename_requested.connect(self.rename_requested)
                row.delete_requested.connect(self.delete_requested)
                self._list_layout.insertWidget(index, row)
                self._rows.append(row)
                index += 1


def _group_chats(chats: list[ChatSummary]) -> list[tuple[str, list[ChatSummary]]]:
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    groups: dict[str, list[ChatSummary]] = {
        "TODAY": [],
        "YESTERDAY": [],
        "PREVIOUS": [],
    }
    for chat in chats:
        date = datetime.fromtimestamp(chat.updated_at).date()
        if date == today:
            groups["TODAY"].append(chat)
        elif date == yesterday:
            groups["YESTERDAY"].append(chat)
        else:
            groups["PREVIOUS"].append(chat)
    return [(name, items) for name, items in groups.items() if items]


def _format_time(timestamp: float) -> str:
    when = datetime.fromtimestamp(timestamp)
    now = datetime.now()
    if when.date() == now.date():
        return when.strftime("%H:%M")
    if when.date() == (now - timedelta(days=1)).date():
        return "YDAY"
    return when.strftime("%b %d").upper()
