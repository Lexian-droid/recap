"""recap – recording session controller.

Orchestrates video capture, audio capture, and the FFmpeg encoding
subprocess.  This is the main entry-point for the library API.

Architecture
------------
Video is captured and encoded to a temporary file via FFmpeg (stdin
pipe).  Audio is captured to a temporary WAV file using WASAPI
loopback.  After recording stops, a final FFmpeg mux step combines
the two into the output file.

Video-only and audio-only modes are also supported.
"""

from __future__ import annotations

import enum
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from recap.config import RecordingConfig
from recap.exceptions import (
    CaptureError,
    FFmpegError,
    RecapError,
)
from recap.ffmpeg import FFmpegInfo, find_ffmpeg

log = logging.getLogger(__name__)

# Cached result of hardware encoder probe (None = not yet tested).
_hw_encoder_cache: Optional[str] = None
_hw_encoder_tested = False


# ====================================================================
# Public API
# ====================================================================

class RecorderState(enum.Enum):
    IDLE      = "idle"
    STARTING  = "starting"
    RECORDING = "recording"
    STOPPING  = "stopping"
    STOPPED   = "stopped"
    ERROR     = "error"


class Recorder:
    """High-level recording session controller.

    Usage::

        config = RecordingConfig(output="out.mp4")
        rec = Recorder(config)
        rec.start()
        # ... wait or do other work ...
        rec.stop()
        rec.wait()
    """

    def __init__(self, config: RecordingConfig) -> None:
        self._config = config
        self._state = RecorderState.IDLE
        self._lock = threading.Lock()

        self._ffmpeg_info: Optional[FFmpegInfo] = None
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._video_capture = None
        self._audio_capture = None
        self._video_relay: Optional[_VideoRelay] = None
        self._duration_timer: Optional[threading.Timer] = None
        self._stop_event = threading.Event()
        self._error: Optional[Exception] = None

        self._has_video = False
        self._has_audio = False
        self._temp_video_path: Optional[Path] = None
        self._temp_audio_path: Optional[Path] = None

    # ── Properties ──────────────────────────────────────────────────

    @property
    def state(self) -> RecorderState:
        return self._state

    @property
    def config(self) -> RecordingConfig:
        return self._config

    @property
    def error(self) -> Optional[Exception]:
        return self._error

    # ── Public API ──────────────────────────────────────────────────

    def start(self) -> None:
        """Begin recording."""
        with self._lock:
            if self._state != RecorderState.IDLE:
                raise RecapError(
                    f"Cannot start: recorder is in state {self._state.value}"
                )
            self._state = RecorderState.STARTING

        try:
            self._ffmpeg_info = find_ffmpeg(self._config.ffmpeg)
            log.info(
                "Using FFmpeg: %s (%s)",
                self._ffmpeg_info.path,
                self._ffmpeg_info.version,
            )
            self._launch()
            with self._lock:
                self._state = RecorderState.RECORDING
            log.info("Recording started -> %s", self._config.output)
        except Exception as exc:
            with self._lock:
                self._state = RecorderState.ERROR
                self._error = exc
            raise

    def stop(self) -> None:
        """Stop recording gracefully."""
        with self._lock:
            if self._state not in (
                RecorderState.RECORDING,
                RecorderState.STARTING,
            ):
                return
            self._state = RecorderState.STOPPING

        log.info("Stopping recording...")

        if self._duration_timer is not None:
            self._duration_timer.cancel()
            self._duration_timer = None

        if self._video_capture is not None:
            self._video_capture.stop()
        if self._audio_capture is not None:
            self._audio_capture.stop()

        self._stop_event.set()

    def wait(self, timeout: Optional[float] = None) -> int:
        """Wait for recording to finish and return 0 on success.

        After this returns the state is either ``STOPPED`` or ``ERROR``.
        """
        if not self._stop_event.wait(timeout=timeout):
            self.stop()

        # Wait for capture threads to finish (they see the stop event).
        if self._video_capture is not None:
            self._video_capture.wait(timeout=30)
        if self._audio_capture is not None:
            self._audio_capture.wait(timeout=30)

        # Close video FFmpeg stdin so it sees EOF and finalizes.
        if self._ffmpeg_proc is not None and self._ffmpeg_proc.stdin:
            try:
                self._ffmpeg_proc.stdin.close()
            except Exception:
                pass

        # Wait for video-encoding FFmpeg to finalize the temp file.
        wait_timeout = timeout or 30
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.wait(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                log.warning("FFmpeg did not exit in time, terminating.")
                self._ffmpeg_proc.terminate()
                self._ffmpeg_proc.wait(timeout=5)
            rc = self._ffmpeg_proc.returncode
            if rc != 0:
                with self._lock:
                    self._state = RecorderState.ERROR
                    self._error = FFmpegError(
                        f"FFmpeg video encode exited with code {rc}"
                    )
                self._cleanup_temp_files(keep_on_failure=True)
                return rc

        # Final mux / conversion step.
        try:
            if self._has_video and self._has_audio:
                self._mux_audio_video()
                self._cleanup_temp_files()
            elif self._has_audio and not self._has_video:
                if self._temp_audio_path is not None:
                    self._convert_audio()
                    self._cleanup_temp_files()
        except Exception as exc:
            with self._lock:
                self._state = RecorderState.ERROR
                self._error = exc
            self._cleanup_temp_files(keep_on_failure=True)
            return 1

        with self._lock:
            self._state = RecorderState.STOPPED
        log.info("Recording saved -> %s", self._config.output)
        return 0

    # ================================================================
    # Internal – launch pipeline
    # ================================================================

    def _launch(self) -> None:
        """Discover capture parameters, start captures, go."""
        self._has_video = self._config.capture_video
        self._has_audio = self._config.capture_audio
        
        # Measure achievable FPS on this system to prevent dropped frames
        target_fps = self._config.fps
        actual_fps = target_fps
        if self._has_video:
            from recap.video import VideoCapture
            actual_fps = VideoCapture.measure_achievable_fps(
                monitor_index=self._config.monitor,
                window_handle=self._config.window_handle,
                target_fps=target_fps,
            )
            if actual_fps < target_fps:
                log.info(
                    "FPS clamped from %d to %d based on system capability",
                    target_fps, actual_fps,
                )

        # ── resolve video target ────────────────────────────────────
        _window_pid: Optional[int] = None
        vid_kwargs: dict = {}
        if self._has_video:
            if self._config.window_handle is not None:
                vid_kwargs["window_handle"] = self._config.window_handle
                if self._has_audio:
                    from recap.discovery import find_window_by_handle
                    _win = find_window_by_handle(self._config.window_handle)
                    if _win is not None:
                        _window_pid = _win.pid
                    else:
                        log.warning(
                            "Could not resolve PID for HWND %d; "
                            "falling back to desktop audio loopback.",
                            self._config.window_handle,
                        )
            elif self._config.window_title is not None:
                from recap.discovery import find_window_by_title

                win = find_window_by_title(self._config.window_title)
                if win is None:
                    raise CaptureError(
                        f"No visible window matching "
                        f"'{self._config.window_title}'"
                    )
                vid_kwargs["window_handle"] = win.handle
                if self._has_audio:
                    _window_pid = win.pid
            else:
                vid_kwargs["monitor_index"] = (
                    self._config.monitor
                    if self._config.monitor is not None
                    else 0
                )

        # ── determine temp file paths ───────────────────────────────
        output = Path(self._config.output)
        if self._has_video and self._has_audio:
            self._temp_video_path = (
                output.parent / f".recap_temp_{os.getpid()}_video.mp4"
            )
            self._temp_audio_path = (
                output.parent / f".recap_temp_{os.getpid()}_audio.wav"
            )
        elif self._has_audio and not self._has_video:
            # Audio-only: write directly if output is WAV, else temp
            if output.suffix.lower() != ".wav":
                self._temp_audio_path = (
                    output.parent / f".recap_temp_{os.getpid()}_audio.wav"
                )

        # ── start video capture + encoding FFmpeg ───────────────────
        if self._has_video:
            self._video_relay = _VideoRelay()
            from recap.video import VideoCapture

            self._video_capture = VideoCapture(
                self._video_relay, fps=actual_fps, **vid_kwargs,
            )
            self._video_capture.start()
            if not self._video_capture.wait_ready(timeout=10):
                raise CaptureError("Video capture did not become ready.")
            width = self._video_capture.width
            height = self._video_capture.height

            venc, venc_opts = _pick_video_encoder(
                str(self._ffmpeg_info.path),
            )
            log.info("Video encoder: %s", venc)

            # Video encodes to a temp file when audio is present,
            # otherwise directly to the final output.
            video_output = (
                str(self._temp_video_path)
                if self._has_audio
                else str(self._config.output)
            )
            # Always overwrite temp files; final overwrite is governed
            # by config (but the mux step handles that).
            ow = (
                "-y"
                if (self._has_audio or self._config.overwrite)
                else "-n"
            )

            cmd = [
                str(self._ffmpeg_info.path), ow,
                "-f", "rawvideo", "-pixel_format", "bgra",
                "-video_size", f"{width}x{height}",
                "-framerate", str(actual_fps),
                "-i", "pipe:0",
            ]
            if self._config.has_crop:
                crop_filter = self._config.build_crop_filter(width, height)
                cmd += ["-vf", crop_filter]
                log.info("Crop filter: %s", crop_filter)

            cmd += [
                "-c:v", venc, *(venc_opts or []),
                "-pix_fmt", "yuv420p", "-an",
                video_output,
            ]
            log.debug("FFmpeg command: %s", cmd)

            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # Attach the relay to FFmpeg stdin immediately so video capture
            # can write frames as soon as it's ready.
            self._video_relay.set_target(self._ffmpeg_proc.stdin)

        # ── start audio capture (WAV file) ──────────────────────────
        if self._has_audio:
            from recap.audio import AudioCapture

            audio_path = (
                str(self._temp_audio_path)
                if self._temp_audio_path is not None
                else str(self._config.output)
            )
            self._audio_capture = AudioCapture(audio_path, process_id=_window_pid)
            self._audio_capture.start()
            if not self._audio_capture.wait_format_ready(timeout=10):
                raise CaptureError("Audio capture did not become ready.")
            if not self._audio_capture.wait_started(timeout=10):
                raise CaptureError("Audio capture did not start in time.")

        # ── optional duration cap ───────────────────────────────────
        if self._config.duration is not None:
            self._duration_timer = threading.Timer(
                self._config.duration, self.stop,
            )
            self._duration_timer.daemon = True
            self._duration_timer.start()

    # ── final mux: temp video + temp audio → output ─────────────────

    def _mux_audio_video(self) -> None:
        """Combine temp video and temp WAV into the final output."""
        ffmpeg = str(self._ffmpeg_info.path)
        ow = "-y" if self._config.overwrite else "-n"
        cmd = [
            ffmpeg, ow,
            "-i", str(self._temp_video_path),
            "-i", str(self._temp_audio_path),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(self._config.output),
        ]
        log.debug("FFmpeg mux command: %s", cmd)
        log.info("Muxing audio and video...")
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            raise FFmpegError(f"FFmpeg mux failed:\n{result.stderr}")

    def _convert_audio(self) -> None:
        """Convert temp WAV to the desired output format."""
        ffmpeg = str(self._ffmpeg_info.path)
        ow = "-y" if self._config.overwrite else "-n"
        ext = Path(self._config.output).suffix.lower()
        cmd: list[str] = [ffmpeg, ow, "-i", str(self._temp_audio_path)]
        if ext == ".wav":
            cmd += ["-c:a", "pcm_s16le"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd.append(str(self._config.output))
        log.debug("FFmpeg audio convert command: %s", cmd)
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            raise FFmpegError(
                f"FFmpeg audio conversion failed:\n{result.stderr}"
            )

    def _cleanup_temp_files(self, *, keep_on_failure: bool = False) -> None:
        """Remove temporary video and audio files."""
        if keep_on_failure:
            for p in (self._temp_video_path, self._temp_audio_path):
                if p is not None and p.exists():
                    log.debug("Keeping temp file for debugging: %s", p)
            return
        for p in (self._temp_video_path, self._temp_audio_path):
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass


# ====================================================================
# Video relay
# ====================================================================

class _VideoRelay:
    """Blocks writes until a target stream (FFmpeg stdin) is attached.

    The video capture thread discovers frame dimensions before the
    FFmpeg command can be built.  This relay lets the capture thread
    pause between dimension discovery and the first frame write until
    FFmpeg is ready.
    """

    def __init__(self) -> None:
        self._target = None
        self._ready = threading.Event()

    def wait_ready(self, timeout: float = 30.0) -> bool:
        return self._ready.wait(timeout=timeout)

    def set_target(self, target) -> None:
        self._target = target
        self._ready.set()

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if not self._ready.wait(timeout=30):
            raise CaptureError("Timed out waiting for FFmpeg.")
        try:
            self._target.write(data)
            return len(data)
        except (BrokenPipeError, OSError, ValueError):
            raise BrokenPipeError("FFmpeg pipe closed")

    def flush(self) -> None:
        if self._target is not None:
            try:
                self._target.flush()
            except Exception:
                pass

    @property
    def closed(self) -> bool:
        if self._target is not None:
            return getattr(self._target, "closed", False)
        return False


# ====================================================================
# Encoder detection
# ====================================================================

def _pick_video_encoder(
    ffmpeg_path: str,
) -> tuple[str, list[str]]:
    """Return ``(encoder_name, extra_flags)`` for the best H.264 encoder.

    Tries GPU-accelerated encoders first (NVENC → QSV → AMF),
    falling back to libx264 with ultrafast preset.  The result is
    cached so subsequent recordings skip the probe.
    """
    global _hw_encoder_cache, _hw_encoder_tested

    if not _hw_encoder_tested:
        _hw_encoder_tested = True
        for enc in ("h264_nvenc", "h264_qsv", "h264_amf"):
            try:
                r = subprocess.run(
                    [
                        ffmpeg_path, "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
                        "-c:v", enc, "-f", "null", "-",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if r.returncode == 0:
                    _hw_encoder_cache = enc
                    break
            except Exception:
                continue

    if _hw_encoder_cache == "h264_nvenc":
        return ("h264_nvenc", ["-preset", "p1"])
    if _hw_encoder_cache == "h264_qsv":
        return ("h264_qsv", ["-preset", "veryfast"])
    if _hw_encoder_cache == "h264_amf":
        return ("h264_amf", [])
    return ("libx264", ["-preset", "ultrafast"])
