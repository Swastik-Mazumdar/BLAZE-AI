"""Tests for local model folder validation and RAM limits."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from blaze_ai.core.local_model import (
    GB,
    HuggingFaceModel,
    clean_model_reply,
    ensure_model_fits,
    inspect_model_folder,
    model_folder_ready,
)


def make_model(root: Path, weight_bytes: int = 4 * 1024 * 1024) -> Path:
    (root / "config.json").write_text("{}", encoding="utf-8")
    weights = root / "model.safetensors"
    weights.write_bytes(b"")
    os.truncate(weights, weight_bytes)
    return root


class ModelFolderTests(unittest.TestCase):
    def test_empty_folder_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(model_folder_ready(Path(folder)))

    def test_config_only_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}", encoding="utf-8")
            self.assertFalse(model_folder_ready(root))

    def test_safetensors_model_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "model.safetensors").write_bytes(b"0")
            self.assertTrue(model_folder_ready(root))

    def test_single_safetensors_file_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = make_model(Path(folder), 4 * 1024 * 1024)
            weights = root / "model.safetensors"
            self.assertTrue(model_folder_ready(weights))
            info = inspect_model_folder(weights)
            self.assertTrue(info.supported, info.reason)
            self.assertEqual(info.backend, "transformers")

    def test_single_safetensors_without_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            weights = Path(folder) / "model.safetensors"
            weights.write_bytes(b"0")
            self.assertFalse(model_folder_ready(weights))
            info = inspect_model_folder(weights)
            self.assertFalse(info.supported)
            self.assertIn("config.json", info.reason)

    def test_pytorch_bin_model_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "pytorch_model.bin").write_bytes(b"0")
            self.assertTrue(model_folder_ready(root))

    def test_partial_download_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "tokenizer.json").write_text("{}", encoding="utf-8")
            (root / "training_args.bin").write_bytes(b"0")
            self.assertFalse(model_folder_ready(root))


class ModelFitTests(unittest.TestCase):
    def test_small_model_fits_and_reports_params(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = make_model(Path(folder), 200 * 1024 * 1024)
            info = inspect_model_folder(root)
            self.assertTrue(info.supported, info.reason)
            self.assertGreater(info.required_bytes, info.weights_bytes)
            self.assertIn("PARAMS", info.param_label)

    def test_oversized_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = make_model(Path(folder), 64 * GB)
            info = inspect_model_folder(root)
            self.assertFalse(info.supported)
            self.assertIn("Too big", info.reason)
            with self.assertRaises(ValueError) as raised:
                ensure_model_fits(root)
            self.assertIn("Too big", str(raised.exception))

    def test_missing_config_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "model.safetensors").write_bytes(b"0" * 2048)
            info = inspect_model_folder(root)
            self.assertFalse(info.supported)
            self.assertIn("config.json", info.reason)

    def test_gguf_model_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "gemma-7b-Q4_K_M.gguf"
            model.write_bytes(b"")
            os.truncate(model, 4 * GB)
            info = inspect_model_folder(root)
            self.assertTrue(info.supported, info.reason)
            self.assertEqual(info.backend, "gguf")
            self.assertEqual(info.param_label, "≈7.0B PARAMS")
            self.assertIn("GGUF", info.reason)

    def test_gguf_model_round_trip_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "model.gguf").write_bytes(b"0")
            self.assertTrue(model_folder_ready(root))

    def test_single_gguf_file_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "gemma-7b-Q4_K_M.gguf"
            model.write_bytes(b"")
            os.truncate(model, 4 * GB)
            self.assertTrue(model_folder_ready(model))
            info = inspect_model_folder(model)
            self.assertTrue(info.supported, info.reason)
            self.assertEqual(info.backend, "gguf")
            self.assertEqual(info.param_label, "≈7.0B PARAMS")

    def test_non_gguf_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "model.bin"
            model.write_bytes(b"0")
            self.assertFalse(model_folder_ready(model))
            info = inspect_model_folder(model)
            self.assertFalse(info.supported)
            self.assertIn(".gguf", info.reason)

    def test_prism_ternary_gguf_is_detected(self) -> None:
        from blaze_ai.core.local_model import is_prism_ternary

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "Ternary-Bonsai-2-9B-PQ2_0.gguf"
            model.write_bytes(
                b"GGUF\x03\x00\x00\x00"
                + b"\x00" * 24
                + b"prism.hadamard.version"
                + b"\x00" * (2 * 1024 * 1024)
            )
            self.assertTrue(is_prism_ternary(model))
            info = inspect_model_folder(model)
            self.assertTrue(info.supported, info.reason)
            self.assertEqual(info.backend, "prism")
            self.assertIn("Prism", info.reason)

    def test_plain_gguf_is_not_prism(self) -> None:
        from blaze_ai.core.local_model import is_prism_ternary

        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "gemma-7b-Q4_K_M.gguf"
            model.write_bytes(b"GGUF" + b"\x00" * 4096)
            self.assertFalse(is_prism_ternary(model))

    def test_clean_model_reply_strips_intro_preamble(self) -> None:
        self.assertEqual(
            clean_model_reply("Hi, I'm Blaze AI. George Washington was the first president."),
            "George Washington was the first president.",
        )
        self.assertEqual(
            clean_model_reply("Hello there! I am Blaze AI\n\nParis is in France."),
            "Paris is in France.",
        )

    def test_clean_model_reply_keeps_identity_answer(self) -> None:
        self.assertEqual(clean_model_reply("I am Blaze AI."), "I am Blaze AI.")
        self.assertEqual(
            clean_model_reply("I am Blaze AI, your local desktop assistant."),
            "I am Blaze AI, your local desktop assistant.",
        )

    def test_gguf_four_bit_twelve_b_fits(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "gemma-12b-Q4_K_M.gguf"
            model.write_bytes(b"")
            os.truncate(model, int(7.6 * GB))
            info = inspect_model_folder(root)
            self.assertTrue(info.supported, info.reason)
            self.assertIn("12.0B", info.param_label)

    def test_oversized_gguf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "huge-7b-F16.gguf"
            model.write_bytes(b"")
            os.truncate(model, 30 * GB)
            info = inspect_model_folder(root)
            self.assertFalse(info.supported)
            self.assertIn("Too big", info.reason)

    def test_incomplete_weights_are_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "model.safetensors").write_bytes(b"0")
            info = inspect_model_folder(root)
            self.assertFalse(info.supported)
            self.assertIn("incomplete", info.reason)


class _FakeLlama:
    last_kwargs: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        self.__class__.last_kwargs = dict(kwargs)

    def create_chat_completion(self, **kwargs: object) -> dict[str, object]:
        return {"choices": [{"message": {"content": "  hello from gguf  "}}]}


class GGUFBackendTests(unittest.TestCase):
    def test_chat_response_uses_fast_bounded_generation(self) -> None:
        from blaze_ai.core.local_model import CHAT_MAX_NEW_TOKENS

        model = HuggingFaceModel()
        with patch.object(model, "_generate", return_value="A concise answer.") as generate:
            self.assertEqual(model.respond([], "Ada"), "A concise answer.")
        kwargs = generate.call_args.kwargs
        self.assertEqual(kwargs["max_new_tokens"], CHAT_MAX_NEW_TOKENS)
        self.assertFalse(kwargs["do_sample"])

    def test_stream_cancellation_closes_local_decoder(self) -> None:
        from blaze_ai.core.local_model import _collect_llama_stream

        state = {"closed": False, "checks": 0}

        def chunks():
            try:
                yield {"choices": [{"delta": {"content": "partial"}}]}
                yield {"choices": [{"delta": {"content": "unwanted"}}]}
            finally:
                state["closed"] = True

        def should_stop() -> bool:
            state["checks"] += 1
            return state["checks"] > 1

        reply = _collect_llama_stream(chunks(), chat=True, should_stop=should_stop)
        self.assertEqual(reply, "")
        self.assertTrue(state["closed"])

    def _make_gguf(self, root: Path, name: str = "tiny-1b-Q4_K_M.gguf") -> Path:
        model = root / name
        model.write_bytes(b"")
        os.truncate(model, 4 * 1024 * 1024)
        return model

    def test_load_gguf_uses_llama_and_generates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._make_gguf(root)
            fake = types.SimpleNamespace(Llama=_FakeLlama)
            with patch.dict(sys.modules, {"llama_cpp": fake}):
                model = HuggingFaceModel()
                mode = model.load_folder(str(root))
                self.assertEqual(mode, "chat")
                self.assertEqual(model.backend, "gguf")
                self.assertTrue(model.loaded)
                self.assertEqual(_FakeLlama.last_kwargs["n_ctx"], 4096)
                self.assertEqual(_FakeLlama.last_kwargs["n_gpu_layers"], -1)
                self.assertEqual(
                    model.respond_action(
                        [{"role": "user", "content": "say hello"}], "Ada"
                    ),
                    "hello from gguf",
                )
                model.unload()
                self.assertFalse(model.loaded)

    def test_load_gguf_file_directly(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model_file = self._make_gguf(root)
            fake = types.SimpleNamespace(Llama=_FakeLlama)
            with patch.dict(sys.modules, {"llama_cpp": fake}):
                model = HuggingFaceModel()
                mode = model.load_folder(str(model_file))
                self.assertEqual(mode, "chat")
                self.assertEqual(model.backend, "gguf")
                self.assertEqual(model.local_path, model_file)
                self.assertEqual(_FakeLlama.last_kwargs["model_path"], str(model_file))
                self.assertEqual(
                    model.respond_action(
                        [{"role": "user", "content": "say hello"}], "Ada"
                    ),
                    "hello from gguf",
                )
                model.unload()

    def test_split_gguf_shard_selection_from_file(self) -> None:
        from blaze_ai.core.local_model import _gguf_shard_set, _primary_gguf

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "model-00001-of-00002.gguf"
            second = root / "model-00002-of-00002.gguf"
            other = root / "unrelated.gguf"
            for path in (first, second, other):
                path.write_bytes(b"0")
            shards = _gguf_shard_set(second)
            self.assertEqual(shards, [first, second])
            self.assertEqual(_primary_gguf(shards), first)

    def test_gguf_without_runtime_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._make_gguf(root)
            with patch.dict(sys.modules, {"llama_cpp": None}):
                with self.assertRaises(RuntimeError) as raised:
                    HuggingFaceModel().load_folder(str(root))
            self.assertIn("llama-cpp-python", str(raised.exception))

    def test_gguf_cannot_be_used_for_speech(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._make_gguf(root)
            with self.assertRaises(ValueError) as raised:
                HuggingFaceModel().load_folder(
                    str(root), "automatic-speech-recognition"
                )
            self.assertIn("speech", str(raised.exception).lower())

    def test_split_gguf_picks_first_shard(self) -> None:
        from blaze_ai.core.local_model import _primary_gguf

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "model-00001-of-00002.gguf"
            second = root / "model-00002-of-00002.gguf"
            first.write_bytes(b"0")
            second.write_bytes(b"0")
            self.assertEqual(_primary_gguf([second, first]), first)

    def test_prism_gguf_uses_server_runtime(self) -> None:
        class _FakeServerRuntime:
            def __init__(self, binary, model_path, *, context=4096) -> None:
                self.binary = binary
                self.model_path = model_path
                self.context = context
                self.started = False

            @property
            def running(self) -> bool:
                return self.started

            def start(self) -> None:
                self.started = True

            def complete(self, messages, *, max_tokens, temperature, should_stop=None) -> str:
                self.messages = messages
                self.max_tokens = max_tokens
                return "  hello from bonsai  "

            def close(self) -> None:
                self.started = False

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model_file = root / "Ternary-Bonsai-2-9B-PQ2_0.gguf"
            model_file.write_bytes(
                b"GGUF\x03\x00\x00\x00"
                + b"\x00" * 24
                + b"prism.hadamard.version"
                + b"\x00" * (2 * 1024 * 1024)
            )
            with (
                patch("blaze_ai.core.local_model.prism_server_binary", return_value=Path("/x/llama-server")),
                patch("blaze_ai.core.local_model.LlamaServerRuntime", _FakeServerRuntime),
            ):
                model = HuggingFaceModel()
                mode = model.load_folder(str(model_file))
                self.assertEqual(mode, "chat")
                self.assertEqual(model.backend, "prism")
                self.assertTrue(model.loaded)
                self.assertIsInstance(model.llama_server, _FakeServerRuntime)
                self.assertEqual(
                    model.respond_action(
                        [{"role": "user", "content": "say hello"}], "Ada"
                    ),
                    "hello from bonsai",
                )
                model.unload()
                self.assertFalse(model.loaded)

    def test_prism_gguf_without_runtime_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model_file = root / "Ternary-Bonsai-2-9B-PQ2_0.gguf"
            model_file.write_bytes(
                b"GGUF\x03\x00\x00\x00"
                + b"\x00" * 24
                + b"prism.hadamard.version"
                + b"\x00" * (2 * 1024 * 1024)
            )
            with patch(
                "blaze_ai.core.local_model.prism_server_binary", return_value=None
            ):
                with self.assertRaises(RuntimeError) as raised:
                    HuggingFaceModel().load_folder(str(model_file))
            self.assertIn("Prism runtime", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
