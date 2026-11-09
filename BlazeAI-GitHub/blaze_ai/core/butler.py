"""Offline daily-life agent / smart system butler.

This module is the zero-key brain. It executes deterministic local actions and
answers common daily prompts without a network connection, while clearly
explaining when an online LLM would add value.
"""

from __future__ import annotations

import logging
import os
import platform
import random
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from .app_finder import AppFinder, clipboard_url
from .command_filter import Command
from .net import download_file_safely, web_search_url
from .screen_recorder import ScreenRecorder, ScreenRecordingError, save_screenshot
from .system import ResourceGovernor, SystemController, format_bytes
from .typing_service import TypingError, TypingService

log = logging.getLogger("blaze.butler")

_CONFIRM_WINDOW_SECONDS = 30.0

# Match greetings at the start of the message, so "hi" and "hi there" work
# without also matching words that merely contain "hi" (e.g. "this", "which").
_GREETING = re.compile(
    r"^\s*(?:hi|hello|hey|yo|howdy|greetings|sup|"
    r"good\s+(?:morning|afternoon|evening))"
    r"(?:\s+(?:there|blaze|ai|friend))?"
    r"\s*(?:[,.!…]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ButlerReply:
    text: str
    ui_action: str = ""


class OfflineButler:
    def __init__(
        self,
        *,
        user_name: str = "friend",
        finder: AppFinder,
        system: SystemController,
        recorder: ScreenRecorder,
        governor: ResourceGovernor,
        typing: TypingService,
        confirm_destructive: bool = True,
        memory=None,
    ) -> None:
        self.user_name = user_name or "friend"
        self.finder = finder
        self.system = system
        self.recorder = recorder
        self.governor = governor
        self.typing = typing
        self.confirm_destructive = confirm_destructive
        self.memory = memory
        self._pending: tuple[str, float] | None = None
        self._pending_message: tuple[str, str, str] | None = None

    @property
    def pending_action(self) -> str | None:
        if self._pending and time.time() - self._pending[1] <= _CONFIRM_WINDOW_SECONDS:
            return self._pending[0]
        return None

    def set_user_name(self, name: str) -> None:
        self.user_name = (name or "friend").strip() or "friend"

    def run_command(self, command: Command) -> ButlerReply:
        handler = getattr(self, f"_cmd_{command.kind}", None)
        if handler is None:
            return ButlerReply("That local command is not wired up yet.")
        try:
            return handler(command)
        except Exception as exc:
            log.exception("Butler command failed: %s", command.kind)
            return ButlerReply(f"That command failed: {exc}")

    def type_text(self, text: str) -> ButlerReply:
        try:
            self.typing.type_text(text)
            return ButlerReply(f"Typed {len(text)} characters into the active app.")
        except TypingError as exc:
            return ButlerReply(
                f"{exc}\n\nTip: on macOS grant Blaze Accessibility permission "
                "(System Settings → Privacy & Security → Accessibility)."
            )

    _GENERIC_BROWSER_NAMES = {
        "",
        "browser",
        "the browser",
        "this browser",
        "current browser",
        "default browser",
        "my browser",
        "web browser",
    }

    def _browser_target(
        self, requested: str
    ) -> tuple["AppEntry | None", ButlerReply | None]:
        value = (requested or "").strip().strip("\"'")
        if value.lower() in self._GENERIC_BROWSER_NAMES:
            return None, None
        cleaned = re.sub(
            r"\s+(?:browser|app|application)$", "", value, flags=re.IGNORECASE
        ).strip()
        if cleaned.lower() in self._GENERIC_BROWSER_NAMES:
            return None, None
        app, score = self.finder.find_best(cleaned)
        if app is None or score < 0.55:
            return None, ButlerReply(
                f"I could not find an installed browser matching “{value}”."
            )
        return app, None

    def _cmd_new_tab(self, command: Command) -> ButlerReply:
        app, problem = self._browser_target(command.argument)
        if problem is not None:
            return problem
        try:
            used = self.finder.open_new_tab(app)
        except Exception as exc:
            log.exception("Could not open a browser tab")
            return ButlerReply(f"I could not open a new tab: {exc}")
        name = used.name if used is not None else "your default browser"
        return ButlerReply(f"Opened a new tab in {name}.")

    def _cmd_open_url(self, command: Command) -> ButlerReply:
        url = (command.argument or "").strip()
        if not url:
            url = clipboard_url()
        if not url:
            return ButlerReply(
                "I need a website address. Copy a link first, or say "
                "“open example.com in Brave”."
            )
        app, problem = self._browser_target(command.target)
        if problem is not None:
            return problem
        try:
            opened = self.finder.open_url(url, app)
        except ValueError as exc:
            return ButlerReply(str(exc))
        except Exception as exc:
            log.exception("Could not open a website")
            return ButlerReply(f"I could not open that website: {exc}")
        if self.memory is not None:
            try:
                self.memory.record_success("open_url", opened)
            except Exception:
                pass
        name = app.name if app is not None else "your default browser"
        return ButlerReply(f"Opened {opened} in {name}.")

    @staticmethod
    def _osascript(script: str, timeout: int = 20) -> tuple[bool, str]:
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

    @staticmethod
    def _platform_key(value: str) -> str:
        key = (value or "").strip().lower().replace("-", " ").replace("_", " ")
        key = re.sub(r"\s+", " ", key)
        aliases = {
            "music": "music",
            "apple music": "apple_music",
            "applemusic": "apple_music",
            "itunes": "itunes",
            "spotify": "spotify",
            "youtube": "youtube",
            "you tube": "youtube",
            "yt": "youtube",
            "youtube music": "youtube_music",
            "yt music": "youtube_music",
            "ytmusic": "youtube_music",
            "soundcloud": "soundcloud",
            "amazon music": "amazon_music",
            "prime music": "amazon_music",
            "tidal": "tidal",
            "deezer": "deezer",
            "jiosaavn": "jiosaavn",
            "gaana": "gaana",
            "wynk": "wynk",
        }
        return aliases.get(key, key)

    def _cmd_play_media(self, command: Command) -> ButlerReply:
        query = (command.argument or "").strip().strip("\"'")
        platform = self._platform_key(command.target or "music")
        browser_hint = (command.extra or "").strip()
        if not query:
            return ButlerReply(
                "What should I play? Try “play Seedhe Maut”, “play it on YouTube”, "
                "or “play [song] on Apple Music”."
            )

        # Native/local-capable apps on macOS.
        if sys.platform == "darwin" and platform in {"music", "apple_music", "itunes"}:
            return self._play_apple_music(query, platform, browser_hint=browser_hint)
        if platform == "spotify":
            return self._play_spotify(query, browser_hint=browser_hint)

        # Web/media platforms: search and attempt playback in browser.
        return self._play_on_web_platform(query, platform, browser_hint=browser_hint)

    @staticmethod
    def _escape_osascript_text(value: str) -> str:
        # Defense against AppleScript command injection: strip control chars and line breaks
        cleaned = (value or "").replace("\0", "").replace("\r", " ").replace("\n", " ")
        return cleaned.replace("\\", "\\\\").replace('"', '\\"')

    def _remember_media(self, action: str, detail: str) -> None:
        if self.memory is None:
            return
        try:
            self.memory.record_success(action, detail)
        except Exception:
            pass

    def _resolve_browser_hint(self, browser_hint: str):
        if not browser_hint:
            return None, None
        app, problem = self._browser_target(browser_hint)
        if problem is not None:
            return None, problem
        return app, None

    def _play_apple_music(self, query: str, platform: str, *, browser_hint: str = "") -> ButlerReply:
        _ = browser_hint  # Music app playback is handled in-app on macOS.
        app_name = "iTunes" if platform == "itunes" else "Music"
        safe_query = self._escape_osascript_text(query)

        # Immediate play for generic "play music" / "play something"
        if query.lower().strip() in {"music", "something"}:
            direct_script = f'''
tell application "{app_name}"
    activate
    play
    return "playing"
end tell
'''
            ok, output = self._osascript(direct_script, timeout=10)
            if ok and "playing" in output.lower():
                self._remember_media("play_media", f"{app_name}:music")
                return ButlerReply(f"Playing music in {app_name}.")

        # Fast path: play from local library if an exact-enough local match exists.
        library_script = f'''
tell application "{app_name}"
    activate
    try
        set matches to (search playlist "Library" for "{safe_query}")
        if (count of matches) > 0 then
            play item 1 of matches
            return "playing"
        end if
    end try
    try
        set matches to (search playlist "Music" for "{safe_query}")
        if (count of matches) > 0 then
            play item 1 of matches
            return "playing"
        end if
    end try
end tell
return "notfound"
'''
        ok, output = self._osascript(library_script, timeout=20)
        if ok and "playing" in output.lower():
            self._remember_media("play_media", f"{app_name}:{query}")
            try:
                if self.memory is not None:
                    self.memory.set_preference("preferred_music_app", app_name)
            except Exception:
                pass
            return ButlerReply(f"Playing “{query}” in {app_name}.")

        # Online/catalog search: use Music's search UI and attempt to play the top result.
        activate_ok, activate_out = self._osascript(
            f'tell application "{app_name}" to activate', timeout=10
        )
        if not activate_ok and app_name == "Music":
            app_name = "iTunes"
            self._osascript('tell application "iTunes" to activate', timeout=10)
        time.sleep(0.8)

        ui_script = f'''
tell application "System Events"
    tell process "{app_name}"
        set frontmost to true
        delay 0.15
        -- Focus search and submit query (searches library/catalog in the Music UI).
        keystroke "f" using {{command down}}
        delay 0.25
        keystroke "a" using {{command down}}
        delay 0.05
        keystroke "{safe_query}"
        delay 0.2
        key code 36 -- Return: run search
        delay 1.2
        -- Best effort: move focus to results and play the top selection.
        -- Different macOS/Music layouts need different navigation; try a few safe keys.
        key code 48 -- Tab
        delay 0.15
        key code 125 -- Down
        delay 0.1
        key code 36 -- Return / play selection where supported
        delay 0.2
        key code 49 -- Space fallback (play/pause) if a playable item is targeted
        return "searched"
    end tell
end tell
'''
        ui_ok, ui_out = self._osascript(ui_script, timeout=18)
        combined = f"{output}\n{activate_out}\n{ui_out}".lower()
        if "not allowed" in combined or "assist" in combined or "not authorised" in combined:
            return ButlerReply(
                f"I searched for “{query}” in {app_name}, but macOS blocked UI automation. "
                "Allow Blaze in System Settings → Privacy & Security → Automation and Accessibility, "
                "then try again. I can still open the search if permissions are fixed."
            )
        if ui_ok and "searched" in ui_out.lower():
            self._remember_media("play_media_online_search", f"{app_name}:{query}")
            try:
                if self.memory is not None:
                    self.memory.set_preference("preferred_music_app", app_name)
            except Exception:
                pass
            return ButlerReply(
                f"Searching “{query}” online in {app_name} and attempting to play the top result. "
                "If it only shows results, pick the song once (macOS Accessibility permissions help Blaze control playback)."
            )

        # Fallback: open Apple Music web search so the user can play online.
        try:
            from urllib.parse import quote_plus

            self.finder.open_url(
                f"https://music.apple.com/search?term={quote_plus(query)}", None
            )
        except Exception:
            pass
        return ButlerReply(
            f"I couldn’t control {app_name} to play “{query}” automatically. "
            "I opened Apple Music search online so you can play it from the results."
        )

    def _play_spotify(self, query: str, *, browser_hint: str = "") -> ButlerReply:
        if query.lower().strip() in {"music", "something"}:
            script = '''
tell application "Spotify"
    activate
    play
end tell
'''
            ok, output = self._osascript(script, timeout=10)
            if ok:
                self._remember_media("play_media", "Spotify:music")
                return ButlerReply("Playing music in Spotify.")

        safe_query = self._escape_osascript_text(query)
        script = f'''
tell application "Spotify"
    activate
    play track "spotify:search:{safe_query}"
end tell
'''
        ok, output = self._osascript(script, timeout=15)
        if ok:
            self._remember_media("play_media", f"Spotify:{query}")
            try:
                if self.memory is not None:
                    self.memory.set_preference("preferred_music_app", "Spotify")
            except Exception:
                pass
            return ButlerReply(f"Searching and playing “{query}” in Spotify.")
        # Fallback to web search/open.
        browser_app, problem = self._resolve_browser_hint(browser_hint)
        if problem is not None:
            browser_app = None
        try:
            from urllib.parse import quote_plus

            self.finder.open_url(
                f"https://open.spotify.com/search/{quote_plus(query)}", browser_app
            )
        except Exception:
            pass
        if "not allowed" in output.lower():
            return ButlerReply(
                "Spotify automation was blocked by macOS. I opened Spotify search in your browser. "
                "Allow Blaze in System Settings → Privacy & Security → Automation to control Spotify directly."
            )
        target = browser_app.name if browser_app is not None else "your browser"
        return ButlerReply(f"I couldn’t control Spotify directly, so I opened a Spotify search for “{query}” in {target}.")

    def _web_media_url(self, query: str, platform: str) -> tuple[str, str]:
        from urllib.parse import quote_plus

        q = quote_plus(query)
        # Honor the requested platform literally so "on YouTube" can find videos,
        # while "on YouTube Music" targets the music player/search.
        mapping = {
            "youtube": (f"https://www.youtube.com/results?search_query={q}", "YouTube"),
            "youtube_music": (f"https://music.youtube.com/search?q={q}", "YouTube Music"),
            "soundcloud": (f"https://soundcloud.com/search?q={q}", "SoundCloud"),
            "amazon_music": (f"https://music.amazon.com/search/{q}", "Amazon Music"),
            "tidal": (f"https://listen.tidal.com/search?q={q}", "TIDAL"),
            "deezer": (f"https://www.deezer.com/search/{q}", "Deezer"),
            "jiosaavn": (f"https://www.jiosaavn.com/search/song/{q}", "JioSaavn"),
            "gaana": (f"https://gaana.com/search/{q}", "Gaana"),
            "wynk": (f"https://wynk.in/music/search/{q}", "Wynk"),
            "apple_music": (f"https://music.apple.com/search?term={q}", "Apple Music"),
        }
        if platform in mapping:
            return mapping[platform]
        # Generic fallback: a web search that targets playback intent.
        return (
            f"https://www.youtube.com/results?search_query={q}",
            platform.replace("_", " ").title() or "the web",
        )

    def _attempt_browser_play(self, browser_app) -> bool:
        """Best-effort: try to activate the first result in the front browser.

        No external browser automation framework; uses macOS System Events.
        Returns True if the attempt ran without an obvious automation error.
        """
        if sys.platform != "darwin":
            return False
        process = "Safari"
        if browser_app is not None:
            name = (browser_app.name or "").strip()
            if name:
                # Common app process names map well; keep the app's display name.
                process = self._escape_osascript_text(name)
        # Heuristic: move focus into page and select/activate first playable result.
        # Works differently per site/browser; safe no-op-ish keystrokes with delays.
        script = f'''
tell application "System Events"
    try
        tell process "{process}" to set frontmost to true
    end try
    delay 0.4
    -- Try to reach first content/result and activate it.
    key code 48 -- Tab
    delay 0.12
    key code 48 -- Tab
    delay 0.12
    key code 48 -- Tab
    delay 0.12
    key code 36 -- Enter
    return "attempted"
end tell
'''
        ok, out = self._osascript(script, timeout=8)
        return bool(ok and "attempted" in out.lower())

    def _play_on_web_platform(self, query: str, platform: str, *, browser_hint: str = "") -> ButlerReply:
        if query.lower().strip() in {"music", "something"}:
            query = "popular music"
        browser_app, problem = self._resolve_browser_hint(browser_hint)
        if problem is not None:
            return problem
        url, label = self._web_media_url(query, platform)
        try:
            self.finder.open_url(url, browser_app)
        except Exception as exc:
            log.exception("Could not open media search URL")
            return ButlerReply(f"I couldn’t open {label} to search for “{query}”: {exc}")

        # Give the page a moment, then best-effort attempt to play/select first result.
        played_attempt = False
        if sys.platform == "darwin":
            time.sleep(1.3)
            played_attempt = self._attempt_browser_play(browser_app)

        self._remember_media("play_media_web_search", f"{label}:{query}")
        target = browser_app.name if browser_app is not None else "your browser"
        if played_attempt:
            return ButlerReply(
                f"Searched for “{query}” on {label} in {target} and attempted to play the top result. "
                "If it didn’t start (some sites/browsers block UI automation), click the first result once."
            )
        return ButlerReply(
            f"Opened {label} in {target} and searched for “{query}”. "
            "I attempted playback where possible; if needed, click the first result to play."
        )

    def _cmd_open_app(self, command: Command) -> ButlerReply:
        if not command.argument:
            return ButlerReply("Which application should I open?")
        app, score = self.finder.find_best(command.argument)
        if app is None or score < 0.58:
            suggestions = self.finder.search(command.argument, limit=4)
            if suggestions:
                names = " · ".join(item.name for _, item in suggestions)
                return ButlerReply(
                    f"I could not confidently match “{command.argument}”. "
                    f"Closest installed matches: {names}."
                )
            return ButlerReply(
                f"I could not find an installed application matching “{command.argument}”. "
                "Try the full app name, or check that it is installed."
            )
        try:
            self.finder.launch(app)
        except Exception as exc:
            return ButlerReply(f"I found {app.name} but could not launch it: {exc}")
        if self.memory is not None:
            try:
                self.memory.record_success("open_app", app.name)
                # Self-learning: remember successful mappings for future offline use.
                if score >= 0.72 and command.argument.strip().lower() != app.name.lower():
                    self.memory.learn_app_alias(command.argument, app.name, app.target)
            except Exception:
                pass
        confidence = f" ({score * 100:.0f}% match)" if score < 0.97 else ""
        learned = ""
        if self.memory is not None and 0.72 <= score < 0.995:
            learned = " I’ll remember that."
        return ButlerReply(f"Opened {app.name}{confidence}.{learned}")

    def _cmd_close_app(self, command: Command) -> ButlerReply:
        app, score = self.finder.find_best(command.argument)
        if app is None or score < 0.58:
            return ButlerReply(
                f"I could not find a running match for “{command.argument}”. "
                "Try the full application name."
            )
        if self.finder.close(app):
            return ButlerReply(f"Closed {app.name}.")
        return ButlerReply(
            f"I found {app.name}, but the system refused to close it. "
            "It may need to be closed manually."
        )

    def _cmd_type_text(self, command: Command) -> ButlerReply:
        return self.type_text(command.argument)

    def _cmd_record_start(self, command: Command) -> ButlerReply:
        _ = command
        try:
            ticket = self.recorder.start()
        except ScreenRecordingError as exc:
            return ButlerReply(f"Screen recording could not start: {exc}")
        return ButlerReply(
            f"Screen recording started at {self.recorder.fps} fps. "
            "Say or type “stop recording” to save it."
        )

    def _cmd_record_stop(self, command: Command) -> ButlerReply:
        _ = command
        if not self.recorder.is_recording:
            return ButlerReply("There is no screen recording in progress.")
        try:
            ticket = self.recorder.stop()
        except ScreenRecordingError as exc:
            return ButlerReply(f"Recording could not be stopped: {exc}")
        return ButlerReply(
            f"{ticket.message}\nI will report the final file path when encoding finishes."
        )

    def _cmd_screenshot(self, command: Command) -> ButlerReply:
        _ = command
        try:
            path = save_screenshot()
        except ScreenRecordingError as exc:
            return ButlerReply(f"Screenshot failed: {exc}")
        return ButlerReply(f"Screenshot saved to {path}")

    def _cmd_system_info(self, command: Command) -> ButlerReply:
        _ = command
        self.governor.refresh()
        return ButlerReply(self.governor.report())

    def _cmd_time(self, command: Command) -> ButlerReply:
        _ = command
        return ButlerReply("It is " + datetime.now().strftime("%I:%M %p").lstrip("0") + ".")

    def _cmd_date(self, command: Command) -> ButlerReply:
        _ = command
        return ButlerReply("Today is " + datetime.now().strftime("%A, %B %d, %Y") + ".")

    def _cmd_help(self, command: Command) -> ButlerReply:
        _ = command
        return ButlerReply(help_text(self.user_name))

    def _cmd_learn_alias(self, command: Command) -> ButlerReply:
        if self.memory is None:
            return ButlerReply("Local learning memory is not available.")
        phrase = (command.argument or "").strip()
        target = (command.target or "").strip()
        if not phrase or not target:
            return ButlerReply(
                "Tell me what to remember, for example: "
                "“remember that when I say code, open VS Code”."
            )
        # Resolve the target app now so we learn the real app.
        app, score = self.finder.find_best(target)
        if app is not None and score >= 0.55:
            self.memory.learn_app_alias(phrase, app.name, app.target)
            return ButlerReply(f"Learned: “{phrase}” → {app.name}. I’ll remember that offline.")
        # Fall back to a name-only alias (finder may resolve later after scan).
        self.memory.learn_app_alias(phrase, target, "")
        return ButlerReply(
            f"Learned: “{phrase}” → “{target}”. I’ll try to use that offline."
        )

    def _cmd_forget_alias(self, command: Command) -> ButlerReply:
        if self.memory is None:
            return ButlerReply("Local learning memory is not available.")
        phrase = (command.argument or "").strip()
        if not phrase:
            return ButlerReply("Which remembered name should I forget?")
        if self.memory.forget_app_alias(phrase):
            return ButlerReply(f"Forgot the alias “{phrase}”.")
        return ButlerReply(f"I don’t have a learned alias for “{phrase}”.")

    def _cmd_learning_status(self, command: Command) -> ButlerReply:
        _ = command
        if self.memory is None:
            return ButlerReply("Local learning memory is not available.")
        summary = self.memory.summary()
        aliases = []
        try:
            for phrase, info in list(self.memory.all_app_aliases().items())[:8]:
                aliases.append(f"{phrase} → {info.get('name','')}")
        except Exception:
            pass
        if aliases:
            return ButlerReply(summary + "\n\nRecent aliases:\n- " + "\n- ".join(aliases))
        return ButlerReply(summary + "\n\nTeach me with: “remember that when I say code, open VS Code”.")

    # -------------------------------------------------------- media transport
    def _cmd_media_pause(self, command: Command) -> ButlerReply:
        _ = command
        self.system.media_play_pause()
        return ButlerReply("Paused playback.")

    def _cmd_media_resume(self, command: Command) -> ButlerReply:
        _ = command
        self.system.media_play_pause()
        return ButlerReply("Resumed playback.")

    def _cmd_media_stop(self, command: Command) -> ButlerReply:
        _ = command
        self.system.media_stop()
        return ButlerReply("Stopped playback.")

    def _cmd_media_next(self, command: Command) -> ButlerReply:
        _ = command
        self.system.media_next()
        return ButlerReply("Skipped to the next track.")

    def _cmd_media_prev(self, command: Command) -> ButlerReply:
        _ = command
        self.system.media_prev()
        return ButlerReply("Went back to the previous track.")

    def _cmd_volume_up(self, command: Command) -> ButlerReply:
        _ = command
        self.system.volume_up()
        return ButlerReply("Volume turned up.")

    def _cmd_volume_down(self, command: Command) -> ButlerReply:
        _ = command
        self.system.volume_down()
        return ButlerReply("Volume turned down.")

    def _cmd_volume_mute(self, command: Command) -> ButlerReply:
        _ = command
        self.system.volume_mute()
        return ButlerReply("Mute toggled.")

    def _cmd_volume_set(self, command: Command) -> ButlerReply:
        try:
            level = max(0, min(100, int(command.argument)))
            self.system.volume_set(level)
            return ButlerReply(f"Volume set to {level}%.")
        except Exception as exc:
            return ButlerReply(f"Could not set volume: {exc}")

    # --------------------------------------------------- contacts & messaging
    def _cmd_list_contacts(self, command: Command) -> ButlerReply:
        _ = command
        if self.memory is None:
            return ButlerReply("Contact memory is not available.")
        contacts = self.memory.all_contacts()
        if not contacts:
            return ButlerReply(
                "You haven't saved any contacts yet.\n"
                "To add one, say “save contact Mom phone +1234567890”."
            )
        lines = [f"Saved contacts ({len(contacts)}):"]
        for _, info in contacts.items():
            name = info.get("name", "Unknown")
            details = []
            if info.get("phone"):
                details.append(info["phone"])
            if info.get("email"):
                details.append(info["email"])
            detail_str = " · ".join(details) if details else "no phone/email"
            lines.append(f"• {name} ({detail_str})")
        return ButlerReply("\n".join(lines))

    def _cmd_save_contact(self, command: Command) -> ButlerReply:
        if self.memory is None:
            return ButlerReply("Contact memory is not available.")
        name = (command.argument or "").strip()
        phone = (command.target or "").strip()
        email = (command.extra or "").strip()
        if not name or (not phone and not email):
            return ButlerReply("Please provide a name and at least a phone number or email.")
        self.memory.save_contact(name, phone=phone, email=email)
        summary = []
        if phone:
            summary.append(f"phone: {phone}")
        if email:
            summary.append(f"email: {email}")
        return ButlerReply(f"Saved contact “{name}” ({', '.join(summary)}).")

    def _cmd_delete_contact(self, command: Command) -> ButlerReply:
        if self.memory is None:
            return ButlerReply("Contact memory is not available.")
        name = (command.argument or "").strip()
        if not name:
            return ButlerReply("Which contact should I remove?")
        if self.memory.delete_contact(name):
            return ButlerReply(f"Removed contact “{name}”.")
        return ButlerReply(f"I couldn't find a contact matching “{name}”.")

    def _cmd_send_message(self, command: Command) -> ButlerReply:
        channel = (command.target or "sms").lower()
        message = (command.argument or "").strip()
        extra = (command.extra or "").strip()
        recipient = extra
        subject = "Message from Blaze AI"
        if channel == "email" and "|" in extra:
            recipient, subject = extra.split("|", 1)
            subject = subject.strip() or "Message from Blaze AI"
            recipient = recipient.strip()

        if not recipient:
            return ButlerReply("Who should I send the message to?")
        if not message:
            return ButlerReply("What message would you like to send?")

        phone = ""
        email = ""
        resolved_name = recipient
        if self.memory is not None:
            contact, score = self.memory.find_contact(recipient)
            if contact and score >= 0.55:
                resolved_name = contact.get("name", recipient)
                phone = contact.get("phone", "")
                email = contact.get("email", "")

        if not phone and re.search(r"\+?[0-9\-\(\)\s]{7,}", recipient):
            phone = recipient
        if not email and "@" in recipient:
            email = recipient

        import urllib.parse

        # 1. WhatsApp
        if channel == "whatsapp":
            clean_phone = re.sub(r"[^\d+]", "", phone).lstrip("+")
            if clean_phone:
                url = f"https://wa.me/{clean_phone}?text={urllib.parse.quote_plus(message)}"
                try:
                    self.finder.open_url(url)
                    return ButlerReply(
                        f"Opened WhatsApp to message {resolved_name} (+{clean_phone}): “{message}”."
                    )
                except Exception as exc:
                    return ButlerReply(f"Could not open WhatsApp: {exc}")
            else:
                url = f"https://web.whatsapp.com/send?text={urllib.parse.quote_plus(message)}"
                try:
                    self.finder.open_url(url)
                    return ButlerReply(
                        f"Opened WhatsApp with your message for {resolved_name}.\n"
                        f"Tip: save their phone number with “save contact {resolved_name} phone +1234567890” to message directly."
                    )
                except Exception as exc:
                    return ButlerReply(f"Could not open WhatsApp: {exc}")

        # 2. Email
        if channel == "email":
            target_email = email or recipient
            url = f"mailto:{target_email}?subject={urllib.parse.quote_plus(subject)}&body={urllib.parse.quote_plus(message)}"
            try:
                self.finder.open_url(url)
                return ButlerReply(
                    f"Opened email client with draft to {resolved_name} ({target_email}): “{subject}”."
                )
            except Exception as exc:
                return ButlerReply(f"Could not open mail client: {exc}")

        # 3. iMessage (Apple Messages) — macOS native
        if channel == "imessage":
            if sys.platform != "darwin":
                return ButlerReply("iMessage is only available on macOS.")
            target = phone or recipient
            if self.confirm_destructive:
                self._pending = ("send_message", time.time())
                self._pending_message = (target, message, resolved_name)
                return ButlerReply(
                    f"Ready to send this iMessage to {resolved_name}: “{message}”. "
                    "Say or type “confirm send message” within 30 seconds, or “cancel”."
                )
            self._send_imessage(target, message)
            return ButlerReply(f"Sent iMessage to {resolved_name}: “{message}”.")

        # 4. Telegram
        if channel == "telegram":
            username_or_phone = phone or recipient
            url = f"https://t.me/{username_or_phone}?text={urllib.parse.quote_plus(message)}"
            try:
                self.finder.open_url(url)
                return ButlerReply(
                    f"Opened Telegram to message {resolved_name}: “{message}”.\n"
                    f"Tip: use “save contact {resolved_name} phone +1234567890” for direct messaging."
                )
            except Exception as exc:
                return ButlerReply(f"Could not open Telegram: {exc}")

        # 5. Signal
        if channel == "signal":
            clean_phone = re.sub(r"[^\d+]", "", phone).lstrip("+")
            if clean_phone:
                url = f"https://signal.org/ua/c?number={clean_phone}&text={urllib.parse.quote_plus(message)}"
            else:
                url = "https://signal.org/"
            try:
                self.finder.open_url(url)
                return ButlerReply(f"Opened Signal to message {resolved_name}: “{message}”.")
            except Exception as exc:
                return ButlerReply(f"Could not open Signal: {exc}")

        # 6. Facebook Messenger
        if channel == "facebook":
            username = recipient
            url = f"https://m.me/{username}?text={urllib.parse.quote_plus(message)}"
            try:
                self.finder.open_url(url)
                return ButlerReply(
                    f"Opened Facebook Messenger to message {resolved_name}: “{message}”.\n"
                    f"Tip: use their Messenger username, e.g. “message john on facebook”"
                )
            except Exception as exc:
                return ButlerReply(f"Could not open Messenger: {exc}")

        # 7. Skype
        if channel == "skype":
            username = recipient
            url = f"skype:{username}?chat&message={urllib.parse.quote_plus(message)}"
            try:
                self.finder.open_url(url)
                return ButlerReply(f"Opened Skype to message {resolved_name}: “{message}”.")
            except Exception as exc:
                return ButlerReply(f"Could not open Skype: {exc}")

        # 8. Threema
        if channel == "threema":
            threema_id = recipient
            url = f"threema://compose?recipient={threema_id}&text={urllib.parse.quote_plus(message)}"
            try:
                self.finder.open_url(url)
                return ButlerReply(f"Opened Threema to message {resolved_name}: “{message}”.")
            except Exception as exc:
                return ButlerReply(f"Could not open Threema: {exc}")

        # 9. SMS / iMessage fallback
        clean_phone = re.sub(r"[^\d+]", "", phone) or phone
        if sys.platform == "darwin":
            sms_target = clean_phone or resolved_name
            sms_url = f"sms:{urllib.parse.quote(sms_target)}&body={urllib.parse.quote(message)}"
            try:
                subprocess.Popen(["open", sms_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return ButlerReply(f"Prepared message to {resolved_name} in Messages: “{message}”.")
            except Exception as exc:
                return ButlerReply(f"Could not open Messages: {exc}")
        elif os.name == "nt":
            sms_url = f"sms:{urllib.parse.quote(clean_phone)}?body={urllib.parse.quote(message)}"
            try:
                os.startfile(sms_url)  # type: ignore[attr-defined]
                return ButlerReply(f"Prepared SMS to {resolved_name}: “{message}”.")
            except Exception as exc:
                return ButlerReply(f"Could not open messaging: {exc}")
        else:
            return ButlerReply(f"Messaging is prepared for {resolved_name}: “{message}”.")

    def _send_imessage(self, target: str, message: str) -> None:
        """Send an iMessage via AppleScript on macOS (requires Messages automation permission)."""
        if sys.platform != "darwin":
            raise TypingError("iMessage is only available on macOS.")
        script = (
            "on run argv\n"
            'set targetAddress to item 1 of argv\n'
            'set bodyText to item 2 of argv\n'
            'tell application "Messages"\n'
            'set targetService to 1st service whose service type = iMessage\n'
            'set targetBuddy to buddy targetAddress of targetService\n'
            'send bodyText to targetBuddy\n'
            "end tell\n"
            "end run"
        )
        result = subprocess.run(
            ["osascript", "-e", script, str(target), str(message)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise TypingError(f"iMessage send failed: {detail or 'access denied (grant Messages automation permission)'}")

    # ---------------------------------------------------- search & downloading
    def _cmd_web_search(self, command: Command) -> ButlerReply:
        query = (command.argument or "").strip()
        engine = (command.target or "google").strip().lower()
        if not query:
            return ButlerReply("What would you like to search for?")
        url, label = web_search_url(query, engine)
        try:
            self.finder.open_url(url)
        except Exception as exc:
            return ButlerReply(f"Could not open {label} search: {exc}")
        if self.memory is not None:
            try:
                self.memory.record_success("web_search", f"{label}:{query}")
            except Exception:
                pass
        return ButlerReply(f"Searching for “{query}” on {label}.")

    def _cmd_download_file(self, command: Command) -> ButlerReply:
        target = (command.argument or "").strip()
        target_dir = (command.target or "").strip() or None
        if not target:
            return ButlerReply("Please provide a download URL or tell me what to search for.")

        # If it looks like a direct URL, download it immediately.
        from .net import is_safe_public_url
        safe, reason = is_safe_public_url(target)
        if safe:
            ok, msg, saved_path, count = download_file_safely(target, destination_dir=target_dir)
            if not ok:
                return ButlerReply(f"Download failed: {msg}")
            size_str = format_bytes(count)
            return ButlerReply(
                f"Successfully downloaded {saved_path.name} ({size_str}).\n"
                f"Saved to: {saved_path}"
            )

        # Not a direct URL: treat it as a search query. Open search results so the
        # user can find a direct download link, then ask Blaze to download that link.
        url, label = web_search_url(target, engine="google")
        try:
            self.finder.open_url(url)
        except Exception as exc:
            return ButlerReply(f"Could not open {label} search: {exc}")
        return ButlerReply(
            f"I opened {label} search results for “{target}”.\n"
            "Please copy the direct download link of the file you want and say “download <link>”."
        )

    def _cmd_ui_settings(self, command: Command) -> ButlerReply:
        _ = command
        return ButlerReply("Opening Settings.", ui_action="open_settings")

    def _cmd_quit(self, command: Command) -> ButlerReply:
        _ = command
        return ButlerReply("Closing Blaze.", ui_action="quit")

    def _cmd_shutdown(self, command: Command) -> ButlerReply:
        return self._guard_power(command, "shutdown")

    def _cmd_restart(self, command: Command) -> ButlerReply:
        return self._guard_power(command, "restart")

    def _cmd_sleep(self, command: Command) -> ButlerReply:
        return self._guard_power(command, "sleep")

    def _cmd_lock(self, command: Command) -> ButlerReply:
        return self._guard_power(command, "lock")

    def _guard_power(self, command: Command, action: str) -> ButlerReply:
        _ = command
        if self.confirm_destructive:
            self._pending = (action, time.time())
            return ButlerReply(
                f"Safety lock engaged. Say or type “confirm {action}” within 30 seconds "
                "to proceed. Say “cancel” to abort."
            )
        self._execute_power(action)
        return ButlerReply(self._power_done_text(action))

    def _cmd_confirm(self, command: Command) -> ButlerReply:
        action = self.pending_action
        if not action:
            self._pending = None
            self._pending_message = None
            return ButlerReply("There is no pending action to confirm.")
        requested = command.argument.replace("_", " ").strip().lower()
        valid_requested = {action, "shut down" if action == "shutdown" else action}
        if action == "send_message":
            valid_requested.update({"message", "send message"})
        if requested and requested not in valid_requested:
            return ButlerReply(
                f"The pending action is “{action}”, not “{requested}”. "
                f"Say “confirm {action}” to proceed."
            )
        self._pending = None
        if action == "send_message":
            pending = self._pending_message
            self._pending_message = None
            if not pending:
                return ButlerReply("The message details expired. Please prepare it again.")
            target, message, name = pending
            try:
                self._send_imessage(target, message)
            except Exception as exc:
                return ButlerReply(f"Could not send iMessage: {exc}")
            return ButlerReply(f"Sent iMessage to {name}: “{message}”.")
        self._execute_power(action)
        return ButlerReply(self._power_done_text(action))

    def _cmd_cancel(self, command: Command) -> ButlerReply:
        _ = command
        if self._pending:
            self._pending = None
            self._pending_message = None
            return ButlerReply("Cancelled. Nothing was changed.")
        return ButlerReply("Nothing is pending.")

    def _execute_power(self, action: str) -> None:
        if action == "shutdown":
            self.system.shutdown()
        elif action == "restart":
            self.system.restart()
        elif action == "sleep":
            self.system.sleep()
        elif action == "lock":
            self.system.lock()

    @staticmethod
    def _power_done_text(action: str) -> str:
        return {
            "shutdown": "Shutdown requested. The system will power down shortly.",
            "restart": "Restart requested. The system will reboot shortly.",
            "sleep": "Putting the system to sleep.",
            "lock": "Locking the session.",
        }[action]

    def fallback(self, text: str) -> ButlerReply:
        """Local conversational fallback. Honest, useful, never fabricated."""

        lower = " ".join(text.lower().strip().split())
        name = self.user_name
        if not lower:
            return ButlerReply("I am here. What should we do?")
        if _GREETING.match(text.strip()):
            hour = datetime.now().hour
            greeting = (
                "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
            )
            return ButlerReply(
                f"{greeting}, {name}. Local butler core is ready. "
                "I can open apps, record the screen, control power, type for you, "
                "or give you a system briefing."
            )
        if lower in {"who are you", "what are you", "introduce yourself"}:
            return ButlerReply(
                "I am Blaze — a local-first desktop intelligence. I handle local "
                "commands and briefings. Add a local model folder for general conversation."
            )
        if any(
            phrase in lower
            for phrase in ("what can you do", "help", "commands", "capabilities")
        ):
            return ButlerReply(help_text(name))
        if any(
            phrase in lower
            for phrase in ("brief me", "briefing", "daily brief", "system report", "status report")
        ):
            self.governor.refresh()
            now = datetime.now()
            return ButlerReply(
                f"{now.strftime('%A, %B %d')} · {now.strftime('%H:%M')}\n\n"
                + self.governor.report()
                + "\n\nSay “open” plus an app name, or “start recording” if you need it."
            )
        if "my name" in lower:
            return ButlerReply(f"You asked me to call you {name}.")
        if any(
            word in lower
            for word in ("weather", "forecast", "temperature outside", "rain today")
        ):
            return ButlerReply(
                "I cannot read live weather while offline, but I can give you a system briefing."
            )
        if any(word in lower for word in ("joke", "something funny")):
            return ButlerReply(random.choice(_JOKES))
        if any(word in lower for word in ("thank", "thanks")):
            return ButlerReply(f"Any time, {name}.")
        if "flip a coin" in lower or lower in {"heads or tails", "coin flip"}:
            return ButlerReply(f"Tossed a coin: {random.choice(('Heads!', 'Tails!'))}")
        if "roll a die" in lower or "roll a dice" in lower or lower in {"roll dice", "dice roll"}:
            return ButlerReply(f"Rolled a 6-sided die: {random.randint(1, 6)}.")

        # Safe offline arithmetic evaluation
        math_match = re.match(
            r"^(?:what\s+(?:is|'s)\s+|calculate\s+|compute\s+)?(?P<a>-?\d+(?:\.\d+)?)\s*(?P<op>[\+\-\*\/%])\s*(?P<b>-?\d+(?:\.\d+)?)\s*[\?\.]*$",
            lower.strip(),
        )
        if math_match:
            try:
                a_val = float(math_match.group("a"))
                op = math_match.group("op")
                b_val = float(math_match.group("b"))
                if op == "/" and b_val == 0:
                    return ButlerReply("Division by zero is undefined.")
                res = {"+": a_val + b_val, "-": a_val - b_val, "*": a_val * b_val, "/": a_val / b_val, "%": a_val % b_val}[op]
                res_str = f"{int(res)}" if res.is_integer() else f"{res:.4g}"
                return ButlerReply(f"{math_match.group('a')} {op} {math_match.group('b')} = {res_str}")
            except Exception:
                pass

        if any(word in lower for word in ("how are you", "you okay", "you good")):
            snap = self.governor.snapshot
            return ButlerReply(
                f"Operational. {format_bytes(snap.process_ram)} resident, "
                f"soft cap at {self.governor.limit_percent}%. "
                "More importantly — how are you?"
            )
        if any(
            phrase in lower
            for phrase in (
                "open ",
                "launch ",
                "start ",
                "close ",
                "type ",
                "shut down",
                "restart",
            )
        ):
            return ButlerReply(
                "I could not parse that as a local command. Try a direct form such as "
                "“open VS Code”, “close Safari”, “type hello”, or “restart computer”."
            )
        return ButlerReply(
            f"I am running offline, {name}, so open-ended reasoning is limited. "
            "I can still control this system: open or close apps, start/stop screen "
            "recording, type into the active app, report resources, and shut down or "
            "restart safely.\n\nLoad a Hugging Face model in Settings for open-ended "
            "conversation, or keep using me as the offline butler."
        )


def help_text(name: str = "friend") -> str:
    return (
        f"Smart Assistant & Butler Command Deck, {name}:\n\n"
        "APPS           “open VS Code” · “close WhatsApp” · “switch to Chrome” · “force quit Safari”\n"
        "MESSAGING      “message Mom I am on my way” · “whatsapp Alex see you at 6”\n"
        "               “email Dave about project saying finished” · “text John hello”\n"
        "CONTACTS       “save contact Mom phone +1234567890” · “list contacts” · “delete contact Alex”\n"
        "PLAY & MEDIA   “play Bohemian Rhapsody on Spotify” · “play music” · “pause” · “resume”\n"
        "               “next track” · “previous track” · “volume up” · “volume down” · “volume 75%”\n"
        "WEB SEARCH     “search for quantum computing” · “google recipe for pancakes”\n"
        "               “search wikipedia for Albert Einstein” · “search github for fastchat”\n"
        "DOWNLOADING    “download https://example.com/file.zip” (safe public downloads)\n"
        "CAPTURE        “start recording” · “stop recording” · “take a screenshot”\n"
        "INPUT          “type your message here” (types or pastes into the active app)\n"
        "SYSTEM         “system status” · “what time is it” · “what is the date” · “battery level”\n"
        "POWER          “restart computer” · “shut down” · “sleep” · “lock screen” (guarded with confirmation)\n"
        "LEARNING       “remember that when I say code, open VS Code” · “what have you learned”\n"
        "DAILY / TOOLS  “brief me” · “what can you do” · “flip a coin” · “25 * 4”\n\n"
        "Local commands run deterministically without needing an external cloud API or LLM."
    )


_JOKES = (
    "I asked my computer to take a break. It said it had too many tabs open — emotionally.",
    "There are two hard things in computing: cache invalidation, naming things, and off-by-one errors.",
    "My butler tried to be spontaneous. It scheduled it for Tuesday.",
    "I would tell you a UDP joke, but you might not get it.",
)


def platform_name() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if sys.platform.startswith("win"):
        return f"Windows {platform.release()}"
    return platform.platform()


def open_path(path: str) -> None:
    """Small helper used for “reveal recordings” affordances."""

    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", path])
    else:
        subprocess.Popen(["xdg-open", path])
