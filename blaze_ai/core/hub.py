"""Live Hugging Face Hub search with device-aware filtering.

The app talks directly to ``https://huggingface.co/api/models``. Results are
normalized into small dictionaries that the settings UI can render and the
loader can download. No hosted inference is involved: search is online, models
are downloaded to disk and executed locally.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .device import DeviceProfile
from .net import ensure_ca_bundle, friendly_network_error, ssl_context

API_ROOT = "https://huggingface.co/api/models"
SUPPORTED_TASKS = (
    "text-generation",
    "text2text-generation",
    "automatic-speech-recognition",
)
TASK_LABELS = {
    "text-generation": "chat",
    "text2text-generation": "chat",
    "automatic-speech-recognition": "speech",
}
_RECOMMEND_MARKER = "__recommend__"
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize_repo_id(value: str) -> str:
    """Accept a bare repo id, an org/model pair or a full huggingface.co URL."""
    text = (value or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0].rstrip("/")
    text = re.sub(r"^https?://huggingface\.co/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^huggingface\.co/", "", text, flags=re.IGNORECASE)
    for prefix in ("models/", "datasets/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def is_repo_id(value: str) -> bool:
    return bool(_REPO_PATTERN.match(normalize_repo_id(value)))


def _param_count(item: dict) -> float:
    safetensors = item.get("safetensors")
    if isinstance(safetensors, dict):
        total = safetensors.get("total")
        if isinstance(total, (int, float)) and total > 0:
            return float(total) / 1e9
        parameters = safetensors.get("parameters")
        if isinstance(parameters, dict):
            values = [v for v in parameters.values() if isinstance(v, (int, float))]
            if values:
                return float(sum(values)) / 1e9
    return 0.0


def _is_gguf(item: dict) -> bool:
    tags = {str(tag).lower() for tag in item.get("tags") or []}
    if "gguf" in tags:
        return True
    repo = str(item.get("id") or "").lower()
    return repo.endswith("-gguf") or "gguf" in repo.rsplit("/", 1)[-1]


def _fit(params_b: float, profile: DeviceProfile) -> tuple[bool, str]:
    if params_b <= 0:
        return True, "size unknown"
    # fp16 weights plus tokenizer/KV-cache/overhead.
    needed = params_b * 2.2
    budget = profile.ram_gb * 0.72
    if needed <= budget:
        return True, f"fits {profile.ram_gb:.0f} GB RAM"
    return False, f"needs ≈{needed:.0f} GB RAM"


def normalize_results(
    payload: list[dict],
    profile: DeviceProfile,
    *,
    task: str = "",
    limit: int = 30,
    only_fitting: bool = False,
) -> list[dict]:
    results: list[dict] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        repo = str(item.get("id") or item.get("modelId") or "").strip()
        if not repo or "/" not in repo or _is_gguf(item):
            continue
        pipeline = str(item.get("pipeline_tag") or "")
        if task and pipeline and pipeline != task:
            continue
        if pipeline and pipeline not in SUPPORTED_TASKS:
            continue
        params_b = _param_count(item)
        fits, fit_label = _fit(params_b, profile)
        if only_fitting and not fits:
            continue
        results.append(
            {
                "id": repo,
                "pipeline_tag": pipeline or "model",
                "downloads": int(item.get("downloads") or 0),
                "likes": int(item.get("likes") or 0),
                "params_b": round(params_b, 2),
                "fits": fits,
                "fit_label": fit_label,
                "gated": bool(item.get("gated")),
                "task_label": TASK_LABELS.get(pipeline, "model"),
            }
        )
    results.sort(key=lambda row: row["downloads"], reverse=True)
    return results[:limit]


def _request(params: dict[str, object], *, timeout: int = 30) -> list[dict]:
    ensure_ca_bundle()
    query = urlencode(params)
    request = Request(
        f"{API_ROOT}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "BlazeAI/1.1 (+https://huggingface.co)",
        },
    )
    with urlopen(request, timeout=timeout, context=ssl_context()) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, list) else []


def search_models(
    query: str,
    profile: DeviceProfile,
    *,
    task: str = "",
    limit: int = 24,
) -> list[dict]:
    """Search the live Hub catalog and annotate results for this device."""
    query = normalize_repo_id(query)
    params: dict[str, object] = {
        "limit": max(limit * 3, 40),
        "sort": "downloads",
        "direction": -1,
        "full": "true",
    }
    if query:
        params["search"] = query
    if task:
        params["filter"] = task
    payload = _request(params)
    return normalize_results(payload, profile, task=task, limit=limit)


def recommended_models(profile: DeviceProfile, limit: int = 12) -> list[dict]:
    """Return popular text-generation models that fit this machine's RAM."""
    payload = _request(
        {
            "filter": "text-generation",
            "limit": 60,
            "sort": "downloads",
            "direction": -1,
            "full": "true",
        }
    )
    results = normalize_results(
        payload, profile, task="text-generation", limit=limit, only_fitting=True
    )
    return results


def is_recommend_request(query: str) -> bool:
    return (query or "").strip().lower() == _RECOMMEND_MARKER


def recommend_marker() -> str:
    return _RECOMMEND_MARKER


def describe_network_error(exc: BaseException) -> str:
    return friendly_network_error(exc)
