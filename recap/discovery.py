"""recap – monitor, window, and audio device discovery.

All discovery functions use Windows-native APIs via ctypes so there is no
hard dependency on pywin32 or comtypes at import time.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Monitor discovery
# ---------------------------------------------------------------------------

@dataclass
class MonitorInfo:
    """Describes a connected display monitor."""

    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    is_primary: bool

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "is_primary": self.is_primary,
        }


def list_monitors() -> list[MonitorInfo]:
    """Enumerate connected monitors using the Win32 *EnumDisplayMonitors* API."""
    user32 = ctypes.windll.user32

    monitors: list[MonitorInfo] = []

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("rcMonitor", ctypes.wintypes.RECT),
            ("rcWork", ctypes.wintypes.RECT),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.wintypes.HMONITOR,
        ctypes.wintypes.HDC,
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.wintypes.LPARAM,
    )

    def _callback(hmon, hdc, lprect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        rc = info.rcMonitor
        monitors.append(
            MonitorInfo(
                index=len(monitors),
                name=info.szDevice.rstrip("\x00"),
                x=rc.left,
                y=rc.top,
                width=rc.right - rc.left,
                height=rc.bottom - rc.top,
                is_primary=bool(info.dwFlags & 1),
            )
        )
        return 1  # continue enumeration

    user32.EnumDisplayMonitors(
        None, None, MONITORENUMPROC(_callback), 0
    )
    return monitors


# ---------------------------------------------------------------------------
# Window discovery
# ---------------------------------------------------------------------------

@dataclass
class WindowInfo:
    """Describes a visible top-level window."""

    handle: int
    title: str
    class_name: str
    pid: int
    visible: bool

    def as_dict(self) -> dict:
        return {
            "handle": self.handle,
            "title": self.title,
            "class_name": self.class_name,
            "pid": self.pid,
            "visible": self.visible,
        }


def list_windows(*, include_hidden: bool = False) -> list[WindowInfo]:
    """Enumerate top-level windows.

    By default only visible windows with a non-empty title are returned.
    """
    user32 = ctypes.windll.user32

    windows: list[WindowInfo] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )

    def _callback(hwnd, lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0 and not include_hidden:
            return True

        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        visible = bool(user32.IsWindowVisible(hwnd))
        if not visible and not include_hidden:
            return True

        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)

        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        windows.append(
            WindowInfo(
                handle=hwnd,
                title=title,
                class_name=cls_buf.value,
                pid=pid.value,
                visible=visible,
            )
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return windows


def find_window_by_title(substring: str) -> Optional[WindowInfo]:
    """Find the first visible window whose title contains *substring*."""
    lower = substring.lower()
    for win in list_windows():
        if lower in win.title.lower():
            return win
    return None


def find_window_by_handle(hwnd: int) -> Optional[WindowInfo]:
    """Return window info for a specific HWND, or ``None``."""
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    cls_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls_buf, 256)
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(
        handle=hwnd,
        title=buf.value,
        class_name=cls_buf.value,
        pid=pid.value,
        visible=bool(user32.IsWindowVisible(hwnd)),
    )


# ---------------------------------------------------------------------------
# Audio device discovery
# ---------------------------------------------------------------------------

@dataclass
class AudioDeviceInfo:
    """Describes a WASAPI audio device."""

    id: str
    name: str
    is_default: bool

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "is_default": self.is_default,
        }


def list_audio_devices() -> list[AudioDeviceInfo]:
    """Enumerate active audio render (output) endpoints via COM/WASAPI.

    Returns at least the default device when full enumeration is not
    available.
    """
    devices: list[AudioDeviceInfo] = []
    try:
        devices = _enumerate_wasapi_devices()
    except Exception:
        # Fallback: report that we cannot enumerate but a default exists
        devices = [
            AudioDeviceInfo(
                id="default",
                name="Default Audio Device",
                is_default=True,
            )
        ]
    return devices


def _enumerate_wasapi_devices() -> list[AudioDeviceInfo]:
    """Use comtypes + MMDevice API to list render endpoints."""
    import comtypes
    from comtypes import GUID

    CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    IID_IPropertyStore = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")

    # Property key for device friendly name
    PKEY_Device_FriendlyName_fmtid = GUID(
        "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
    )
    PKEY_Device_FriendlyName_pid = 14

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", comtypes.GUID), ("pid", ctypes.wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("reserved1", ctypes.c_ushort),
            ("reserved2", ctypes.c_ushort),
            ("reserved3", ctypes.c_ushort),
            ("data", ctypes.c_void_p),
            ("padding", ctypes.c_void_p),
        ]

    class IMMDevice(comtypes.IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "Activate",
                (["in"], ctypes.POINTER(comtypes.GUID), "iid"),
                (["in"], ctypes.wintypes.DWORD, "dwClsCtx"),
                (["in"], ctypes.c_void_p, "pActivationParams"),
                (["out"], ctypes.POINTER(ctypes.c_void_p), "ppInterface"),
            ),
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "OpenPropertyStore",
                (["in"], ctypes.wintypes.DWORD, "stgmAccess"),
                (["out"], ctypes.POINTER(ctypes.c_void_p), "ppProperties"),
            ),
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "GetId",
                (["out"], ctypes.POINTER(ctypes.wintypes.LPWSTR), "ppstrId"),
            ),
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "GetState",
                (["out"], ctypes.POINTER(ctypes.wintypes.DWORD), "pdwState"),
            ),
        ]

    class IMMDeviceCollection(comtypes.IUnknown):
        _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
        _methods_ = [
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "GetCount",
                (["out"], ctypes.POINTER(ctypes.c_uint), "pcDevices"),
            ),
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "Item",
                (["in"], ctypes.c_uint, "nDevice"),
                (["out"], ctypes.POINTER(ctypes.POINTER(IMMDevice)), "ppDevice"),
            ),
        ]

    class IMMDeviceEnumerator(comtypes.IUnknown):
        _iid_ = IID_IMMDeviceEnumerator
        _methods_ = [
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "EnumAudioEndpoints",
                (["in"], ctypes.wintypes.DWORD, "dataFlow"),
                (["in"], ctypes.wintypes.DWORD, "dwStateMask"),
                (
                    ["out"],
                    ctypes.POINTER(ctypes.POINTER(IMMDeviceCollection)),
                    "ppDevices",
                ),
            ),
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "GetDefaultAudioEndpoint",
                (["in"], ctypes.wintypes.DWORD, "dataFlow"),
                (["in"], ctypes.wintypes.DWORD, "role"),
                (
                    ["out"],
                    ctypes.POINTER(ctypes.POINTER(IMMDevice)),
                    "ppEndpoint",
                ),
            ),
        ]

    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    try:
        enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator
        )

        # Get default device ID for comparison
        eRender = 0  # render endpoints
        eConsole = 0  # default console role
        DEVICE_STATE_ACTIVE = 0x00000001

        default_dev = enumerator.GetDefaultAudioEndpoint(eRender, eConsole)
        default_id_ptr = default_dev.GetId()
        default_id = ctypes.wstring_at(default_id_ptr)
        ctypes.windll.ole32.CoTaskMemFree(default_id_ptr)

        collection = enumerator.EnumAudioEndpoints(
            eRender, DEVICE_STATE_ACTIVE
        )
        count = collection.GetCount()

        devices: list[AudioDeviceInfo] = []
        for i in range(count):
            device = collection.Item(i)
            dev_id_ptr = device.GetId()
            dev_id = ctypes.wstring_at(dev_id_ptr)
            ctypes.windll.ole32.CoTaskMemFree(dev_id_ptr)

            # Get friendly name via property store
            name = f"Audio Device {i}"
            try:
                ps_ptr = device.OpenPropertyStore(0)  # STGM_READ=0
                if ps_ptr:
                    pk = PROPERTYKEY()
                    pk.fmtid = PKEY_Device_FriendlyName_fmtid
                    pk.pid = PKEY_Device_FriendlyName_pid
                    pv = PROPVARIANT()
                    # We'd call IPropertyStore::GetValue here but it
                    # requires more COM plumbing.  For the first pass we
                    # use the device ID as a recognisable label.
                    name = dev_id.split("}")[-1].strip(".") or f"Device {i}"
            except Exception:
                pass

            devices.append(
                AudioDeviceInfo(
                    id=dev_id,
                    name=name,
                    is_default=(dev_id == default_id),
                )
            )
        return devices
    finally:
        comtypes.CoUninitialize()
