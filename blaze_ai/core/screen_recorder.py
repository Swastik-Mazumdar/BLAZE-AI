"""Local screen recording with a Qt capture fallback and ffmpeg encoding.

Capturing through Qt keeps the feature dependency-light and identical on macOS
and Windows. Frames are written as JPEGs, then encoded to H.264 by a bundled
imageio-ffmpeg binary (preferred) or a system ffmpeg installation.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage

log = logging.getLogger("blaze.recorder")

MAX_RECORDING_SECONDS = 60 * 60
MAX_RECORDING_BYTES = 1024**3


class ScreenRecordingError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordingTicket:
    session_dir: Path
    output_path: Path
    frames_captured: int
    message: str


class EncoderWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, frames_dir: Path, output_path: Path, fps: int) -> None:
        super().__init__()
        self.frames_dir = frames_dir
        self.output_path = output_path
        self.fps = max(2, min(30, int(fps)))

    def run(self) -> None:
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            self.failed.emit(
                "ffmpeg was not found. The captured frames were preserved so nothing was lost."
            )
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        args = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(self.fps),
            "-i",
            str(self.frames_dir / "frame_%06d.jpg"),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(self.output_path),
        ]
        try:
            result = subprocess.run(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.failed.emit(f"Encoding failed: {exc}")
            return
        if result.returncode != 0:
            message = result.stderr.decode(errors="replace").strip()
            self.failed.emit(f"Encoding failed: {message[-500:]}")
            return
        self.finished_ok.emit(str(self.output_path))


class ScreenRecorder(QObject):
    started = pyqtSignal()
    stopped = pyqtSignal(object)
    encoded = pyqtSignal(str)
    encode_failed = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, fps: int = 8, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.fps = max(2, min(30, int(fps)))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._capture_frame)
        self._session_dir: Path | None = None
        self._output_path: Path | None = None
        self._frames = 0
        self._started_at = 0.0
        self._worker: EncoderWorker | None = None
        self._last_image_size = (0, 0)
        self._recording_bytes = 0

    @property
    def is_recording(self) -> bool:
        return self._session_dir is not None

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self._started_at if self._started_at else 0.0

    def start(self) -> RecordingTicket:
        if self.is_recording:
            raise ScreenRecordingError("A screen recording is already running.")
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            raise ScreenRecordingError("No screen is available to record.")
        session_dir = Path(tempfile.mkdtemp(prefix="blaze-recording-"))
        output_dir = default_capture_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"Blaze_{stamp}.mp4"
        self._session_dir = session_dir
        self._output_path = output_path
        self._frames = 0
        self._recording_bytes = 0
        self._started_at = time.time()
        self._timer.setInterval(max(30, int(1000 / self.fps)))
        self._timer.start()
        self._capture_frame()
        self.started.emit()
        log.info("Screen recording started: %s", session_dir)
        return RecordingTicket(
            session_dir,
            output_path,
            self._frames,
            "Recording. Say “stop recording” when you are done.",
        )

    def stop(self) -> RecordingTicket:
        if not self.is_recording or self._session_dir is None or self._output_path is None:
            raise ScreenRecordingError("There is no recording in progress.")
        self._timer.stop()
        session_dir = self._session_dir
        output_path = self._output_path
        frames = self._frames
        self._session_dir = None
        self._output_path = None
        self._started_at = 0.0
        self._worker = EncoderWorker(session_dir, output_path, self.fps)
        self._worker.finished_ok.connect(self._on_encoded)
        self._worker.failed.connect(self._on_encode_failed)
        self._worker.start()
        ticket = RecordingTicket(
            session_dir,
            output_path,
            frames,
            f"Recording stopped · {frames} frames · encoding to {output_path.name}.",
        )
        self.stopped.emit(ticket)
        return ticket

    def stop_and_wait(self, timeout_ms: int = 120_000) -> None:
        if self.is_recording:
            self.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(timeout_ms)

    def _capture_frame(self) -> None:
        if self._session_dir is None:
            return
        if self.elapsed_seconds >= MAX_RECORDING_SECONDS:
            self._stop_for_limit("Recording stopped at the one-hour safety limit.")
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        image = screen.grabWindow(0)
        if image.isNull():
            return
        # Keep files manageable; 1440px is plenty for a screen-inspection clip.
        if image.width() > 1440:
            image = image.scaledToWidth(1440, _smooth_transformation())
        self._last_image_size = (image.width(), image.height())
        self._frames += 1
        path = self._session_dir / f"frame_{self._frames:06d}.jpg"
        if not image.save(str(path), "JPEG", 88):
            log.warning("Could not save recording frame %s", path)
            self._stop_for_limit("Recording stopped because a frame could not be saved.")
            return
        try:
            frame_size = path.stat().st_size
            self._recording_bytes += frame_size
        except OSError:
            self._stop_for_limit("Recording stopped because storage could not be checked.")
            return
        if self._recording_bytes > MAX_RECORDING_BYTES:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            self._frames = max(0, self._frames - 1)
            self._recording_bytes = max(0, self._recording_bytes - frame_size)
            self._stop_for_limit("Recording stopped at the 1 GB storage safety limit.")

    def _stop_for_limit(self, message: str) -> None:
        self.error.emit(message)
        try:
            self.stop()
        except ScreenRecordingError:
            log.debug("Recording was already stopped at a safety limit.")

    def _on_encoded(self, path: str) -> None:
        if self._worker is not None:
            shutil.rmtree(self._worker.frames_dir, ignore_errors=True)
        self.encoded.emit(path)
        log.info("Recording encoded: %s", path)

    def _on_encode_failed(self, message: str) -> None:
        fallback = default_capture_dir() / f"Blaze_frames_{datetime.now():%Y%m%d_%H%M%S}"
        if self._worker is not None:
            session = self._worker.frames_dir
            try:
                shutil.copytree(session, fallback)
            except OSError:
                fallback = session
        self.encode_failed.emit(f"{message} Frames saved at: {fallback}")
        log.error("Recording encode failed: %s", message)


def default_capture_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Movies" / "BlazeAI"
    if os.name == "nt":
        return Path.home() / "Videos" / "BlazeAI"
    return Path.home() / "Videos" / "BlazeAI"


def save_screenshot() -> Path:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        raise ScreenRecordingError("No screen is available.")
    pixmap = screen.grabWindow(0)
    if pixmap.isNull():
        raise ScreenRecordingError(
            "The screen could not be captured. Grant screen-recording permission in "
            "system privacy settings."
        )
    output_dir = default_capture_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"Blaze_screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    if not pixmap.save(str(path), "PNG"):
        raise ScreenRecordingError("The screenshot could not be saved.")
    return path


def _find_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable:
            return executable
    except (ImportError, RuntimeError):
        pass
    return shutil.which("ffmpeg")


def _smooth_transformation():
    from PyQt6.QtCore import Qt

    return Qt.TransformationMode.SmoothTransformation


def current_screen_dimensions() -> tuple[int, int]:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return (0, 0)
    geometry = screen.geometry()
    return (geometry.width(), geometry.height())
