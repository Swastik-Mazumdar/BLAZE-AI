"""Unit tests for the strict local command grammar."""

from __future__ import annotations

import unittest

from blaze_ai.core.command_filter import Command, is_command, match_command


def command(text: str, *, voice: bool = False) -> Command | None:
    return match_command(text, voice=voice)


class CommandMatchingTests(unittest.TestCase):
    def assert_kind(self, text: str, kind: str, *, voice: bool = False) -> Command:
        result = command(text, voice=voice)
        self.assertIsNotNone(result, f"{text!r} should be a command")
        assert result is not None
        self.assertEqual(result.kind, kind, text)
        return result

    def test_open_application(self) -> None:
        result = self.assert_kind("open VS Code", "open_app")
        self.assertEqual(result.argument, "VS Code")

    def test_oversized_command_is_rejected_before_pattern_matching(self) -> None:
        from blaze_ai.core.command_filter import MAX_COMMAND_CHARS

        self.assertIsNone(command("open " + "A" * MAX_COMMAND_CHARS))

    def test_open_application_with_wake_and_polite_prefix(self) -> None:
        result = self.assert_kind("hey Blaze, please open Safari", "open_app")
        self.assertEqual(result.argument, "Safari")

    def test_launch_application(self) -> None:
        self.assert_kind("launch the calculator app", "open_app")

    def test_close_application(self) -> None:
        result = self.assert_kind("close WhatsApp", "close_app")
        self.assertEqual(result.argument, "WhatsApp")

    def test_open_settings_ui(self) -> None:
        self.assert_kind("open settings", "ui_settings")

    def test_screen_recording(self) -> None:
        self.assert_kind("start recording", "record_start")
        self.assert_kind("record the screen", "record_start")
        self.assert_kind("stop recording", "record_stop")
        self.assert_kind("save the screen recording", "record_stop")

    def test_screenshot(self) -> None:
        self.assert_kind("take a screenshot", "screenshot")
        self.assert_kind("screenshot", "screenshot")

    def test_type_text(self) -> None:
        result = self.assert_kind("type hello from Blaze", "type_text")
        self.assertEqual(result.argument, "hello from Blaze")
        quoted = self.assert_kind('type "quoted text"', "type_text")
        self.assertEqual(quoted.argument, "quoted text")

    def test_system_and_clock(self) -> None:
        self.assert_kind("system status", "system_info")
        self.assert_kind("battery level", "system_info")
        self.assert_kind("what time is it", "time")
        self.assert_kind("what's the date", "date")

    def test_help(self) -> None:
        self.assert_kind("what can you do", "help")
        self.assert_kind("help", "help")

    def test_power_commands(self) -> None:
        self.assert_kind("shut down", "shutdown")
        self.assert_kind("restart computer", "restart")
        self.assert_kind("sleep", "sleep")
        self.assert_kind("lock screen", "lock")

    def test_confirmation_and_cancel(self) -> None:
        result = self.assert_kind("confirm restart", "confirm")
        self.assertEqual(result.argument, "restart")
        message = self.assert_kind("confirm send message", "confirm")
        self.assertEqual(message.argument, "send_message")
        self.assert_kind("yes", "confirm")
        self.assert_kind("cancel", "cancel")

    def test_quit(self) -> None:
        self.assert_kind("quit blaze", "quit")
        self.assert_kind("close blaze", "quit")
        self.assert_kind("exit blaze", "quit")

    def test_natural_language_is_not_a_command(self) -> None:
        self.assertIsNone(command("explain quantum entanglement"))
        self.assertIsNone(command("what's the weather like today"))
        self.assertIsNone(command("write a poem about the sea"))
        self.assertTrue(is_command("open Spotify"))
        self.assertFalse(is_command("tell me a story"))

    def test_voice_rejects_wandering_sentences(self) -> None:
        # The same phrase would be a typed app launch, but voice is stricter.
        phrase = "open a conversation about the future of local artificial intelligence"
        self.assertIsNone(command(phrase, voice=True))
        self.assertIsNotNone(command(phrase))

    def test_unclear_app_targets_are_rejected(self) -> None:
        self.assertIsNone(command("open it"))
        self.assertIsNone(command("close something"))

    def test_wake_prefix_without_command_is_ignored(self) -> None:
        self.assertIsNone(command("hey blaze"))
        self.assertIsNone(command("blaze, what is the point of all this"))

    def test_play_media_platforms(self) -> None:
        result = self.assert_kind("play Seedhe Maut", "play_media")
        self.assertEqual(result.argument, "Seedhe Maut")
        self.assertEqual(result.target, "music")

        result = self.assert_kind("play Seedhe Maut on YouTube", "play_media")
        self.assertEqual(result.argument, "Seedhe Maut")
        self.assertEqual(result.target, "youtube")

        result = self.assert_kind("play Shape of You on YouTube Music", "play_media")
        self.assertEqual(result.argument, "Shape of You")
        self.assertEqual(result.target, "youtube_music")

        result = self.assert_kind("play Karan Aujla on Spotify", "play_media")
        self.assertEqual(result.argument, "Karan Aujla")
        self.assertEqual(result.target, "spotify")

        result = self.assert_kind("play Nattu Nattu in Apple Music", "play_media")
        self.assertEqual(result.argument, "Nattu Nattu")
        self.assertEqual(result.target, "apple_music")

        result = self.assert_kind("play lo-fi beats on SoundCloud", "play_media")
        self.assertEqual(result.argument, "lo-fi beats")
        self.assertEqual(result.target, "soundcloud")

        result = self.assert_kind("play any song name here on jiosaavn", "play_media")
        self.assertEqual(result.argument, "any song name here")
        self.assertEqual(result.target, "jiosaavn")

    def test_search_and_play_variants(self) -> None:
        result = self.assert_kind("search and play Seedhe Maut on YouTube", "play_media")
        self.assertEqual(result.argument, "Seedhe Maut")
        self.assertEqual(result.target, "youtube")

        result = self.assert_kind("search for Seedhe Maut on YouTube", "play_media")
        self.assertEqual(result.argument, "Seedhe Maut")
        self.assertEqual(result.target, "youtube")

        result = self.assert_kind("find and play Arijit Singh songs on Spotify", "play_media")
        self.assertEqual(result.argument, "Arijit Singh songs")
        self.assertEqual(result.target, "spotify")

        result = self.assert_kind("listen to lofi hip hop on YouTube Music", "play_media")
        self.assertEqual(result.argument, "lofi hip hop")
        self.assertEqual(result.target, "youtube_music")

    def test_open_app_and_play(self) -> None:
        result = self.assert_kind("open Music and play Seedhe Maut", "play_media")
        self.assertEqual(result.argument, "Seedhe Maut")
        self.assertEqual(result.target, "music")
        self.assertEqual(result.extra, "Music")

        result = self.assert_kind("open Brave and play Seedhe Maut on YouTube", "play_media")
        self.assertEqual(result.argument, "Seedhe Maut")
        self.assertEqual(result.target, "youtube")
        self.assertEqual(result.extra, "Brave")

        result = self.assert_kind("open Brave and play Seedhe Maut", "play_media")
        self.assertEqual(result.argument, "Seedhe Maut")
        self.assertEqual(result.target, "youtube")
        self.assertEqual(result.extra, "Brave")

        result = self.assert_kind("open Brave and search for Seedhe Maut on YouTube", "play_media")
        self.assertEqual(result.argument, "Seedhe Maut")
        self.assertEqual(result.target, "youtube")

    def test_open_browser_and_site_still_works(self) -> None:
        result = self.assert_kind("open Brave and open YouTube", "open_url")
        self.assertIn("youtube", result.argument.lower())
        self.assertEqual(result.target, "Brave")

        result = self.assert_kind("open Brave and open YouTube after that", "open_url")
        self.assertIn("youtube", result.argument.lower())

    def test_play_vague_pronouns_rejected(self) -> None:
        self.assertIsNone(command("play this"))
        self.assertIsNone(command("play something"))
        self.assertIsNone(command("play this on spotify"))


if __name__ == "__main__":
    unittest.main()
