"""Unit tests for fuzzy installed-application matching."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from blaze_ai.core.app_finder import AppEntry, AppFinder, app_match_score


def entry(name: str, target: str, *aliases: str) -> AppEntry:
    return AppEntry(name=name, target=target, aliases=tuple(aliases))


class AppMatchScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.code = entry(
            "Visual Studio Code",
            "/Applications/Visual Studio Code.app",
            "vscode",
            "code",
        )
        self.hp = entry("HP", "/Applications/HP.app")

    def test_exact_name(self) -> None:
        self.assertGreater(app_match_score("Visual Studio Code", self.code), 0.99)

    def test_alias(self) -> None:
        self.assertGreater(app_match_score("vs code", self.code), 0.9)
        self.assertGreater(app_match_score("vscode", self.code), 0.9)

    def test_alias_does_not_poison_unrelated_apps(self) -> None:
        # Regression: alias expansion used to score every app at ~1.0.
        self.assertGreater(app_match_score("vs code", self.code), 0.9)
        self.assertLess(app_match_score("vs code", self.hp), 0.6)

    def test_partial_and_compact_queries(self) -> None:
        chrome = entry("Google Chrome", "/Applications/Google Chrome.app")
        self.assertGreater(app_match_score("chrome", chrome), 0.8)
        whatsapp = entry("WhatsApp", "/Applications/WhatsApp.app")
        self.assertGreater(app_match_score("whats app", whatsapp), 0.8)

    def test_nonsense_scores_low(self) -> None:
        self.assertLess(app_match_score("quantum blender", self.code), 0.5)


class AppFinderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.apps = [
            entry("Visual Studio Code", "/Applications/Code.app", "vscode", "code"),
            entry("Safari", "/Applications/Safari.app"),
            entry("Terminal", "/System/Applications/Utilities/Terminal.app"),
            entry("Google Chrome", "/Applications/Chrome.app", "chrome"),
            entry("HP", "/Applications/HP.app"),
        ]
        self.finder = AppFinder(
            cache_path=Path(self.temp.name) / "apps.json",
            apps_override=self.apps,
        )

    def test_find_best(self) -> None:
        app, score = self.finder.find_best("vs code")
        assert app is not None
        self.assertEqual(app.name, "Visual Studio Code")
        self.assertGreater(score, 0.9)

    def test_unrelated_fuzzy_query_does_not_win(self) -> None:
        app, _score = self.finder.find_best("vs code")
        self.assertNotEqual(app.name, "HP")

    def test_no_match(self) -> None:
        app, score = self.finder.find_best("zzzzzz nonexistent")
        self.assertIsNone(app)
        self.assertEqual(score, 0.0)

    def test_search_limit_and_order(self) -> None:
        results = self.finder.search("a", limit=3)
        self.assertLessEqual(len(results), 3)
        self.assertGreaterEqual(results[0][0], results[-1][0])

    def test_unknown_query_returns_suggestions(self) -> None:
        results = self.finder.search("visual studio", limit=4)
        self.assertTrue(results)
        self.assertTrue(any(app.name == "Visual Studio Code" for _score, app in results))


if __name__ == "__main__":
    unittest.main()
