"""First-boot onboarding: identity, minimalist theme, personalized welcome."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..theme import THEMES, theme_manager
from ..version import APP_NAME, TAGLINE
from .components import LogoMark, ThemeCard, section_label


class OnboardingDialog(QDialog):
    completed = pyqtSignal(dict)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._selected_theme = settings.get("theme", "onyx")
        self._theme_cards: dict[str, ThemeCard] = {}
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.resize(940, 620)
        self.setMinimumSize(860, 580)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        self._card = QFrame(self)
        self._card.setObjectName("OnboardingCard")
        self._card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self._card)

        root = QVBoxLayout(self._card)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget(self._card)
        header.setFixedHeight(72)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(28, 0, 22, 0)
        header_layout.setSpacing(10)
        header_layout.addWidget(LogoMark(26))
        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel(APP_NAME.upper(), header)
        name.setObjectName("TitleLabel")
        tag = QLabel("FIRST BOOT · " + TAGLINE, header)
        tag.setObjectName("TinyLabel")
        brand.addWidget(name)
        brand.addWidget(tag)
        header_layout.addLayout(brand)
        header_layout.addStretch(1)
        self._step = QLabel("01 / 03", header)
        self._step.setObjectName("MonoLabel")
        header_layout.addWidget(self._step)
        skip = QPushButton("SKIP", header)
        skip.setProperty("variant", "ghost")
        skip.setCursor(Qt.CursorShape.PointingHandCursor)
        skip.clicked.connect(self._skip)
        header_layout.addWidget(skip)
        root.addWidget(header)
        line = QFrame(header)
        line.setObjectName("Divider")
        line.setFixedHeight(1)
        root.addWidget(line)

        self._stack = QStackedWidget(self._card)
        self._stack.addWidget(self._identity_page())
        self._stack.addWidget(self._theme_page())
        self._stack.addWidget(self._welcome_page())
        root.addWidget(self._stack, 1)

        self._step.setText("01 / 03")
        self._center_on_screen()

    def _identity_page(self) -> QWidget:
        page = QWidget(self._card)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(58, 44, 58, 38)
        layout.setSpacing(14)
        layout.addWidget(section_label("01 — IDENTITY", page))
        title = QLabel("WHAT SHOULD\nWE CALL YOU?", page)
        title.setObjectName("OnboardingTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "A name makes the butler yours. It stays on this machine, in local storage.",
            page,
        )
        subtitle.setObjectName("EmptySubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(12)
        self._name_edit = QLineEdit(page)
        self._name_edit.setPlaceholderText("Type your name")
        self._name_edit.setMaxLength(40)
        self._name_edit.setStyleSheet(
            "QLineEdit { font-size: 22px; padding: 14px 16px; border-radius: 10px; }"
        )
        self._name_edit.textChanged.connect(self._update_identity_state)
        self._name_edit.returnPressed.connect(self._go_theme)
        layout.addWidget(self._name_edit)
        layout.addStretch(1)
        row = QHBoxLayout()
        privacy = QLabel("LOCAL ONLY  ·  NO ACCOUNT  ·  NO CLOUD REQUIRED", page)
        privacy.setObjectName("TinyLabel")
        row.addWidget(privacy)
        row.addStretch(1)
        self._identity_next = QPushButton("CONTINUE  →", page)
        self._identity_next.setProperty("variant", "primary")
        self._identity_next.setMinimumWidth(170)
        self._identity_next.setMinimumHeight(42)
        self._identity_next.setEnabled(False)
        self._identity_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._identity_next.clicked.connect(self._go_theme)
        row.addWidget(self._identity_next)
        layout.addLayout(row)
        return page

    def _theme_page(self) -> QWidget:
        page = QWidget(self._card)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(58, 42, 58, 34)
        layout.setSpacing(12)
        layout.addWidget(section_label("02 — SIGNAL", page))
        title = QLabel("CHOOSE YOUR\nMONOCHROME.", page)
        title.setObjectName("OnboardingTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Four restrained palettes. Signal red appears only when you act.",
            page,
        )
        subtitle.setObjectName("EmptySubtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(8)
        grid = QGridLayout()
        grid.setSpacing(10)
        for index, (key, theme) in enumerate(THEMES.items()):
            card = ThemeCard(theme, page)
            card.setChecked(key == self._selected_theme)
            card.clicked.connect(lambda _checked=False, k=key: self._choose_theme(k))
            self._theme_cards[key] = card
            grid.addWidget(card, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addStretch(1)
        row = QHBoxLayout()
        back = QPushButton("←  BACK", page)
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(lambda: self._show_step(0, "01 / 03"))
        row.addWidget(back)
        row.addStretch(1)
        cont = QPushButton("CONTINUE  →", page)
        cont.setProperty("variant", "primary")
        cont.setMinimumWidth(170)
        cont.setMinimumHeight(42)
        cont.setCursor(Qt.CursorShape.PointingHandCursor)
        cont.clicked.connect(self._go_welcome)
        row.addWidget(cont)
        layout.addLayout(row)
        return page

    def _welcome_page(self) -> QWidget:
        page = QWidget(self._card)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(58, 42, 58, 36)
        layout.setSpacing(12)
        layout.addWidget(section_label("03 — READY", page))
        self._welcome_title = QLabel("HELLO,\nFRIEND.", page)
        self._welcome_title.setObjectName("WelcomeTitle")
        layout.addWidget(self._welcome_title)
        self._welcome_sub = QLabel("", page)
        self._welcome_sub.setObjectName("EmptySubtitle")
        self._welcome_sub.setWordWrap(True)
        layout.addWidget(self._welcome_sub)
        layout.addSpacing(12)
        features = [
            ("01", "LOCAL BUTLER", "Apps, screen capture, typing and power controls."),
            ("02", "AGENT MODE", "A loaded local model can edit videos, email and control apps."),
            ("03", "PRIVATE VOICE", "Push-to-talk faster-whisper. Never always-on."),
        ]
        for number, label, detail in features:
            card = QFrame(page)
            card.setObjectName("Card")
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(14, 10, 14, 10)
            num = QLabel(number, card)
            num.setObjectName("MonoLabel")
            num.setFixedWidth(28)
            card_layout.addWidget(num)
            text = QVBoxLayout()
            text.setSpacing(1)
            title = QLabel(label, card)
            title.setObjectName("TitleLabel")
            desc = QLabel(detail, card)
            desc.setObjectName("TinyLabel")
            text.addWidget(title)
            text.addWidget(desc)
            card_layout.addLayout(text, 1)
            layout.addWidget(card)
        layout.addStretch(1)
        row = QHBoxLayout()
        back = QPushButton("←  BACK", page)
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(lambda: self._show_step(1, "02 / 03"))
        row.addWidget(back)
        row.addStretch(1)
        enter = QPushButton("ENTER BLAZE  ↗", page)
        enter.setProperty("variant", "primary")
        enter.setMinimumWidth(190)
        enter.setMinimumHeight(44)
        enter.setCursor(Qt.CursorShape.PointingHandCursor)
        enter.clicked.connect(self._finish)
        row.addWidget(enter)
        layout.addLayout(row)
        return page

    def _go_theme(self) -> None:
        if not self._name_edit.text().strip():
            self._name_edit.setFocus()
            return
        self._show_step(1, "02 / 03")

    def _go_welcome(self) -> None:
        name = self._name_edit.text().strip() or "Friend"
        hour = datetime.now().hour
        greeting = (
            "GOOD MORNING" if hour < 12 else "GOOD AFTERNOON" if hour < 18 else "GOOD EVENING"
        )
        self._welcome_title.setText(f"HELLO,\n{name.upper()}.")
        self._welcome_sub.setText(
            f"{greeting}. Your local intelligence is calibrated and ready. "
            "The butler runs offline by default — add a key whenever you want "
            "open-ended conversation."
        )
        self._show_step(2, "03 / 03")

    def _show_step(self, index: int, step: str) -> None:
        self._stack.setCurrentIndex(index)
        self._step.setText(step)

    def _choose_theme(self, key: str) -> None:
        self._selected_theme = key
        for card_key, card in self._theme_cards.items():
            card.setChecked(card_key == key)
            card.update()
        theme_manager.set_theme(key)

    def _skip(self) -> None:
        name = self._name_edit.text().strip() or "Friend"
        self._complete(name)

    def _finish(self) -> None:
        name = self._name_edit.text().strip() or "Friend"
        self._complete(name)

    def _complete(self, name: str) -> None:
        payload = {
            "name": name,
            "theme": self._selected_theme,
            "onboarding_complete": True,
        }
        self.completed.emit(payload)
        self.accept()

    def _update_identity_state(self, text: str) -> None:
        self._identity_next.setEnabled(bool(text.strip()))

    def _center_on_screen(self) -> None:
        screen = self.screen()
        if screen:
            geometry = screen.availableGeometry()
            self.move(
                geometry.center().x() - self.width() // 2,
                geometry.center().y() - self.height() // 2,
            )

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._center_on_screen()
        self._name_edit.setFocus()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # Frameless drag from the top band; child controls remain unaffected.
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 84:
            handle = self.windowHandle()
            if handle:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)
