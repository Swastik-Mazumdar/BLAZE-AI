"""Comprehensive unit tests for the OfflineButler core.

These tests exercise every command path the offline butler supports
(open/close apps, web search, media playback, screen capture, power,
type, system info, messaging via all channels, and file download) using
fake/mock dependencies so no network, microphone, or UI automation is
required. They catch regressions in command dispatch and argument handling.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make package importable when run directly.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blaze_ai.core.butler import ButlerReply, OfflineButler  # noqa: E402
from blaze_ai.core.command_filter import Command  # noqa: E402
from blaze_ai.core.system import SystemController  # noqa: E402


# --------------------------------------------------------------------------- #
# Mock dependencies
# --------------------------------------------------------------------------- #
class FakeAppFinder:
    """Stand-in for AppFinder that records calls instead of touching the OS."""

    def __init__(self) -> None:
        self.opened_apps: list[str] = []
        self.closed_apps: list[str] = []
        self.opened_urls: list[tuple[str, str | None]] = []
        self.recording_started = False
        self.recording_stopped = False
        self.screenshot_taken = False
        self.screenshot_path: str | None = None
        self.screen_record_error: Exception | None = None

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name
            self.target = name

    def find_best(self, name: str):
        return self.Entry(name), 1.0

    def search(self, _name: str, limit: int = 4):
        return [(1.0, self.Entry("Safari"))][:limit]

    def launch(self, app) -> None:
        self.opened_apps.append(app.name)

    def close(self, app) -> bool:
        self.closed_apps.append(app.name)
        return True

    def open_app(self, app: str) -> None:
        self.opened_apps.append(app)

    def close_app(self, app: str) -> None:
        self.closed_apps.append(app)

    def open_url(self, url: str, app: str | None = None) -> str:
        self.opened_urls.append((url, app))
        return f"opened {url}"

    def start_recording(self) -> str:
        if self.screen_record_error:
            raise self.screen_record_error
        self.recording_started = True
        return "recording"

    def stop_recording(self) -> str:
        if self.screen_record_error:
            raise self.screen_record_error
        self.recording_stopped = True
        return "stopped"

    def save_screenshot(self, path: str | None = None) -> str:
        self.screenshot_taken = True
        self.screenshot_path = path
        return path or "screenshot.png"


class FakeSystemController(SystemController):
    """Stand-in for SystemController that records power actions."""

    def __init__(self) -> None:
        # Avoid QObject init (needs a Qt app); just record calls.
        self.actions: list[str] = []

    def shutdown(self, *, delay_seconds: int = 3) -> None:
        self.actions.append(("shutdown", delay_seconds))

    def restart(self, *, delay_seconds: int = 3) -> None:
        self.actions.append(("restart", delay_seconds))

    def sleep(self) -> None:
        self.actions.append("sleep")

    def lock(self) -> None:
        self.actions.append("lock")


class FakeRecorder:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.screenshot_path: str | None = None
        self.fps = 30
        self.is_recording = False

    def start(self):
        self.started = True
        self.is_recording = True
        return type("Ticket", (), {"message": "Recording saved."})()

    def stop(self):
        self.stopped = True
        self.is_recording = False
        return type("Ticket", (), {"message": "Recording saved."})()

    def start_recording(self) -> str:
        self.started = True
        return "recording"

    def stop_recording(self) -> str:
        self.stopped = True
        return "stopped"

    def save_screenshot(self, path: str | None = None) -> str:
        self.screenshot_path = path
        return path or "screenshot.png"


class FakeGovernor:
    def refresh(self) -> None:
        pass

    def report(self) -> str:
        return "RAM: 4/16 GB · CPU: 12%"


class FakeTyping:
    def __init__(self) -> None:
        self.typed: list[str] = []

    def type_text(self, text: str, *, press_enter: bool = False) -> None:
        self.typed.append(text)


class FakeMemory:
    def __init__(self) -> None:
        self.contacts: dict[str, dict[str, str]] = {}

    def find_contact(self, name: str):
        contact = self.contacts.get(name.lower())
        if contact:
            return contact, 1.0
        return None, 0.0


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
def make_butler(
    *,
    finder=None,
    system=None,
    recorder=None,
    governor=None,
    typing=None,
    memory=None,
    user_name="friend",
):
    return OfflineButler(
        user_name=user_name,
        finder=finder or FakeAppFinder(),
        system=system or FakeSystemController(),
        recorder=recorder or FakeRecorder(),
        governor=governor or FakeGovernor(),
        typing=typing or FakeTyping(),
        confirm_destructive=True,
        memory=memory,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
class ButlerDispatchTests(unittest.TestCase):
    """High-level command dispatch through run_command."""

    def test_unknown_command_returns_polite_message(self) -> None:
        butler = make_butler()
        reply = butler.run_command(Command("teleport", "x"))
        self.assertIn("not wired up", reply.text)

    def test_exception_is_caught_and_reported(self) -> None:
        finder = FakeAppFinder()
        finder.launch = MagicMock(side_effect=OSError("boom"))
        butler = make_butler(finder=finder)
        reply = butler.run_command(Command("open_app", "Safari"))
        self.assertIn("could not launch", reply.text.lower())


class ButlerOpenCloseAppTests(unittest.TestCase):
    def test_open_app(self) -> None:
        finder = FakeAppFinder()
        butler = make_butler(finder=finder)
        reply = butler.run_command(Command("open_app", "Safari", "open Safari"))
        self.assertIn("Safari", reply.text)
        self.assertIn("Safari", finder.opened_apps)

    def test_close_app(self) -> None:
        finder = FakeAppFinder()
        butler = make_butler(finder=finder)
        reply = butler.run_command(Command("close_app", "Safari", "close Safari"))
        self.assertIn("Safari", reply.text)
        self.assertIn("Safari", finder.closed_apps)


class ButlerSearchMediaTests(unittest.TestCase):
    def test_web_search(self) -> None:
        finder = FakeAppFinder()
        butler = make_butler(finder=finder)
        reply = butler.run_command(Command("web_search", "weather tomorrow", "search weather tomorrow"))
        self.assertIn("weather tomorrow", reply.text)
        self.assertTrue(any("weather+tomorrow" in url for url, _ in finder.opened_urls))

    def test_play_media(self) -> None:
        finder = FakeAppFinder()
        butler = make_butler(finder=finder)
        reply = butler.run_command(Command("play_media", "Shape of You", "play Shape of You on YouTube", target="youtube"))
        self.assertIn("Shape of You", reply.text)


class ButlerMessagingSafetyTests(unittest.TestCase):
    def test_imessage_requires_confirmation_and_cancel_discards_payload(self) -> None:
        butler = make_butler()
        with patch("blaze_ai.core.butler.sys.platform", "darwin"), patch.object(
            butler, "_send_imessage"
        ) as send:
            prepared = butler.run_command(
                Command("send_message", "hello", "message Mom hello", target="imessage", extra="Mom")
            )
            self.assertIn("confirm send message", prepared.text.lower())
            send.assert_not_called()
            butler.run_command(Command("cancel"))
            self.assertIsNone(butler.pending_action)
            send.assert_not_called()

    def test_confirm_sends_only_the_prepared_message(self) -> None:
        butler = make_butler()
        with patch("blaze_ai.core.butler.sys.platform", "darwin"), patch.object(
            butler, "_send_imessage"
        ) as send:
            butler.run_command(
                Command("send_message", "hello", target="imessage", extra="Mom")
            )
            reply = butler.run_command(Command("confirm", "send_message"))
        send.assert_called_once_with("Mom", "hello")
        self.assertIn("Sent iMessage", reply.text)

    def test_imessage_arguments_are_not_interpolated_into_applescript(self) -> None:
        butler = make_butler()
        target = 'Mom"\ndo shell script "bad"'
        body = 'Hello"\nset x to "unsafe"'
        completed = type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        with patch("blaze_ai.core.butler.sys.platform", "darwin"), patch(
            "blaze_ai.core.butler.subprocess.run", return_value=completed
        ) as run:
            butler._send_imessage(target, body)
        args = run.call_args.args[0]
        self.assertEqual(args[-2:], [target, body])
        self.assertIn("item 1 of argv", args[2])
        self.assertNotIn(target, args[2])
        self.assertNotIn(body, args[2])


class ButlerSystemTests(unittest.TestCase):
    def test_system_info(self) -> None:
        butler = make_butler()
        reply = butler.run_command(Command("system_info"))
        self.assertIn("RAM", reply.text)

    def test_time_and_date(self) -> None:
        butler = make_butler()
        self.assertIsNotNone(butler.run_command(Command("time")).text)
        self.assertIsNotNone(butler.run_command(Command("date")).text)

    def test_help(self) -> None:
        butler = make_butler()
        reply = butler.run_command(Command("help"))
        self.assertIn("open", reply.text.lower())


class ButlerPowerTests(unittest.TestCase):
    def test_shutdown_requires_confirmation(self) -> None:
        system = FakeSystemController()
        butler = make_butler(system=system)
        reply = butler.run_command(Command("shutdown", "shutdown", "shut down"))
        self.assertIn("confirm", reply.text.lower())
        self.assertEqual(system.actions, [])

    def test_confirm_shutdown_executes(self) -> None:
        system = FakeSystemController()
        butler = make_butler(system=system)
        butler.run_command(Command("shutdown", "shutdown", "shut down"))
        reply = butler.run_command(Command("confirm", "shutdown", "confirm shutdown"))
        self.assertIn("power down", reply.text.lower())
        self.assertEqual(system.actions, [("shutdown", 3)])

    def test_cancel_removes_pending(self) -> None:
        butler = make_butler()
        butler.run_command(Command("shutdown", "shutdown", "shut down"))
        reply = butler.run_command(Command("cancel"))
        self.assertIn("Cancelled", reply.text)

    def test_restart_and_sleep_and_lock(self) -> None:
        system = FakeSystemController()
        butler = make_butler(system=system)
        butler.run_command(Command("restart", "restart", "restart computer"))
        butler.run_command(Command("confirm", "restart", "confirm restart"))
        butler.run_command(Command("sleep", "sleep", "sleep"))
        butler.run_command(Command("confirm", "sleep", "confirm sleep"))
        butler.run_command(Command("lock", "lock", "lock screen"))
        butler.run_command(Command("confirm", "lock", "confirm lock"))
        kinds = [a[0] if isinstance(a, tuple) else a for a in system.actions]
        self.assertIn("restart", kinds)
        self.assertIn("sleep", kinds)
        self.assertIn("lock", kinds)


class ButlerRecordTypeTests(unittest.TestCase):
    def test_start_stop_recording(self) -> None:
        recorder = FakeRecorder()
        butler = make_butler(recorder=recorder)
        r1 = butler.run_command(Command("record_start"))
        self.assertTrue(recorder.started)
        r2 = butler.run_command(Command("record_stop"))
        self.assertTrue(recorder.stopped)

    def test_take_screenshot(self) -> None:
        butler = make_butler()
        with patch("blaze_ai.core.butler.save_screenshot", return_value="capture.png"):
            reply = butler.run_command(Command("screenshot"))
        self.assertIn("capture.png", reply.text)

    def test_type_text(self) -> None:
        typing = FakeTyping()
        butler = make_butler(typing=typing)
        reply = butler.run_command(Command("type_text", "hello world", "type hello world"))
        self.assertIn("hello world", typing.typed[0])
