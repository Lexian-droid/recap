"""recap._native – bridge to the Rust native backend.

Tries to import the compiled Rust extension (``recap_core``).  If the
native module is unavailable (e.g., not yet compiled or running on an
unsupported platform) we set ``NATIVE_AVAILABLE = False`` so the rest
of the package can fall back to the pure-Python implementation.
"""
from __future__ import annotations

NATIVE_AVAILABLE: bool

try:
    from recap._rust_core import (  # type: ignore[import-not-found]
        # Discovery
        MonitorInfo as NativeMonitorInfo,
        WindowInfo as NativeWindowInfo,
        AudioDeviceInfo as NativeAudioDeviceInfo,
        list_monitors as native_list_monitors,
        list_windows as native_list_windows,
        list_audio_devices as native_list_audio_devices,
        find_window_by_title as native_find_window_by_title,
        find_window_by_handle as native_find_window_by_handle,
        # Video
        VideoCapture as NativeVideoCapture,
        # Audio
        AudioCapture as NativeAudioCapture,
    )
    NATIVE_AVAILABLE = True
except ImportError:
    NATIVE_AVAILABLE = False
