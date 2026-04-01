"""recap.platforms.macos – audio capture using FFmpeg avfoundation.

On macOS, system audio loopback is not natively available without a
virtual audio device (e.g. BlackHole, Soundflower).  This module uses
FFmpeg's ``avfoundation`` input to capture audio to a WAV file.

If a known virtual loopback device is detected it is selected
automatically.  Otherwise the default audio input device is used with
a warning.
"""

from __future__ import annotations

import logging
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from recap.exceptions import AudioCaptureError

log = logging.getLogger(__name__)

# Well-known virtual loopback device names (case-insensitive substring match)
_LOOPBACK_DEVICE_NAMES = [
    "blackhole",
    "soundflower",
    "loopback",
    "virtual audio",
]


class AudioCapture:
    """macOS audio capture via FFmpeg avfoundation backend."""

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
                "Per-process audio capture is not natively supported on macOS. "
                "Capturing from the default audio input device instead."
            )

        ffmpeg_info = find_ffmpeg()
        ffmpeg_path = str(ffmpeg_info.path)

        # Detect best audio device
        device_idx = self._find_loopback_device(ffmpeg_path)
        device_desc = f"device index {device_idx}"
        log.info("macOS audio: using %s", device_desc)

        # Publish format info
        self._format_event.set()

        cmd = [
            ffmpeg_path, "-y",
            "-f", "avfoundation",
            "-i", f":{device_idx}",
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
        log.info("macOS audio capture started")

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

        log.info("macOS audio capture stopped")

    def _find_loopback_device(self, ffmpeg_path: str) -> int:
        """Detect the best audio capture device.

        Prefers known virtual loopback devices (BlackHole, Soundflower)
        over the default input.
        """
        try:
            result = subprocess.run(
                [
                    ffmpeg_path,
                    "-f", "avfoundation",
                    "-list_devices", "true",
                    "-i", "",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            in_audio = False
            audio_devices: list[tuple[int, str]] = []
            for line in result.stderr.split("\n"):
                if "AVFoundation audio devices" in line:
                    in_audio = True
                    continue
                if in_audio:
                    match = re.search(r"\[(\d+)\]\s+(.*)", line)
                    if match:
                        idx = int(match.group(1))
                        name = match.group(2).strip()
                        audio_devices.append((idx, name))

            # Prefer loopback device
            for idx, name in audio_devices:
                if any(lb in name.lower() for lb in _LOOPBACK_DEVICE_NAMES):
                    log.info("Using loopback audio device: %s (index %d)", name, idx)
                    return idx

            # Fall back to first device
            if audio_devices:
                log.info(
                    "No virtual loopback device found. Using default input: %s. "
                    "For system audio capture, install BlackHole: "
                    "https://github.com/ExistentialAudio/BlackHole",
                    audio_devices[0][1],
                )
                return audio_devices[0][0]
        except Exception as exc:
            log.debug("Audio device detection failed: %s", exc)

        return 0  # Default device
