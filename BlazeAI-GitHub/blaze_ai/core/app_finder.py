"""Cross-platform installed-application discovery, fuzzy matching and control."""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from ..config import app_data_dir

log = logging.getLogger("blaze.apps")

_CACHE_TTL_SECONDS = 24 * 60 * 60
_CACHE_VERSION = 2

# Common spoken names → canonical fragments. This is deliberately broad: local
# system control should feel forgiving, not brittle.
_ALIASES: dict[str, tuple[str, ...]] = {
    "vs code": ("visual studio code", "vscode"),
    "vscode": ("visual studio code",),
    "vs": ("visual studio code",),
    "code": ("visual studio code", "visual studio code - insiders"),
    "vs code insiders": ("visual studio code - insiders",),
    "chrome": ("google chrome",),
    "browser": ("google chrome", "brave browser", "safari", "firefox", "microsoft edge"),
    "brave": ("brave browser",),
    "brave browser": ("brave browser",),
    "whats app": ("whatsapp",),
    "whatsapp": ("whatsapp",),
    "word": ("microsoft word",),
    "excel": ("microsoft excel",),
    "powerpoint": ("microsoft powerpoint",),
    "power point": ("microsoft powerpoint",),
    "outlook": ("microsoft outlook",),
    "teams": ("microsoft teams",),
    "edge": ("microsoft edge",),
    "spotify": ("spotify",),
    "discord": ("discord",),
    "slack": ("slack",),
    "zoom": ("zoom",),
    "terminal": ("terminal", "iterm", "windows terminal", "powershell"),
    "console": ("terminal", "iterm", "windows terminal", "command prompt"),
    "cmd": ("command prompt", "terminal"),
    "powershell": ("powershell", "windows powershell"),
    "finder": ("finder",),
    "explorer": ("file explorer", "finder"),
    "files": ("file explorer", "finder"),
    "notepad": ("notepad", "textedit"),
    "text editor": ("notepad", "textedit", "visual studio code"),
    "calculator": ("calculator",),
    "calc": ("calculator",),
    "calendar": ("calendar",),
    "mail": ("mail", "outlook", "gmail"),
    "music": ("music", "spotify"),
    "photos": ("photos",),
    "settings": ("system settings", "settings"),
    "system settings": ("system settings", "settings"),
    "preferences": ("system settings", "settings"),
    "activity monitor": ("activity monitor",),
    "task manager": ("task manager",),
    "snipping tool": ("snipping tool", "screenshot"),
    "steam": ("steam",),
    "obs": ("obs studio",),
    "figma": ("figma",),
    "notion": ("notion",),
    "telegram": ("telegram",),
    "signal": ("signal",),
    "instagram": ("instagram",),
    "facebook": ("facebook",),
    "messenger": ("messenger",),
    "maps": ("maps",),
    "weather": ("weather",),
    "notes": ("notes", "notepad"),
    "reminders": ("reminders",),
    "preview": ("preview",),
    "quicktime": ("quicktime player",),
}

_SKIP_BUNDLE_NAMES = {
    "uninstall",
    "uninstaller",
    "crash reporter",
    "helper",
    "updater",
    "uninstall product",
    "license",
}


@dataclass(frozen=True)
class AppEntry:
    name: str
    target: str
    bundle_id: str = ""
    launch_id: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["aliases"] = list(self.aliases)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AppEntry":
        return cls(
            name=str(data.get("name", "")),
            target=str(data.get("target", "")),
            bundle_id=str(data.get("bundle_id", "")),
            launch_id=str(data.get("launch_id", "")),
            aliases=tuple(data.get("aliases", ())),
            source=str(data.get("source", "")),
        )


def _normalize(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"\.(app|exe|lnk|desktop|url)$", "", value)
    value = re.sub(r"[^a-z0-9+#]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_DOMAIN_ONLY = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}"
    r"(?::\d{2,5})?(?:/[^\s]*)?$",
    re.IGNORECASE,
)
_URL_IN_TEXT = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)


def normalize_web_url(value: str) -> str:
    """Return a usable http(s) URL, or an empty string if the text is not one."""
    text = (value or "").strip().strip("\"'")
    if not text:
        return ""
    if _SCHEME.match(text):
        return text if text.lower().startswith(("http://", "https://")) else "https://" + text.split("://", 1)[1]
    if text.lower().startswith(("localhost", "127.0.0.1", "[::1]")):
        return f"http://{text}"
    if _DOMAIN_ONLY.fullmatch(text):
        return f"https://{text}"
    return ""


def extract_first_url(text: str) -> str:
    """Find the first URL or bare domain inside arbitrary clipboard text."""
    match = _URL_IN_TEXT.search(text or "")
    if not match:
        return ""
    return normalize_web_url(match.group(0).rstrip(").,;!?]}'"))


def clipboard_text() -> str:
    """Read plain text from the system clipboard without extra dependencies."""
    command: list[str]
    if sys.platform == "darwin":
        command = ["pbpaste"]
    elif os.name == "nt":
        command = ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
    else:
        command = ["xclip", "-selection", "clipboard", "-o"]
    for args in (command, ["xsel", "--clipboard", "--output"]):
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=4
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return ""


def clipboard_url() -> str:
    text = clipboard_text()
    if not text:
        return ""
    direct = normalize_web_url(text)
    return direct or extract_first_url(text)


def _mac_default_browser_bundle() -> str:
    try:
        result = subprocess.run(
            [
                "defaults",
                "export",
                "com.apple.LaunchServices/com.apple.launchservices.secure",
                "-",
            ],
            capture_output=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    try:
        data = plistlib.loads(result.stdout)
    except (plistlib.InvalidFileException, ValueError, ExpatError):
        return ""
    for handler in data.get("LSHandlers", []) or []:
        if not isinstance(handler, dict):
            continue
        if str(handler.get("LSHandlerURLScheme", "")).lower() != "https":
            continue
        bundle = handler.get("LSHandlerRoleAll") or handler.get("LSHandlerRoleViewer")
        if bundle:
            return str(bundle)
    return ""


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", "", value.lower())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def app_match_score(
    query: str, app: AppEntry, *, learned_names: tuple[str, ...] = ()
) -> float:
    """Return a 0..1 fuzzy confidence that ``app`` is what the user meant."""

    q = _normalize(query)
    if not q:
        return 0.0

    # The user-facing phrase and any canonical names it maps to. These are all
    # compared against the app's own name/aliases; the app's aliases must never
    # be scored against themselves or every app would match every queried alias.
    query_terms: list[str] = [q]
    query_terms.extend(
        normalized
        for normalized in (_normalize(alias) for alias in _ALIASES.get(q, ()))
        if normalized
    )
    query_terms.extend(
        normalized for normalized in (_normalize(name) for name in learned_names) if normalized
    )
    app_names: set[str] = {_normalize(app.name)}
    app_names.update(_normalize(alias) for alias in app.aliases if alias)
    app_names.discard("")
    if not app_names:
        return 0.0
    app_compacts = {_compact(name) for name in app_names}

    best = 0.0
    for term in query_terms:
        compact_term = _compact(term)
        term_tokens = set(term.split())
        for name in app_names:
            if term == name:
                best = max(best, 1.0)
                continue
            if name.startswith(term):
                best = max(best, 0.94 - min(0.12, (len(name) - len(term)) * 0.006))
            if term in name:
                best = max(best, 0.88 - min(0.12, (len(name) - len(term)) * 0.005))
            name_tokens = set(name.split())
            if term_tokens and term_tokens.issubset(name_tokens):
                best = max(best, 0.83)
            best = max(best, _ratio(term, name) * 0.86)
        # Compact matching catches "whats app", "vs-code", etc.
        if compact_term and compact_term in app_compacts:
            best = max(best, 0.98)
        if compact_term and any(compact_term in compact for compact in app_compacts):
            best = max(
                best,
                0.86 - min(0.14, max(0, len(app.name) - len(term)) * 0.006),
            )
    # Learned exact mappings are strong signals.
    if learned_names and best > 0:
        best = max(best, 0.985)
    # A shorter installed application is normally the better match.
    if best > 0.7:
        best += max(0.0, 0.025 - len(app.name) * 0.00025)
    return min(best, 1.0)


class AppFinder:
    """Discovers applications and launches/closes the best fuzzy match."""

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        apps_override: list[AppEntry] | None = None,
        memory: Any = None,
    ) -> None:
        self._apps: list[AppEntry] = list(apps_override or [])
        self._provided_apps = apps_override is not None
        self._cache_path = cache_path or (app_data_dir() / "installed_apps.json")
        self._lock = threading.Lock()
        self._loaded = bool(apps_override)
        self._scan_warm = threading.Event()
        self.memory = memory
        if self._loaded:
            self._scan_warm.set()

    def _learned_names(self, query: str) -> tuple[str, ...]:
        if self.memory is None:
            return ()
        try:
            learned = self.memory.resolve_app_alias(query)
            if learned and learned.get("name"):
                return (str(learned["name"]),)
        except Exception:
            log.debug("Could not read learned alias", exc_info=True)
        return ()

    @property
    def apps(self) -> list[AppEntry]:
        self._ensure_loaded()
        return list(self._apps)

    def start_warmup(self) -> None:
        """Scan installed apps off the UI thread at startup."""

        if self._loaded or self._scan_warm.is_set():
            return

        def work() -> None:
            try:
                self.refresh(use_cache=True)
            except Exception:  # pragma: no cover - discovery must never crash app
                log.exception("Application discovery warmup failed")
            finally:
                self._scan_warm.set()

        threading.Thread(target=work, name="blaze-app-scan", daemon=True).start()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self.refresh(use_cache=True)
        self._loaded = True
        self._scan_warm.set()

    def refresh(self, *, use_cache: bool = True) -> list[AppEntry]:
        if self._provided_apps:
            self._loaded = True
            return list(self._apps)
        with self._lock:
            if use_cache:
                cached = self._read_cache()
                if cached is not None:
                    self._apps = cached
                    self._loaded = True
                    return list(self._apps)
            started = time.perf_counter()
            try:
                apps = self._scan_platform()
            except Exception:
                log.exception("Application scan failed")
                apps = []
            # De-duplicate by normalized name + target.
            seen: set[tuple[str, str]] = set()
            unique: list[AppEntry] = []
            for app in apps:
                key = (_normalize(app.name), app.target.lower())
                if not app.name or key in seen:
                    continue
                seen.add(key)
                unique.append(app)
            unique.sort(key=lambda item: item.name.lower())
            self._apps = unique
            self._loaded = True
            self._write_cache(unique)
            log.info(
                "Discovered %d applications in %.2fs", len(unique), time.perf_counter() - started
            )
            return list(self._apps)

    def search(self, query: str, *, limit: int = 6) -> list[tuple[float, AppEntry]]:
        self._ensure_loaded()
        learned = self._learned_names(query)
        scored = [
            (app_match_score(query, app, learned_names=learned), app) for app in self._apps
        ]
        scored = [(score, app) for score, app in scored if score >= 0.52]
        scored.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].name.lower()))
        # If the name looks unusual, scan once more; a new app may be installed.
        if not scored and self._loaded:
            self.refresh(use_cache=False)
            scored = [
                (app_match_score(query, app, learned_names=learned), app)
                for app in self._apps
            ]
            scored = [(score, app) for score, app in scored if score >= 0.52]
            scored.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].name.lower()))
        return scored[:limit]

    def find_best(self, query: str) -> tuple[AppEntry | None, float]:
        # Learned alias can point directly to a known target/name even before scan details.
        if self.memory is not None:
            try:
                learned = self.memory.resolve_app_alias(query)
                if learned and learned.get("name"):
                    target = str(learned.get("target", "") or "")
                    name = str(learned["name"])
                    if target:
                        for app in self.apps:
                            if app.target == target or _normalize(app.name) == _normalize(name):
                                return app, 0.995
                    # Fallback: score by learned name.
                    matches = self.search(name, limit=1)
                    if matches:
                        return matches[0][1], 0.99
            except Exception:
                log.debug("Learned alias lookup failed", exc_info=True)
        matches = self.search(query, limit=1)
        if not matches:
            return None, 0.0
        return matches[0][1], matches[0][0]

    def launch(self, app: AppEntry) -> None:
        log.info("Launching %s (%s)", app.name, app.target)
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", app.target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        if os.name == "nt":
            if app.target.lower().endswith((".lnk", ".url")):
                os.startfile(app.target)  # type: ignore[attr-defined]
            elif app.target.lower().endswith(".exe"):
                subprocess.Popen(
                    [app.target],
                    cwd=str(Path(app.target).parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            else:
                os.startfile(app.target)  # type: ignore[attr-defined]
            return
        if app.launch_id:
            try:
                subprocess.Popen(
                    ["gtk-launch", app.launch_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return
            except OSError:
                pass
        subprocess.Popen(
            ["xdg-open", app.target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def default_browser(self) -> AppEntry | None:
        """Best-effort default browser, falling back to a common installed one."""
        if sys.platform == "darwin":
            bundle = _mac_default_browser_bundle()
            if bundle:
                for app in self.apps:
                    if app.bundle_id == bundle:
                        return app
        for name in ("brave browser", "google chrome", "safari", "firefox", "microsoft edge"):
            app, score = self.find_best(name)
            if app is not None and score >= 0.62:
                return app
        return None

    def open_url(self, url: str, app: AppEntry | None = None) -> str:
        """Open a website in the default browser or a specific app."""
        target = normalize_web_url(url)
        if not target:
            raise ValueError("That does not look like a website address.")
        if sys.platform == "darwin":
            command = ["open"]
            if app is not None:
                command += ["-a", app.target]
            command.append(target)
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return target
        if os.name == "nt":
            if app is not None and app.target.lower().endswith(".exe"):
                subprocess.Popen(
                    [app.target, target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            else:
                os.startfile(target)  # type: ignore[attr-defined]
            return target
        if app is not None and app.target:
            subprocess.Popen(
                [app.target, target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            subprocess.Popen(
                ["xdg-open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        return target

    def open_new_tab(self, app: AppEntry | None = None) -> AppEntry | None:
        """Open a fresh tab, creating the browser window when needed."""
        target = app or self.default_browser()
        if sys.platform == "darwin":
            if target is not None and self._mac_new_tab(target):
                return target
            # Last resort: opening a page always creates a tab in the browser.
            self.open_url("https://www.google.com", target)
            return target
        if os.name == "nt":
            if target is None:
                os.startfile("https://www.google.com")  # type: ignore[attr-defined]
                return None
            flag = "-new-tab" if "firefox" in target.name.lower() else "--new-tab"
            if target.target.lower().endswith(".exe"):
                subprocess.Popen(
                    [target.target, flag],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            else:
                os.startfile(target.target)  # type: ignore[attr-defined]
            return target
        if target is None:
            subprocess.Popen(
                ["xdg-open", "https://www.google.com"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return None
        flag = "-new-tab" if "firefox" in target.name.lower() else "--new-tab"
        subprocess.Popen(
            [target.target, flag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return target

    def _mac_new_tab(self, app: AppEntry) -> bool:
        name = _escape_osascript_name(app.name)
        lowered = app.name.lower()
        if "firefox" in lowered:
            return self._mac_keyboard_new_tab(name)
        script = (
            f'tell application "{name}"\n'
            "  activate\n"
            "  if (count of windows) = 0 then make new window\n"
            "  make new tab at end of tabs of window 1\n"
            "end tell"
        )
        ok, _ = _osascript(script)
        if ok:
            return True
        if "safari" in lowered:
            ok, _ = _osascript(f'tell application "{name}" to make new document')
            if ok:
                return True
        return self._mac_keyboard_new_tab(name)

    def _mac_keyboard_new_tab(self, name: str) -> bool:
        ok, _ = _osascript(f'tell application "{name}" to activate')
        if not ok:
            return False
        time.sleep(0.4)
        ok, _ = _osascript(
            'tell application "System Events" to keystroke "t" using command down'
        )
        return ok

    def close(self, app: AppEntry) -> bool:
        log.info("Closing %s", app.name)
        if sys.platform == "darwin":
            identifier = app.bundle_id or app.name
            script = (
                'tell application id "%s" to quit' % identifier
                if app.bundle_id
                else 'tell application "%s" to quit' % _escape_osascript_name(app.name)
            )
            try:
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                if result.returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                log.exception("AppleScript quit failed for %s", app.name)
            executable = Path(app.target).stem
            return _run_quiet(["pkill", "-x", executable]) == 0

        if os.name == "nt":
            exe = _resolve_windows_shortcut(app.target)
            if exe:
                result = _run_quiet(["taskkill", "/IM", Path(exe).name, "/F"])
                return result == 0
            # Best effort: the shortcut usually shares the executable name.
            return _run_quiet(["taskkill", "/IM", f"{app.name}.exe", "/F"]) == 0

        pattern = app.target
        return _run_quiet(["pkill", "-f", pattern]) == 0

    def _read_cache(self) -> list[AppEntry] | None:
        try:
            if not self._cache_path.exists():
                return None
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if data.get("version") != _CACHE_VERSION:
                return None
            if data.get("platform") != sys.platform:
                return None
            if time.time() - float(data.get("scanned_at", 0)) > _CACHE_TTL_SECONDS:
                return None
            apps = [AppEntry.from_dict(item) for item in data.get("apps", [])]
            return apps or None
        except (OSError, ValueError, TypeError):
            log.debug("Ignoring invalid application cache", exc_info=True)
            return None

    def _write_cache(self, apps: list[AppEntry]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": _CACHE_VERSION,
                "platform": sys.platform,
                "scanned_at": time.time(),
                "apps": [app.to_dict() for app in apps],
            }
            self._cache_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        except OSError:
            log.debug("Could not write application cache", exc_info=True)

    def _scan_platform(self) -> list[AppEntry]:
        if sys.platform == "darwin":
            return self._scan_macos()
        if os.name == "nt":
            return self._scan_windows()
        return self._scan_linux()

    def _scan_macos(self) -> list[AppEntry]:
        roots = [
            Path("/Applications"),
            Path.home() / "Applications",
            Path("/System/Applications"),
            Path("/System/Applications/Utilities"),
        ]
        apps: list[AppEntry] = []
        for root in roots:
            if not root.exists():
                continue
            for current, dirs, _files in os.walk(root):
                current_path = Path(current)
                # Never descend inside application bundles.
                keep: list[str] = []
                for directory in dirs:
                    candidate = current_path / directory
                    if directory.endswith(".app"):
                        app = self._mac_app(candidate)
                        if app:
                            apps.append(app)
                    else:
                        keep.append(directory)
                dirs[:] = keep
        return apps

    def _mac_app(self, path: Path) -> AppEntry | None:
        name = path.stem
        bundle_id = ""
        aliases: set[str] = set()
        plist_path = path / "Contents" / "Info.plist"
        bundle_name = path.stem
        try:
            with plist_path.open("rb") as handle:
                info = plistlib.load(handle)
            display = info.get("CFBundleDisplayName") or info.get("CFBundleName")
            if display:
                aliases.add(bundle_name)
                display = str(display)
                # A generic display name such as VS Code's "Code" is less
                # discoverable than the bundle filename ("Visual Studio Code").
                if (
                    len(display) < len(bundle_name)
                    and display.lower() in bundle_name.lower()
                ):
                    name = bundle_name
                else:
                    name = display
            bundle_id = str(info.get("CFBundleIdentifier", ""))
            executable = info.get("CFBundleExecutable")
            if executable:
                aliases.add(str(executable).replace(".app", ""))
                aliases.add(Path(str(executable)).stem)
        except (OSError, plistlib.InvalidFileException, ExpatError, ValueError):
            pass
        if any(part.lower() in _SKIP_BUNDLE_NAMES for part in name.lower().split()):
            return None
        if name.startswith(".") or name.lower() in {"helper", "electron"}:
            return None
        if bundle_id:
            aliases.update(part for part in bundle_id.split(".") if len(part) > 2)
        return AppEntry(
            name=name,
            target=str(path),
            bundle_id=bundle_id,
            aliases=tuple(sorted(aliases)),
            source="macos",
        )

    def _scan_windows(self) -> list[AppEntry]:
        roots: list[Path] = []
        appdata = os.environ.get("APPDATA")
        programdata = os.environ.get("PROGRAMDATA")
        if appdata:
            roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        if programdata:
            roots.append(
                Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            )
        apps: list[AppEntry] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
                if path.suffix.lower() not in {".lnk", ".url", ".exe"}:
                    continue
                name = path.stem.strip()
                if not name or name.lower().startswith("uninstall"):
                    continue
                aliases: set[str] = set()
                if path.suffix.lower() == ".lnk":
                    # Cheap aliases now; the target is resolved only when closing.
                    aliases.add(path.stem)
                apps.append(
                    AppEntry(
                        name=name,
                        target=str(path),
                        aliases=tuple(sorted(aliases)),
                        source="windows-start-menu",
                    )
                )
        # Add common executables that are often absent from the Start Menu.
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if not base:
                continue
            candidates = [
                Path(base) / "Microsoft VS Code" / "Code.exe",
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(base) / "Spotify" / "Spotify.exe",
            ]
            for candidate in candidates:
                if candidate.exists():
                    apps.append(
                        AppEntry(
                            name=candidate.stem.replace("Code", "Visual Studio Code"),
                            target=str(candidate),
                            aliases=(candidate.stem,),
                            source="windows-common",
                        )
                    )
        return apps

    def _scan_linux(self) -> list[AppEntry]:
        roots = [
            Path("/usr/share/applications"),
            Path("/usr/local/share/applications"),
            Path.home() / ".local" / "share" / "applications",
            Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local" / "share" / "flatpak" / "exports" / "share" / "applications",
        ]
        apps: list[AppEntry] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.desktop"):
                try:
                    values: dict[str, str] = {}
                    for line in path.read_text(errors="replace").splitlines():
                        if "=" in line and not line.lstrip().startswith("#"):
                            key, value = line.split("=", 1)
                            values.setdefault(key.strip(), value.strip())
                except OSError:
                    continue
                if values.get("NoDisplay", "").lower() == "true":
                    continue
                if values.get("Hidden", "").lower() == "true":
                    continue
                name = values.get("Name", "").split(" (", 1)[0].strip() or path.stem
                if not name:
                    continue
                aliases = {
                    path.stem,
                    values.get("GenericName", ""),
                    values.get("Exec", "").split()[0] if values.get("Exec") else "",
                }
                apps.append(
                    AppEntry(
                        name=name,
                        target=str(path),
                        bundle_id=values.get("StartupWMClass", ""),
                        launch_id=path.stem,
                        aliases=tuple(sorted(alias for alias in aliases if alias)),
                        source="linux-desktop",
                    )
                )
        return apps


def _escape_osascript_name(value: str) -> str:
    """Escape an application name for safe interpolation into an AppleScript string literal.

    Escapes backslashes, double quotes, and control characters so a crafted app
    alias cannot break out of the string and inject AppleScript commands (e.g.
    ``do shell script``).
    """
    cleaned = (value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ")
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def _run_quiet(args: list[str]) -> int:
    try:
        return subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def _osascript(script: str, timeout: int = 10) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (result.stderr or result.stdout or "").strip()
    return result.returncode == 0, output


def _resolve_windows_shortcut(target: str) -> str | None:
    if os.name != "nt" or not target.lower().endswith(".lnk"):
        return target if target.lower().endswith(".exe") else None
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return None
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]); "
        "Write-Output $s.TargetPath"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script, target],
            capture_output=True,
            text=True,
            timeout=12,
        )
        if result.returncode == 0:
            resolved = result.stdout.strip().splitlines()
            if resolved:
                return resolved[-1]
    except (OSError, subprocess.SubprocessError):
        log.debug("Could not resolve Windows shortcut", exc_info=True)
    return None
