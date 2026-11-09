"""Tests for TLS setup, device probing and Hub response filtering."""

from __future__ import annotations

import os
import ssl
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import blaze_ai.core.net as net
from blaze_ai.core import hub
from blaze_ai.core.device import DeviceProfile, device_profile

GB = 1024**3


class NetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (net._CONFIGURED, net._CERT_FILE)
        self._env = {
            key: os.environ[key]
            for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
            if key in os.environ
        }
        for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            os.environ.pop(key, None)
        net._CONFIGURED = False
        net._CERT_FILE = ""

    def tearDown(self) -> None:
        net._CONFIGURED, net._CERT_FILE = self._saved
        for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            os.environ.pop(key, None)
        os.environ.update(self._env)

    def test_ensure_ca_bundle_uses_certifi_and_exports_env(self) -> None:
        bundle = net.ensure_ca_bundle()
        self.assertTrue(bundle)
        self.assertTrue(Path(bundle).is_file())
        self.assertEqual(os.environ["SSL_CERT_FILE"], bundle)
        self.assertEqual(os.environ["REQUESTS_CA_BUNDLE"], bundle)
        self.assertEqual(os.environ["CURL_CA_BUNDLE"], bundle)

    def test_ensure_ca_bundle_honours_existing_override(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as handle:
            handle.write(b"test")
            custom = handle.name
        self.addCleanup(lambda: os.unlink(custom))
        os.environ["SSL_CERT_FILE"] = custom
        self.assertEqual(net.ensure_ca_bundle(), custom)

    def test_ssl_context_verifies_peer(self) -> None:
        context = net.ssl_context()
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_friendly_error_detects_certificate_failure(self) -> None:
        message = net.friendly_network_error(
            Exception("certificate verify failed: unable to get local issuer certificate")
        )
        self.assertIn("TLS certificate verification failed", message)
        self.assertIn("certifi", message)

    def test_friendly_error_detects_dns_and_gated_models(self) -> None:
        self.assertIn(
            "No internet connection",
            net.friendly_network_error(Exception("Name or service not known")),
        )
        self.assertIn(
            "gated",
            net.friendly_network_error(Exception("401 Client Error: Unauthorized")),
        )

    def test_redirect_handler_blocks_private_targets_and_tls_downgrades(self) -> None:
        handler = net._PublicOnlyRedirect()
        request = urllib.request.Request("https://example.com/file")
        headers = {}
        with patch.object(net, "is_safe_public_url", return_value=(False, "private address")):
            with self.assertRaises(urllib.error.HTTPError):
                handler.redirect_request(request, None, 302, "Found", headers, "http://127.0.0.1/x")
        with patch.object(net, "is_safe_public_url", return_value=(True, "")):
            with self.assertRaises(urllib.error.HTTPError):
                handler.redirect_request(request, None, 302, "Found", headers, "http://example.com/file")

    def test_download_target_is_contained_and_never_reuses_existing_name(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "report.txt").write_text("keep", encoding="utf-8")
            selected = net._public_download_target(root, "report.txt")
            self.assertEqual(selected.parent, root.resolve())
            self.assertEqual(selected.name, "report_1.txt")
            self.assertEqual((root / "report.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual(net.sanitize_download_filename("../../escape.txt"), "_._escape.txt")

    def test_download_rejects_private_connected_peer(self) -> None:
        class Socket:
            def getpeername(self):
                return ("127.0.0.1", 443)

        class Response:
            headers = {"Content-Length": "0"}
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=Socket()))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def open(self, *_args, **_kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as folder, patch.object(
            net, "is_safe_public_url", return_value=(True, "")
        ), patch.object(net, "ssl_context"), patch.object(
            net.urllib.request, "build_opener", return_value=Opener()
        ):
            ok, message, path, _count = net.download_file_safely(
                "https://public.example/file.txt", destination_dir=folder
            )
        self.assertFalse(ok)
        self.assertIsNone(path)
        self.assertIn("private", message.lower())

    def test_download_stops_a_slow_stream_at_its_absolute_deadline(self) -> None:
        class Socket:
            def getpeername(self):
                return ("93.184.216.34", 443)

        class Response:
            headers = {"Content-Length": "1"}
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=Socket()))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b"x"

        class Opener:
            def open(self, *_args, **_kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as folder, patch.object(
            net, "is_safe_public_url", return_value=(True, "")
        ), patch.object(net, "ssl_context"), patch.object(
            net.urllib.request, "build_opener", return_value=Opener()
        ), patch.object(net.time, "monotonic", side_effect=[10.0, 21.0]):
            ok, _message, path, _count = net.download_file_safely(
                "https://public.example/file.txt",
                destination_dir=folder,
                max_seconds=10,
            )
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertFalse(ok)
        self.assertIsNone(path)


def profile_with(ram_gb: float = 16.0) -> DeviceProfile:
    return DeviceProfile(
        system="Darwin",
        release="25.0.0",
        machine="arm64",
        chip="Apple M2",
        total_ram=int(ram_gb * GB),
        accelerator="Apple Silicon · Metal",
        max_params_b=max(0.5, round((ram_gb - 3.0) * 0.45, 1)),
    )


class DeviceTests(unittest.TestCase):
    def test_profile_computes_conservative_target(self) -> None:
        memory = type("Memory", (), {"total": 16 * GB})()
        with patch("blaze_ai.core.device.psutil.virtual_memory", return_value=memory), patch(
            "blaze_ai.core.device.platform.system", return_value="Darwin"
        ), patch(
            "blaze_ai.core.device.platform.machine", return_value="arm64"
        ), patch(
            "blaze_ai.core.device.platform.release", return_value="25.0.0"
        ), patch(
            "blaze_ai.core.device._chip", return_value="Apple M2"
        ), patch(
            "blaze_ai.core.device._accelerator", return_value="Apple Silicon · Metal"
        ):
            profile = device_profile()
        self.assertEqual(profile.total_ram, 16 * GB)
        self.assertAlmostEqual(profile.max_params_b, 5.9)
        self.assertIn("16 GB RAM", profile.summary)
        self.assertIn("≤ 6B", profile.target_label)

    def test_small_machine_has_floor_target(self) -> None:
        memory = type("Memory", (), {"total": 4 * GB})()
        with patch("blaze_ai.core.device.psutil.virtual_memory", return_value=memory), patch(
            "blaze_ai.core.device.platform.system", return_value="Windows"
        ), patch(
            "blaze_ai.core.device.platform.machine", return_value="AMD64"
        ), patch(
            "blaze_ai.core.device.platform.release", return_value="10"
        ), patch(
            "blaze_ai.core.device._chip", return_value="Intel"
        ), patch(
            "blaze_ai.core.device._accelerator", return_value="CPU"
        ):
            profile = device_profile()
        self.assertEqual(profile.max_params_b, 0.5)


class RepoNormalizationTests(unittest.TestCase):
    def test_accepts_ids_and_urls(self) -> None:
        cases = {
            "Qwen/Qwen2.5-1.5B-Instruct": "Qwen/Qwen2.5-1.5B-Instruct",
            "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct": (
                "Qwen/Qwen2.5-1.5B-Instruct"
            ),
            "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/": (
                "Qwen/Qwen2.5-1.5B-Instruct"
            ),
            "huggingface.co/models/Qwen/Qwen2.5-1.5B-Instruct?library=transformers": (
                "Qwen/Qwen2.5-1.5B-Instruct"
            ),
        }
        for raw, expected in cases.items():
            self.assertEqual(hub.normalize_repo_id(raw), expected)

    def test_repo_id_validation(self) -> None:
        self.assertTrue(hub.is_repo_id("org/model"))
        self.assertFalse(hub.is_repo_id("model-only"))
        self.assertFalse(hub.is_repo_id(""))

    def test_recommend_marker(self) -> None:
        self.assertTrue(hub.is_recommend_request(hub.recommend_marker()))
        self.assertFalse(hub.is_recommend_request("qwen"))


class HubResultTests(unittest.TestCase):
    def test_filters_gguf_unsupported_and_annotates_fit(self) -> None:
        payload = [
            {
                "id": "org/tiny-chat",
                "pipeline_tag": "text-generation",
                "downloads": 100,
                "likes": 4,
                "safetensors": {"total": 1_500_000_000},
            },
            {
                "id": "org/too-big",
                "pipeline_tag": "text-generation",
                "downloads": 999,
                "safetensors": {"total": 30_000_000_000},
            },
            {
                "id": "org/quantized-gguf",
                "pipeline_tag": "text-generation",
                "tags": ["gguf"],
                "downloads": 500,
            },
            {
                "id": "org/vision",
                "pipeline_tag": "image-classification",
                "downloads": 400,
            },
            {"id": "no-org", "pipeline_tag": "text-generation", "downloads": 300},
            "not-a-dict",
        ]
        results = hub.normalize_results(payload, profile_with(), task="text-generation")
        self.assertEqual([row["id"] for row in results], ["org/too-big", "org/tiny-chat"])
        tiny = results[1]
        self.assertTrue(tiny["fits"])
        self.assertIn("fits 16 GB RAM", tiny["fit_label"])
        self.assertEqual(tiny["task_label"], "chat")
        self.assertAlmostEqual(tiny["params_b"], 1.5)

    def test_only_fitting_and_limit(self) -> None:
        payload = [
            {
                "id": f"org/model-{index}",
                "pipeline_tag": "text-generation",
                "downloads": 100 - index,
                "safetensors": {"total": 2_000_000_000},
            }
            for index in range(5)
        ]
        results = hub.normalize_results(
            payload, profile_with(), task="text-generation", limit=2, only_fitting=True
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], "org/model-0")

    def test_task_mismatch_is_excluded(self) -> None:
        payload = [
            {
                "id": "openai/whisper-small",
                "pipeline_tag": "automatic-speech-recognition",
                "downloads": 10,
            }
        ]
        self.assertEqual(
            hub.normalize_results(payload, profile_with(), task="text-generation"), []
        )
        speech = hub.normalize_results(payload, profile_with())
        self.assertEqual(speech[0]["task_label"], "speech")

    def test_search_normalizes_query_and_delegates(self) -> None:
        with patch("blaze_ai.core.hub._request", return_value=[]) as request:
            hub.search_models(
                "https://huggingface.co/org/model/",
                profile_with(),
                task="text-generation",
                limit=5,
            )
        params = request.call_args.args[0]
        self.assertEqual(params["search"], "org/model")
        self.assertEqual(params["filter"], "text-generation")
        self.assertEqual(params["sort"], "downloads")
        self.assertEqual(params["full"], "true")


if __name__ == "__main__":
    unittest.main()
