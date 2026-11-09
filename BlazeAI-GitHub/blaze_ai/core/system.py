"""System inspection, resource limiting and power controls."""

from __future__ import annotations

import gc
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

try:
    import psutil
except ImportError:  # pragma: no cover - psutil ships with the app
    psutil = None  # type: ignore[assignment]

log = logging.getLogger("blaze.system")


def format_bytes(value: float) -> str:
    value = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit in {"B", "KB"}:
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@dataclass(frozen=True)
class SystemSnapshot:
    total_ram: int
    used_ram: int
    available_ram: int
    process_ram: int
    system_percent: float
    process_percent: float
    cpu_percent: float
    battery_percent: float | None
    battery_plugged: bool | None
    uptime_seconds: float


def _fallback_total_ram() -> int:
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=4
            )
            return int(result.stdout.strip())
        if os.path.exists("/proc/meminfo"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 8 * 1024**3


def _fallback_process_ram() -> int:
    # ru_maxrss is bytes on macOS, KiB on Linux.
    usage = getattr(os, "times", None)
    _ = usage
    import resource

    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


class ResourceGovernor(QObject):
    """Live RAM telemetry and a soft process-memory cap.

    The limiter does not call ``RLIMIT_AS`` because doing so can abruptly kill a
    running Qt/Whisper process. Instead it defines a user-controlled budget,
    releases caches when the budget is crossed, and exposes pressure so the app
    can avoid loading large optional models.
    """

    updated = pyqtSignal(object)
    pressure = pyqtSignal(int)

    def __init__(self, limit_percent: int = 65, interval_ms: int = 1400) -> None:
        super().__init__()
        self.limit_percent = max(20, min(95, int(limit_percent)))
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh)
        self._process = psutil.Process(os.getpid()) if psutil else None
        self._last_pressure = 0.0
        self._cache_clearers: list[Callable[[], None]] = []
        self.snapshot = self._collect()
        if self._process is not None:
            self._process.cpu_percent(None)

    @property
    def soft_budget_bytes(self) -> int:
        return int(self.snapshot.total_ram * (self.limit_percent / 100.0))

    def set_limit_percent(self, value: int) -> None:
        self.limit_percent = max(20, min(95, int(value)))
        self.refresh()

    def add_cache_clearer(self, callback: Callable[[], None]) -> None:
        self._cache_clearers.append(callback)

    def start(self) -> None:
        self._timer.start()
        self.refresh()

    def stop(self) -> None:
        self._timer.stop()

    def refresh(self) -> SystemSnapshot:
        self.snapshot = self._collect()
        self.updated.emit(self.snapshot)
        budget = self.soft_budget_bytes
        if self.snapshot.process_ram > budget:
            now = time.time()
            if now - self._last_pressure > 20:
                self._last_pressure = now
                self._relieve_pressure(budget)
        return self.snapshot

    def _relieve_pressure(self, budget: int) -> None:
        log.warning(
            "Soft memory cap exceeded: process=%s budget=%s",
            format_bytes(self.snapshot.process_ram),
            format_bytes(budget),
        )
        gc.collect()
        for callback in list(self._cache_clearers):
            try:
                callback()
            except Exception:
                log.debug("Cache clearer failed", exc_info=True)
        self.pressure.emit(int(self.snapshot.process_ram - budget))

    def _collect(self) -> SystemSnapshot:
        if psutil is not None:
            memory = psutil.virtual_memory()
            process_ram = self._process.memory_info().rss if self._process else 0
            cpu = self._process.cpu_percent(None) if self._process else 0.0
            try:
                battery = psutil.sensors_battery()
            except (OSError, PermissionError):
                battery = None
            try:
                uptime = max(0.0, time.time() - psutil.boot_time())
            except (OSError, PermissionError, RuntimeError):
                # Some managed macOS environments deny sysctl access to the
                # boot timestamp. Telemetry must never prevent the UI starting.
                uptime = 0.0
            return SystemSnapshot(
                total_ram=int(memory.total),
                used_ram=int(memory.used),
                available_ram=int(memory.available),
                process_ram=int(process_ram),
                system_percent=float(memory.percent),
                process_percent=(process_ram / memory.total * 100.0) if memory.total else 0.0,
                cpu_percent=float(cpu),
                battery_percent=float(battery.percent) if battery else None,
                battery_plugged=bool(battery.power_plugged) if battery else None,
                uptime_seconds=uptime,
            )
        total = _fallback_total_ram()
        process_ram = _fallback_process_ram()
        return SystemSnapshot(
            total_ram=total,
            used_ram=0,
            available_ram=total,
            process_ram=process_ram,
            system_percent=0.0,
            process_percent=process_ram / total * 100.0 if total else 0.0,
            cpu_percent=0.0,
            battery_percent=None,
            battery_plugged=None,
            uptime_seconds=time.time(),
        )

    def report(self) -> str:
        snap = self.snapshot
        lines = [
            f"System RAM  {format_bytes(snap.used_ram)} / {format_bytes(snap.total_ram)}"
            f"  ·  {snap.system_percent:.0f}% used",
            f"Blaze RAM   {format_bytes(snap.process_ram)}"
            f"  ·  soft cap {self.limit_percent}%"
            f" ({format_bytes(self.soft_budget_bytes)})",
            f"Processor   {snap.cpu_percent:.1f}% core load",
            f"Uptime      {format_duration(snap.uptime_seconds)}",
        ]
        if snap.battery_percent is not None:
            state = "charging" if snap.battery_plugged else "on battery"
            lines.append(f"Battery     {snap.battery_percent:.0f}% · {state}")
        return "\n".join(lines)


class SystemController:
    """Native power and session controls with conservative argument handling."""

    def shutdown(self, *, delay_seconds: int = 3) -> None:
        if sys.platform == "darwin":
            self._run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to shut down',
                ]
            )
        elif os.name == "nt":
            self._run(["shutdown", "/s", "/t", str(max(1, delay_seconds))])
        else:
            if self._run(["systemctl", "poweroff"]) != 0:
                self._run(["shutdown", "-h", "now"])

    def restart(self, *, delay_seconds: int = 3) -> None:
        if sys.platform == "darwin":
            self._run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to restart',
                ]
            )
        elif os.name == "nt":
            self._run(["shutdown", "/r", "/t", str(max(1, delay_seconds))])
        else:
            if self._run(["systemctl", "reboot"]) != 0:
                self._run(["shutdown", "-r", "now"])

    def sleep(self) -> None:
        if sys.platform == "darwin":
            self._run(["pmset", "sleepnow"])
        elif os.name == "nt":
            self._run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        else:
            if self._run(["systemctl", "suspend"]) != 0:
                self._run(["pm-suspend"])

    def lock(self) -> None:
        if sys.platform == "darwin":
            self._run(
                [
                    "/System/Library/CoreServices/Menu Extras/User.menu/Contents/"
                    "Resources/CGSession",
                    "-suspend",
                ]
            )
        elif os.name == "nt":
            self._run(["rundll32.exe", "user32.dll,LockWorkStation"])
        else:
            commands = [
                ["loginctl", "lock-session"],
                ["gnome-screensaver-command", "-l"],
                ["xdg-screensaver", "lock"],
            ]
            for command in commands:
                if self._run(command) == 0:
                    break

    def _run(self, args: list[str]) -> int:
        log.info("System command: %s", " ".join(args))
        try:
            result = subprocess.run(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=15,
            )
            if result.returncode != 0:
                log.warning(
                    "System command failed (%s): %s",
                    result.returncode,
                    result.stderr.decode(errors="replace").strip(),
                )
            return result.returncode
        except (OSError, subprocess.SubprocessError):
            log.exception("System command failed: %s", args)
            return 1

    def media_play_pause(self) -> bool:
        if sys.platform == "darwin":
            script = (
                'tell application "Music"\n'
                '    if it is running then\n'
                '        playpause\n'
                '        return "ok"\n'
                '    end if\n'
                'end tell\n'
                'tell application "Spotify"\n'
                '    if it is running then\n'
                '        playpause\n'
                '        return "ok"\n'
                '    end if\n'
                'end tell\n'
                'tell application "System Events" to key code 49'
            )
            return self._run(["osascript", "-e", script]) == 0
        if os.name == "nt":
            return _win_send_media_key(0xB3)
        if shutil.which("playerctl"):
            return self._run(["playerctl", "play-pause"]) == 0
        return False

    def media_next(self) -> bool:
        if sys.platform == "darwin":
            script = (
                'tell application "Music"\n'
                '    if it is running then\n'
                '        next track\n'
                '        return "ok"\n'
                '    end if\n'
                'end tell\n'
                'tell application "Spotify"\n'
                '    if it is running then\n'
                '        next track\n'
                '        return "ok"\n'
                '    end if\n'
                'end tell'
            )
            return self._run(["osascript", "-e", script]) == 0
        if os.name == "nt":
            return _win_send_media_key(0xB0)
        if shutil.which("playerctl"):
            return self._run(["playerctl", "next"]) == 0
        return False

    def media_prev(self) -> bool:
        if sys.platform == "darwin":
            script = (
                'tell application "Music"\n'
                '    if it is running then\n'
                '        previous track\n'
                '        return "ok"\n'
                '    end if\n'
                'end tell\n'
                'tell application "Spotify"\n'
                '    if it is running then\n'
                '        previous track\n'
                '        return "ok"\n'
                '    end if\n'
                'end tell'
            )
            return self._run(["osascript", "-e", script]) == 0
        if os.name == "nt":
            return _win_send_media_key(0xB1)
        if shutil.which("playerctl"):
            return self._run(["playerctl", "previous"]) == 0
        return False

    def media_stop(self) -> bool:
        if sys.platform == "darwin":
            script = (
                'tell application "Music"\n'
                '    if it is running then pause\n'
                'end tell\n'
                'tell application "Spotify"\n'
                '    if it is running then pause\n'
                'end tell'
            )
            return self._run(["osascript", "-e", script]) == 0
        if os.name == "nt":
            return _win_send_media_key(0xB2)
        if shutil.which("playerctl"):
            return self._run(["playerctl", "stop"]) == 0
        return False

    def volume_up(self, step: int = 6) -> bool:
        if sys.platform == "darwin":
            script = f'set volume output volume ((output volume of (get volume settings)) + {int(step)})'
            return self._run(["osascript", "-e", script]) == 0
        if os.name == "nt":
            for _ in range(max(1, step // 2)):
                _win_send_media_key(0xAF)
            return True
        if shutil.which("amixer"):
            return self._run(["amixer", "-D", "pulse", "sset", "Master", f"{step}%+"]) == 0
        return False

    def volume_down(self, step: int = 6) -> bool:
        if sys.platform == "darwin":
            script = f'set volume output volume ((output volume of (get volume settings)) - {int(step)})'
            return self._run(["osascript", "-e", script]) == 0
        if os.name == "nt":
            for _ in range(max(1, step // 2)):
                _win_send_media_key(0xAE)
            return True
        if shutil.which("amixer"):
            return self._run(["amixer", "-D", "pulse", "sset", "Master", f"{step}%-"]) == 0
        return False

    def volume_mute(self) -> bool:
        if sys.platform == "darwin":
            script = 'set volume output muted (not (output muted of (get volume settings)))'
            return self._run(["osascript", "-e", script]) == 0
        if os.name == "nt":
            return _win_send_media_key(0xAD)
        if shutil.which("amixer"):
            return self._run(["amixer", "-D", "pulse", "sset", "Master", "toggle"]) == 0
        return False

    def volume_set(self, percent: int) -> bool:
        val = max(0, min(100, int(percent)))
        if sys.platform == "darwin":
            script = f'set volume output volume {val}'
            return self._run(["osascript", "-e", script]) == 0
        if shutil.which("amixer"):
            return self._run(["amixer", "-D", "pulse", "sset", "Master", f"{val}%"]) == 0
        return False

    def reveal_in_file_manager(self, path: str | Path) -> None:
        target = str(path)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", target])
            elif os.name == "nt":
                subprocess.Popen(["explorer", "/select,", target])
            else:
                parent = str(Path(target).parent)
                subprocess.Popen(["xdg-open", parent])
        except OSError:
            log.exception("Could not reveal path %s", path)


def terminate_self() -> None:
    """Quit through Qt first when possible; this is a last-resort helper."""
    try:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app:
            app.quit()
            return
    except Exception:
        pass
    os.kill(os.getpid(), signal.SIGTERM)


def _win_send_media_key(vk: int) -> bool:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, 2, 0)
        return True
    except Exception:
        return False
