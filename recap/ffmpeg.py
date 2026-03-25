"""recap – FFmpeg location and validation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from recap.exceptions import FFmpegNotFoundError


@dataclass
class FFmpegInfo:
    """Metadata about a discovered FFmpeg binary."""

    path: Path
    version: str

    def as_dict(self) -> dict:
        return {"path": str(self.path), "version": self.version}


# Well-known install locations on Windows.
_COMMON_PATHS: list[Path] = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ffmpeg" / "bin",
    Path(os.environ.get("ProgramFiles", "")) / "ffmpeg" / "bin",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "ffmpeg" / "bin",
    Path(r"C:\ffmpeg\bin"),
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
        exe = d / "ffmpeg.exe"
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
            creationflags=subprocess.CREATE_NO_WINDOW,
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

    diag: dict = {
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "windows": platform.system() == "Windows",
        "ffmpeg": None,
        "issues": [],
    }

    if not diag["windows"]:
        diag["issues"].append("recap requires Windows.")

    try:
        info = find_ffmpeg(ffmpeg_path)
        diag["ffmpeg"] = info.as_dict()
    except FFmpegNotFoundError as exc:
        diag["issues"].append(str(exc))

    # Check for WGC availability (Windows 10 1903+)
    if diag["windows"]:
        try:
            build = int(platform.version().split(".")[-1])
            if build < 18362:
                diag["issues"].append(
                    "Windows Graphics Capture requires Windows 10 build "
                    "18362 (1903) or later."
                )
        except (ValueError, IndexError):
            pass

    return diag
