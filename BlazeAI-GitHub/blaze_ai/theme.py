"""Monochrome Nothing-OS-inspired design system for Blaze AI."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PyQt6.QtWidgets import QApplication

from .version import APP_NAME, TAGLINE


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    tagline: str
    bg: str
    surface: str
    surface_alt: str
    raised: str
    text: str
    muted: str
    faint: str
    border: str
    border_strong: str
    accent: str
    accent_soft: str
    accent_text: str
    user_bubble: str
    user_text: str
    is_light: bool = False


THEMES: dict[str, Theme] = {
    "onyx": Theme(
        key="onyx",
        name="ONYX",
        tagline="Absolute black / signal white",
        bg="#000000",
        surface="#080808",
        surface_alt="#111111",
        raised="#161616",
        text="#F4F4F4",
        muted="#8D8D8D",
        faint="#525252",
        border="#252525",
        border_strong="#3D3D3D",
        accent="#FF2D2D",
        accent_soft="#2A0808",
        accent_text="#FFFFFF",
        user_bubble="#F4F4F4",
        user_text="#050505",
    ),
    "bone": Theme(
        key="bone",
        name="BONE",
        tagline="Warm paper / ink black",
        bg="#F1EFEA",
        surface="#FAF9F6",
        surface_alt="#E8E5DE",
        raised="#FFFFFF",
        text="#0A0A0A",
        muted="#6D6A64",
        faint="#A8A49B",
        border="#D0CCC3",
        border_strong="#ABA69B",
        accent="#E22B26",
        accent_soft="#F7D9D6",
        accent_text="#FFFFFF",
        user_bubble="#0A0A0A",
        user_text="#FAF9F6",
        is_light=True,
    ),
    "graphite": Theme(
        key="graphite",
        name="GRAPHITE",
        tagline="Soft dark / concrete grey",
        bg="#141414",
        surface="#1B1B1B",
        surface_alt="#232323",
        raised="#282828",
        text="#EDEDED",
        muted="#9A9A9A",
        faint="#606060",
        border="#343434",
        border_strong="#4A4A4A",
        accent="#FF3B30",
        accent_soft="#35100E",
        accent_text="#FFFFFF",
        user_bubble="#EDEDED",
        user_text="#141414",
    ),
    "void": Theme(
        key="void",
        name="VOID",
        tagline="True black / dimmed signal",
        bg="#000000",
        surface="#030303",
        surface_alt="#0A0A0A",
        raised="#101010",
        text="#E6E6E6",
        muted="#767676",
        faint="#444444",
        border="#1B1B1B",
        border_strong="#303030",
        accent="#FF1F1F",
        accent_soft="#230505",
        accent_text="#FFFFFF",
        user_bubble="#E6E6E6",
        user_text="#000000",
    ),
}

UI_FONT = (
    '".AppleSystemUIFont", "Helvetica Neue", "Avenir Next", "Segoe UI Variable Display", '
    '"Segoe UI", "Inter", sans-serif'
)
MONO_FONT = '"SF Mono", "Cascadia Code", "JetBrains Mono", "Consolas", "Courier New", monospace'


class ThemeManager:
    """Holds the active theme and applies it to every Qt widget at once."""

    def __init__(self) -> None:
        self.theme: Theme = THEMES["onyx"]

    def set_theme(self, key: str, *, apply: bool = True) -> Theme:
        self.theme = THEMES.get(key, THEMES["onyx"])
        if apply:
            self.apply()
        return self.theme

    def apply(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self.theme))


theme_manager = ThemeManager()


def build_stylesheet(t: Theme) -> str:
    """Build the single source of truth for Blaze's visual language."""
    qss = f"""
    * {{
        font-family: {UI_FONT};
        font-size: 13px;
        outline: none;
    }}
    QWidget {{
        color: {t.text};
        background: transparent;
    }}
    QMainWindow, QDialog {{
        background: transparent;
    }}
    #Root {{
        background-color: {t.bg};
        border: 1px solid {t.border};
        border-radius: 16px;
    }}
    #Root[maximized="true"] {{
        border-radius: 0px;
        border: none;
    }}
    #TopBar {{
        background-color: {t.bg};
        border-bottom: 1px solid {t.border};
        border-top-left-radius: 16px;
        border-top-right-radius: 16px;
    }}
    #Sidebar {{
        background-color: {t.surface};
        border-right: 1px solid {t.border};
        border-bottom-left-radius: 16px;
    }}
    #ChatPane {{
        background-color: {t.bg};
    }}
    #SettingsPanel {{
        background-color: {t.surface};
        border-left: 1px solid {t.border};
        border-bottom-right-radius: 16px;
    }}
    #ChatHeader {{
        background-color: {t.bg};
        border-bottom: 1px solid {t.border};
    }}
    #ComposerShell {{
        background-color: {t.bg};
        border-top: 1px solid {t.border};
    }}
    #Composer {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 14px;
        color: {t.text};
        padding: 10px 14px;
        selection-background-color: {t.accent};
        selection-color: {t.accent_text};
    }}
    #Composer:focus {{
        border: 1px solid {t.border_strong};
    }}
    #ComposerHint {{
        color: {t.faint};
        font-family: {MONO_FONT};
        font-size: 9px;
        letter-spacing: 1px;
    }}
    #SectionLabel {{
        color: {t.muted};
        font-family: {MONO_FONT};
        font-size: 9px;
        letter-spacing: 2px;
    }}
    #MonoLabel {{
        color: {t.muted};
        font-family: {MONO_FONT};
        font-size: 10px;
        letter-spacing: 1px;
    }}
    #MonoLabel[style="hot"] {{
        color: {t.accent};
    }}
    #TinyLabel {{
        color: {t.faint};
        font-family: {MONO_FONT};
        font-size: 9px;
        letter-spacing: 1px;
    }}
    #DisplayLabel {{
        color: {t.text};
        font-size: 26px;
        font-weight: 600;
        letter-spacing: -1px;
    }}
    #TitleLabel {{
        color: {t.text};
        font-size: 17px;
        font-weight: 600;
        letter-spacing: -0.3px;
    }}
    #MutedLabel {{
        color: {t.muted};
    }}
    #FaintLabel {{
        color: {t.faint};
    }}
    #Divider {{
        background-color: {t.border};
        min-height: 1px;
        max-height: 1px;
    }}
    #VerticalDivider {{
        background-color: {t.border};
        min-width: 1px;
        max-width: 1px;
    }}
    #Card {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 12px;
    }}
    #RaisedCard {{
        background-color: {t.raised};
        border: 1px solid {t.border};
        border-radius: 12px;
    }}
    #AccentCard {{
        background-color: {t.accent_soft};
        border: 1px solid {t.accent};
        border-radius: 12px;
    }}
    #StatusPill {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 11px;
        color: {t.muted};
        font-family: {MONO_FONT};
        font-size: 9px;
        letter-spacing: 1px;
        padding: 3px 9px;
    }}
    #StatusPill[active="true"] {{
        border-color: {t.border_strong};
        color: {t.text};
    }}
    #KeyStatus {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 10px;
        color: {t.muted};
        font-family: {MONO_FONT};
        font-size: 10px;
        padding: 9px 11px;
    }}
    #KeyStatus[active="true"] {{
        border-color: {t.accent};
        color: {t.text};
    }}

    /* Buttons: every interactive surface inverts into signal red on hover. */
    QPushButton {{
        background-color: transparent;
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: 9px;
        padding: 8px 14px;
        font-weight: 500;
    }}
    QPushButton:hover {{
        background-color: {t.accent};
        border-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton:pressed {{
        background-color: {t.text};
        border-color: {t.text};
        color: {t.bg};
    }}
    QPushButton:disabled {{
        color: {t.faint};
        border-color: {t.border};
        background-color: transparent;
    }}
    QPushButton[variant="primary"] {{
        background-color: {t.text};
        color: {t.bg};
        border: 1px solid {t.text};
        font-weight: 600;
    }}
    QPushButton[variant="primary"]:hover {{
        background-color: {t.accent};
        border-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton[variant="ghost"] {{
        background-color: transparent;
        border: 1px solid transparent;
        color: {t.muted};
    }}
    QPushButton[variant="ghost"]:hover {{
        background-color: {t.accent};
        border-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton[variant="danger"] {{
        border-color: {t.border};
        color: {t.muted};
    }}
    QPushButton[variant="danger"]:hover {{
        background-color: {t.accent};
        border-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton[variant="round"] {{
        border-radius: 22px;
        padding: 0px;
        font-size: 17px;
    }}
    QPushButton[variant="round"]:hover {{
        background-color: {t.accent};
        border-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton#WindowButton {{
        border: none;
        border-radius: 7px;
        color: {t.muted};
        font-family: {MONO_FONT};
        font-size: 13px;
        padding: 0px;
    }}
    QPushButton#WindowButton:hover {{
        background-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton#CloseButton:hover {{
        background-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton#SendButton {{
        background-color: {t.text};
        border: 1px solid {t.text};
        border-radius: 17px;
        color: {t.bg};
        font-size: 19px;
        padding: 0px;
    }}
    QPushButton#SendButton:hover {{
        background-color: {t.accent};
        border-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton#MicButton {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 17px;
        color: {t.muted};
        font-family: {MONO_FONT};
        font-size: 15px;
        padding: 0px;
    }}
    QPushButton#MicButton:hover, QPushButton#MicButton[recording="true"] {{
        background-color: {t.accent};
        border-color: {t.accent};
        color: {t.accent_text};
    }}
    QPushButton#QuickAction {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 10px;
        color: {t.muted};
        padding: 10px 13px;
        text-align: left;
        font-size: 12px;
    }}
    QPushButton#QuickAction:hover {{
        background-color: {t.accent};
        border-color: {t.accent};
        color: {t.accent_text};
    }}

    QLineEdit, QTextEdit, QPlainTextEdit {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 9px;
        color: {t.text};
        padding: 9px 11px;
        selection-background-color: {t.accent};
        selection-color: {t.accent_text};
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
        border-color: {t.border_strong};
    }}
    QLineEdit:disabled, QTextEdit:disabled {{
        color: {t.faint};
    }}
    #SearchField {{
        background-color: transparent;
        border: 1px solid {t.border};
        border-radius: 9px;
        padding: 7px 10px;
        color: {t.text};
        font-size: 12px;
    }}
    #SearchField:focus {{
        border-color: {t.accent};
    }}

    QComboBox {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 9px;
        color: {t.text};
        padding: 8px 10px;
        min-height: 18px;
    }}
    QComboBox#ModelCombo {{
        background-color: transparent;
        border: 1px solid {t.border};
        border-radius: 8px;
        color: {t.muted};
        padding: 4px 8px;
        min-height: 16px;
        max-height: 28px;
        font-size: 10px;
        letter-spacing: 1px;
    }}
    QComboBox#ModelCombo:hover {{
        border-color: {t.accent};
        color: {t.text};
    }}
    QComboBox:hover {{
        border-color: {t.accent};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 26px;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {t.muted};
        width: 0px;
        height: 0px;
        margin-right: 10px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border_strong};
        selection-background-color: {t.accent};
        selection-color: {t.accent_text};
        color: {t.text};
        padding: 4px;
        outline: none;
    }}

    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QScrollArea > QWidget > QWidget {{
        background: transparent;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 7px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {t.border_strong};
        border-radius: 3px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t.accent};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
    }}
    QScrollBar:horizontal {{
        height: 6px;
        background: transparent;
    }}
    QScrollBar::handle:horizontal {{
        background: {t.border_strong};
        border-radius: 3px;
    }}

    QSlider::groove:horizontal {{
        height: 3px;
        background: {t.border};
        border-radius: 1px;
    }}
    QSlider::sub-page:horizontal {{
        background: {t.text};
        border-radius: 1px;
    }}
    QSlider::handle:horizontal {{
        background: {t.text};
        border: 2px solid {t.bg};
        width: 14px;
        height: 14px;
        margin: -7px 0;
        border-radius: 8px;
    }}
    QSlider::handle:horizontal:hover {{
        background: {t.accent};
        border-color: {t.accent};
    }}
    QSlider::sub-page:horizontal:hover {{
        background: {t.accent};
    }}

    QCheckBox {{
        color: {t.text};
        spacing: 9px;
    }}
    QCheckBox::indicator {{
        width: 15px;
        height: 15px;
        border: 1px solid {t.border_strong};
        border-radius: 4px;
        background: transparent;
    }}
    QCheckBox::indicator:hover {{
        border-color: {t.accent};
    }}
    QCheckBox::indicator:checked {{
        background: {t.text};
        border-color: {t.text};
    }}

    QMenu {{
        background-color: {t.surface_alt};
        color: {t.text};
        border: 1px solid {t.border_strong};
        border-radius: 8px;
        padding: 5px;
    }}
    QMenu::item {{
        padding: 7px 24px 7px 12px;
        border-radius: 5px;
    }}
    QMenu::item:selected {{
        background-color: {t.accent};
        color: {t.accent_text};
    }}
    QMenu::separator {{
        height: 1px;
        background: {t.border};
        margin: 4px 7px;
    }}

    QToolTip {{
        background-color: {t.text};
        color: {t.bg};
        border: none;
        padding: 5px 8px;
        font-family: {MONO_FONT};
        font-size: 10px;
    }}

    QMessageBox, QInputDialog {{
        background-color: {t.surface};
        border: 1px solid {t.border_strong};
        color: {t.text};
    }}
    QMessageBox QLabel, QInputDialog QLabel {{
        color: {t.text};
        background: transparent;
    }}
    QMessageBox QPushButton, QInputDialog QPushButton {{
        min-width: 74px;
    }}

    #ChatBubbleUser {{
        background-color: {t.user_bubble};
        border-radius: 14px;
    }}
    #ChatBubbleAssistant {{
        background-color: transparent;
        border: 1px solid {t.border};
        border-radius: 14px;
    }}
    #BubbleText {{
        color: {t.text};
        background: transparent;
        font-size: 13px;
    }}
    #BubbleText[role="user"] {{
        color: {t.user_text};
    }}
    #BubbleRole {{
        color: {t.muted};
        font-family: {MONO_FONT};
        font-size: 9px;
        letter-spacing: 1.5px;
        background: transparent;
    }}
    #BubbleRole[role="user"] {{
        color: {t.user_text};
    }}
    #EmptyTitle {{
        color: {t.text};
        font-size: 34px;
        font-weight: 600;
        letter-spacing: -1.5px;
    }}
    #OnboardingTitle {{
        color: {t.text};
        font-size: 40px;
        font-weight: 600;
        letter-spacing: -2px;
    }}
    #WelcomeTitle {{
        color: {t.text};
        font-size: 46px;
        font-weight: 600;
        letter-spacing: -2px;
    }}
    #EmptySubtitle {{
        color: {t.muted};
        font-size: 13px;
    }}
    #ChatRow {{
        background-color: transparent;
        border: 1px solid transparent;
        border-radius: 9px;
    }}
    #ChatRow:hover {{
        background-color: {t.surface_alt};
        border-color: {t.border};
    }}
    #ChatRow[selected="true"] {{
        background-color: {t.surface_alt};
        border-color: {t.border_strong};
    }}
    #ChatRowTitle {{
        color: {t.text};
        font-size: 12px;
        background: transparent;
    }}
    #ChatRowTitle[selected="true"] {{
        color: {t.text};
        font-weight: 600;
    }}
    #ChatRowPreview {{
        color: {t.faint};
        font-size: 10px;
        background: transparent;
    }}
    #ChatRowTime {{
        color: {t.faint};
        font-family: {MONO_FONT};
        font-size: 8px;
        background: transparent;
    }}
    #ChatMenuButton {{
        border: none;
        border-radius: 6px;
        color: {t.faint};
        font-family: {MONO_FONT};
        font-size: 14px;
        padding: 0px;
        background: transparent;
    }}
    #ChatMenuButton:hover {{
        color: {t.accent_text};
        background-color: {t.accent};
    }}
    #ThemeCard {{
        background-color: transparent;
        border: 1px solid {t.border};
        border-radius: 10px;
    }}
    #ThemeCard:hover {{
        border-color: {t.accent};
    }}
    #ThemeCard[selected="true"] {{
        border-color: {t.text};
    }}
    #OnboardingCard {{
        background-color: {t.surface};
        border: 1px solid {t.border_strong};
        border-radius: 16px;
    }}
    #RAMBar {{
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 4px;
        min-height: 7px;
        max-height: 7px;
    }}
    #RAMBar::chunk {{
        background-color: {t.text};
        border-radius: 3px;
    }}
    #RAMBar[hot="true"]::chunk {{
        background-color: {t.accent};
    }}
    #RecDot {{
        color: {t.accent};
        font-family: {MONO_FONT};
        font-size: 10px;
    }}
    #ErrorLabel {{
        color: {t.accent};
        font-size: 11px;
    }}
    #ModelNotice {{
        color: {t.text};
        font-family: {MONO_FONT};
        font-size: 10px;
        letter-spacing: 1px;
        padding: 1px 0 5px 0;
    }}
    #LinkLabel {{
        color: {t.muted};
        font-size: 10px;
    }}
    """
    return qss


def font_stack() -> tuple[str, str]:
    """Return the UI and monospace font names for QFont consumers."""
    if sys.platform == "darwin":
        return "Helvetica Neue", "SF Mono"
    if sys.platform.startswith("win"):
        return "Segoe UI", "Cascadia Code"
    return "Inter", "DejaVu Sans Mono"


def app_identity() -> str:
    return f"{APP_NAME} · {TAGLINE}"
