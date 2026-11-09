"""Unit tests for the local STT transcript hygiene rules."""

from __future__ import annotations

import unittest

from blaze_ai.core.stt import (
    _clean_transcript,
    _logprob_confidence,
    voice_dependencies,
    voice_ready,
)


class TranscriptCleaningTests(unittest.TestCase):
    def test_normal_sentence_is_preserved(self) -> None:
        self.assertEqual(
            _clean_transcript("  Open Safari and type Hello World.  "),
            "Open Safari and type Hello World.",
        )

    def test_whisper_hallucinations_are_removed(self) -> None:
        for phrase in (
            "Thanks for watching!",
            "Please like and subscribe",
            "Subtitles by the Amara.org community",
            "transcribed by whisper",
        ):
            self.assertEqual(_clean_transcript(phrase), "", phrase)

    def test_silence_markers_and_punctuation_only_are_dropped(self) -> None:
        self.assertEqual(_clean_transcript("[music]"), "")
        self.assertEqual(_clean_transcript("(applause)"), "")
        self.assertEqual(_clean_transcript(" ... -- "), "")

    def test_repetitive_noise_is_rejected(self) -> None:
        self.assertEqual(_clean_transcript("aaaaaaaaaa"), "")
        self.assertEqual(_clean_transcript("you you you you you you you you"), "")

    def test_trailing_clause_after_hallucination_survives(self) -> None:
        self.assertEqual(
            _clean_transcript("Thanks for watching. Open Finder."),
            "Open Finder",
        )


class VoiceReadinessTests(unittest.TestCase):
    def test_dependency_probe_reports_every_extra(self) -> None:
        status = voice_dependencies()
        self.assertEqual(set(status), {"faster_whisper", "sounddevice", "numpy"})
        self.assertTrue(all(isinstance(value, bool) for value in status.values()))
        self.assertEqual(voice_ready(), all(status.values()))

    def test_confidence_is_bounded(self) -> None:
        for logprob, no_speech in ((0.0, 0.0), (-1.0, 0.5), (-9.0, 1.0)):
            value = _logprob_confidence(logprob, no_speech)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


if __name__ == "__main__":
    unittest.main()
