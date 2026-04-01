"""recap.platforms – platform detection and utilities."""

from __future__ import annotations

import subprocess
import sys

PLATFORM: str = sys.platform


def is_windows() -> bool:
    """Return True if running on Windows."""
    return PLATFORM == "win32"


def is_macos() -> bool:
    """Return True if running on macOS."""
    return PLATFORM == "darwin"


def is_linux() -> bool:
    """Return True if running on Linux."""
    return PLATFORM.startswith("linux")


def subprocess_flags() -> dict:
    """Return platform-specific keyword arguments for subprocess calls.

    On Windows this includes ``creationflags=CREATE_NO_WINDOW`` to prevent
    console windows from flashing.  On other platforms returns an empty dict.
    """
    if is_windows():
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def platform_name() -> str:
    """Return a human-readable platform name."""
    if is_windows():
        return "Windows"
    if is_macos():
        return "macOS"
    if is_linux():
        return "Linux"
    return PLATFORM
