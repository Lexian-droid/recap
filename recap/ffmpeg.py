"""recap – FFmpeg location and validation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from recap.exceptions import FFmpegNotFoundError
from recap.platforms import subprocess_flags as _subprocess_flags


@dataclass
class FFmpegInfo:
    """Metadata about a discovered FFmpeg binary."""

    path: Path
    version: str

    def as_dict(self) -> dict:
        return {"path": str(self.path), "version": self.version}


# Well-known install locations per platform.
if sys.platform == "win32":
    _COMMON_PATHS: list[Path] = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ffmpeg" / "bin",
        Path(os.environ.get("ProgramFiles", "")) / "ffmpeg" / "bin",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "ffmpeg" / "bin",
        Path(r"C:\ffmpeg\bin"),
    ]
elif sys.platform == "darwin":
    _COMMON_PATHS = [
        Path("/opt/homebrew/bin"),      # Apple Silicon Homebrew
        Path("/usr/local/bin"),         # Intel Homebrew
        Path("/opt/local/bin"),         # MacPorts
    ]
else:
    _COMMON_PATHS = [
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/snap/bin"),
    ]


def find_ffmpeg(explicit_path: Optional[str | Path] = None) -> FFmpegInfo:
    """Locate the FFmpeg binary.

    Resolution order:

    1. *explicit_path* if given
    2. ``FFMPEG_BINARY`` environment variable
    3. ``ffmpeg`` on ``PATH``
    4. Well-known Windows install directories

    Raises :class:`FFmpegNotFoundError` if nothing works.
    """
    candidates: list[Path] = []

    if explicit_path is not None:
        candidates.append(Path(explicit_path))

    env = os.environ.get("FFMPEG_BINARY")
    if env:
        candidates.append(Path(env))

    # shutil.which checks PATH
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(Path(on_path))

    for d in _COMMON_PATHS:
        exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        exe = d / exe_name
        if exe.is_file():
            candidates.append(exe)

    for candidate in candidates:
        info = _probe(candidate)
        if info is not None:
            return info

    raise FFmpegNotFoundError(
        "Could not find a working ffmpeg binary. "
        "Install FFmpeg and ensure it is on PATH, or pass --ffmpeg."
    )


def _probe(path: Path) -> Optional[FFmpegInfo]:
    """Run ``ffmpeg -version`` and extract the version string."""
    try:
        result = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            **_subprocess_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    first_line = result.stdout.split("\n", 1)[0]
    # Typical: "ffmpeg version 6.1 Copyright ..."
    parts = first_line.split()
    version = parts[2] if len(parts) >= 3 else "unknown"
    return FFmpegInfo(path=path.resolve(), version=version)


def validate_environment(ffmpeg_path: Optional[str | Path] = None) -> dict:
    """Check the runtime environment.

    Returns a dict with diagnostic information suitable for ``recap doctor``.
    """
    import platform
    import sys

    current_os = platform.system()
    diag: dict = {
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "os": current_os,
        "windows": current_os == "Windows",
        "ffmpeg": None,
        "issues": [],
    }

    supported = {"Windows", "Darwin", "Linux"}
    if current_os not in supported:
        diag["issues"].append(
            f"Unsupported platform: {current_os}. "
            "Recap supports Windows, macOS, and Linux."
        )

    try:
        info = find_ffmpeg(ffmpeg_path)
        diag["ffmpeg"] = info.as_dict()
    except FFmpegNotFoundError as exc:
        diag["issues"].append(str(exc))

    # Platform-specific checks
    if current_os == "Windows":
        try:
            build = int(platform.version().split(".")[-1])
            if build < 18362:
                diag["issues"].append(
                    "Windows Graphics Capture requires Windows 10 build "
                    "18362 (1903) or later."
                )
        except (ValueError, IndexError):
            pass
    elif current_os == "Darwin":
        mac_ver = platform.mac_ver()[0]
        if mac_ver:
            try:
                major = int(mac_ver.split(".")[0])
                if major < 11:
                    diag["issues"].append(
                        "macOS 11 (Big Sur) or later is recommended for "
                        "screen capture support."
                    )
            except (ValueError, IndexError):
                pass
    elif current_os == "Linux":
        import os as _os
        display = _os.environ.get("DISPLAY", "")
        wayland = _os.environ.get("WAYLAND_DISPLAY", "")
        if not display and not wayland:
            diag["issues"].append(
                "No display server detected (DISPLAY / WAYLAND_DISPLAY not set). "
                "Video capture requires X11 or Wayland."
            )
        elif wayland and not display:
            diag["issues"].append(
                "Wayland detected without X11. Direct screen capture requires "
                "X11 or XWayland. Set DISPLAY=:0 for XWayland support."
            )
        # Check for PulseAudio (audio capture)
        import shutil as _shutil
        if not _shutil.which("pactl"):
            diag["issues"].append(
                "PulseAudio/PipeWire not detected.  Audio capture uses "
                "PulseAudio loopback (pactl).  Install pulseaudio-utils."
            )

    return diag
