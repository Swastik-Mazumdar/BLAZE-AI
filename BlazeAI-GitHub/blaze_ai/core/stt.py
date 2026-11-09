"""Push-to-talk speech input with faster-whisper and strict anti-hallucination.

The microphone is never opened at rest. Audio is captured only while the user
holds the mic button, then faster-whisper performs fully local inference. VAD,
decoding thresholds, segment filtering and a known-hallucination phrase filter
work together to keep the transcript clean before the command layer sees it.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from ..config import app_data_dir

log = logging.getLogger("blaze.voice")

SAMPLE_RATE = 16_000
MAX_RECORD_SECONDS = 45
MIN_RECORD_SECONDS = 0.35

_HALLUCINATION_PATTERNS = [
    r"thank(s| you) (so much )?for watching",
    r"please\s+(?:like|share|subscribe)(?:\s*(?:,|and|&)?\s*(?:like|share|subscribe))*(?:\s+(?:my|our|the)\s+(?:channel|video|podcast|content))?",
    r"(?:like|share|subscribe)(?:\s*(?:,|and|&)\s*(?:like|share|subscribe))+(?:\s+(?:my|our|the)\s+(?:channel|video|podcast|content))?",
    r"subtitles?\s+(?:by|provided by)(?:\s+the)?(?:\s+amara\.org)?(?:\s+community)?",
    r"amara\.org",
    r"transcribed\s+by(?:\s+(?:whisper|ai|openai|machine))?",
    r"www\.[a-z0-9.-]+",
    r"^\s*[\[\(]?(music|applause|laughter|silence)[\]\)]?\s*$",
    r"^\s*[.\-–—_,!?]+\s*$",
]
_HALLUCINATION_RE = re.compile("|".join(_HALLUCINATION_PATTERNS), re.IGNORECASE)


def voice_dependencies() -> dict[str, bool]:
    status = {"faster_whisper": False, "sounddevice": False, "numpy": False}
    try:
        import faster_whisper  # noqa: F401

        status["faster_whisper"] = True
    except ImportError:
        pass
    try:
        import sounddevice  # noqa: F401

        status["sounddevice"] = True
    except ImportError:
        pass
    try:
        import numpy  # noqa: F401

        status["numpy"] = True
    except ImportError:
        pass
    return status


def voice_ready() -> bool:
    status = voice_dependencies()
    return all(status.values())


def voice_capture_ready() -> bool:
    status = voice_dependencies()
    return status["sounddevice"] and status["numpy"]


class VoiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float
    duration: float


class TranscribeWorker(QThread):
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, audio, model_size: str, language: str = "en", asr_pipeline=None) -> None:
        super().__init__()
        self.audio = audio
        self.model_size = model_size
        self.language = language
        self.asr_pipeline = asr_pipeline

    def run(self) -> None:
        try:
            self.progress.emit("loading")
            if self.asr_pipeline is not None:
                result = self.asr_pipeline({"raw": self.audio, "sampling_rate": SAMPLE_RATE})
                text = str(result.get("text", "")).strip() if isinstance(result, dict) else str(result).strip()
                transcript = Transcript(text, 0.9 if text else 0.0, len(self.audio) / SAMPLE_RATE)
            else:
                transcript = transcribe_audio(self.audio, self.model_size, self.language)
            self.finished_ok.emit(transcript)
        except Exception as exc:
            log.exception("Transcription failed")
            self.failed.emit(_friendly_error(exc))


class AudioRecorder:
    """Callback-based 16 kHz mono capture; opened only during push-to-talk."""

    def __init__(self) -> None:
        self._stream = None
        self._chunks: list = []
        self._frames = 0
        self._started_at = 0.0
        self._overflow = False

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise VoiceError(
                "Voice input needs sounddevice. Install voice extras with "
                "`pip install faster-whisper sounddevice numpy`."
            ) from exc
        self._chunks = []
        self._frames = 0
        self._overflow = False
        self._started_at = time.time()

        def callback(indata, frames, _time_info, status) -> None:  # noqa: ANN001
            if status:
                log.debug("Audio stream status: %s", status)
            self._chunks.append(indata.copy())
            self._frames += frames
            if self._frames > SAMPLE_RATE * MAX_RECORD_SECONDS:
                self._overflow = True

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=callback,
                blocksize=1024,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise VoiceError(f"Could not open the microphone: {exc}") from exc

    def stop(self):
        if self._stream is None:
            raise VoiceError("The microphone is not recording.")
        stream = self._stream
        self._stream = None
        try:
            stream.stop()
            stream.close()
        except Exception:
            log.debug("Microphone close failed", exc_info=True)
        duration = time.time() - self._started_at
        if duration < MIN_RECORD_SECONDS or not self._chunks:
            self._chunks = []
            raise VoiceError("That was too short. Hold the mic and speak a command.")
        try:
            import numpy as np
        except ImportError as exc:
            raise VoiceError("numpy is required for voice input.") from exc
        audio = np.concatenate(self._chunks, axis=0).reshape(-1).astype(np.float32)
        self._chunks = []
        return audio, duration, self._overflow


class VoiceEngine(QObject):
    state_changed = pyqtSignal(str)
    transcript_ready = pyqtSignal(object)
    error = pyqtSignal(str)
    level_changed = pyqtSignal(float)

    def __init__(self, model_size: str = "base.en", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.model_size = model_size
        self._recorder = AudioRecorder()
        self._worker: TranscribeWorker | None = None
        self._state = "idle"
        self.asr_pipeline = None
        self._max_timer = QTimer(self)
        self._max_timer.setSingleShot(True)
        self._max_timer.timeout.connect(self._auto_stop)

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_recording(self) -> bool:
        return self._recorder.is_recording

    def set_model_size(self, size: str) -> None:
        if not self.is_recording:
            self.model_size = size

    def set_asr_pipeline(self, pipeline) -> None:
        self.asr_pipeline = pipeline

    def start_recording(self) -> None:
        if self._state == "transcribing":
            self.error.emit("Still transcribing the previous clip.")
            return
        if self._recorder.is_recording:
            return
        try:
            self._recorder.start()
        except VoiceError as exc:
            self._set_state("idle")
            self.error.emit(str(exc))
            return
        self._set_state("recording")
        self._max_timer.start(MAX_RECORD_SECONDS * 1000)

    def stop_recording(self) -> None:
        self._max_timer.stop()
        if not self._recorder.is_recording:
            self._set_state("idle")
            return
        try:
            audio, duration, overflow = self._recorder.stop()
        except VoiceError as exc:
            self._set_state("idle")
            self.error.emit(str(exc))
            return
        if overflow:
            log.info("Voice clip reached maximum length and was trimmed.")
        self._set_state("transcribing")
        self._worker = TranscribeWorker(audio, self.model_size, asr_pipeline=self.asr_pipeline)
        self._worker.finished_ok.connect(lambda result: self._on_transcribed(result, duration))
        self._worker.failed.connect(self._on_failed)
        self._worker.progress.connect(self._on_progress)
        self._worker.start()

    def cancel(self) -> None:
        self._max_timer.stop()
        if self._recorder.is_recording:
            try:
                self._recorder.stop()
            except VoiceError:
                pass
        self._set_state("idle")

    def shutdown(self) -> None:
        self._max_timer.stop()
        if self._recorder.is_recording:
            try:
                self._recorder.stop()
            except VoiceError:
                pass
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(30_000)

    def _auto_stop(self) -> None:
        if self._recorder.is_recording:
            self.stop_recording()

    def _on_transcribed(self, transcript: Transcript, duration: float) -> None:
        self._set_state("idle")
        if transcript.text:
            self.transcript_ready.emit(transcript)
        else:
            self.error.emit("No clear speech detected. Try again a little closer.")

    def _on_failed(self, message: str) -> None:
        self._set_state("idle")
        self.error.emit(message)

    def _on_progress(self, state: str) -> None:
        if state == "loading":
            self.state_changed.emit("loading")

    def _set_state(self, state: str) -> None:
        self._state = state
        self.state_changed.emit(state)


_MODEL_LOCK = None


def _model_lock():
    global _MODEL_LOCK
    if _MODEL_LOCK is None:
        import threading

        _MODEL_LOCK = threading.Lock()
    return _MODEL_LOCK


_MODEL_CACHE: dict[tuple[str, str], object] = {}


def _get_model(model_size: str, device: str = "auto"):
    from faster_whisper import WhisperModel

    key = (model_size, device)
    with _model_lock():
        model = _MODEL_CACHE.get(key)
        if model is not None:
            return model
        if device == "auto":
            # CPU int8 is the reliable, low-memory default across packaged builds.
            device = "cpu"
        compute_type = "int8" if device == "cpu" else "float16"
        threads = max(1, min(4, (os.cpu_count() or 4) // 2))
        model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=threads,
            download_root=str(app_data_dir() / "whisper-models"),
        )
        _MODEL_CACHE[key] = model
        return model


def transcribe_audio(audio, model_size: str, language: str = "en") -> Transcript:
    """Run faster-whisper with deliberately conservative decoding settings."""

    import numpy as np

    if audio is None or len(audio) == 0:
        return Transcript("", 0.0, 0.0)
    # Normalize gain only when extremely quiet; never amplify room noise.
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak < 0.01:
        return Transcript("", 0.0, 0.0)
    if peak > 0.98:
        audio = audio / max(peak, 1e-6) * 0.96

    model = _get_model(model_size)
    duration = len(audio) / SAMPLE_RATE
    segments, _info = model.transcribe(
        audio,
        language=language,
        beam_size=5,
        best_of=5,
        temperature=0.0,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 450,
            "speech_pad_ms": 200,
            "threshold": 0.5,
        },
        condition_on_previous_text=False,
        no_speech_threshold=0.62,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        without_timestamps=True,
        word_timestamps=False,
    )

    accepted: list[str] = []
    probabilities: list[float] = []
    for segment in segments:
        text = _clean_transcript(segment.text)
        if not text:
            continue
        if getattr(segment, "no_speech_prob", 0.0) > 0.65:
            continue
        if getattr(segment, "avg_logprob", 0.0) < -1.15:
            continue
        accepted.append(text)
        # faster-whisper exposes avg_logprob, not token probabilities; map it to
        # a conservative display confidence without pretending it is exact.
        probabilities.append(_logprob_confidence(segment.avg_logprob, segment.no_speech_prob))
    text = _clean_transcript(" ".join(accepted))
    if not text:
        return Transcript("", 0.0, duration)
    confidence = sum(probabilities) / len(probabilities) if probabilities else 0.0
    return Transcript(text, confidence, duration)


def _logprob_confidence(logprob: float, no_speech: float) -> float:
    # -0.1 is excellent, -1.0 is weak. Clamp for display only.
    base = 1.0 - min(1.0, abs(float(logprob)) / 1.2)
    speech = 1.0 - min(1.0, max(0.0, float(no_speech)))
    return max(0.0, min(1.0, (base * 0.8) + (speech * 0.2)))


def _clean_transcript(text: str) -> str:
    value = " ".join(str(text).replace("\x00", " ").split()).strip()
    value = value.strip(" \t\n-–—")
    if not value:
        return ""
    if _HALLUCINATION_RE.search(value):
        # Drop only the hallucinated phrase, not a legitimate trailing clause.
        value = _HALLUCINATION_RE.sub("", value).strip(" ,.-–—")
    if len(value) < 2:
        return ""
    if len(set(value.lower().replace(" ", ""))) <= 1:
        return ""
    # Reject runaway repetitive output from silent/noisy clips.
    words = value.lower().split()
    if len(words) >= 8 and len(set(words)) <= 2:
        return ""
    return value


def _friendly_error(exc: Exception) -> str:
    message = str(exc)
    lower = message.lower()
    if "model" in lower and ("download" in lower or "connection" in lower):
        return (
            "The Whisper model could not be downloaded. Connect once to fetch it, "
            "or place a model in the BlazeAI data folder."
        )
    if "cublas" in lower or "cuda" in lower:
        return "Whisper CUDA initialization failed. Blaze will fall back to CPU next launch."
    if "permission" in lower or "access" in lower:
        return "Microphone access was denied. Grant Blaze microphone permission in system settings."
    return f"Transcription failed: {message[:240]}"
