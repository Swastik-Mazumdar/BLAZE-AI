"""Local-only self-learning memory for the offline butler/agent.

Stores user-taught aliases, preferences, and lightweight usage signals in the
app data directory. No network access, no cloud sync.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import tempfile
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ..config import app_data_dir

log = logging.getLogger("blaze.memory")

_MAX_ALIASES = 500
_MAX_LOG = 200


def _normalize(value: str) -> str:
    value = (value or "").lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9+#]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


class LearningMemory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_data_dir() / "memory.json")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "version": 1,
            "app_aliases": {},      # normalized phrase -> {"name": str, "target": str}
            "site_aliases": {},     # normalized phrase -> url
            "contacts": {},         # normalized name -> {"name": str, "phone": str, "email": str, "notes": str}
            "preferences": {},      # key -> value
            "usage": {},            # normalized action key -> count
            "events": [],           # recent learning events (bounded)
        }
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for key in ("app_aliases", "site_aliases", "contacts", "preferences", "usage"):
                if isinstance(raw.get(key), dict):
                    self._data[key].update(raw[key])
            if isinstance(raw.get("events"), list):
                self._data["events"] = raw["events"][-_MAX_LOG:]
        except Exception:
            log.debug("Could not load learning memory", exc_info=True)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".memory-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._data, handle, indent=2, ensure_ascii=False)
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    try:
                        os.unlink(temp)
                    except OSError:
                        pass

    def _event(self, kind: str, detail: dict[str, Any]) -> None:
        entry = {"kind": kind, "at": time.time(), **detail}
        events = self._data.setdefault("events", [])
        events.append(entry)
        if len(events) > _MAX_LOG:
            del events[:-_MAX_LOG]

    # ---------------------------------------------------------------- aliases
    def learn_app_alias(self, phrase: str, app_name: str, target: str = "") -> bool:
        key = _normalize(phrase)
        name = (app_name or "").strip()
        if not key or not name or len(key) > 80:
            return False
        with self._lock:
            aliases = self._data.setdefault("app_aliases", {})
            if len(aliases) >= _MAX_ALIASES and key not in aliases:
                # Drop oldest-ish by rewriting (dict preserves insertion in py3.7+)
                for old in list(aliases)[:20]:
                    aliases.pop(old, None)
            aliases[key] = {"name": name, "target": (target or "").strip()}
            self._event("app_alias", {"phrase": phrase.strip(), "name": name})
            self.save()
        return True

    def forget_app_alias(self, phrase: str) -> bool:
        key = _normalize(phrase)
        with self._lock:
            aliases = self._data.setdefault("app_aliases", {})
            if key not in aliases:
                return False
            aliases.pop(key, None)
            self._event("forget_app_alias", {"phrase": phrase.strip()})
            self.save()
        return True

    def resolve_app_alias(self, phrase: str) -> dict[str, str] | None:
        key = _normalize(phrase)
        with self._lock:
            value = self._data.get("app_aliases", {}).get(key)
            if isinstance(value, dict) and value.get("name"):
                return {"name": str(value["name"]), "target": str(value.get("target", ""))}
        return None

    def all_app_aliases(self) -> dict[str, dict[str, str]]:
        with self._lock:
            raw = self._data.get("app_aliases", {})
            return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def learn_site_alias(self, phrase: str, url: str) -> bool:
        key = _normalize(phrase)
        if not key or not url:
            return False
        with self._lock:
            self._data.setdefault("site_aliases", {})[key] = url.strip()
            self._event("site_alias", {"phrase": phrase.strip(), "url": url.strip()})
            self.save()
        return True

    def resolve_site_alias(self, phrase: str) -> str:
        key = _normalize(phrase)
        with self._lock:
            value = self._data.get("site_aliases", {}).get(key)
            return str(value) if value else ""

    # ---------------------------------------------------------------- contacts
    def save_contact(self, name: str, phone: str = "", email: str = "", notes: str = "") -> bool:
        key = _normalize(name)
        clean_name = (name or "").strip()
        if not key or not clean_name:
            return False
        with self._lock:
            contacts = self._data.setdefault("contacts", {})
            contacts[key] = {
                "name": clean_name,
                "phone": (phone or "").strip(),
                "email": (email or "").strip(),
                "notes": (notes or "").strip(),
            }
            self._event("save_contact", {"name": clean_name, "phone": phone, "email": email})
            self.save()
        return True

    def delete_contact(self, name: str) -> bool:
        key = _normalize(name)
        with self._lock:
            contacts = self._data.setdefault("contacts", {})
            if key not in contacts:
                found, score = self.find_contact(name)
                if found and score >= 0.8:
                    key = _normalize(found["name"])
            if key in contacts:
                removed = contacts.pop(key)
                self._event("delete_contact", {"name": removed.get("name", name)})
                self.save()
                return True
        return False

    def get_contact(self, name: str) -> dict[str, str] | None:
        key = _normalize(name)
        with self._lock:
            contacts = self._data.get("contacts", {})
            if key in contacts:
                return dict(contacts[key])
        return None

    def find_contact(self, query: str) -> tuple[dict[str, str] | None, float]:
        key = _normalize(query)
        if not key:
            return None, 0.0
        with self._lock:
            contacts = self._data.get("contacts", {})
            if key in contacts:
                return dict(contacts[key]), 1.0
            best_match: dict[str, str] | None = None
            best_score = 0.0
            for c_key, c_info in contacts.items():
                if not isinstance(c_info, dict):
                    continue
                if key in c_key or c_key in key:
                    score = 0.85
                else:
                    score = SequenceMatcher(None, key, c_key).ratio()
                if score > best_score:
                    best_score = score
                    best_match = dict(c_info)
            if best_score >= 0.55:
                return best_match, best_score
        return None, 0.0

    def all_contacts(self) -> dict[str, dict[str, str]]:
        with self._lock:
            raw = self._data.get("contacts", {})
            return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}

    # ------------------------------------------------------------- preferences
    def set_preference(self, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault("preferences", {})[key] = value
            self._event("preference", {"key": key, "value": value})
            self.save()

    def get_preference(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get("preferences", {}).get(key, default)

    # ------------------------------------------------------------------- usage
    def record_success(self, kind: str, detail: str = "") -> None:
        key = _normalize(f"{kind}:{detail}")[:120] if detail else _normalize(kind)
        with self._lock:
            usage = self._data.setdefault("usage", {})
            usage[key] = int(usage.get(key, 0) or 0) + 1
            self.save()

    def get_success_count(self, kind: str, detail: str = "") -> int:
        key = _normalize(f"{kind}:{detail}")[:120] if detail else _normalize(kind)
        with self._lock:
            return int(self._data.get("usage", {}).get(key, 0) or 0)

    def summary(self) -> str:
        with self._lock:
            aliases = len(self._data.get("app_aliases", {}))
            sites = len(self._data.get("site_aliases", {}))
            contacts = len(self._data.get("contacts", {}))
            prefs = len(self._data.get("preferences", {}))
            events = len(self._data.get("events", []))
        parts = [
            f"{aliases} app alias(es)",
            f"{sites} site alias(es)",
            f"{contacts} contact(s)",
            f"{prefs} preference(s)",
            f"{events} event(s)",
        ]
        return "Offline learning memory: " + ", ".join(parts) + "."
