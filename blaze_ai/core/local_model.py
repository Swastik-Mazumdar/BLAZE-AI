"""Local model-folder validation, RAM checks and local inference.

Blaze loads models that the user selects from disk. There is no in-app model
search or download: the picker opens Finder/Explorer, the folder is validated,
and oversized or unsupported folders are rejected before anything is loaded.

Three runtimes are supported:

* **Transformers** folders (``config.json`` + safetensors/pytorch_model.bin)
  loaded at fp16/bf16.
* **GGUF** files/folders loaded through ``llama.cpp`` (``llama-cpp-python``),
  which makes 4-bit 7B–12B models practical on 16 GB machines.
* **Prism ternary GGUF** files (Ternary-Bonsai-2) loaded through a bundled
  ``llama-server`` build, because stock llama.cpp cannot decode their
  ``PQ2_0``/``PTQ1_0`` tensor types.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..config import app_data_dir
from .hub import normalize_repo_id

GB = 1024**3
CHAT_MAX_NEW_TOKENS = 96
_SMALL_WEIGHT_BYTES = 1024 * 1024
_PARAM_IN_NAME = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")


def model_path(repo_id: str) -> Path:
    """Legacy location for models downloaded by earlier Blaze versions."""
    safe = normalize_repo_id(repo_id).replace("/", "--").replace("..", "_")
    return app_data_dir() / "hf-models" / safe


def prism_runtime_dir() -> Path:
    """Folder that holds the bundled Prism (Bonsai) llama-server runtime."""
    override = os.environ.get("BLAZE_PRISM_RUNTIME", "").strip()
    if override:
        return Path(override).expanduser()
    return app_data_dir() / "runtimes" / "llama-bonsai"


def prism_server_binary() -> Path | None:
    """Locate the optional llama-server build that understands Prism types."""
    name = "llama-server.exe" if sys.platform.startswith("win") else "llama-server"
    override = os.environ.get("BLAZE_PRISM_SERVER", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(prism_runtime_dir() / name)
    candidates.append(prism_runtime_dir() / "bin" / name)
    bundle_roots = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        bundle_roots.append(Path(meipass))
    executable_dir = Path(sys.executable).resolve().parent
    bundle_roots.extend(
        [
            executable_dir,
            executable_dir.parent / "Resources",
            executable_dir.parent / "Frameworks",
        ]
    )
    bundle_roots.append(Path(__file__).resolve().parents[2])
    for root in bundle_roots:
        candidates.append(root / "llama-bonsai" / name)
        candidates.append(root / "runtimes" / "llama-bonsai" / name)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def is_prism_ternary(path: Path) -> bool:
    """True when a GGUF file is a Prism ternary checkpoint (Bonsai 2).

    These files declare ``PQ2_0``/``PTQ1_0`` tensor types and carry
    ``prism.hadamard.*`` metadata. Mainline llama.cpp rejects them, so Blaze
    routes them to the bundled Prism runtime instead.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(2 * 1024 * 1024)
    except OSError:
        return False
    return b"prism.hadamard" in header or b"prism.hadamard.version" in header



def model_folder_ready(folder: Path) -> bool:
    """True when a path is a supported Transformers folder or model file."""
    folder = folder.expanduser()
    if folder.is_file():
        suffix = folder.suffix.lower()
        if suffix == ".gguf":
            return True
        # A single .safetensors file is loadable when it sits beside the
        # config/tokenizer files that Transformers needs.
        if suffix == ".safetensors":
            return (folder.parent / "config.json").is_file()
        return False
    if not folder.is_dir():
        return False
    if _quantized_files(folder):
        return True
    if not (folder / "config.json").is_file():
        return False
    if any(folder.glob("*.safetensors")):
        return True
    return any(folder.glob("pytorch_model*.bin"))


def _weight_files(folder: Path) -> list[Path]:
    safetensors = sorted(
        path for path in folder.glob("*.safetensors") if path.is_file()
    )
    if safetensors:
        # A checkpoint often ships both formats; count the preferred one only.
        return safetensors
    files: list[Path] = []
    for pattern in ("pytorch_model*.bin", "*.pt", "*.pth"):
        files.extend(path for path in folder.glob(pattern) if path.is_file())
    return sorted(set(files))


def _quantized_files(folder: Path) -> list[Path]:
    files: list[Path] = []
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".gguf":
            files.append(path)
    return sorted(set(files))


def _primary_gguf(files: list[Path]) -> Path:
    """Pick the entry file for a GGUF model, including split shards."""
    if not files:
        raise ValueError("No GGUF files found.")
    for path in files:
        if re.search(r"00001-of-\d+", path.name):
            return path
    return files[0]


def _collect_llama_stream(reply, *, chat: bool, should_stop=None) -> str:
    """Collect a llama.cpp completion and close the stream on cancellation."""
    if isinstance(reply, dict):
        try:
            item = reply["choices"][0]
            message = item.get("message", {})
            return str(
                message.get("content")
                or message.get("reasoning_content")
                or item.get("text")
                or ""
            ).strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""

    parts: list[str] = []
    try:
        for item in reply:
            if should_stop and should_stop():
                return ""
            try:
                choice = item["choices"][0]
                if chat:
                    delta = choice.get("delta", {})
                    value = delta.get("content") or delta.get("reasoning_content") or ""
                else:
                    value = choice.get("text") or ""
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
            if value:
                parts.append(str(value))
    finally:
        close = getattr(reply, "close", None)
        if close:
            close()
    return "".join(parts).strip()


_SHARD_NAME = re.compile(r"^(?P<prefix>.*?)-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)


def _gguf_shard_set(entry: Path) -> list[Path]:
    """Return the selected .gguf file plus any split siblings beside it."""
    entry = entry.expanduser()
    if not entry.is_file():
        return []
    match = _SHARD_NAME.match(entry.name)
    if not match:
        return [entry]
    prefix = match.group("prefix").lower()
    try:
        siblings = [
            path
            for path in entry.parent.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".gguf"
            and path.name.lower().startswith(prefix + "-")
        ]
    except OSError:
        return [entry]
    return sorted(siblings) or [entry]


def _gguf_params_b(files: list[Path]) -> float:
    """Estimate parameters from the file name or on-disk size."""
    for path in files:
        match = _PARAM_IN_NAME.search(path.name)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if 0.05 <= value <= 400:
                return value
    # Fall back to a 4.5 bits/weight estimate, typical for Q4 GGUF files.
    total = sum(path.stat().st_size for path in files)
    return (total * 8 / 4.5) / 1e9


def _config_params_b(folder: Path) -> float:
    """Estimate parameter count from a Transformers config when possible."""
    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    if not isinstance(config, dict):
        return 0.0
    for key in ("num_parameters", "n_params", "parameters"):
        value = config.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value) / 1e9
    hidden = config.get("hidden_size") or config.get("n_embd") or config.get("d_model")
    layers = config.get("num_hidden_layers") or config.get("n_layer")
    if not all(isinstance(value, (int, float)) and value > 0 for value in (hidden, layers)):
        return 0.0
    hidden = float(hidden)
    layers = float(layers)
    heads = config.get("num_attention_heads") or config.get("n_head") or 1
    head_dim = config.get("head_dim") or hidden / max(1.0, float(heads))
    kv_heads = config.get("num_key_value_heads") or heads
    kv_dim = float(head_dim) * float(kv_heads)
    intermediate = config.get("intermediate_size") or config.get("n_inner")
    if not isinstance(intermediate, (int, float)) or intermediate <= 0:
        intermediate = hidden * 4
    # Per layer: Q/O projections + K/V projections + gated MLP + 2 norms.
    per_layer = (
        2.0 * hidden * hidden
        + 2.0 * hidden * kv_dim
        + 3.0 * hidden * float(intermediate)
        + 2.0 * hidden
    )
    params = per_layer * layers
    vocab = config.get("vocab_size")
    if isinstance(vocab, (int, float)) and vocab > 0:
        tied = bool(config.get("tie_word_embeddings", False))
        params += float(vocab) * hidden * (1.0 if tied else 2.0)
    return params / 1e9


@dataclass(frozen=True)
class ModelFolderInfo:
    path: Path
    weights_bytes: int
    params_b: float
    required_bytes: int
    supported: bool
    reason: str
    backend: str = "transformers"

    @property
    def param_label(self) -> str:
        if self.params_b >= 0.05:
            return f"≈{self.params_b:.1f}B PARAMS"
        return "SIZE UNKNOWN"

    @property
    def backend_label(self) -> str:
        if self.backend == "gguf":
            return "GGUF"
        if self.backend == "prism":
            return "PRISM GGUF"
        return "TRANSFORMERS"


def inspect_model_folder(
    folder: Path, *, ram_limit_percent: int = 90
) -> ModelFolderInfo:
    """Validate a local model folder, .gguf file or .safetensors file."""
    folder = folder.expanduser()

    # A single model file can be added directly from the picker.
    if folder.is_file():
        suffix = folder.suffix.lower()
        if suffix == ".gguf":
            quantized = _gguf_shard_set(folder)
            return _inspect_gguf(folder, quantized, ram_limit_percent)
        if suffix == ".safetensors":
            sibling = folder.parent / "config.json"
            if not sibling.is_file():
                return ModelFolderInfo(
                    folder,
                    0,
                    0.0,
                    0,
                    False,
                    "Unsupported .safetensors file: config.json and tokenizer "
                    "files must be in the same folder. Choose the model folder "
                    "instead.",
                    "transformers",
                )
            return inspect_model_folder(
                folder.parent, ram_limit_percent=ram_limit_percent
            )
        return ModelFolderInfo(
            folder,
            0,
            0.0,
            0,
            False,
            "Unsupported model file: choose a .gguf file, a .safetensors file "
            "beside its config.json, or a Transformers model folder.",
            "gguf",
        )

    if not folder.is_dir():
        return ModelFolderInfo(
            folder, 0, 0.0, 0, False, "That path is not a folder or model file."
        )

    config_present = (folder / "config.json").is_file()
    weights = _weight_files(folder)
    quantized = _quantized_files(folder)

    # A folder with both formats (rare) prefers the Transformers checkpoint.
    if config_present and weights:
        weights_bytes = sum(path.stat().st_size for path in weights)
        if weights_bytes < _SMALL_WEIGHT_BYTES:
            return ModelFolderInfo(
                folder,
                weights_bytes,
                0.0,
                0,
                False,
                "Unsupported model folder: the weight files look incomplete or empty.",
                "transformers",
            )
        params_b = _config_params_b(folder)
        if params_b <= 0:
            # Most Transformers checkpoints are fp16/bf16: 2 bytes per parameter.
            params_b = (weights_bytes / 2) / 1e9
        # Weight files + tokenizer/activations/KV cache headroom.
        required_bytes = int(weights_bytes * 1.18 + 1.0 * GB)
        return _fit_or_reject(
            folder,
            weights_bytes,
            params_b,
            required_bytes,
            ram_limit_percent,
            "transformers",
        )

    # GGUF checkpoints: a single file or a split shard set.
    if quantized:
        return _inspect_gguf(folder, quantized, ram_limit_percent)

    if not config_present:
        return ModelFolderInfo(
            folder,
            0,
            0.0,
            0,
            False,
            "Unsupported model folder: config.json is missing and no .gguf "
            "model file was found.",
        )
    return ModelFolderInfo(
        folder,
        0,
        0.0,
        0,
        False,
        "Unsupported model folder: no .safetensors, pytorch_model.bin or "
        ".gguf weights were found.",
    )


def _inspect_gguf(
    path: Path, files: list[Path], ram_limit_percent: int
) -> ModelFolderInfo:
    weights_bytes = sum(item.stat().st_size for item in files)
    if weights_bytes < _SMALL_WEIGHT_BYTES:
        return ModelFolderInfo(
            path,
            weights_bytes,
            0.0,
            0,
            False,
            "Unsupported GGUF model: the file looks incomplete or empty.",
            "gguf",
        )
    params_b = _gguf_params_b(files)
    prism = is_prism_ternary(_primary_gguf(files))
    # llama.cpp keeps weights close to file size; the extra covers the KV
    # cache, compute buffers and tokenizer. The Prism runtime uses the same
    # layout, so the estimate is identical.
    required_bytes = int(weights_bytes * 1.20 + 1.0 * GB)
    return _fit_or_reject(
        path,
        weights_bytes,
        params_b,
        required_bytes,
        ram_limit_percent,
        "prism" if prism else "gguf",
    )


def _fit_or_reject(
    folder: Path,
    weights_bytes: int,
    params_b: float,
    required_bytes: int,
    ram_limit_percent: int,
    backend: str,
) -> ModelFolderInfo:
    try:
        import psutil

        total_ram = int(psutil.virtual_memory().total)
    except Exception:
        total_ram = 0
    limit_percent = max(50, min(100, int(ram_limit_percent)))
    budget = int(total_ram * (limit_percent / 100)) if total_ram else 0
    if budget and required_bytes > budget:
        return ModelFolderInfo(
            folder,
            weights_bytes,
            params_b,
            required_bytes,
            False,
            f"Too big for this device: {required_bytes / GB:.1f} GB is needed "
            f"but only {total_ram / GB:.1f} GB RAM is installed. Choose a "
            "smaller or more quantized model.",
            backend,
        )
    detail = f"GGUF · fits in {total_ram / GB:.0f} GB RAM" if backend == "gguf" else (
        f"Prism GGUF · fits in {total_ram / GB:.0f} GB RAM" if backend == "prism" else (
            f"Fits in {total_ram / GB:.0f} GB RAM" if total_ram else "Fits"
        )
    )
    return ModelFolderInfo(
        folder,
        weights_bytes,
        params_b,
        required_bytes,
        True,
        detail,
        backend,
    )


def ensure_model_fits(folder: Path, *, ram_limit_percent: int = 90) -> ModelFolderInfo:
    info = inspect_model_folder(folder, ram_limit_percent=ram_limit_percent)
    if not info.supported:
        raise ValueError(info.reason)
    return info


CHAT_SYSTEM_PROMPT = (
    "You are Blaze AI, a concise local desktop assistant. Answer the user's "
    "latest message directly. Never open with a greeting or introduce "
    "yourself unless the user explicitly asks who you are. Do not list your "
    "capabilities unless asked. Keep replies short and useful."
)

_SELF_INTRO = re.compile(
    r"^\s*(?:(?:hi|hello|hey)(?:\s+there)?[\s,.!:\-–—]*)?"
    r"(?:i(?:'m| am)|this\s+is)\s+blaze(?:\s+ai)?"
    r"(?:\s*[,!:.\-–—]+\s*|\s+)",
    re.IGNORECASE,
)
_INTRO_ONLY_LINE = re.compile(
    r"^(?:hi|hello|hey)(?:\s+there)?[\s,.!:\-–—]*"
    r"(?:i(?:'m| am)\s+blaze(?:\s+ai)?[\s.!]*)?$",
    re.IGNORECASE,
)


_THINK_TAG_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_HARMONY_FINAL_RE = re.compile(
    r"<\|channel\|>\s*final(?:<\|message\|>|:)\s*(.*?)(?:<\|end\|>|<\|channel\|>|$)",
    re.IGNORECASE | re.DOTALL,
)
_HARMONY_BLOCK_RE = re.compile(
    r"<\|(?:channel|message|start|end|return)\|>[^<\n]*", re.IGNORECASE
)
_REASONING_HEADER_RE = re.compile(
    r"(?im)^\s*(?:analysis|reasoning|thought process|thinking)\s*:\s*$"
)


def _strip_model_thinking(text: str) -> str:
    """Remove hidden reasoning/thinking formats; never surface chain-of-thought."""
    result = (text or "").strip()
    if not result:
        return ""
    # Harmony: prefer explicit final channel content when present.
    finals = _HARMONY_FINAL_RE.findall(result)
    if finals:
        result = finals[-1].strip()
    result = _THINK_TAG_RE.sub("", result)
    if "<|channel|>" in result or "<|message|>" in result:
        # Keep JSON/tool payloads if they appear; otherwise drop harmony markers.
        json_start = result.find("{")
        json_end = result.rfind("}")
        if json_start != -1 and json_end > json_start and '"tool"' in result:
            result = result[json_start : json_end + 1]
        else:
            result = _HARMONY_BLOCK_RE.sub("", result)
    result = _REASONING_HEADER_RE.sub("", result)
    # If the whole thing is a leaked analysis transcript, discard it.
    if re.search(r"(?i)\bwe need to respond\b|\bthe user wants\b.*\bjson\b", result) and '"tool"' not in result:
        return ""
    return result.strip()


def safe_model_reply(text: str, *, fallback: str = "Cannot comply with request. Please try again.") -> str:
    cleaned = _strip_model_thinking(text)
    cleaned = clean_model_reply(cleaned)
    # A bare analysis/thinking leak with no user-facing content is unsafe to show.
    if not cleaned or cleaned == (text or "").strip() and "<|channel|>" in (text or ""):
        return fallback
    if "<|channel|>" in cleaned or "<|message|>" in cleaned:
        cleaned = _strip_model_thinking(cleaned)
    return cleaned.strip() or fallback


def clean_model_reply(text: str) -> str:
    """Strip a boilerplate "Hi, I'm Blaze AI" preamble from model output.

    The introduction is only removed when a real answer follows it, so an
    explicit "who are you" reply is left intact. Hidden reasoning/thinking is
    removed so it never appears in user-facing output.
    """
    result = _strip_model_thinking(text)
    if not result:
        return ""
    original = result
    for _ in range(3):
        match = _SELF_INTRO.match(result)
        if not match:
            break
        remainder = result[match.end() :].strip()
        if not remainder or remainder[0].islower():
            break
        result = remainder
    lines = result.splitlines()
    while len(lines) > 1 and _INTRO_ONLY_LINE.match(lines[0].strip()):
        lines.pop(0)
    result = "\n".join(lines).strip()
    return result or original


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _runtime_library_dirs(binary: Path) -> list[str]:
    """Directories that hold the runtime's shared libraries."""
    root = binary.parent
    directories = {root}
    for pattern in ("*.dylib", "*.so", "*.so.*", "*.dll"):
        for item in root.rglob(pattern):
            directories.add(item.parent)
    return [str(item) for item in sorted(directories)]


class LlamaServerRuntime:
    """Small client for the bundled ``llama-server`` (Prism/Bonsai builds)."""

    def __init__(self, binary: Path, model_path: Path, *, context: int = 4096) -> None:
        self.binary = binary
        self.model_path = model_path
        self.context = context
        self.process: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.log_path: Path | None = None
        self._log_handle = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _log_tail(self, limit: int = 1200) -> str:
        if self.log_path is None or not self.log_path.is_file():
            return ""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:].strip()

    def start(self) -> None:
        if self.running:
            return
        self.port = _free_port()
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / "prism-llama-server.log"
        self._log_handle = open(self.log_path, "w", encoding="utf-8")
        env = os.environ.copy()
        library_dirs = _runtime_library_dirs(self.binary)
        if sys.platform == "darwin":
            key = "DYLD_LIBRARY_PATH"
        elif sys.platform.startswith("win"):
            key = "PATH"
        else:
            key = "LD_LIBRARY_PATH"
        existing = env.get(key, "")
        env[key] = os.pathsep.join(library_dirs + ([existing] if existing else []))
        command = [
            str(self.binary),
            "-m",
            str(self.model_path),
            "-c",
            str(self.context),
            "-b",
            "512",
            "-ngl",
            "99",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "-np",
            "1",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(self.binary.parent),
                env=env,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self.close()
            raise RuntimeError(f"Could not start the Prism runtime: {exc}") from exc

        deadline = time.monotonic() + 180.0
        health = f"http://127.0.0.1:{self.port}/health"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = self._log_tail()
                self.close()
                raise RuntimeError(
                    "The Prism runtime exited while loading the model."
                    + (f"\n{detail}" if detail else "")
                )
            try:
                with urllib.request.urlopen(health, timeout=2) as response:
                    if response.status == 200:
                        body = response.read(200)
                        if b"loading" not in body.lower():
                            return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        detail = self._log_tail()
        self.close()
        raise RuntimeError(
            "The Prism runtime did not finish loading within 3 minutes."
            + (f"\n{detail}" if detail else "")
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        should_stop=None,
    ) -> str:
        if not self.running:
            raise RuntimeError("The Prism runtime is not running.")
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.95,
            "stream": True,
            # Bonsai is a hybrid reasoning model. Blaze needs the direct
            # answer, not a long "Thinking Process" block.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            with urllib.request.urlopen(request, timeout=600) as response:
                for raw_line in response:
                    if should_stop and should_stop():
                        response.close()
                        return ""
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta", {})
                    except (KeyError, IndexError, TypeError, ValueError):
                        continue
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content")
                    if content:
                        content_parts.append(str(content))
                    if reasoning:
                        reasoning_parts.append(str(reasoning))
            if should_stop and should_stop():
                return ""
            return ("".join(content_parts) or "".join(reasoning_parts)).strip()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            detail = self._log_tail()
            raise RuntimeError(
                f"The Prism model request failed. ({exc})"
                + (f"\n{detail}" if detail else "")
            ) from exc
    def close(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None



class HuggingFaceModel:
    def __init__(self) -> None:
        self.repo_id = ""
        self.local_path: Path | None = None
        self.param_label = ""
        self.backend = ""
        self.tokenizer = None
        self.model = None
        self.llama = None
        self.llama_server: LlamaServerRuntime | None = None
        self.asr_pipeline = None
        self.last_download_note = ""

    @property
    def loaded(self) -> bool:
        if self.llama is not None:
            return True
        if self.llama_server is not None and self.llama_server.running:
            return True
        return self.model is not None and self.tokenizer is not None

    def load_folder(
        self, folder_value: str, task: str = "", *, ram_limit_percent: int = 90
    ) -> str:
        """Validate and load a user-selected local model folder.

        Raises ``ValueError`` for unsupported or oversized folders before any
        weights are loaded.
        """
        folder_value = (folder_value or "").strip()
        if not folder_value:
            raise ValueError("Choose a model folder, .gguf or .safetensors file first.")
        selected = Path(folder_value).expanduser()
        folder = selected
        # A single .safetensors file belongs to the folder that holds its
        # config/tokenizer; resolve it to that folder for Transformers.
        if folder.is_file() and folder.suffix.lower() == ".safetensors":
            folder = folder.parent
        if not folder.is_dir() and not (
            selected.is_file() and selected.suffix.lower() == ".gguf"
        ):
            legacy = model_path(folder_value) if "/" in folder_value else None
            if legacy is not None and legacy.is_dir():
                folder = legacy
                selected = legacy
            elif not (folder.is_file() and folder.suffix.lower() == ".gguf"):
                raise ValueError(
                    "Unsupported model location: choose a Transformers model "
                    "folder, a single .safetensors file beside config.json, or "
                    "a .gguf model file."
                )

        info = ensure_model_fits(folder, ram_limit_percent=ram_limit_percent)
        display_name = selected.stem if selected.is_file() else selected.name
        self.last_download_note = ""

        if task == "automatic-speech-recognition":
            if info.backend in {"gguf", "prism"}:
                raise ValueError(
                    "GGUF models are for chat. Choose a Transformers speech "
                    "model folder for voice-to-text."
                )
            self.unload()
            try:
                from transformers import pipeline

                self.asr_pipeline = pipeline(
                    "automatic-speech-recognition", model=str(folder), device=-1
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self.unload()
                raise RuntimeError(
                    "Unsupported speech model folder: the local Transformers "
                    f"pipeline could not load it. ({exc})"
                ) from exc
            self.repo_id = display_name
            self.local_path = selected
            self.param_label = info.param_label
            self.backend = "transformers"
            return "speech"

        if info.backend in {"gguf", "prism"}:
            quantized = (
                _gguf_shard_set(folder) if folder.is_file() else _quantized_files(folder)
            )
            entry = _primary_gguf(quantized)
            self.unload()
            if info.backend == "prism":
                self._load_prism_runtime(entry, selected, display_name, info)
                return "chat"
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise RuntimeError(
                    "GGUF models need the llama-cpp-python runtime. Install "
                    "requirements-local-model.txt to enable quantized models."
                ) from exc
            try:
                self.llama = Llama(
                    model_path=str(entry),
                    n_ctx=4096,
                    n_batch=512,
                    n_gpu_layers=-1,
                    verbose=False,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self.unload()
                raise RuntimeError(
                    "Unsupported GGUF model: llama.cpp could not load it. "
                    f"({exc})"
                ) from exc
            self.repo_id = display_name
            self.local_path = selected
            self.param_label = info.param_label
            self.backend = "gguf"
            return "chat"

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Unsupported local model: Transformers/Torch is not installed. "
                "Install requirements-local-model.txt."
            ) from exc

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.unload()
        load_dtype = torch.float16 if device == "mps" else torch.float32
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(str(folder))
            load_kwargs = {
                "low_cpu_mem_usage": True,
                "dtype": load_dtype,
            }
            try:
                self.model = AutoModelForCausalLM.from_pretrained(
                    str(folder), **load_kwargs
                ).to(device)
            except TypeError:
                load_kwargs.pop("dtype", None)
                load_kwargs["torch_dtype"] = load_dtype
                self.model = AutoModelForCausalLM.from_pretrained(
                    str(folder), **load_kwargs
                ).to(device)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.unload()
            raise RuntimeError(
                "Unsupported chat model folder: Transformers could not load it. "
                f"Use a text-generation safetensors or GGUF model. ({exc})"
            ) from exc
        self.repo_id = display_name
        self.local_path = selected
        self.param_label = info.param_label
        self.backend = "transformers"
        return "chat"

    def _load_prism_runtime(
        self,
        entry: Path,
        folder: Path,
        display_name: str,
        info: ModelFolderInfo,
    ) -> None:
        binary = prism_server_binary()
        if binary is None:
            raise RuntimeError(
                "This is a Prism ternary GGUF (Ternary-Bonsai-2), which stock "
                "llama.cpp cannot read. The Prism runtime is not installed. "
                f"Place llama-server in {prism_runtime_dir()} or reinstall "
                "Blaze AI."
            )
        self.last_download_note = ""
        server = LlamaServerRuntime(binary, entry, context=4096)
        try:
            server.start()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            server.close()
            raise RuntimeError(
                "Unsupported Prism GGUF: the Bonsai runtime could not load it. "
                f"({exc})"
            ) from exc
        self.llama_server = server
        self.repo_id = display_name
        self.local_path = folder
        self.param_label = info.param_label
        self.backend = "prism"

    # Backwards-compatible name used by older callers.
    def download_from_hub(self, repo_id: str, task: str = "") -> str:
        return self.load_folder(repo_id, task)

    def unload(self) -> None:
        if self.llama_server is not None:
            self.llama_server.close()
            self.llama_server = None
        self.model = None
        self.tokenizer = None
        self.llama = None
        self.backend = ""
        self.repo_id = ""
        self.local_path = None
        self.param_label = ""
        self.asr_pipeline = None
        try:
            import gc

            gc.collect()
            try:
                import torch

                if torch.backends.mps.is_available():
                    torch.mps.empty_cache()
                elif torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
        except Exception:
            pass

    def remove(self, repo_id: str) -> None:
        if self.repo_id == repo_id or (
            self.local_path is not None and self.local_path == Path(repo_id).expanduser()
        ):
            self.unload()
        if Path(repo_id).expanduser().is_absolute():
            # Locally added folders are owned by the user; never delete them.
            return
        folder = model_path(repo_id)
        if folder.exists():
            shutil.rmtree(folder)

    def respond(self, history: list[dict[str, str]], user_name: str, *, should_stop=None) -> str:
        # Short chat replies keep latency low on consumer hardware.
        reply = clean_model_reply(
            self._generate(
                history,
                max_new_tokens=CHAT_MAX_NEW_TOKENS,
                do_sample=False,
                should_stop=should_stop,
            )
        )
        return reply or "Cannot comply with request. Please try again."

    def respond_action(self, history: list[dict[str, str]], user_name: str, *, should_stop=None) -> str:
        """Deterministic short reply used by the computer-control agent."""
        # Agent actions need room for a full JSON tool call plus minimal thought.
        raw = self._generate(
            history, max_new_tokens=256, do_sample=False, should_stop=should_stop
        )
        stripped = _strip_model_thinking(raw)
        # Preserve JSON for the agent parser; if only thinking leaked, fail safely.
        if stripped:
            return stripped
        return '{"tool":"finish","args":{"message":"Cannot comply with request. Please try again."}}'

    def _generate(
        self,
        history: list[dict[str, str]],
        *,
        max_new_tokens: int,
        do_sample: bool,
        temperature: float = 0.7,
        should_stop=None,
    ) -> str:
        if not self.loaded:
            raise RuntimeError("No local model is loaded.")
        if should_stop and should_stop():
            return ""
        if self.llama_server is not None and self.llama_server.running:
            return self._generate_prism(
                history,
                max_new_tokens=max_new_tokens,
                do_sample=do_sample,
                temperature=temperature,
                should_stop=should_stop,
            )
        if self.llama is not None:
            return self._generate_gguf(
                history,
                max_new_tokens=max_new_tokens,
                do_sample=do_sample,
                temperature=temperature,
                should_stop=should_stop,
            )
        import torch

        try:
            prompt = self.tokenizer.apply_chat_template(
                history,
                tokenize=False,
                add_generation_prompt=True,
            )
        except Exception:
            # Some chat templates reject a system role. Fold the instruction
            # into the first user turn instead of failing the request.
            system_parts = [
                str(message.get("content", ""))
                for message in history
                if message.get("role") == "system" and message.get("content")
            ]
            merged = [
                dict(message)
                for message in history
                if message.get("role") in {"user", "assistant"} and message.get("content")
            ]
            if system_parts and merged:
                first = merged[0]
                prefix = "\n\n".join(system_parts)
                if first.get("role") == "user":
                    first["content"] = f"{prefix}\n\n{first.get('content', '')}"
                else:
                    merged.insert(0, {"role": "user", "content": prefix})
            prompt = self.tokenizer.apply_chat_template(
                merged,
                tokenize=False,
                add_generation_prompt=True,
            )
        inputs = self.tokenizer(prompt, return_tensors="pt")
        device = next(self.model.parameters()).device
        inputs = {key: value.to(device) for key, value in inputs.items()}
        options = {
            "max_new_tokens": max_new_tokens,
            "do_sample": do_sample,
            "use_cache": True,
        }
        if do_sample:
            options["temperature"] = temperature
        if should_stop:
            try:
                from transformers import StoppingCriteria, StoppingCriteriaList

                class _Stop(StoppingCriteria):
                    def __call__(self, input_ids, scores, **kwargs):  # noqa: ANN001
                        return bool(should_stop())

                options["stopping_criteria"] = StoppingCriteriaList([_Stop()])
            except Exception:
                pass
        with torch.inference_mode():
            output = self.model.generate(**inputs, **options)
        if should_stop and should_stop():
            return ""
        generated = output[0][inputs["input_ids"].shape[1]:]
        return self.tokenizer.decode(generated, skip_special_tokens=True).strip()

    def _generate_gguf(
        self,
        history: list[dict[str, str]],
        *,
        max_new_tokens: int,
        do_sample: bool,
        temperature: float = 0.7,
        should_stop=None,
    ) -> str:
        if self.llama is None:
            raise RuntimeError("No GGUF model is loaded.")
        if should_stop and should_stop():
            return ""
        messages = [
            {"role": message["role"], "content": message["content"]}
            for message in history[-24:]
            if message.get("role") in {"system", "user", "assistant"}
            and message.get("content")
        ]
        if not messages:
            raise RuntimeError("Nothing to send to the GGUF model.")
        try:
            reply = self.llama.create_chat_completion(
                messages=messages,
                max_tokens=max_new_tokens,
                temperature=temperature if do_sample else 0.0,
                top_p=0.95 if do_sample else 1.0,
                stream=True,
            )
            content = _collect_llama_stream(reply, chat=True, should_stop=should_stop)
        except Exception:
            if should_stop and should_stop():
                return ""
            # Fallback for GGUF files with missing or incompatible chat templates.
            prompt_parts: list[str] = []
            for message in messages:
                role = str(message.get("role", "user")).upper()
                prompt_parts.append(f"{role}: {message.get('content', '')}")
            prompt_parts.append("ASSISTANT:")
            try:
                reply = self.llama(
                    "\n\n".join(prompt_parts),
                    max_tokens=max_new_tokens,
                    temperature=temperature if do_sample else 0.0,
                    top_p=0.95 if do_sample else 1.0,
                    stop=["\nUSER:", "\nSYSTEM:"],
                    stream=True,
                )
                content = _collect_llama_stream(reply, chat=False, should_stop=should_stop)
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                raise RuntimeError(f"The GGUF model could not generate a reply. ({exc})") from exc
        if should_stop and should_stop():
            return ""
        return str(content).strip()

    def _generate_prism(
        self,
        history: list[dict[str, str]],
        *,
        max_new_tokens: int,
        do_sample: bool,
        temperature: float = 0.7,
        should_stop=None,
    ) -> str:
        if self.llama_server is None:
            raise RuntimeError("No Prism model is loaded.")
        messages = [
            {"role": message["role"], "content": message["content"]}
            for message in history[-24:]
            if message.get("role") in {"system", "user", "assistant"}
            and message.get("content")
        ]
        if not messages:
            raise RuntimeError("Nothing to send to the Prism model.")
        return self.llama_server.complete(
            messages,
            max_tokens=max_new_tokens,
            temperature=temperature if do_sample else 0.0,
            should_stop=should_stop,
        )
