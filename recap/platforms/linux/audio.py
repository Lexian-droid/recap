"""recap.platforms.linux – audio capture using FFmpeg PulseAudio/ALSA backend.

On Linux, system audio loopback is captured via PulseAudio's monitor
device (``default.monitor``) or ALSA.  This module launches an FFmpeg
subprocess with the appropriate input format.
"""

from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from recap.exceptions import AudioCaptureError

log = logging.getLogger(__name__)


def _detect_audio_backend() -> tuple[str, str]:
    """Detect the best audio backend and device for loopback capture.

    Returns ``(device, ffmpeg_format)`` e.g. ``("default.monitor", "pulse")``.
    """
    # PulseAudio / PipeWire-PulseAudio: use the monitor of the default sink
    if shutil.which("pactl"):
        try:
            result = subprocess.run(
                ["pactl", "get-default-sink"],
                capture_output=True, text=True, timeout=5,
            )
            default_sink = result.stdout.strip()
            if default_sink:
                return f"{default_sink}.monitor", "pulse"
            return "default.monitor", "pulse"
        except Exception:
            return "default.monitor", "pulse"

    # ALSA fallback
    return "default", "alsa"


class AudioCapture:
    """Linux audio capture via FFmpeg PulseAudio/ALSA backend."""

    def __init__(
        self,
        wav_path: str | Path,
        process_id: Optional[int] = None,
    ) -> None:
        self._wav_path = str(wav_path)
        self._process_id = process_id
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._format_event = threading.Event()
        self._started_event = threading.Event()
        self._started_at: Optional[float] = None
        self._sample_rate: int = 48000
        self._channels: int = 2
        self._bits_per_sample: int = 16
        self._ffmpeg_proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def bits_per_sample(self) -> int:
        return self._bits_per_sample

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._format_event.clear()
        self._started_event.clear()
        self._started_at = None
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="recap-audio",
        )
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
            try:
                self._ffmpeg_proc.send_signal(signal.SIGINT)
            except (OSError, ProcessLookupError):
                pass

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wait_format_ready(self, timeout: float = 10.0) -> bool:
        return self._format_event.wait(timeout=timeout)

    def wait_started(self, timeout: float = 10.0) -> bool:
        return self._started_event.wait(timeout=timeout)

    @property
    def started_at(self) -> Optional[float]:
        return self._started_at

    # ------------------------------------------------------------------
    # Internal capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        try:
            self._capture_loop_impl()
        except Exception as exc:
            log.error("Audio capture error: %s", exc, exc_info=True)
            raise AudioCaptureError(str(exc)) from exc

    def _capture_loop_impl(self) -> None:
        from recap.ffmpeg import find_ffmpeg

        if self._process_id is not None:
            log.warning(
                "Per-process audio capture is not directly supported on Linux. "
                "Capturing system audio instead. For application-specific "
                "capture, configure PulseAudio manually."
            )

        ffmpeg_info = find_ffmpeg()
        ffmpeg_path = str(ffmpeg_info.path)

        device, fmt = _detect_audio_backend()
        log.info("Linux audio: using %s (format: %s)", device, fmt)

        # Publish format info
        self._format_event.set()

        cmd = [
            ffmpeg_path, "-y",
            "-f", fmt,
            "-i", device,
            "-acodec", "pcm_s16le",
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            self._wav_path,
        ]
        log.debug("FFmpeg audio capture command: %s", cmd)

        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._started_at = time.perf_counter()
        self._started_event.set()
        log.info("Linux audio capture started")

        # Wait for stop signal
        self._stop_event.wait()

        # Gracefully stop FFmpeg
        if self._ffmpeg_proc.poll() is None:
            try:
                self._ffmpeg_proc.send_signal(signal.SIGINT)
                self._ffmpeg_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()
                self._ffmpeg_proc.wait(timeout=5)
            except (OSError, ProcessLookupError):
                pass

        log.info("Linux audio capture stopped")
