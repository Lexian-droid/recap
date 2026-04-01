"""recap.platforms.linux – video capture using X11 (XGetImage / XShmGetImage).

Captures screen frames via X11 ``XGetImage`` on the root window (monitor)
or a specific window, and writes raw BGRA32 data to a writable binary
stream (typically feeding FFmpeg).

Wayland-only sessions are not supported for direct capture; the user is
guided to use X11/XWayland.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import threading
import time
from typing import BinaryIO, Optional

from recap.exceptions import VideoCaptureError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# X11 library loading
# ---------------------------------------------------------------------------

_x11 = None
_x11_loaded = False


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
    ]


# X11 constants
ZPixmap = 2
AllPlanes = 0xFFFFFFFF
InputOutput = 1


def _load_x11():
    global _x11, _x11_loaded
    if _x11_loaded:
        return _x11
    _x11_loaded = True

    lib_path = ctypes.util.find_library("X11")
    if lib_path is None:
        lib_path = "libX11.so.6"
    try:
        _x11 = ctypes.cdll.LoadLibrary(lib_path)
        _setup_x11_video(_x11)
    except OSError:
        _x11 = None
    return _x11


def _setup_x11_video(x11) -> None:
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p

    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int

    x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x11.XDefaultScreen.restype = ctypes.c_int

    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong

    x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XDisplayWidth.restype = ctypes.c_int

    x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XDisplayHeight.restype = ctypes.c_int

    x11.XGetImage.argtypes = [
        ctypes.c_void_p,  # display
        ctypes.c_ulong,   # drawable
        ctypes.c_int,     # x
        ctypes.c_int,     # y
        ctypes.c_uint,    # width
        ctypes.c_uint,    # height
        ctypes.c_ulong,   # plane_mask
        ctypes.c_int,     # format
    ]
    x11.XGetImage.restype = ctypes.POINTER(XImage)

    x11.XDestroyImage.argtypes = [ctypes.POINTER(XImage)]
    x11.XDestroyImage.restype = ctypes.c_int

    x11.XGetWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
    ]
    x11.XGetWindowAttributes.restype = ctypes.c_int

    x11.XGetGeometry.argtypes = [
        ctypes.c_void_p,                    # display
        ctypes.c_ulong,                     # drawable
        ctypes.POINTER(ctypes.c_ulong),     # root_return
        ctypes.POINTER(ctypes.c_int),       # x_return
        ctypes.POINTER(ctypes.c_int),       # y_return
        ctypes.POINTER(ctypes.c_uint),      # width_return
        ctypes.POINTER(ctypes.c_uint),      # height_return
        ctypes.POINTER(ctypes.c_uint),      # border_width_return
        ctypes.POINTER(ctypes.c_uint),      # depth_return
    ]
    x11.XGetGeometry.restype = ctypes.c_int

    x11.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p,                    # display
        ctypes.c_ulong,                     # src_w
        ctypes.c_ulong,                     # dest_w
        ctypes.c_int,                       # src_x
        ctypes.c_int,                       # src_y
        ctypes.POINTER(ctypes.c_int),       # dest_x_return
        ctypes.POINTER(ctypes.c_int),       # dest_y_return
        ctypes.POINTER(ctypes.c_ulong),     # child_return
    ]
    x11.XTranslateCoordinates.restype = ctypes.c_int


def _ensure_x11():
    x11 = _load_x11()
    if x11 is None:
        ds = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if ds == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            raise VideoCaptureError(
                "Direct screen capture is not supported under Wayland. "
                "Options:\n"
                "  • Set DISPLAY=:0 to use X11/XWayland\n"
                "  • Switch to an X11 session\n"
                "  • Run your application with XWayland compatibility"
            )
        raise VideoCaptureError(
            "X11 library (libX11.so) not available. "
            "Install X11 development libraries: sudo apt install libx11-6"
        )
    return x11


def _ximage_to_bgra(image_p: ctypes.POINTER, width: int, height: int) -> bytes:
    """Convert an XImage to BGRA32 bytes.

    Most modern X11 visuals use 32-bit depth with BGRA byte order on
    little-endian systems.  This function ensures the output is
    consistently BGRA32.
    """
    image = image_p.contents
    total_bytes = image.bytes_per_line * height
    raw = ctypes.string_at(image.data, total_bytes)

    # If bytes_per_line == width * 4 and bits_per_pixel == 32,
    # the data is already in the correct BGRA layout on little-endian
    if image.bits_per_pixel == 32 and image.bytes_per_line == width * 4:
        return raw

    # Handle stride padding: extract only width*4 bytes per row
    if image.bits_per_pixel == 32:
        row_bytes = width * 4
        rows = []
        for y in range(height):
            offset = y * image.bytes_per_line
            rows.append(raw[offset:offset + row_bytes])
        return b"".join(rows)

    # 24-bit: convert to 32-bit BGRA
    if image.bits_per_pixel == 24:
        row_bytes_24 = image.bytes_per_line
        result = bytearray(width * height * 4)
        for y in range(height):
            src_off = y * row_bytes_24
            dst_off = y * width * 4
            for x in range(width):
                s = src_off + x * 3
                d = dst_off + x * 4
                result[d:d + 3] = raw[s:s + 3]
                result[d + 3] = 0xFF
        return bytes(result)

    raise VideoCaptureError(
        f"Unsupported X11 pixel format: {image.bits_per_pixel} bpp"
    )


# =========================================================================
# VideoCapture
# =========================================================================


class VideoCapture:
    """Capture video frames on Linux using X11."""

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

    @staticmethod
    def measure_achievable_fps(
        monitor_index: Optional[int] = None,
        window_handle: Optional[int] = None,
        target_fps: int = 60,
    ) -> int:
        """Measure achievable FPS for X11 capture."""
        try:
            x11 = _ensure_x11()
            display = x11.XOpenDisplay(None)
            if not display:
                return target_fps

            try:
                root = x11.XDefaultRootWindow(display)

                if window_handle is not None:
                    drawable = window_handle
                else:
                    drawable = root

                # Determine capture region
                root_ret = ctypes.c_ulong()
                x_ret = ctypes.c_int()
                y_ret = ctypes.c_int()
                w_ret = ctypes.c_uint()
                h_ret = ctypes.c_uint()
                bw_ret = ctypes.c_uint()
                d_ret = ctypes.c_uint()
                x11.XGetGeometry(
                    display, drawable,
                    ctypes.byref(root_ret),
                    ctypes.byref(x_ret), ctypes.byref(y_ret),
                    ctypes.byref(w_ret), ctypes.byref(h_ret),
                    ctypes.byref(bw_ret), ctypes.byref(d_ret),
                )
                w, h = w_ret.value, h_ret.value
                if w == 0 or h == 0:
                    return target_fps

                timings: list[float] = []
                for _ in range(15):
                    t0 = time.perf_counter()
                    img = x11.XGetImage(display, drawable, 0, 0, w, h, AllPlanes, ZPixmap)
                    t1 = time.perf_counter()
                    if img:
                        x11.XDestroyImage(img)
                    timings.append(t1 - t0)

                avg = sum(timings[5:]) / len(timings[5:])
                achievable = max(15, int(1.0 / avg))
                return target_fps if achievable >= target_fps * 0.9 else achievable
            finally:
                x11.XCloseDisplay(display)
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
        x11 = _ensure_x11()
        display = x11.XOpenDisplay(None)
        if not display:
            raise VideoCaptureError("Cannot open X display.")

        try:
            from recap.platforms.linux.discovery import list_monitors

            monitors = list_monitors()
            idx = self._monitor_index if self._monitor_index is not None else 0
            if idx >= len(monitors):
                raise VideoCaptureError(
                    f"Monitor index {idx} not found (have {len(monitors)})."
                )
            mon = monitors[idx]
            self._width = mon.width
            self._height = mon.height
            root = x11.XDefaultRootWindow(display)

            log.info(
                "Monitor %d: %s %dx%d @ (%d,%d)",
                idx, mon.name, self._width, self._height, mon.x, mon.y,
            )
            self._ready_event.set()

            frame_interval = 1.0 / self._fps
            next_frame: Optional[float] = None

            if hasattr(self._stream, "wait_ready"):
                if not self._stream.wait_ready(timeout=30):
                    raise VideoCaptureError("Timed out waiting for video pipe.")

            while not self._stop_event.is_set():
                image_p = x11.XGetImage(
                    display, root,
                    mon.x, mon.y,
                    self._width, self._height,
                    AllPlanes, ZPixmap,
                )
                if not image_p:
                    continue

                try:
                    data = _ximage_to_bgra(image_p, self._width, self._height)
                finally:
                    x11.XDestroyImage(image_p)

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
        finally:
            x11.XCloseDisplay(display)

    def _capture_window(self, window_id: int) -> None:
        x11 = _ensure_x11()
        display = x11.XOpenDisplay(None)
        if not display:
            raise VideoCaptureError("Cannot open X display.")

        try:
            # Get window geometry
            root_ret = ctypes.c_ulong()
            x_ret = ctypes.c_int()
            y_ret = ctypes.c_int()
            w_ret = ctypes.c_uint()
            h_ret = ctypes.c_uint()
            bw_ret = ctypes.c_uint()
            d_ret = ctypes.c_uint()

            status = x11.XGetGeometry(
                display, window_id,
                ctypes.byref(root_ret),
                ctypes.byref(x_ret), ctypes.byref(y_ret),
                ctypes.byref(w_ret), ctypes.byref(h_ret),
                ctypes.byref(bw_ret), ctypes.byref(d_ret),
            )
            if not status:
                raise VideoCaptureError(
                    f"Window {window_id:#x} not found or invalid."
                )

            self._width = w_ret.value
            self._height = h_ret.value

            if self._width <= 0 or self._height <= 0:
                raise VideoCaptureError(
                    f"Window has no client area ({self._width}x{self._height}). "
                    "It may be minimised."
                )

            log.info(
                "Window capture (X11): ID=%#x %dx%d",
                window_id, self._width, self._height,
            )
            self._ready_event.set()

            frame_interval = 1.0 / self._fps
            next_frame: Optional[float] = None

            if hasattr(self._stream, "wait_ready"):
                if not self._stream.wait_ready(timeout=30):
                    raise VideoCaptureError("Timed out waiting for video pipe.")

            while not self._stop_event.is_set():
                image_p = x11.XGetImage(
                    display, window_id,
                    0, 0,
                    self._width, self._height,
                    AllPlanes, ZPixmap,
                )
                if not image_p:
                    continue

                try:
                    data = _ximage_to_bgra(image_p, self._width, self._height)
                finally:
                    x11.XDestroyImage(image_p)

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
        finally:
            x11.XCloseDisplay(display)
