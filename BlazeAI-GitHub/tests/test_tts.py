"""Tests for cancellable system speech."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from blaze_ai.core.tts import speak_text


class FakeSpeechEngine:
    def __init__(self) -> None:
        self.busy = True
        self.stopped = False
        self.loop_started = False
        self.loop_ended = False
        self.spoken = ""
        self.iterations = 0

    def setProperty(self, _name, _value) -> None:
        pass

    def say(self, text) -> None:
        self.spoken = text

    def startLoop(self, _use_driver_loop) -> None:
        self.loop_started = True

    def iterate(self) -> None:
        self.iterations += 1
        if self.iterations >= 2:
            self.busy = False

    def isBusy(self) -> bool:
        return self.busy

    def stop(self) -> None:
        self.stopped = True
        self.busy = False

    def endLoop(self) -> None:
        self.loop_ended = True


class SpeakTextTests(unittest.TestCase):
    def _run(self, *, should_stop=None):
        engine = FakeSpeechEngine()
        fake = types.SimpleNamespace(init=lambda: engine)
        with patch.dict(sys.modules, {"pyttsx3": fake}), patch("blaze_ai.core.tts.time.sleep"):
            speak_text("  hello  ", should_stop=should_stop)
        return engine

    def test_speech_runs_and_cleans_up(self) -> None:
        engine = self._run()
        self.assertEqual(engine.spoken, "hello")
        self.assertTrue(engine.loop_started)
        self.assertTrue(engine.loop_ended)
        self.assertTrue(engine.stopped)

    def test_interrupt_stops_the_driver(self) -> None:
        engine = self._run(should_stop=lambda: True)
        self.assertTrue(engine.stopped)
        self.assertTrue(engine.loop_ended)


if __name__ == "__main__":
    unittest.main()
