"""recap.platforms.macos – video capture using CoreGraphics (Quartz).

Captures screen frames via CGDisplayCreateImage (monitor) or
CGWindowListCreateImage (window) and writes raw BGRA32 data to
a writable binary stream (typically feeding FFmpeg).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from typing import BinaryIO, Optional

from recap.exceptions import VideoCaptureError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CoreGraphics framework loading
# ---------------------------------------------------------------------------

_cg_lib = ctypes.util.find_library("CoreGraphics")
_cf_lib = ctypes.util.find_library("CoreFoundation")

_cg = ctypes.cdll.LoadLibrary(_cg_lib) if _cg_lib else None
_cf = ctypes.cdll.LoadLibrary(_cf_lib) if _cf_lib else None

# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------

CGDirectDisplayID = ctypes.c_uint32


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


_CGRectNull = CGRect(CGPoint(float("inf"), float("inf")), CGSize(0, 0))

# CG constants
kCGWindowListOptionIncludingWindow = 1 << 3
kCGWindowImageBoundsIgnoreFraming = 1 << 0
kCGWindowImageDefault = 0
kCGImageAlphaPremultipliedFirst = 2
kCGBitmapByteOrder32Little = 2 << 12
kCGBitmapInfoBGRA = kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little

# ---------------------------------------------------------------------------
# Function signatures
# ---------------------------------------------------------------------------


def _setup_cg() -> None:
    if _cg is None:
        return
    _cg.CGDisplayCreateImage.argtypes = [CGDirectDisplayID]
    _cg.CGDisplayCreateImage.restype = ctypes.c_void_p

    _cg.CGWindowListCreateImage.argtypes = [
        CGRect, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ]
    _cg.CGWindowListCreateImage.restype = ctypes.c_void_p

    _cg.CGImageGetWidth.argtypes = [ctypes.c_void_p]
    _cg.CGImageGetWidth.restype = ctypes.c_size_t

    _cg.CGImageGetHeight.argtypes = [ctypes.c_void_p]
    _cg.CGImageGetHeight.restype = ctypes.c_size_t

    _cg.CGImageRelease.argtypes = [ctypes.c_void_p]
    _cg.CGImageRelease.restype = None

    _cg.CGColorSpaceCreateDeviceRGB.argtypes = []
    _cg.CGColorSpaceCreateDeviceRGB.restype = ctypes.c_void_p

    _cg.CGColorSpaceRelease.argtypes = [ctypes.c_void_p]
    _cg.CGColorSpaceRelease.restype = None

    _cg.CGBitmapContextCreate.argtypes = [
        ctypes.c_void_p,  # data
        ctypes.c_size_t,  # width
        ctypes.c_size_t,  # height
        ctypes.c_size_t,  # bitsPerComponent
        ctypes.c_size_t,  # bytesPerRow
        ctypes.c_void_p,  # colorSpace
        ctypes.c_uint32,  # bitmapInfo
    ]
    _cg.CGBitmapContextCreate.restype = ctypes.c_void_p

    _cg.CGContextDrawImage.argtypes = [
        ctypes.c_void_p, CGRect, ctypes.c_void_p,
    ]
    _cg.CGContextDrawImage.restype = None

    _cg.CGContextRelease.argtypes = [ctypes.c_void_p]
    _cg.CGContextRelease.restype = None

    _cg.CGGetActiveDisplayList.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(CGDirectDisplayID),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    _cg.CGGetActiveDisplayList.restype = ctypes.c_int32

    _cg.CGDisplayBounds.argtypes = [CGDirectDisplayID]
    _cg.CGDisplayBounds.restype = CGRect

    _cg.CGDisplayIsMain.argtypes = [CGDirectDisplayID]
    _cg.CGDisplayIsMain.restype = ctypes.c_bool


_setup_cg()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_display_ids() -> list[int]:
    """Return a list of active CGDirectDisplayIDs."""
    max_displays = 32
    display_ids = (CGDirectDisplayID * max_displays)()
    count = ctypes.c_uint32()
    err = _cg.CGGetActiveDisplayList(max_displays, display_ids, ctypes.byref(count))
    if err != 0:
        raise VideoCaptureError(f"CGGetActiveDisplayList failed with error {err}")
    return [display_ids[i] for i in range(count.value)]


def _cgimage_to_bgra(image_ref: ctypes.c_void_p) -> tuple[bytes, int, int]:
    """Convert a CGImageRef to raw BGRA32 bytes. Returns (data, width, height)."""
    width = _cg.CGImageGetWidth(image_ref)
    height = _cg.CGImageGetHeight(image_ref)
    if width == 0 or height == 0:
        raise VideoCaptureError("CGImage has zero dimensions.")

    color_space = _cg.CGColorSpaceCreateDeviceRGB()
    bytes_per_row = width * 4
    buf_size = bytes_per_row * height
    buf = (ctypes.c_uint8 * buf_size)()
    context = _cg.CGBitmapContextCreate(
        ctypes.cast(buf, ctypes.c_void_p),
        width, height, 8, bytes_per_row,
        color_space, kCGBitmapInfoBGRA,
    )
    if not context:
        _cg.CGColorSpaceRelease(color_space)
        raise VideoCaptureError("CGBitmapContextCreate returned NULL.")

    rect = CGRect(CGPoint(0, 0), CGSize(width, height))
    _cg.CGContextDrawImage(context, rect, image_ref)
    data = bytes(buf)

    _cg.CGContextRelease(context)
    _cg.CGColorSpaceRelease(color_space)
    return data, width, height


# =========================================================================
# VideoCapture
# =========================================================================


class VideoCapture:
    """Capture video frames on macOS using CoreGraphics."""

    def __init__(
        self,
        output_stream: BinaryIO,
        *,
        fps: int = 30,
        monitor_index: Optional[int] = None,
        window_handle: Optional[int] = None,
    ) -> None:
        if _cg is None:
            raise VideoCaptureError(
                "CoreGraphics framework not available. "
                "Ensure you are running on macOS."
            )
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

    @staticmethod
    def measure_achievable_fps(
        monitor_index: Optional[int] = None,
        window_handle: Optional[int] = None,
        target_fps: int = 60,
    ) -> int:
        """Measure achievable FPS for CoreGraphics capture.

        Performs a quick benchmarking loop and returns the estimated FPS.
        """
        if _cg is None:
            return target_fps

        try:
            timings: list[float] = []
            for _ in range(15):
                t0 = time.perf_counter()
                if window_handle is not None:
                    img = _cg.CGWindowListCreateImage(
                        _CGRectNull,
                        kCGWindowListOptionIncludingWindow,
                        window_handle,
                        kCGWindowImageBoundsIgnoreFraming,
                    )
                else:
                    display_ids = _get_display_ids()
                    idx = monitor_index if monitor_index is not None else 0
                    if idx >= len(display_ids):
                        return target_fps
                    img = _cg.CGDisplayCreateImage(display_ids[idx])
                t1 = time.perf_counter()
                if img:
                    _cg.CGImageRelease(img)
                timings.append(t1 - t0)

            avg = sum(timings[5:]) / len(timings[5:])
            achievable = max(15, int(1.0 / avg))
            return target_fps if achievable >= target_fps * 0.9 else achievable
        except Exception:
            return target_fps

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
            target=self._capture_loop, daemon=True, name="recap-video",
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

    def _capture_monitor(self) -> None:
        display_ids = _get_display_ids()
        idx = self._monitor_index if self._monitor_index is not None else 0
        if idx >= len(display_ids):
            raise VideoCaptureError(
                f"Monitor index {idx} not found (have {len(display_ids)})."
            )
        display_id = display_ids[idx]

        # Get dimensions from a test capture
        test_img = _cg.CGDisplayCreateImage(display_id)
        if not test_img:
            raise VideoCaptureError(
                "Screen capture returned no data. On macOS, you must grant "
                "Screen Recording permission in System Settings → "
                "Privacy & Security → Screen Recording."
            )
        self._width = _cg.CGImageGetWidth(test_img)
        self._height = _cg.CGImageGetHeight(test_img)
        _cg.CGImageRelease(test_img)

        log.info(
            "Monitor %d: Display %d %dx%d",
            idx, display_id, self._width, self._height,
        )
        self._ready_event.set()

        frame_interval = 1.0 / self._fps
        next_frame: Optional[float] = None

        if hasattr(self._stream, "wait_ready"):
            if not self._stream.wait_ready(timeout=30):
                raise VideoCaptureError("Timed out waiting for video pipe.")

        while not self._stop_event.is_set():
            image_ref = _cg.CGDisplayCreateImage(display_id)
            if not image_ref:
                continue
            try:
                data, _, _ = _cgimage_to_bgra(image_ref)
            finally:
                _cg.CGImageRelease(image_ref)

            try:
                self._stream.write(data)
            except (BrokenPipeError, OSError):
                break

            now = time.perf_counter()
            if next_frame is None:
                next_frame = now
            next_frame += frame_interval
            remaining = next_frame - now
            if remaining > 0:
                time.sleep(remaining)

    def _capture_window(self, window_id: int) -> None:
        # Test capture to get dimensions
        test_img = _cg.CGWindowListCreateImage(
            _CGRectNull,
            kCGWindowListOptionIncludingWindow,
            window_id,
            kCGWindowImageBoundsIgnoreFraming,
        )
        if not test_img:
            raise VideoCaptureError(
                f"Cannot capture window {window_id}. The window may not exist, "
                "or Screen Recording permission may not be granted. "
                "Check System Settings → Privacy & Security → Screen Recording."
            )
        self._width = _cg.CGImageGetWidth(test_img)
        self._height = _cg.CGImageGetHeight(test_img)
        _cg.CGImageRelease(test_img)

        if self._width <= 0 or self._height <= 0:
            raise VideoCaptureError(
                f"Window has no visible area ({self._width}x{self._height}). "
                "It may be minimised."
            )

        log.info(
            "Window capture (CoreGraphics): ID=%d %dx%d",
            window_id, self._width, self._height,
        )
        self._ready_event.set()

        frame_interval = 1.0 / self._fps
        next_frame: Optional[float] = None

        if hasattr(self._stream, "wait_ready"):
            if not self._stream.wait_ready(timeout=30):
                raise VideoCaptureError("Timed out waiting for video pipe.")

        while not self._stop_event.is_set():
            image_ref = _cg.CGWindowListCreateImage(
                _CGRectNull,
                kCGWindowListOptionIncludingWindow,
                window_id,
                kCGWindowImageBoundsIgnoreFraming,
            )
            if not image_ref:
                continue
            try:
                data, _, _ = _cgimage_to_bgra(image_ref)
            finally:
                _cg.CGImageRelease(image_ref)

            try:
                self._stream.write(data)
            except (BrokenPipeError, OSError):
                break

            now = time.perf_counter()
            if next_frame is None:
                next_frame = now
            next_frame += frame_interval
            remaining = next_frame - now
            if remaining > 0:
                time.sleep(remaining)
