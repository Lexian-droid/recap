"""recap – video capture backends.

Provides frame capture for monitors and windows, streaming raw BGRA32
data to a writable binary stream (typically a named-pipe feeding FFmpeg).

Backend priority:
  Monitor capture → GDI BitBlt (reliable, works everywhere)
  Window capture  → PrintWindow (PW_RENDERFULLCONTENT)

The capture runs on a background thread.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import threading
import time
from typing import BinaryIO, Optional

from recap.exceptions import VideoCaptureError

log = logging.getLogger(__name__)

# GDI constants
SRCCOPY = 0x00CC0020
BI_RGB = 0
PW_RENDERFULLCONTENT = 0x00000002


class VideoCapture:
    """Capture video frames and write raw BGRA32 to *output_stream*."""

    def __init__(
        self,
        output_stream: BinaryIO,
        *,
        fps: int = 30,
        monitor_index: Optional[int] = None,
        window_handle: Optional[int] = None,
    ) -> None:
        self._stream = output_stream
        self._fps = fps
        self._monitor_index = monitor_index
        self._window_handle = window_handle
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._width: int = 0
        self._height: int = 0
        self._ready_event = threading.Event()

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="recap-video"
        )
        self._running = True
        self._thread.start()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready_event.wait(timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        try:
            if self._window_handle is not None:
                self._capture_window(self._window_handle)
            else:
                self._capture_monitor()
        except Exception as exc:
            log.error("Video capture error: %s", exc, exc_info=True)
            raise VideoCaptureError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Monitor capture – GDI BitBlt
    # ------------------------------------------------------------------

    def _capture_monitor(self) -> None:
        from recap.discovery import list_monitors

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        monitors = list_monitors()
        idx = self._monitor_index if self._monitor_index is not None else 0
        if idx >= len(monitors):
            raise VideoCaptureError(
                f"Monitor index {idx} not found (have {len(monitors)})."
            )
        mon = monitors[idx]
        self._width = mon.width
        self._height = mon.height
        log.info(
            "Monitor %d: %s %dx%d @ (%d,%d)",
            idx, mon.name, self._width, self._height, mon.x, mon.y,
        )
        self._ready_event.set()

        hdc_screen = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbm = gdi32.CreateCompatibleBitmap(hdc_screen, self._width, self._height)
        old_bm = gdi32.SelectObject(hdc_mem, hbm)

        bmi = _make_bitmapinfo(self._width, self._height)
        buf_size = self._width * self._height * 4
        buf = (ctypes.c_char * buf_size)()
        frame_interval = 1.0 / self._fps
        next_frame: Optional[float] = None

        # Wait for the pipe to connect before capturing the first frame.
        # This ensures video PTS 0 contains fresh screen content from the
        # same real-time instant as audio PTS 0 — not a frame that was
        # captured ~200 ms earlier while FFmpeg was still starting up.
        if hasattr(self._stream, 'wait_ready'):
            if not self._stream.wait_ready(timeout=30):
                raise VideoCaptureError("Timed out waiting for video pipe.")

        try:
            while not self._stop_event.is_set():
                gdi32.BitBlt(
                    hdc_mem, 0, 0,
                    self._width, self._height,
                    hdc_screen,
                    mon.x, mon.y,
                    SRCCOPY,
                )
                gdi32.GetDIBits(
                    hdc_mem, hbm, 0, self._height,
                    ctypes.byref(buf), ctypes.byref(bmi), 0,
                )

                try:
                    self._stream.write(bytes(buf))
                except (BrokenPipeError, OSError):
                    break

                # Deadline-based timing: advance next_frame by exactly
                # one frame interval so any overshoot is recovered on
                # the next iteration.
                now = time.perf_counter()
                if next_frame is None:
                    next_frame = now
                next_frame += frame_interval
                remaining = next_frame - now
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            gdi32.SelectObject(hdc_mem, old_bm)
            gdi32.DeleteObject(hbm)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(0, hdc_screen)

    # ------------------------------------------------------------------
    # Window capture – PrintWindow (PW_RENDERFULLCONTENT)
    # ------------------------------------------------------------------

    def _capture_window(self, hwnd: int) -> None:
        """Capture using PrintWindow with PW_RENDERFULLCONTENT.

        This renders the window into a memory DC even when the window is
        covered, off-screen, or partially visible.  It is *not* a
        desktop-crop approach.
        """
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        if not user32.IsWindow(hwnd):
            raise VideoCaptureError(f"HWND {hwnd:#x} is not a valid window.")

        rect = ctypes.wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        self._width = rect.right - rect.left
        self._height = rect.bottom - rect.top

        if self._width <= 0 or self._height <= 0:
            raise VideoCaptureError(
                f"Window has no client area ({self._width}x{self._height}). "
                "It may be minimised."
            )

        log.info(
            "Window capture (PrintWindow): HWND=%#x %dx%d",
            hwnd, self._width, self._height,
        )
        self._ready_event.set()

        hdc_window = user32.GetDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbm = gdi32.CreateCompatibleBitmap(hdc_window, self._width, self._height)
        old_bm = gdi32.SelectObject(hdc_mem, hbm)

        bmi = _make_bitmapinfo(self._width, self._height)
        buf_size = self._width * self._height * 4
        buf = (ctypes.c_char * buf_size)()
        frame_interval = 1.0 / self._fps
        next_frame: Optional[float] = None

        # Wait for the pipe to connect before the first frame capture.
        if hasattr(self._stream, 'wait_ready'):
            if not self._stream.wait_ready(timeout=30):
                raise VideoCaptureError("Timed out waiting for video pipe.")

        try:
            while not self._stop_event.is_set():
                user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
                gdi32.GetDIBits(
                    hdc_mem, hbm, 0, self._height,
                    ctypes.byref(buf), ctypes.byref(bmi), 0,
                )

                try:
                    self._stream.write(bytes(buf))
                except (BrokenPipeError, OSError):
                    break

                now = time.perf_counter()
                if next_frame is None:
                    next_frame = now
                next_frame += frame_interval
                remaining = next_frame - now
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            gdi32.SelectObject(hdc_mem, old_bm)
            gdi32.DeleteObject(hbm)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_window)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_uint),
        ("biSizeImage", ctypes.c_uint),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint),
        ("biClrImportant", ctypes.c_uint),
    ]


def _make_bitmapinfo(width: int, height: int) -> _BITMAPINFOHEADER:
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height  # top-down DIB
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = BI_RGB
    bmi.biSizeImage = width * height * 4
    return bmi
