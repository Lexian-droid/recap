"""recap.platforms.macos – monitor, window, and audio device discovery.

Uses CoreGraphics (Quartz) via ctypes for monitor and window enumeration,
and FFmpeg avfoundation for audio device discovery.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import re
import subprocess
from typing import Optional

from recap.discovery import AudioDeviceInfo, MonitorInfo, WindowInfo
from recap.exceptions import CaptureError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CoreGraphics / CoreFoundation framework loading
# ---------------------------------------------------------------------------

_cg_lib = ctypes.util.find_library("CoreGraphics")
_cf_lib = ctypes.util.find_library("CoreFoundation")

_cg = ctypes.cdll.LoadLibrary(_cg_lib) if _cg_lib else None
_cf = ctypes.cdll.LoadLibrary(_cf_lib) if _cf_lib else None

# ---------------------------------------------------------------------------
# CoreGraphics type definitions
# ---------------------------------------------------------------------------

CGDirectDisplayID = ctypes.c_uint32


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


# CGRectNull equivalent for CGWindowListCreateImage
_CGRectNull = CGRect(CGPoint(float("inf"), float("inf")), CGSize(0, 0))

# CG constants
kCGWindowListOptionAll = 0
kCGWindowListOptionOnScreenOnly = 1 << 0
kCGWindowListExcludeDesktopElements = 1 << 4
kCGWindowListOptionIncludingWindow = 1 << 3
kCGNullWindowID = 0

# CF constants
kCFNumberSInt32Type = 3
kCFNumberSInt64Type = 4
kCFStringEncodingUTF8 = 0x08000100

# ---------------------------------------------------------------------------
# Function signature setup
# ---------------------------------------------------------------------------


def _setup_cg() -> None:
    if _cg is None:
        return
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

    _cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    _cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p


def _setup_cf() -> None:
    if _cf is None:
        return
    _cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    _cf.CFArrayGetCount.restype = ctypes.c_long

    _cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    _cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p

    _cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _cf.CFDictionaryGetValue.restype = ctypes.c_void_p

    _cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
    ]
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p

    _cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]
    _cf.CFStringGetCString.restype = ctypes.c_bool

    _cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    _cf.CFStringGetLength.restype = ctypes.c_long

    _cf.CFNumberGetValue.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
    ]
    _cf.CFNumberGetValue.restype = ctypes.c_bool

    _cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    _cf.CFBooleanGetValue.restype = ctypes.c_bool

    _cf.CFRelease.argtypes = [ctypes.c_void_p]
    _cf.CFRelease.restype = None


_setup_cg()
_setup_cf()

# ---------------------------------------------------------------------------
# CF helper functions
# ---------------------------------------------------------------------------


def _cfstr(s: str) -> ctypes.c_void_p:
    """Create a CFString from a Python string."""
    return _cf.CFStringCreateWithCString(
        None, s.encode("utf-8"), kCFStringEncodingUTF8,
    )


def _cfdict_get_string(d: ctypes.c_void_p, key: ctypes.c_void_p) -> str:
    value = _cf.CFDictionaryGetValue(d, key)
    if not value:
        return ""
    buf = ctypes.create_string_buffer(1024)
    if _cf.CFStringGetCString(value, buf, 1024, kCFStringEncodingUTF8):
        return buf.value.decode("utf-8", errors="replace")
    return ""


def _cfdict_get_int(d: ctypes.c_void_p, key: ctypes.c_void_p) -> Optional[int]:
    value = _cf.CFDictionaryGetValue(d, key)
    if not value:
        return None
    result = ctypes.c_int64()
    if _cf.CFNumberGetValue(value, kCFNumberSInt64Type, ctypes.byref(result)):
        return result.value
    result32 = ctypes.c_int32()
    if _cf.CFNumberGetValue(value, kCFNumberSInt32Type, ctypes.byref(result32)):
        return result32.value
    return None


def _cfdict_get_bool(d: ctypes.c_void_p, key: ctypes.c_void_p) -> bool:
    value = _cf.CFDictionaryGetValue(d, key)
    if not value:
        return False
    return bool(_cf.CFBooleanGetValue(value))


# CGWindowInfo dictionary keys (lazily created)
_kCGWindowNumber: ctypes.c_void_p = None  # type: ignore[assignment]
_kCGWindowName: ctypes.c_void_p = None  # type: ignore[assignment]
_kCGWindowOwnerName: ctypes.c_void_p = None  # type: ignore[assignment]
_kCGWindowOwnerPID: ctypes.c_void_p = None  # type: ignore[assignment]
_kCGWindowIsOnscreen: ctypes.c_void_p = None  # type: ignore[assignment]
_kCGWindowLayer: ctypes.c_void_p = None  # type: ignore[assignment]


def _ensure_window_keys() -> None:
    global _kCGWindowNumber, _kCGWindowName, _kCGWindowOwnerName
    global _kCGWindowOwnerPID, _kCGWindowIsOnscreen, _kCGWindowLayer
    if _kCGWindowNumber is not None:
        return
    _kCGWindowNumber = _cfstr("kCGWindowNumber")
    _kCGWindowName = _cfstr("kCGWindowName")
    _kCGWindowOwnerName = _cfstr("kCGWindowOwnerName")
    _kCGWindowOwnerPID = _cfstr("kCGWindowOwnerPID")
    _kCGWindowIsOnscreen = _cfstr("kCGWindowIsOnscreen")
    _kCGWindowLayer = _cfstr("kCGWindowLayer")


# =========================================================================
# Public API
# =========================================================================


def list_monitors() -> list[MonitorInfo]:
    """Enumerate connected displays using CoreGraphics."""
    if _cg is None:
        raise CaptureError(
            "CoreGraphics framework not available. "
            "Ensure you are running on macOS."
        )

    max_displays = 32
    display_ids = (CGDirectDisplayID * max_displays)()
    count = ctypes.c_uint32()

    err = _cg.CGGetActiveDisplayList(max_displays, display_ids, ctypes.byref(count))
    if err != 0:
        raise CaptureError(f"CGGetActiveDisplayList failed with error {err}")

    monitors: list[MonitorInfo] = []
    for i in range(count.value):
        display_id = display_ids[i]
        bounds = _cg.CGDisplayBounds(display_id)
        is_main = _cg.CGDisplayIsMain(display_id)
        monitors.append(MonitorInfo(
            index=i,
            name=f"Display {display_id}",
            x=int(bounds.origin.x),
            y=int(bounds.origin.y),
            width=int(bounds.size.width),
            height=int(bounds.size.height),
            is_primary=bool(is_main),
        ))

    return monitors


def list_windows(*, include_hidden: bool = False) -> list[WindowInfo]:
    """Enumerate visible windows using CoreGraphics."""
    if _cg is None or _cf is None:
        raise CaptureError(
            "CoreGraphics/CoreFoundation frameworks not available."
        )

    _ensure_window_keys()

    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    if include_hidden:
        options = kCGWindowListOptionAll

    window_list = _cg.CGWindowListCopyWindowInfo(options, kCGNullWindowID)
    if not window_list:
        return []

    windows: list[WindowInfo] = []
    try:
        count = _cf.CFArrayGetCount(window_list)
        for i in range(count):
            info = _cf.CFArrayGetValueAtIndex(window_list, i)

            window_id = _cfdict_get_int(info, _kCGWindowNumber)
            if window_id is None:
                continue

            name = _cfdict_get_string(info, _kCGWindowName)
            owner = _cfdict_get_string(info, _kCGWindowOwnerName)
            pid = _cfdict_get_int(info, _kCGWindowOwnerPID) or 0
            on_screen = _cfdict_get_bool(info, _kCGWindowIsOnscreen)
            layer = _cfdict_get_int(info, _kCGWindowLayer) or 0

            # Skip windows without titles unless include_hidden
            if not name and not include_hidden:
                continue

            # Layer 0 = normal windows; other layers are system UI
            if layer != 0 and not include_hidden:
                continue

            title = f"{owner}: {name}" if name else owner

            windows.append(WindowInfo(
                handle=window_id,
                title=title,
                class_name=owner,
                pid=pid,
                visible=on_screen,
            ))
    finally:
        _cf.CFRelease(window_list)

    return windows


def find_window_by_title(substring: str) -> Optional[WindowInfo]:
    """Find the first visible window whose title contains *substring*."""
    lower = substring.lower()
    for win in list_windows():
        if lower in win.title.lower():
            return win
    return None


def find_window_by_handle(window_id: int) -> Optional[WindowInfo]:
    """Return window info for a specific CGWindowID."""
    for win in list_windows(include_hidden=True):
        if win.handle == window_id:
            return win
    return None


def list_audio_devices() -> list[AudioDeviceInfo]:
    """Enumerate audio devices via FFmpeg avfoundation listing."""
    devices: list[AudioDeviceInfo] = []
    try:
        from recap.ffmpeg import find_ffmpeg

        ffmpeg_info = find_ffmpeg()
        result = subprocess.run(
            [
                str(ffmpeg_info.path),
                "-f", "avfoundation",
                "-list_devices", "true",
                "-i", "",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # FFmpeg outputs device list to stderr
        in_audio = False
        for line in result.stderr.split("\n"):
            if "AVFoundation audio devices" in line:
                in_audio = True
                continue
            if in_audio:
                match = re.search(r"\[(\d+)\]\s+(.*)", line)
                if match:
                    idx = int(match.group(1))
                    name = match.group(2).strip()
                    devices.append(AudioDeviceInfo(
                        id=str(idx),
                        name=name,
                        is_default=(idx == 0),
                    ))
    except Exception as exc:
        log.debug("AVFoundation device enumeration failed: %s", exc)

    if not devices:
        devices = [
            AudioDeviceInfo(id="0", name="Default Audio Device", is_default=True),
        ]
    return devices
