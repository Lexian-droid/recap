"""recap.platforms.linux – monitor, window, and audio device discovery.

Uses ``xrandr`` for monitor enumeration, X11 via ctypes for window
enumeration, and ``pactl`` for PulseAudio device enumeration.

If the display server is Wayland-only (no XWayland), window enumeration
is limited and some features may not be available.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import re
import shutil
import subprocess
from typing import Optional

from recap.discovery import AudioDeviceInfo, MonitorInfo, WindowInfo
from recap.exceptions import CaptureError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Display server detection
# ---------------------------------------------------------------------------

_DISPLAY_SERVER: Optional[str] = None


def _detect_display_server() -> str:
    """Detect the display server in use: 'x11', 'wayland', or 'none'."""
    global _DISPLAY_SERVER
    if _DISPLAY_SERVER is not None:
        return _DISPLAY_SERVER

    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    x_display = os.environ.get("DISPLAY", "")

    if x_display:
        _DISPLAY_SERVER = "x11"
    elif session_type == "wayland" or wayland:
        _DISPLAY_SERVER = "wayland"
    elif session_type == "x11":
        _DISPLAY_SERVER = "x11"
    else:
        _DISPLAY_SERVER = "none"

    return _DISPLAY_SERVER


# ---------------------------------------------------------------------------
# X11 library loading (lazy)
# ---------------------------------------------------------------------------

_x11 = None
_x11_loaded = False


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
        _setup_x11(_x11)
    except OSError:
        _x11 = None
    return _x11


class _XTextProperty(ctypes.Structure):
    _fields_ = [
        ("value", ctypes.c_char_p),
        ("encoding", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("nitems", ctypes.c_ulong),
    ]


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("class_", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


def _setup_x11(x11) -> None:
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p

    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int

    x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x11.XDefaultScreen.restype = ctypes.c_int

    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong

    x11.XQueryTree.argtypes = [
        ctypes.c_void_p,  # display
        ctypes.c_ulong,   # window
        ctypes.POINTER(ctypes.c_ulong),  # root_return
        ctypes.POINTER(ctypes.c_ulong),  # parent_return
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),  # children_return
        ctypes.POINTER(ctypes.c_uint),   # nchildren_return
    ]
    x11.XQueryTree.restype = ctypes.c_int

    x11.XGetWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(_XWindowAttributes),
    ]
    x11.XGetWindowAttributes.restype = ctypes.c_int

    x11.XFetchName.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    x11.XFetchName.restype = ctypes.c_int

    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XFree.restype = ctypes.c_int

    x11.XInternAtom.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
    ]
    x11.XInternAtom.restype = ctypes.c_ulong

    x11.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,       # display
        ctypes.c_ulong,        # window
        ctypes.c_ulong,        # property
        ctypes.c_long,         # long_offset
        ctypes.c_long,         # long_length
        ctypes.c_int,          # delete
        ctypes.c_ulong,        # req_type
        ctypes.POINTER(ctypes.c_ulong),   # actual_type_return
        ctypes.POINTER(ctypes.c_int),     # actual_format_return
        ctypes.POINTER(ctypes.c_ulong),   # nitems_return
        ctypes.POINTER(ctypes.c_ulong),   # bytes_after_return
        ctypes.POINTER(ctypes.c_void_p),  # prop_return
    ]
    x11.XGetWindowProperty.restype = ctypes.c_int


def _open_display() -> ctypes.c_void_p:
    x11 = _load_x11()
    if x11 is None:
        raise CaptureError(
            "X11 library (libX11) not available. "
            "Install X11 development libraries or use an X11 session."
        )
    display = x11.XOpenDisplay(None)
    if not display:
        raise CaptureError(
            "Cannot open X display. Ensure DISPLAY is set and X11 is running."
        )
    return display


def _get_window_pid(display: ctypes.c_void_p, window: int) -> int:
    """Get the PID of a window using _NET_WM_PID property."""
    x11 = _load_x11()
    if x11 is None:
        return 0

    atom = x11.XInternAtom(display, b"_NET_WM_PID", False)
    XA_CARDINAL = 6  # X11 type for unsigned int

    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    prop = ctypes.c_void_p()

    status = x11.XGetWindowProperty(
        display, window, atom,
        0, 1, False, XA_CARDINAL,
        ctypes.byref(actual_type),
        ctypes.byref(actual_format),
        ctypes.byref(nitems),
        ctypes.byref(bytes_after),
        ctypes.byref(prop),
    )

    pid = 0
    if status == 0 and nitems.value > 0 and prop.value:
        pid = ctypes.cast(prop.value, ctypes.POINTER(ctypes.c_uint32))[0]
        x11.XFree(prop)

    return pid


def _get_window_name(display: ctypes.c_void_p, window: int) -> str:
    """Get the title of a window."""
    x11 = _load_x11()
    if x11 is None:
        return ""

    # Try _NET_WM_NAME first (UTF-8)
    atom_name = x11.XInternAtom(display, b"_NET_WM_NAME", False)
    atom_utf8 = x11.XInternAtom(display, b"UTF8_STRING", False)

    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    prop = ctypes.c_void_p()

    status = x11.XGetWindowProperty(
        display, window, atom_name,
        0, 0x7FFFFFFF, False, atom_utf8,
        ctypes.byref(actual_type),
        ctypes.byref(actual_format),
        ctypes.byref(nitems),
        ctypes.byref(bytes_after),
        ctypes.byref(prop),
    )

    if status == 0 and nitems.value > 0 and prop.value:
        name = ctypes.string_at(prop.value, nitems.value).decode(
            "utf-8", errors="replace"
        )
        x11.XFree(prop)
        return name

    # Fallback to XFetchName (Latin-1)
    name_ptr = ctypes.c_char_p()
    if x11.XFetchName(display, window, ctypes.byref(name_ptr)):
        if name_ptr.value:
            name = name_ptr.value.decode("latin-1", errors="replace")
            x11.XFree(name_ptr)
            return name

    return ""


# =========================================================================
# Public API
# =========================================================================


def list_monitors() -> list[MonitorInfo]:
    """Enumerate monitors using ``xrandr`` command output."""
    monitors: list[MonitorInfo] = []

    xrandr = shutil.which("xrandr")
    if xrandr is None:
        # Fallback: report root window as single monitor
        ds = _detect_display_server()
        if ds == "x11":
            return _list_monitors_x11_fallback()
        raise CaptureError(
            "xrandr not found. Install xrandr or use an X11 desktop session."
        )

    try:
        result = subprocess.run(
            [xrandr, "--query"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.split("\n"):
            if " connected " not in line:
                continue
            match = re.search(
                r"(\S+)\s+connected\s+(primary\s+)?(\d+)x(\d+)\+(\d+)\+(\d+)",
                line,
            )
            if match:
                name = match.group(1)
                is_primary = match.group(2) is not None
                width = int(match.group(3))
                height = int(match.group(4))
                x = int(match.group(5))
                y = int(match.group(6))
                monitors.append(MonitorInfo(
                    index=len(monitors),
                    name=name,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    is_primary=is_primary,
                ))
    except Exception as exc:
        log.debug("xrandr enumeration failed: %s", exc)
        return _list_monitors_x11_fallback()

    if not monitors:
        return _list_monitors_x11_fallback()
    return monitors


def _list_monitors_x11_fallback() -> list[MonitorInfo]:
    """Use X11 root window as a single-monitor fallback."""
    x11 = _load_x11()
    if x11 is None:
        return []
    display = x11.XOpenDisplay(None)
    if not display:
        return []
    try:
        screen = x11.XDefaultScreen(display)
        # XDisplayWidth / XDisplayHeight
        x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDisplayWidth.restype = ctypes.c_int
        x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDisplayHeight.restype = ctypes.c_int
        w = x11.XDisplayWidth(display, screen)
        h = x11.XDisplayHeight(display, screen)
        return [MonitorInfo(
            index=0,
            name="X11 Root Window",
            x=0, y=0,
            width=w, height=h,
            is_primary=True,
        )]
    finally:
        x11.XCloseDisplay(display)


def list_windows(*, include_hidden: bool = False) -> list[WindowInfo]:
    """Enumerate top-level windows.

    Uses _NET_CLIENT_LIST if available (most EWMH-compliant WMs),
    otherwise falls back to XQueryTree.
    """
    ds = _detect_display_server()
    if ds == "wayland":
        log.warning(
            "Window enumeration is limited on Wayland. "
            "For full support, use X11 or XWayland."
        )
        # Try via XWayland if DISPLAY is set
        if not os.environ.get("DISPLAY"):
            return []

    x11 = _load_x11()
    if x11 is None:
        return []

    display = x11.XOpenDisplay(None)
    if not display:
        return []

    try:
        return _list_windows_x11(display, include_hidden)
    finally:
        x11.XCloseDisplay(display)


def _list_windows_x11(
    display: ctypes.c_void_p,
    include_hidden: bool,
) -> list[WindowInfo]:
    """Enumerate windows via X11 _NET_CLIENT_LIST or XQueryTree."""
    x11 = _load_x11()
    windows: list[WindowInfo] = []

    # Try _NET_CLIENT_LIST first (preferred, gives managed windows)
    atom = x11.XInternAtom(display, b"_NET_CLIENT_LIST", False)
    XA_WINDOW = 33

    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    prop = ctypes.c_void_p()

    root = x11.XDefaultRootWindow(display)
    status = x11.XGetWindowProperty(
        display, root, atom,
        0, 0x7FFFFFFF, False, XA_WINDOW,
        ctypes.byref(actual_type),
        ctypes.byref(actual_format),
        ctypes.byref(nitems),
        ctypes.byref(bytes_after),
        ctypes.byref(prop),
    )

    if status == 0 and nitems.value > 0 and prop.value:
        win_ids = ctypes.cast(
            prop.value,
            ctypes.POINTER(ctypes.c_ulong * nitems.value),
        ).contents
        for win_id in win_ids:
            name = _get_window_name(display, win_id)
            if not name and not include_hidden:
                continue

            attrs = _XWindowAttributes()
            x11.XGetWindowAttributes(display, win_id, ctypes.byref(attrs))
            # map_state: 0=IsUnmapped, 1=IsUnviewable, 2=IsViewable
            visible = attrs.map_state == 2

            if not visible and not include_hidden:
                continue

            pid = _get_window_pid(display, win_id)
            windows.append(WindowInfo(
                handle=int(win_id),
                title=name,
                class_name="",
                pid=pid,
                visible=visible,
            ))
        x11.XFree(prop)
        return windows

    # Fallback: XQueryTree on root
    root_return = ctypes.c_ulong()
    parent_return = ctypes.c_ulong()
    children_ptr = ctypes.POINTER(ctypes.c_ulong)()
    nchildren = ctypes.c_uint()

    x11.XQueryTree(
        display, root,
        ctypes.byref(root_return),
        ctypes.byref(parent_return),
        ctypes.byref(children_ptr),
        ctypes.byref(nchildren),
    )

    for i in range(nchildren.value):
        win_id = children_ptr[i]
        name = _get_window_name(display, win_id)
        if not name and not include_hidden:
            continue

        attrs = _XWindowAttributes()
        x11.XGetWindowAttributes(display, win_id, ctypes.byref(attrs))
        visible = attrs.map_state == 2

        if not visible and not include_hidden:
            continue

        pid = _get_window_pid(display, win_id)
        windows.append(WindowInfo(
            handle=int(win_id),
            title=name,
            class_name="",
            pid=pid,
            visible=visible,
        ))

    if children_ptr:
        x11.XFree(children_ptr)

    return windows


def find_window_by_title(substring: str) -> Optional[WindowInfo]:
    """Find the first visible window whose title contains *substring*."""
    lower = substring.lower()
    for win in list_windows():
        if lower in win.title.lower():
            return win
    return None


def find_window_by_handle(window_id: int) -> Optional[WindowInfo]:
    """Return window info for a specific X11 window ID."""
    for win in list_windows(include_hidden=True):
        if win.handle == window_id:
            return win
    return None


def list_audio_devices() -> list[AudioDeviceInfo]:
    """Enumerate audio output devices via PulseAudio/PipeWire."""
    devices: list[AudioDeviceInfo] = []

    # Try pactl (PulseAudio / PipeWire PulseAudio compat)
    pactl = shutil.which("pactl")
    if pactl:
        try:
            default_sink = _get_default_sink(pactl)
            result = subprocess.run(
                [pactl, "list", "sinks", "short"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    sink_name = parts[1]
                    # Get friendly name
                    friendly = _get_sink_description(pactl, sink_name) or sink_name
                    devices.append(AudioDeviceInfo(
                        id=sink_name,
                        name=friendly,
                        is_default=(sink_name == default_sink),
                    ))
        except Exception as exc:
            log.debug("pactl device enumeration failed: %s", exc)

    if not devices:
        devices = [
            AudioDeviceInfo(
                id="default",
                name="Default Audio Device",
                is_default=True,
            ),
        ]
    return devices


def _get_default_sink(pactl: str) -> str:
    try:
        result = subprocess.run(
            [pactl, "get-default-sink"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _get_sink_description(pactl: str, sink_name: str) -> str:
    """Get the human-readable description of a PulseAudio sink."""
    try:
        result = subprocess.run(
            [pactl, "list", "sinks"],
            capture_output=True, text=True, timeout=5,
        )
        in_target = False
        for line in result.stdout.split("\n"):
            if f"Name: {sink_name}" in line:
                in_target = True
                continue
            if in_target and "Description:" in line:
                return line.split("Description:", 1)[1].strip()
            if in_target and line.strip().startswith("Name:"):
                break
    except Exception:
        pass
    return ""
