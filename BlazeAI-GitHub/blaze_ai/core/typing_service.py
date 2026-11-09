"""Native text-injection service used by voice commands such as “type hello”."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time

log = logging.getLogger("blaze.typing")


class TypingError(RuntimeError):
    pass


class TypingService:
    """Pastes text into whichever app has focus after Blaze steps aside."""

    def type_text(self, text: str, *, press_enter: bool = False) -> None:
        value = text.rstrip("\n")
        if not value:
            raise TypingError("There is no text to type.")
        if sys.platform == "darwin":
            self._type_macos(value, press_enter=press_enter)
        elif os.name == "nt":
            self._type_windows(value, press_enter=press_enter)
        else:
            self._type_linux(value, press_enter=press_enter)

    def _type_macos(self, text: str, *, press_enter: bool) -> None:
        script = (
            "on run argv\n"
            "set the clipboard to item 1 of argv\n"
            "delay 0.18\n"
            'tell application "System Events" to keystroke "v" using command down\n'
            + ('delay 0.1\ntell application "System Events" to key code 36\n' if press_enter else "")
            + "end run"
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script, text],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TypingError(f"macOS text injection failed: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or "Accessibility permission may be required."
            raise TypingError(f"macOS text injection failed: {detail}")

    def _type_windows(self, text: str, *, press_enter: bool) -> None:
        try:
            _win_set_clipboard(text)
            time.sleep(0.12)
            _win_key_combo(0x11, 0x56)  # Ctrl+V
            if press_enter:
                time.sleep(0.08)
                _win_key(0x0D)
        except Exception as exc:
            raise TypingError(f"Windows text injection failed: {exc}") from exc

    def _type_linux(self, text: str, *, press_enter: bool) -> None:
        if shutil.which("xdotool"):
            try:
                subprocess.run(
                    ["xdotool", "type", "--clearmodifiers", "--delay", "6", text],
                    check=True,
                    timeout=30,
                    capture_output=True,
                )
                if press_enter:
                    subprocess.run(
                        ["xdotool", "key", "--clearmodifiers", "Return"],
                        check=True,
                        timeout=5,
                        capture_output=True,
                    )
                return
            except (OSError, subprocess.SubprocessError) as exc:
                raise TypingError(f"xdotool text injection failed: {exc}") from exc
        if shutil.which("wtype"):
            try:
                subprocess.run(["wtype", text], check=True, timeout=30)
                if press_enter:
                    subprocess.run(["wtype", "-k", "Return"], check=True, timeout=5)
                return
            except (OSError, subprocess.SubprocessError) as exc:
                raise TypingError(f"wtype text injection failed: {exc}") from exc
        raise TypingError(
            "Install xdotool (X11) or wtype (Wayland) to enable text injection."
        )


def _win_set_clipboard(text: str) -> None:
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL

    payload = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        raise OSError("GlobalAlloc failed")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        raise OSError("GlobalLock failed")
    try:
        ctypes.memmove(pointer, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(handle)
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("SetClipboardData failed")
        handle = None  # ownership transferred to Windows
    finally:
        user32.CloseClipboard()


def _win_key(vk: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


def _win_key_combo(modifier: int, key: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(modifier, 0, 0, 0)
    user32.keybd_event(key, 0, 0, 0)
    user32.keybd_event(key, 0, 2, 0)
    user32.keybd_event(modifier, 0, 2, 0)
