"""Blaze AI application entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import traceback

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from .config import Settings, app_data_dir, assets_dir, setup_logging
from .core.net import ensure_ca_bundle
from .theme import font_stack, theme_manager
from .ui.main_window import MainWindow
from .version import APP_NAME, TAGLINE, VERSION


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="BlazeAI", description=f"{APP_NAME} — {TAGLINE}")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument(
        "--data-dir", action="store_true", help="Print the local data directory and exit"
    )
    parser.add_argument(
        "--fresh", action="store_true", help="Run onboarding again without deleting data"
    )
    return parser.parse_args(argv)


def _install_exception_hook(logger: logging.Logger) -> None:
    def hook(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        app = QApplication.instance()
        if app is not None:
            QMessageBox.critical(
                None,
                f"{APP_NAME} encountered an error",
                f"{exc_value}\n\nDetails were written to:\n{app_data_dir() / 'blaze.log'}",
            )

    sys.excepthook = hook


def create_application(argv: list[str]) -> tuple[QApplication, argparse.Namespace, Settings]:
    ensure_ca_bundle()
    args = _parse_args(argv)
    if args.version:
        print(VERSION)
        raise SystemExit(0)
    if args.data_dir:
        print(app_data_dir())
        raise SystemExit(0)

    logger = setup_logging()
    logger.info("Starting %s %s on %s", APP_NAME, VERSION, sys.platform)
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(APP_NAME)
    QApplication.setOrganizationName("Blaze AI")
    QApplication.setDesktopFileName("ai.blaze.desktop")
    app = QApplication([sys.argv[0], *argv])
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(True)
    font_name, _ = font_stack()
    app.setFont(QFont(font_name, 10))
    icon_path = assets_dir() / "blaze_logo.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    _install_exception_hook(logger)

    settings = Settings()
    if args.fresh:
        settings.set("onboarding_complete", False)
    theme_manager.set_theme(settings.get("theme", "onyx"))
    return app, args, settings


def main(argv: list[str] | None = None) -> int:
    app, _args, settings = create_application(
        list(sys.argv[1:] if argv is None else argv)
    )
    window = MainWindow(settings)
    window.show()
    # Terminal launches on macOS do not always bring the frameless window to
    # the front; nudge it above the launching app.
    window.raise_()
    window.activateWindow()
    if not settings.get("onboarding_complete", False):
        QTimer.singleShot(350, window.show_onboarding)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
