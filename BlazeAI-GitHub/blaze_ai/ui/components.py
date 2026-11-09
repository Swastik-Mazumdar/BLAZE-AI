"""Reusable monochrome UI atoms and custom-painted Nothing-style components."""

from __future__ import annotations

import math

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QApplication,
    QGraphicsOpacityEffect,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import THEMES, Theme, font_stack, theme_manager


class VoiceOrb(QWidget):
    """System-wide always-on-top voice indicator.

    This is a real top-level window, not a child of the main window, so it stays
    visible over other apps while Blaze is minimised or in the background. On
    macOS ``WA_MacAlwaysShowToolWindow`` keeps the tool window on screen when the
    app is inactive.
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        always_show = getattr(Qt.WidgetAttribute, "WA_MacAlwaysShowToolWindow", None)
        if always_show is not None:
            self.setAttribute(always_show, True)
        self.setFixedSize(118, 70)
        self._state = "idle"
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._animate)
        self.setAccessibleName("Blaze voice activity")
        self.hide()

    def _place(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        rect = screen.availableGeometry()
        self.move(
            rect.center().x() - self.width() // 2,
            rect.bottom() - self.height() - 84,
        )

    def show_state(self, state: str) -> None:
        self._state = state
        if state == "hidden":
            self._timer.stop()
            self.hide()
            return
        self._place()
        self.show()
        # Re-raise every time so the indicator stays above the app being used.
        self.raise_()
        self._timer.start()
        self.update()

    def _animate(self) -> None:
        self._phase += 1
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        theme = theme_manager.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        panel = QColor(theme.surface)
        panel.setAlpha(242)
        painter.setPen(QPen(QColor(theme.border_strong), 1))
        painter.setBrush(panel)
        painter.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 24, 24)
        accent = QColor(theme.accent)
        center_x, center_y = self.width() / 2, self.height() / 2
        if self._state in {"listening", "speaking"}:
            painter.setPen(QPen(accent, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for index, offset in enumerate((-30, -20, -10, 0, 10, 20, 30)):
                phase = (self._phase * 0.37) + index * 0.9
                height = 7 + abs(math.sin(phase)) * 23
                painter.drawLine(
                    int(center_x + offset), int(center_y - height / 2),
                    int(center_x + offset), int(center_y + height / 2),
                )
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            for index, offset in enumerate((-10, 0, 10)):
                pulse = 0.55 + 0.45 * abs(math.sin(self._phase * 0.16 + index))
                dot = QColor(accent)
                dot.setAlphaF(pulse)
                painter.setBrush(dot)
                radius = 3.3 if self._state == "idle" else 3.3 + pulse * 1.6
                painter.drawEllipse(QRectF(center_x + offset - radius, center_y - radius, radius * 2, radius * 2))


class LogoMark(QWidget):
    def __init__(self, size: int = 30, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event) -> None:  # noqa: N802
        theme = theme_manager.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2.0, 2.0, self.width() - 4.0, self.height() - 4.0)
        pen = QPen(QColor(theme.text), 1.7)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)
        # An open aperture with a signal-red node: the Blaze glyph.
        painter.drawArc(rect.adjusted(4, 4, -4, -4), 35 * 16, 250 * 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.accent))
        dot = max(3.0, self.width() * 0.14)
        painter.drawEllipse(
            QRectF(self.width() - dot - 2.0, 3.0, dot, dot)
        )
        painter.setBrush(QColor(theme.text))
        inner = max(2.0, self.width() * 0.08)
        painter.drawEllipse(
            QRectF(
                self.width() / 2 - inner / 2,
                self.height() / 2 - inner / 2,
                inner,
                inner,
            )
        )


class Avatar(QWidget):
    def __init__(self, size: int = 36, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = "B"
        self.setFixedSize(size, size)

    def set_text(self, text: str) -> None:
        self._text = (text or "B").strip()[:1].upper()
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        theme = theme_manager.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        painter.setPen(QPen(QColor(theme.border_strong), 1.0))
        painter.setBrush(QColor(theme.surface_alt))
        painter.drawEllipse(rect)
        painter.setPen(QColor(theme.text))
        font = QFont(font_stack()[0], max(9, self.width() // 3))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


class ElidedLabel(QLabel):
    """Single-line label that elides cleanly without changing layout width."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text or ""
        self.setToolTip(self._full)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full)
        self.update()

    def text(self) -> str:
        return self._full

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        return QSize(20, fm.height())

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._full), fm.height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        self.drawFrame(painter)
        rect = self.contentsRect()
        elided = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, rect.width()
        )
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(rect, int(self.alignment()), elided)


class SegmentMeter(QWidget):
    """A compact segmented meter, drawn rather than using a stock progress bar."""

    def __init__(
        self, segments: int = 22, height: int = 9, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.segments = max(4, segments)
        self._fraction = 0.0
        self._hot = False
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, fraction: float, *, hot: bool = False) -> None:
        self._fraction = max(0.0, min(1.0, float(fraction)))
        self._hot = hot
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        theme = theme_manager.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gap = 2.0
        width = (self.width() - gap * (self.segments - 1)) / self.segments
        active = int(round(self._fraction * self.segments + 0.0001))
        for index in range(self.segments):
            rect = QRectF(index * (width + gap), 0.0, width, self.height())
            if index < active:
                color = QColor(theme.accent if self._hot else theme.text)
            else:
                color = QColor(theme.surface_alt)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 1.5, 1.5)


class ThemeCard(QPushButton):
    """Canvas swatch for onboarding and settings theme selection."""

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(f"{theme.name} — {theme.tagline}")

    def paintEvent(self, _event) -> None:  # noqa: N802
        theme = self.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        if self.isChecked():
            border = QColor(theme_manager.theme.accent)
            border_width = 1.8
        else:
            border = QColor(theme_manager.theme.border)
            border_width = 1.0
        painter.setPen(QPen(border, border_width))
        painter.setBrush(QColor(theme.bg))
        painter.drawRoundedRect(rect, 10, 10)

        # Three monochrome signal bars + one red node.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.text))
        for index, height in enumerate((7, 12, 17)):
            painter.drawRoundedRect(
                QRectF(14 + index * 5, 22 - height / 2, 3, height), 1.5, 1.5
            )
        painter.setBrush(QColor(theme.accent))
        painter.drawEllipse(QRectF(37, 18, 5, 5))

        name_font = QFont(font_stack()[1], 9)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.1)
        painter.setFont(name_font)
        painter.setPen(QColor(theme.text))
        painter.drawText(QRectF(14, 40, self.width() - 28, 18), "  " + theme.name)

        detail_font = QFont(font_stack()[0], 8)
        painter.setFont(detail_font)
        painter.setPen(QColor(theme.muted))
        painter.drawText(
            QRectF(14, 56, self.width() - 28, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            theme.tagline,
        )
        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme_manager.theme.accent))
            painter.drawEllipse(QRectF(self.width() - 18, 12, 6, 6))


class Toast(QFrame):
    """Transient, non-blocking status message anchored to the window bottom."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._label = QLabel("", self)
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 11, 18, 11)
        layout.addWidget(self._label)
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)
        self._anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self.hide()

    def show_message(self, text: str, *, duration_ms: int = 4200) -> None:
        theme = theme_manager.theme
        self.setStyleSheet(
            f"#Toast {{ background-color: {theme.text}; color: {theme.bg}; "
            f"border: 1px solid {theme.text}; border-radius: 10px; }}"
            f"#Toast QLabel {{ color: {theme.bg}; font-family: {font_stack()[1]}; "
            f"font-size: 10px; }}"
        )
        self._label.setText(text)
        self.adjustSize()
        self.setMaximumWidth(max(260, min(620, self.parentWidget().width() - 100)))
        self.adjustSize()
        self.position()
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setDuration(180)
        self._anim.setStartValue(self._opacity.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._hide_timer.start(duration_ms)

    def position(self) -> None:
        parent = self.parentWidget()
        if not parent:
            return
        self.adjustSize()
        x = max(20, (parent.width() - self.width()) // 2)
        y = max(20, parent.height() - self.height() - 132)
        self.move(x, y)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._anim.setDuration(280)
        self._anim.setStartValue(self._opacity.opacity())
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._after_fade)
        self._anim.start()

    def _after_fade(self) -> None:
        try:
            self._anim.finished.disconnect(self._after_fade)
        except TypeError:
            pass
        if self._opacity.opacity() <= 0.01:
            self.hide()


def section_label(text: str, parent: QWidget | None = None) -> QLabel:
    label = QLabel(text.upper(), parent)
    label.setObjectName("SectionLabel")
    return label


def mono_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("MonoLabel")
    return label


def tiny_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("TinyLabel")
    return label


def divider(parent: QWidget | None = None) -> QFrame:
    frame = QFrame(parent)
    frame.setObjectName("Divider")
    frame.setFixedHeight(1)
    return frame


def make_icon_button(text: str, tooltip: str = "", size: int = 32) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("WindowButton")
    button.setFixedSize(size, size)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        button.setToolTip(tooltip)
    return button


def size_hint(width: int, height: int) -> QSize:
    return QSize(width, height)
