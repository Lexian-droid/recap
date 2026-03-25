"""recap – headless screen and audio capture for Windows.

Public API
----------
.. autoclass:: RecordingConfig
.. autoclass:: Recorder
.. autofunction:: list_monitors
.. autofunction:: list_windows
.. autofunction:: list_audio_devices
.. autofunction:: find_ffmpeg
.. autofunction:: validate_environment
"""

from __future__ import annotations

import ctypes
import sys

__version__ = "0.1.0"

# Enable DPI awareness so Win32 APIs return physical pixel coordinates ------
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor v2
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()

# Re-export public surface --------------------------------------------------
from recap.config import RecordingConfig
from recap.discovery import (
    AudioDeviceInfo,
    MonitorInfo,
    WindowInfo,
    list_audio_devices,
    list_monitors,
    list_windows,
)
from recap.exceptions import (
    AudioCaptureError,
    CaptureError,
    ConfigError,
    FFmpegError,
    FFmpegNotFoundError,
    RecapError,
    VideoCaptureError,
)
from recap.ffmpeg import find_ffmpeg, validate_environment
from recap.recorder import Recorder, RecorderState

__all__ = [
    # Version
    "__version__",
    # Config
    "RecordingConfig",
    # Recorder
    "Recorder",
    "RecorderState",
    # Discovery
    "list_monitors",
    "list_windows",
    "list_audio_devices",
    "MonitorInfo",
    "WindowInfo",
    "AudioDeviceInfo",
    # FFmpeg
    "find_ffmpeg",
    "validate_environment",
    # Exceptions
    "RecapError",
    "FFmpegNotFoundError",
    "FFmpegError",
    "CaptureError",
    "AudioCaptureError",
    "VideoCaptureError",
    "ConfigError",
]
