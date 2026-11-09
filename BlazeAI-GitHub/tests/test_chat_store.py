"""Persistence tests for the SQLite chat store."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from blaze_ai.core.chat_store import MAX_MESSAGE_CHARS, ChatStore


class ChatStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ChatStore(Path(self.temp.name) / "chats.sqlite3")

    def test_create_and_list(self) -> None:
        chat_id = self.store.create_chat()
        chats = self.store.list_chats()
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0].id, chat_id)
        self.assertEqual(chats[0].title, "New transmission")

    def test_messages_preview_and_ordering(self) -> None:
        chat_id = self.store.create_chat()
        self.store.add_message(chat_id, "user", "  hello there  ")
        self.store.add_message(chat_id, "assistant", "Hi.\nSecond line.")
        messages = self.store.get_messages(chat_id)
        self.assertEqual([m.role for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0].content, "hello there")
        summary = self.store.list_chats()[0]
        self.assertEqual(summary.preview, "Hi. Second line.")
        self.assertEqual(summary.message_count, 2)

    def test_auto_title_only_for_fresh_thread(self) -> None:
        chat_id = self.store.create_chat()
        self.store.auto_title(chat_id, "  Build me a local assistant with a very long name  ")
        title = self.store.get_chat(chat_id).title
        self.assertTrue(title.startswith("Build me"))
        self.assertLessEqual(len(title), 46)

        self.store.add_message(chat_id, "user", "second prompt")
        self.store.auto_title(chat_id, "this should not replace the title")
        self.assertEqual(self.store.get_chat(chat_id).title, title)

    def test_rename_and_prevent_empty_titles(self) -> None:
        chat_id = self.store.create_chat()
        self.store.rename_chat(chat_id, "  Signals  ")
        self.assertEqual(self.store.get_chat(chat_id).title, "Signals")
        self.store.rename_chat(chat_id, "   ")
        self.assertEqual(self.store.get_chat(chat_id).title, "Untitled transmission")

    def test_recent_context_limit(self) -> None:
        chat_id = self.store.create_chat()
        for index in range(30):
            self.store.add_message(chat_id, "user", f"message {index}")
        recent = self.store.recent_context(chat_id, limit=5)
        self.assertEqual([m.content for m in recent], [f"message {i}" for i in range(25, 30)])

    def test_delete_chat_cascades_messages(self) -> None:
        chat_id = self.store.create_chat()
        self.store.add_message(chat_id, "user", "hello")
        self.store.delete_chat(chat_id)
        self.assertIsNone(self.store.get_chat(chat_id))
        self.assertEqual(self.store.get_messages(chat_id), [])

    def test_clear_all_chats(self) -> None:
        first = self.store.create_chat()
        second = self.store.create_chat()
        self.store.add_message(first, "user", "a")
        self.store.add_message(second, "user", "b")
        self.store.clear_all_chats()
        self.assertEqual(self.store.list_chats(), [])

    def test_invalid_role_rejected(self) -> None:
        chat_id = self.store.create_chat()
        with self.assertRaises(ValueError):
            self.store.add_message(chat_id, "root", "nope")

    def test_sql_injection_payloads_are_stored_as_data(self) -> None:
        chat_id = self.store.create_chat()
        payload = "x'; DROP TABLE messages; --"
        self.store.rename_chat(chat_id, payload)
        self.store.add_message(chat_id, "user", payload)
        self.assertEqual(self.store.get_chat(chat_id).title, payload)
        self.assertEqual(self.store.get_messages(chat_id)[0].content, payload)
        self.assertIsNone(self.store.get_chat("' OR 1=1 --"))
        self.assertEqual(len(self.store.list_chats()), 1)

    def test_oversized_message_is_rejected_before_sqlite_write(self) -> None:
        chat_id = self.store.create_chat()
        with self.assertRaises(ValueError):
            self.store.add_message(chat_id, "user", "x" * (MAX_MESSAGE_CHARS + 1))
        self.assertEqual(self.store.get_messages(chat_id), [])

    def test_ordering_by_updated_at(self) -> None:
        first = self.store.create_chat()
        time.sleep(0.01)
        second = self.store.create_chat()
        time.sleep(0.01)
        self.store.add_message(first, "user", "bump")
        self.assertEqual(self.store.list_chats()[0].id, first)
        # Touch second so it moves to the top.
        time.sleep(0.01)
        self.store.add_message(second, "user", "bump")
        self.assertEqual(self.store.list_chats()[0].id, second)

    def test_per_chat_model_round_trip(self) -> None:
        chat_id = self.store.create_chat(model_path="/models/qwen")
        self.assertEqual(self.store.get_chat(chat_id).model_path, "/models/qwen")
        self.assertEqual(self.store.list_chats()[0].model_path, "/models/qwen")
        self.store.set_chat_model(chat_id, "/models/gemma-Q4.gguf")
        self.assertEqual(
            self.store.get_chat(chat_id).model_path, "/models/gemma-Q4.gguf"
        )

    def test_model_path_defaults_to_butler(self) -> None:
        chat_id = self.store.create_chat()
        self.assertEqual(self.store.get_chat(chat_id).model_path, "")

    def test_legacy_database_gets_model_path_column(self) -> None:
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        import sqlite3

        with sqlite3.connect(legacy) as connection:
            connection.execute(
                "CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO chats VALUES ('old', 'Old chat', 1, 1)"
            )
        store = ChatStore(legacy)
        self.assertEqual(store.get_chat("old").model_path, "")
        store.set_chat_model("old", "/models/new")
        self.assertEqual(store.get_chat("old").model_path, "/models/new")


if __name__ == "__main__":
    unittest.main()
