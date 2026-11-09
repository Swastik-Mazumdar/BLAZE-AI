"""Persistent application paths and user preferences."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

APP_DIR_NAME = "BlazeAI"
SETTINGS_FILE = "settings.json"
LOG_FILE = "blaze.log"
DATABASE_FILE = "chats.sqlite3"

DEFAULT_SETTINGS: dict[str, Any] = {
    "name": "",
    "onboarding_complete": False,
    "theme": "onyx",
    "hf_model": "",
    "hf_voice_model": "",
    "hf_model_task": "text-generation",
    "hf_downloaded_models": [],
    "model_registry": [],
    "chat_models_initialized": False,
    "agent_mode": True,
    "agent_confirm": True,
    "agent_max_steps": 8,
    "ram_limit_percent": 65,
    "stt_model": "base.en",
    "voice_enabled": True,
    "confirm_destructive": True,
    "recording_fps": 8,
    "last_chat_id": "",
}


def app_data_dir() -> Path:
    """Return the per-user writable data directory, creating it if needed."""
    override = os.environ.get("BLAZE_AI_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    elif os.name == "nt":
        path = Path(os.environ.get("APPDATA", Path.home())) / APP_DIR_NAME
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "blaze-ai"
    path.mkdir(parents=True, exist_ok=True)
    return path


def assets_dir() -> Path:
    """Return the bundled asset directory, including inside a PyInstaller app."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "blaze_ai" / "assets"
    return Path(__file__).resolve().parent / "assets"


def settings_path() -> Path:
    return app_data_dir() / SETTINGS_FILE


def log_path() -> Path:
    return app_data_dir() / LOG_FILE


def database_path() -> Path:
    return app_data_dir() / DATABASE_FILE


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("blaze")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path(), encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s  %(message)s")
    )
    logger.addHandler(handler)
    if not getattr(sys, "frozen", False):
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(console)
    return logger


class Settings:
    """Small, defensive JSON-backed settings store."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_path()
        self._data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            migrated = False
            if isinstance(raw, dict):
                migrated = any(key not in DEFAULT_SETTINGS for key in raw)
                for key, value in raw.items():
                    if key in DEFAULT_SETTINGS:
                        self._data[key] = value
            if migrated:
                # Remove legacy provider and API-key fields from old settings files.
                self.save()
        except (OSError, ValueError):
            # A corrupt preferences file must never stop Blaze from launching.
            logging.getLogger("blaze").exception("Could not read settings; using defaults")
            self._data = dict(DEFAULT_SETTINGS)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".settings-", suffix=".json", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        self._data[key] = value
        if save:
            self.save()

    def update(self, values: dict[str, Any], *, save: bool = True) -> None:
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                self._data[key] = value
        if save:
            self.save()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    # Dictionary-ish access is convenient in small UI handlers.
    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)
