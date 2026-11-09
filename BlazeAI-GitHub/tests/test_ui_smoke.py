"""Headless UI smoke tests.

These run with Qt's offscreen platform so they are safe in CI and on build
machines. They verify controller wiring rather than pixel appearance.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from blaze_ai.config import Settings  # noqa: E402
from blaze_ai.core.app_finder import AppFinder  # noqa: E402
from blaze_ai.theme import theme_manager  # noqa: E402
from blaze_ai.ui.main_window import MainWindow  # noqa: E402
from blaze_ai.ui.onboarding import OnboardingDialog  # noqa: E402

_app: QApplication | None = None


def app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([sys.argv[0]])
        _app.setStyle("Fusion")
    return _app


class WindowSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        app()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        os.environ["BLAZE_AI_DATA_DIR"] = self.temp.name
        self.settings = Settings(Path(self.temp.name) / "settings.json")
        self.settings.update(
            {
                "name": "Ada",
                "onboarding_complete": True,
                "theme": "onyx",
            }
        )
        theme_manager.set_theme("onyx")
        with patch.object(AppFinder, "start_warmup", lambda self: None):
            self.window = MainWindow(self.settings)
        self.window._bootstrap_chats()
        self.window.show()
        # Flush deferred timers (bootstrap/refresh) while the temp data dir is
        # still alive; a late timer firing after cleanup would abort the run.
        time.sleep(0.15)
        app().processEvents()
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.window.close()
        app().processEvents()
        theme_manager.set_theme("onyx")

    def _fake_loaded_model(self) -> None:
        """Mark the current chat's model as loaded without real weights."""
        chat_id = self.window._current_chat_id
        path = "/models/test-chat-model"
        self.window.store.set_chat_model(chat_id, path)
        model = self.window.local_model
        model.model = object()
        model.tokenizer = object()
        model.local_path = Path(path)

        def cleanup() -> None:
            model.model = None
            model.tokenizer = None
            model.local_path = None

        self.addCleanup(cleanup)
        self.addCleanup(self.window.store.set_chat_model, chat_id, "")

    def test_offline_conversation_is_persisted(self) -> None:
        self.window._send_message("hello")
        app().processEvents()
        chat_id = self.window._current_chat_id
        self.assertTrue(chat_id)
        messages = self.window.store.get_messages(chat_id)
        self.assertIn("hello", [message.content for message in messages])
        self.assertEqual(messages[-1].role, "assistant")
        self.assertIn("Ada", messages[-1].content)

    def test_offline_butler_greets_on_short_greetings(self) -> None:
        for text in ("hi", "hi there", "hello", "hey", "hi blaze"):
            reply = self.window.butler.fallback(text)
            self.assertIn(
                "Local butler core is ready",
                reply.text,
                msg=f"expected a greeting for {text!r}",
            )

    def test_offline_butler_does_not_greet_on_similar_words(self) -> None:
        for text in ("this is a test", "which app is open", "history"):
            reply = self.window.butler.fallback(text)
            self.assertNotIn("Local butler core is ready", reply.text)

    def test_local_command_runs_without_provider(self) -> None:
        self.window._send_message("system status")
        app().processEvents()
        messages = self.window.store.get_messages(self.window._current_chat_id)
        self.assertIn("system status", [message.content for message in messages])
        self.assertIn("System RAM", messages[-1].content)

    def test_new_thread_and_switch(self) -> None:
        self.window._send_message("hello")
        first = self.window._current_chat_id
        self.window._new_chat()
        app().processEvents()
        second = self.window._current_chat_id
        self.assertNotEqual(first, second)
        self.window._select_chat(first)
        app().processEvents()
        self.assertEqual(self.window._current_chat_id, first)
        self.assertGreaterEqual(len(self.window.store.list_chats()), 2)

    def test_theme_switch_updates_settings(self) -> None:
        self.window._set_theme("bone")
        app().processEvents()
        self.assertEqual(self.settings.get("theme"), "bone")
        self.assertEqual(theme_manager.theme.key, "bone")
        self.window._set_theme("onyx")

    def test_local_model_setting_defaults_to_offline_butler(self) -> None:
        self.assertEqual(self.settings.get("hf_model"), "")
        self.assertFalse(self.window.local_model.loaded)

    def test_voice_without_dependencies_is_graceful(self) -> None:
        before = len(self.window.chat._messages)
        with patch("blaze_ai.ui.main_window.voice_ready", return_value=False):
            self.window._start_voice()
        app().processEvents()
        self.assertEqual(len(self.window.chat._messages), before + 1)
        self.assertIn("STT", self.window.store.get_messages(self.window._current_chat_id)[-1].content)

    def test_insert_text_reaches_composer(self) -> None:
        self.window.chat.insert_text("transcribed words")
        self.assertEqual(self.window.chat.composer.toPlainText(), "transcribed words")
        self.window.chat.composer.clear()

    def test_settings_drawer_opens_and_closes(self) -> None:
        self.window.open_settings()
        app().processEvents()
        self.assertTrue(self.window.settings_panel.isVisible())
        self.window.close_settings()
        app().processEvents()
        self.assertFalse(self.window._settings_open)

    def test_model_picker_emits_selected_folder_and_task(self) -> None:
        from blaze_ai.ui.settings_panel import SettingsPanel

        panel = SettingsPanel(self.settings)
        payloads: list[tuple[str, str]] = []
        panel.model_load_requested.connect(
            lambda folder, task: payloads.append((folder, task))
        )
        with tempfile.TemporaryDirectory() as folder:
            with patch(
                "blaze_ai.ui.settings_panel.QFileDialog.getExistingDirectory",
                return_value=folder,
            ):
                panel._manual_task.setCurrentIndex(0)
                panel._pick_model_folder()
        app().processEvents()
        self.assertEqual(payloads, [(folder, "text-generation")])
        self.assertIn("CHECKING", panel._key_status.text())

    def test_model_picker_supports_voice_task(self) -> None:
        from blaze_ai.ui.settings_panel import SettingsPanel

        panel = SettingsPanel(self.settings)
        payloads: list[tuple[str, str]] = []
        panel.model_load_requested.connect(
            lambda folder, task: payloads.append((folder, task))
        )
        with tempfile.TemporaryDirectory() as folder:
            with patch(
                "blaze_ai.ui.settings_panel.QFileDialog.getExistingDirectory",
                return_value=folder,
            ):
                panel._manual_task.setCurrentIndex(1)
                panel._pick_model_folder()
        app().processEvents()
        self.assertEqual(payloads, [(folder, "automatic-speech-recognition")])

    def test_model_picker_cancel_emits_nothing(self) -> None:
        from blaze_ai.ui.settings_panel import SettingsPanel

        panel = SettingsPanel(self.settings)
        payloads: list[tuple[str, str]] = []
        panel.model_load_requested.connect(
            lambda folder, task: payloads.append((folder, task))
        )
        with patch(
            "blaze_ai.ui.settings_panel.QFileDialog.getExistingDirectory",
            return_value="",
        ):
            panel._pick_model_folder()
        app().processEvents()
        self.assertEqual(payloads, [])

    def test_gguf_file_picker_emits_file_and_chat_task(self) -> None:
        from blaze_ai.ui.settings_panel import SettingsPanel

        panel = SettingsPanel(self.settings)
        payloads: list[tuple[str, str]] = []
        panel.model_load_requested.connect(
            lambda path, task: payloads.append((path, task))
        )
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "gemma-7b-Q4_K_M.gguf"
            model.write_bytes(b"0")
            with patch(
                "blaze_ai.ui.settings_panel.QFileDialog.getOpenFileName",
                return_value=(str(model), "GGUF models (*.gguf)"),
            ):
                panel._manual_task.setCurrentIndex(1)
                panel._pick_model_file()
        app().processEvents()
        self.assertEqual(payloads, [(str(model), "text-generation")])
        self.assertIn("CHECKING", panel._key_status.text())

    def test_gguf_file_picker_rejects_other_files(self) -> None:
        from blaze_ai.ui.settings_panel import SettingsPanel

        panel = SettingsPanel(self.settings)
        payloads: list[tuple[str, str]] = []
        panel.model_load_requested.connect(
            lambda path, task: payloads.append((path, task))
        )
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "model.bin"
            model.write_bytes(b"0")
            with patch(
                "blaze_ai.ui.settings_panel.QFileDialog.getOpenFileName",
                return_value=(str(model), ""),
            ):
                panel._pick_model_file()
        app().processEvents()
        self.assertEqual(payloads, [])
        self.assertIn(".gguf", panel._key_error.text())

    def test_gguf_notice_is_bold_and_underlined(self) -> None:
        from blaze_ai.ui.settings_panel import SettingsPanel

        panel = SettingsPanel(self.settings)
        text = panel._gguf_notice.text().lower()
        self.assertIn("only .gguf and .safetensors files", text)
        self.assertIn("<b>", text)
        self.assertIn("<u>", text)

    def test_chat_model_selector_lists_models_and_emits(self) -> None:
        self.window.settings.set(
            "model_registry",
            [
                {
                    "path": "/models/qwen",
                    "task": "text-generation",
                    "label": "qwen",
                    "params": "≈1.5B PARAMS",
                    "backend": "transformers",
                },
                {
                    "path": "/models/gemma-Q4.gguf",
                    "task": "text-generation",
                    "label": "gemma-Q4",
                    "params": "≈7.0B PARAMS",
                    "backend": "gguf",
                },
                {
                    "path": "/models/whisper",
                    "task": "automatic-speech-recognition",
                },
            ],
        )
        choices = self.window._chat_model_choices()
        paths = [path for path, _ in choices]
        self.assertEqual(paths[0], "")
        self.assertIn("/models/qwen", paths)
        self.assertIn("/models/gemma-Q4.gguf", paths)
        self.assertNotIn("/models/whisper", paths)

        self.window.chat.set_model_choices(choices, "/models/gemma-Q4.gguf")
        emitted: list[str] = []
        self.window.chat.model_selected.connect(emitted.append)
        index = self.window.chat._model_combo.findData("/models/qwen")
        self.assertGreaterEqual(index, 0)
        self.window.chat._model_combo.setCurrentIndex(index)
        app().processEvents()
        self.assertEqual(emitted, ["/models/qwen"])

    def test_chat_model_selection_is_saved(self) -> None:
        chat_id = self.window._current_chat_id
        self.assertTrue(chat_id)
        self.window.store.set_chat_model(chat_id, "/models/qwen")
        self.window._on_chat_model_selected("")
        self.assertEqual(self.window.store.get_chat(chat_id).model_path, "")

    def test_chat_model_selection_loads_for_that_chat(self) -> None:
        chat_id = self.window._current_chat_id
        self.window.store.set_chat_model(chat_id, "")
        with patch.object(self.window, "_load_local_model") as load:
            self.window._on_chat_model_selected("/models/qwen")
        self.assertEqual(self.window.store.get_chat(chat_id).model_path, "/models/qwen")
        load.assert_called_once_with(
            "/models/qwen", "text-generation", for_chat=True
        )

    def test_selecting_chat_restores_its_model(self) -> None:
        first = self.window._current_chat_id
        self.window._new_chat()
        second = self.window._current_chat_id
        self.assertNotEqual(first, second)
        self.window.store.set_chat_model(first, "/models/alpha")
        self.window.store.set_chat_model(second, "/models/beta")
        with patch.object(self.window, "_sync_chat_model") as sync:
            self.window._select_chat(first)
        self.assertEqual(self.window.chat._model_combo.currentData(), "/models/alpha")
        sync.assert_called_once_with(first)

    def test_registry_migrates_existing_models(self) -> None:
        self.window.settings.set("model_registry", [])
        self.window.settings.set(
            "hf_downloaded_models", ["/models/a", "/models/b"]
        )
        self.window.settings.set("hf_voice_model", "/models/b")
        entries = self.window._registry()
        self.assertEqual([entry["path"] for entry in entries], ["/models/a", "/models/b"])
        self.assertEqual(entries[1]["task"], "automatic-speech-recognition")

    def test_chat_models_are_migrated_once(self) -> None:
        self.window.settings.set("chat_models_initialized", False)
        self.window.settings.set("hf_model", "/models/default")
        chat_id = self.window._current_chat_id
        self.window.store.set_chat_model(chat_id, "")
        self.window._migrate_chat_models()
        self.assertEqual(
            self.window.store.get_chat(chat_id).model_path, "/models/default"
        )
        self.assertTrue(self.window.settings.get("chat_models_initialized"))

    def test_voice_orb_is_system_wide_always_on_top(self) -> None:
        from blaze_ai.ui.components import VoiceOrb

        orb = VoiceOrb()
        self.addCleanup(orb.deleteLater)
        self.assertIsNone(orb.parent())
        flags = orb.windowFlags()
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.WindowType.Tool)
        self.assertTrue(
            orb.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        )
        orb.show_state("listening")
        app().processEvents()
        self.assertTrue(orb.isVisible())
        orb.show_state("hidden")
        app().processEvents()
        self.assertFalse(orb.isVisible())

    def test_agent_mode_setting_round_trip(self) -> None:
        panel = self.window.settings_panel
        panel._agent_mode.setChecked(False)
        app().processEvents()
        self.assertFalse(self.settings.get("agent_mode"))
        panel._agent_mode.setChecked(True)
        app().processEvents()
        self.assertTrue(self.settings.get("agent_mode"))
        panel._agent_confirm.setChecked(False)
        app().processEvents()
        self.assertFalse(self.settings.get("agent_confirm"))
        panel._agent_confirm.setChecked(True)

    def test_loaded_model_dispatches_to_agent(self) -> None:
        self._fake_loaded_model()
        with patch.object(self.window, "_dispatch_agent") as agent_dispatch, patch.object(
            self.window, "_dispatch_llm"
        ) as chat_dispatch:
            self.window._send_message("edit this video and export a short clip")
        app().processEvents()
        agent_dispatch.assert_called_once()
        chat_dispatch.assert_not_called()

    def test_agent_off_dispatches_to_chat(self) -> None:
        self.settings.set("agent_mode", False)
        self._fake_loaded_model()
        with patch.object(self.window, "_dispatch_agent") as agent_dispatch, patch.object(
            self.window, "_dispatch_llm"
        ) as chat_dispatch:
            self.window._send_message("tell me a short story")
        app().processEvents()
        agent_dispatch.assert_not_called()
        chat_dispatch.assert_called_once()

    def test_knowledge_question_dispatches_to_chat_not_agent(self) -> None:
        self._fake_loaded_model()
        with patch.object(self.window, "_dispatch_agent") as agent_dispatch, patch.object(
            self.window, "_dispatch_llm"
        ) as chat_dispatch:
            self.window._send_message("who is george washington?")
        app().processEvents()
        agent_dispatch.assert_not_called()
        chat_dispatch.assert_called_once()

    def test_identity_question_dispatches_to_chat(self) -> None:
        self._fake_loaded_model()
        with patch.object(self.window, "_dispatch_agent") as agent_dispatch, patch.object(
            self.window, "_dispatch_llm"
        ) as chat_dispatch:
            self.window._send_message("what is your name")
        app().processEvents()
        agent_dispatch.assert_not_called()
        chat_dispatch.assert_called_once()

    def test_greeting_dispatches_to_chat_not_agent(self) -> None:
        self._fake_loaded_model()
        with patch.object(self.window, "_dispatch_agent") as agent_dispatch, patch.object(
            self.window, "_dispatch_llm"
        ) as chat_dispatch:
            self.window._send_message("hi there")
        app().processEvents()
        agent_dispatch.assert_not_called()
        chat_dispatch.assert_called_once()

    def test_web_search_command_runs_locally(self) -> None:
        self._fake_loaded_model()
        with patch.object(self.window, "_dispatch_agent") as agent_dispatch, patch.object(
            self.window, "_dispatch_llm"
        ) as chat_dispatch, patch.object(self.window, "_execute_command") as command_dispatch:
            self.window._send_message("search for who george washington was")
        app().processEvents()
        agent_dispatch.assert_not_called()
        chat_dispatch.assert_not_called()
        command_dispatch.assert_called_once()

    def test_agent_steps_are_not_rendered(self) -> None:
        from blaze_ai.core.agent import AgentAction, AgentStep

        before = len(self.window.chat._messages)
        step = AgentStep(
            action=AgentAction("run_shell", {"command": "ls"}),
            output="file.txt",
            ok=True,
        )
        self.window._on_agent_step(self.window._current_chat_id, step)
        app().processEvents()
        self.assertEqual(len(self.window.chat._messages), before)

    def test_complex_request_bypasses_simple_command(self) -> None:
        from blaze_ai.core.command_filter import Command

        self._fake_loaded_model()
        with patch(
            "blaze_ai.ui.main_window.match_command",
            return_value=Command("open_app", "Safari"),
        ), patch.object(self.window, "_execute_command") as execute, patch.object(
            self.window, "_dispatch_agent"
        ) as agent_dispatch:
            self.window._send_message("open Safari and draft an email to Ada")
        app().processEvents()
        agent_dispatch.assert_called_once()
        execute.assert_not_called()

    def test_simple_command_stays_deterministic(self) -> None:
        from blaze_ai.core.command_filter import Command

        self._fake_loaded_model()
        with patch(
            "blaze_ai.ui.main_window.match_command",
            return_value=Command("open_app", "Safari"),
        ), patch.object(self.window, "_execute_command") as execute, patch.object(
            self.window, "_dispatch_agent"
        ) as agent_dispatch:
            self.window._send_message("open Safari")
        app().processEvents()
        execute.assert_called_once()
        agent_dispatch.assert_not_called()

    def test_app_name_with_task_hint_word_stays_deterministic(self) -> None:
        from blaze_ai.core.command_filter import Command

        self._fake_loaded_model()
        with patch(
            "blaze_ai.ui.main_window.match_command",
            return_value=Command("open_app", "Messages"),
        ), patch.object(self.window, "_execute_command") as execute, patch.object(
            self.window, "_dispatch_agent"
        ) as agent_dispatch:
            self.window._send_message("open Messages")
        app().processEvents()
        execute.assert_called_once()
        agent_dispatch.assert_not_called()


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        app()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name) / "settings.json")

    def test_three_step_flow_emits_payload(self) -> None:
        dialog = OnboardingDialog(self.settings)
        payloads: list[dict] = []
        dialog.completed.connect(payloads.append)
        dialog._name_edit.setText("Grace")
        dialog._go_theme()
        self.assertEqual(dialog._stack.currentIndex(), 1)
        dialog._go_welcome()
        self.assertEqual(dialog._stack.currentIndex(), 2)
        self.assertIn("GRACE", dialog._welcome_title.text())
        dialog._finish()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["name"], "Grace")
        self.assertTrue(payloads[0]["onboarding_complete"])
        self.assertIn(payloads[0]["theme"], {"onyx", "bone", "graphite", "void"})
        theme_manager.set_theme("onyx")


if __name__ == "__main__":
    unittest.main()
