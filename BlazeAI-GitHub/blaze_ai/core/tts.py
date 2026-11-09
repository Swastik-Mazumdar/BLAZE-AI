"""Offline text-to-speech for spoken replies."""

from __future__ import annotations

import time


def speak_text(text: str, *, should_stop=None) -> None:
    """Speak with the system voice while polling for an interrupt request."""
    import pyttsx3

    engine = pyttsx3.init()
    engine.setProperty("rate", 185)
    engine.say((text or "").strip()[:5000])
    loop_started = False
    try:
        # Drive pyttsx3's event loop here so stop() runs on the same thread as
        # the speech driver. This makes barge-in reliable across macOS/Windows.
        engine.startLoop(False)
        loop_started = True
        engine.iterate()  # dispatch the queued say() before checking busy state
        while engine.isBusy():
            if should_stop and should_stop():
                engine.stop()
                break
            engine.iterate()
            time.sleep(0.01)
    finally:
        if loop_started:
            try:
                engine.endLoop()
            except RuntimeError:
                pass
        engine.stop()
