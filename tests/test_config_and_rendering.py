"""Tests for settings persistence, markdown and formatting helpers."""

from __future__ import annotations

import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path

from blaze_ai.config import DEFAULT_SETTINGS, Settings
from blaze_ai.core.system import format_bytes, format_duration
from blaze_ai.theme import THEMES, build_stylesheet
from blaze_ai.ui.markdown import markdown_to_html

logging.getLogger("blaze").addHandler(logging.NullHandler())
logging.getLogger("blaze").propagate = False


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings.json"

    def test_defaults_and_round_trip(self) -> None:
        settings = Settings(self.path)
        self.assertEqual(settings.get("theme"), "onyx")
        settings.update({"name": "Ada", "theme": "bone", "hf_model": "Qwen/Qwen3-1.7B"})
        reloaded = Settings(self.path)
        self.assertEqual(reloaded.get("name"), "Ada")
        self.assertEqual(reloaded.get("theme"), "bone")
        self.assertEqual(reloaded.get("hf_model"), "Qwen/Qwen3-1.7B")

    def test_unknown_keys_are_ignored(self) -> None:
        settings = Settings(self.path)
        settings.update({"not_a_real_setting": 123})
        self.assertNotIn("not_a_real_setting", settings.as_dict())

    def test_file_is_user_only(self) -> None:
        settings = Settings(self.path)
        settings.set("name", "Ada")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_falls_back_to_defaults(self) -> None:
        self.path.write_text("{ this is not json", encoding="utf-8")
        settings = Settings(self.path)
        self.assertEqual(settings.get("theme"), DEFAULT_SETTINGS["theme"])

    def test_dictionary_sugar(self) -> None:
        settings = Settings(self.path)
        settings["theme"] = "void"
        self.assertEqual(settings["theme"], "void")


class FormattingTests(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1024), "1 KB")
        self.assertEqual(format_bytes(1024**3), "1.0 GB")

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(30), "0m")
        self.assertEqual(format_duration(90), "1m")
        self.assertEqual(format_duration(3700), "1h 1m")
        self.assertEqual(format_duration(90000), "1d 1h")


class MarkdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.theme = THEMES["onyx"]

    def test_bold_italic_and_inline_code(self) -> None:
        rendered = markdown_to_html("**bold** and *it* and `code`", self.theme)
        self.assertIn("<b>bold</b>", rendered)
        self.assertIn("<i>it</i>", rendered)
        self.assertIn("code", rendered)

    def test_link(self) -> None:
        rendered = markdown_to_html("[Blaze](https://example.com)", self.theme)
        self.assertIn('href="https://example.com"', rendered)
        self.assertIn(">Blaze</a>", rendered)

    def test_code_block_is_preserved(self) -> None:
        rendered = markdown_to_html("```python\nprint('hi')\n```", self.theme)
        self.assertIn("PYTHON", rendered)
        self.assertIn("print", rendered)

    def test_html_is_escaped(self) -> None:
        rendered = markdown_to_html("<script>alert(1)</script>", self.theme)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_self_xss_payloads_cannot_create_active_markup(self) -> None:
        payload = '<img src=x onerror="alert(1)"><svg onload="alert(2)"></svg>'
        rendered = markdown_to_html(payload, self.theme)
        self.assertNotIn("<img", rendered.lower())
        self.assertNotIn("<svg", rendered.lower())
        self.assertIn("&lt;img", rendered.lower())
        javascript_link = markdown_to_html(
            '[click](javascript:alert(1))\n[bad](https://example.com/" onmouseover="alert(2))',
            self.theme,
        )
        self.assertNotIn('href="javascript:', javascript_link.lower())
        self.assertNotIn('" onmouseover="', javascript_link.lower())

    def test_pathological_markdown_is_truncated_before_rendering(self) -> None:
        rendered = markdown_to_html("x" * 2_000_000, self.theme)
        self.assertLess(len(rendered), 130_000)

    def test_bullets_and_quote(self) -> None:
        rendered = markdown_to_html("- one\n- two\n> quoted", self.theme)
        self.assertIn("•", rendered)
        self.assertIn("quoted", rendered)


class ThemeTests(unittest.TestCase):
    def test_every_theme_builds_a_stylesheet(self) -> None:
        for key, theme in THEMES.items():
            with self.subTest(theme=key):
                stylesheet = build_stylesheet(theme)
                self.assertIn(theme.bg, stylesheet)
                self.assertIn(theme.accent, stylesheet)
                self.assertIn("#Root", stylesheet)


if __name__ == "__main__":
    unittest.main()
