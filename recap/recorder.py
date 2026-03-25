"""recap – recording session controller.

Orchestrates video capture, audio capture, and the FFmpeg encoding
subprocess.  This is the main entry-point for the library API.

Architecture
------------
All modes use a **single FFmpeg process**.

- Video + audio: both streams arrive via Windows named pipes so FFmpeg
  can read them concurrently without deadlocking on sequential probing.
- Video-only / audio-only: the single stream goes through stdin.

Both inputs use natural frame-count timestamps so FFmpeg can reliably
interleave them.  Pre-connection audio is discarded so both streams
start from the same instant.  The video capture loop uses deadline-
based timing to hold the declared frame rate accurately.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import enum
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO, Optional

from recap.config import RecordingConfig
from recap.exceptions import (
    CaptureError,
    FFmpegError,
    RecapError,
)
from recap.ffmpeg import FFmpegInfo, find_ffmpeg

log = logging.getLogger(__name__)

# ── Windows named-pipe constants ────────────────────────────────────
_PIPE_ACCESS_OUTBOUND = 0x00000002
_PIPE_TYPE_BYTE       = 0x00000000
_PIPE_WAIT            = 0x00000000
_INVALID_HANDLE       = ctypes.wintypes.HANDLE(-1).value
_kernel32             = ctypes.windll.kernel32

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
        self._pipe_writers: list[_PipeWriter] = []
        self._duration_timer: Optional[threading.Timer] = None
        self._stop_event = threading.Event()
        self._error: Optional[Exception] = None

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
        """Wait for recording to finish and return the FFmpeg exit code.

        Returns 0 on success, nonzero otherwise.  After this returns
        the state is either ``STOPPED`` or ``ERROR``.
        """
        # 0. Block until stop() has been called (e.g. by a duration
        #    timer).  If stop() was already called, this returns
        #    immediately.  On timeout, force a stop.
        if not self._stop_event.wait(timeout=timeout):
            self.stop()

        # 1. Close every data channel so FFmpeg sees EOF.
        #    This must happen before waiting for capture threads,
        #    because FFmpeg stalls reads on one pipe when the other
        #    stream has no data — which blocks WriteFile in the
        #    capture thread, preventing it from exiting.
        for pw in self._pipe_writers:
            try:
                pw.close()
            except Exception:
                pass
        self._pipe_writers.clear()

        if self._ffmpeg_proc is not None and self._ffmpeg_proc.stdin:
            try:
                self._ffmpeg_proc.stdin.close()
            except Exception:
                pass

        # 2. Wait for capture threads to exit (they will see
        #    BrokenPipeError from the closed channels).
        if self._video_capture is not None:
            self._video_capture.wait(timeout=timeout)
        if self._audio_capture is not None:
            self._audio_capture.wait(timeout=timeout)

        # 3. Wait for FFmpeg to finalize the container.
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
                        f"FFmpeg exited with code {rc}"
                    )
                return rc

        with self._lock:
            self._state = RecorderState.STOPPED
        log.info("Recording saved -> %s", self._config.output)
        return 0

    # ================================================================
    # Internal – launch pipeline
    # ================================================================

    def _launch(self) -> None:
        """Discover capture parameters, build the FFmpeg command, go."""
        has_video = self._config.capture_video
        has_audio = (
            self._config.capture_audio
            and not self._config.video_only
            and not self._config.no_audio
        )

        # ── resolve video target ────────────────────────────────────
        vid_kwargs: dict = {}
        if has_video:
            if self._config.window_handle is not None:
                vid_kwargs["window_handle"] = self._config.window_handle
            elif self._config.window_title is not None:
                from recap.discovery import find_window_by_title

                win = find_window_by_title(self._config.window_title)
                if win is None:
                    raise CaptureError(
                        f"No visible window matching "
                        f"'{self._config.window_title}'"
                    )
                vid_kwargs["window_handle"] = win.handle
            else:
                vid_kwargs["monitor_index"] = (
                    self._config.monitor
                    if self._config.monitor is not None
                    else 0
                )

        # ── create data relays ──────────────────────────────────────
        video_relay: Optional[_DataRelay] = None
        audio_relay: Optional[_DataRelay] = None

        if has_video:
            # Blocking mode: the capture thread pauses on the first
            # write() until the pipe is connected.  Video frames are
            # large so buffering them wastes memory needlessly.
            video_relay = _DataRelay(buffered=False)
        if has_audio:
            # Buffered mode: the WASAPI thread never stalls, so its
            # internal ring-buffer cannot overflow.  Buffered audio is
            # flushed to the pipe as soon as it connects.
            audio_relay = _DataRelay(buffered=True)

        # ── start captures to discover format info ──────────────────
        width = height = 0
        if has_video:
            from recap.video import VideoCapture

            self._video_capture = VideoCapture(
                video_relay, fps=self._config.fps, **vid_kwargs,
            )
            self._video_capture.start()
            if not self._video_capture.wait_ready(timeout=10):
                raise CaptureError("Video capture did not become ready.")
            width = self._video_capture.width
            height = self._video_capture.height

        audio_fmt, audio_sr, audio_ch = "f32le", 48000, 2
        if has_audio:
            from recap.audio import AudioCapture

            self._audio_capture = AudioCapture(audio_relay)
            self._audio_capture.start()
            if not self._audio_capture.wait_format_ready(timeout=10):
                raise CaptureError("Audio capture did not become ready.")
            audio_fmt = (
                "f32le"
                if self._audio_capture.bits_per_sample == 32
                else "s16le"
            )
            audio_sr = self._audio_capture.sample_rate
            audio_ch = self._audio_capture.channels

        # ── pick video encoder ──────────────────────────────────────
        venc, venc_opts = _pick_video_encoder(
            str(self._ffmpeg_info.path),
        )
        log.info("Video encoder: %s", venc)

        # ── build FFmpeg command & start ────────────────────────────
        ffmpeg = str(self._ffmpeg_info.path)
        ow = "-y" if self._config.overwrite else "-n"

        if has_video and has_audio:
            self._start_combined(
                ffmpeg, ow, width, height,
                audio_fmt, audio_sr, audio_ch,
                video_relay, audio_relay,
                venc, venc_opts,
            )
        elif has_video:
            self._start_video_only(
                ffmpeg, ow, width, height, video_relay,
                venc, venc_opts,
            )
        elif has_audio:
            self._start_audio_only(
                ffmpeg, ow, audio_fmt, audio_sr, audio_ch, audio_relay,
            )

        # ── optional duration cap ───────────────────────────────────
        if self._config.duration is not None:
            self._duration_timer = threading.Timer(
                self._config.duration, self.stop,
            )
            self._duration_timer.daemon = True
            self._duration_timer.start()

    # ── combined video + audio ──────────────────────────────────────

    def _start_combined(
        self,
        ffmpeg: str,
        ow: str,
        w: int,
        h: int,
        afmt: str,
        asr: int,
        ach: int,
        video_relay: _DataRelay,
        audio_relay: _DataRelay,
        venc: str = "libx264",
        venc_opts: list[str] | None = None,
    ) -> None:
        """One FFmpeg, two named pipes (video + audio)."""
        base = f"\\\\.\\pipe\\recap_{os.getpid()}_{id(self)}"
        vpipe = f"{base}_v"
        apipe = f"{base}_a"

        # Large out-buffer for video to absorb big frames without
        # blocking on every write while FFmpeg processes them.
        vh = _create_named_pipe(vpipe, buf=1 << 20)
        ah = _create_named_pipe(apipe)

        cmd = [
            ffmpeg, ow,
            # video input — skip probing (format is specified)
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "rawvideo", "-pixel_format", "bgra",
            "-video_size", f"{w}x{h}",
            "-framerate", str(self._config.fps),
            "-thread_queue_size", "64",
            "-i", vpipe,
            # audio input — skip probing (format is specified)
            "-probesize", "32", "-analyzeduration", "0",
            "-f", afmt, "-ar", str(asr), "-ac", str(ach),
            "-thread_queue_size", "1024",
            "-i", apipe,
            # encoding
            "-c:v", venc, *(venc_opts or []),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(self._config.output),
        ]
        log.debug("FFmpeg command: %s", cmd)

        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        # FFmpeg opens each -i sequentially.  Each ConnectNamedPipe
        # unblocks as soon as FFmpeg reaches that input.
        # Pre-connection audio is discarded (not flushed) because
        # wallclock timestamps would compress the burst into a
        # near-zero PTS span, distorting playback.
        def _wire(handle, relay, label):
            _connect_named_pipe(handle)
            pw = _PipeWriter(handle)
            self._pipe_writers.append(pw)
            relay.set_target(pw)
            log.debug("%s pipe connected", label)

        threading.Thread(
            target=_wire, args=(vh, video_relay, "Video"),
            daemon=True, name="recap-vpipe",
        ).start()
        threading.Thread(
            target=_wire, args=(ah, audio_relay, "Audio"),
            daemon=True, name="recap-apipe",
        ).start()

    # ── video only ──────────────────────────────────────────────────

    def _start_video_only(
        self, ffmpeg: str, ow: str, w: int, h: int,
        relay: _DataRelay,
        venc: str = "libx264",
        venc_opts: list[str] | None = None,
    ) -> None:
        cmd = [
            ffmpeg, ow,
            "-f", "rawvideo", "-pixel_format", "bgra",
            "-video_size", f"{w}x{h}",
            "-framerate", str(self._config.fps),
            "-i", "pipe:0",
            "-c:v", venc, *(venc_opts or []),
            "-pix_fmt", "yuv420p", "-an",
            str(self._config.output),
        ]
        log.debug("FFmpeg command: %s", cmd)
        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        relay.set_target(self._ffmpeg_proc.stdin)

    # ── audio only ──────────────────────────────────────────────────

    def _start_audio_only(
        self,
        ffmpeg: str,
        ow: str,
        afmt: str,
        asr: int,
        ach: int,
        relay: _DataRelay,
    ) -> None:
        cmd: list[str] = [
            ffmpeg, ow,
            "-f", afmt, "-ar", str(asr), "-ac", str(ach),
            "-i", "pipe:0",
        ]
        ext = Path(self._config.output).suffix.lower()
        if ext == ".wav":
            cmd += ["-c:a", "pcm_s16le"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd.append(str(self._config.output))
        log.debug("FFmpeg command: %s", cmd)

        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        relay.set_target(self._ffmpeg_proc.stdin)


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


# ====================================================================
# Data relay
# ====================================================================

class _DataRelay:
    """Thread-safe write relay between a capture thread and its target.

    *buffered=False* (video):
        The capture thread blocks on the first ``write()`` until
        ``set_target()`` is called.  This is fine for video because the
        capture thread generates frames at its own pace.

    *buffered=True* (audio):
        Writes are stored in memory until the target becomes available.
        The buffer is **discarded** on connection so only fresh audio
        (with correct wallclock timestamps from FFmpeg) enters the
        output.  This also prevents the WASAPI capture thread from
        stalling, which would cause its internal ring-buffer to
        overflow and drop samples.
    """

    def __init__(self, *, buffered: bool = False) -> None:
        self._target: Optional[BinaryIO] = None
        self._ready = threading.Event()
        self._buffered = buffered
        self._buf: list[bytes] = []
        self._lock = threading.Lock()

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """Block until a target has been attached (pipe connected).

        Used by the video capture loop to avoid writing a stale first
        frame: the loop waits here before the first BitBlt so that
        PTS 0 is current screen content rather than content captured
        during FFmpeg's startup delay.
        """
        return self._ready.wait(timeout=timeout)

    def set_target(self, target) -> None:
        """Attach the real target.

        In buffered mode the pre-connection buffer is **discarded**.
        With ``-use_wallclock_as_timestamps`` on the FFmpeg input,
        only data written *after* this call gets meaningful PTS
        values, so flushing old audio would create a burst with
        compressed timestamps.
        """
        if self._buffered:
            with self._lock:
                self._target = target
                self._buf.clear()
        else:
            self._target = target
        self._ready.set()

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self._buffered:
            with self._lock:
                if self._target is not None:
                    try:
                        self._target.write(data)
                    except (BrokenPipeError, OSError):
                        raise BrokenPipeError("Target closed")
                else:
                    self._buf.append(bytes(data))
                return len(data)

        # Blocking mode — wait until a target is wired up.
        if not self._ready.wait(timeout=30):
            raise CaptureError("Timed out waiting for FFmpeg.")
        try:
            self._target.write(data)
            return len(data)
        except (BrokenPipeError, OSError):
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
# Windows named-pipe helpers
# ====================================================================

def _create_named_pipe(name: str, *, buf: int = 65536) -> int:
    """Create a byte-mode, outbound-only named pipe (server side)."""
    h = _kernel32.CreateNamedPipeW(
        name,
        _PIPE_ACCESS_OUTBOUND,
        _PIPE_TYPE_BYTE | _PIPE_WAIT,
        1,     # max instances
        buf,   # out buffer size
        0,     # in buffer (unused for outbound)
        0,     # default timeout
        None,  # security attributes
    )
    if h == _INVALID_HANDLE:
        raise CaptureError(f"CreateNamedPipeW failed: {name}")
    return h


def _connect_named_pipe(handle: int) -> None:
    """Block until a client (FFmpeg) connects to the pipe."""
    if not _kernel32.ConnectNamedPipe(handle, None):
        err = ctypes.GetLastError()
        # ERROR_PIPE_CONNECTED (535) = client connected before the call
        if err != 535:
            raise CaptureError(f"ConnectNamedPipe failed (error {err})")


class _PipeWriter:
    """File-like wrapper around a Windows named-pipe HANDLE."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if isinstance(data, memoryview):
            data = bytes(data)
        written = ctypes.wintypes.DWORD()
        if not _kernel32.WriteFile(
            self._handle, data, len(data),
            ctypes.byref(written), None,
        ):
            raise BrokenPipeError("WriteFile on named pipe failed")
        return written.value

    def flush(self) -> None:
        if self._handle is not None:
            _kernel32.FlushFileBuffers(self._handle)

    def close(self) -> None:
        h = self._handle
        if h is not None:
            self._handle = None
            # Don't FlushFileBuffers — it blocks if a capture thread has
            # a pending WriteFile and FFmpeg has stalled reads.
            _kernel32.DisconnectNamedPipe(h)
            _kernel32.CloseHandle(h)

    @property
    def closed(self) -> bool:
        return self._handle is None
