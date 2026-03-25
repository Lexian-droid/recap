"""recap – recording configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from recap.exceptions import ConfigError


@dataclass
class RecordingConfig:
    """Configuration for a recording session.

    Parameters
    ----------
    output : str | Path
        Destination file path for the recording.
    monitor : int | None
        Monitor index to capture (0-based). Mutually exclusive with
        *window_title* / *window_handle*.
    window_title : str | None
        Substring to match against window titles for capture.
    window_handle : int | None
        Native HWND of the window to capture.
    no_audio : bool
        If True, suppress audio capture even in a video recording.
    audio_only : bool
        Record audio only (no video).
    video_only : bool
        Record video only (no audio).
    duration : float | None
        Maximum recording duration in seconds. ``None`` means record until
        :meth:`Recorder.stop` is called.
    fps : int
        Target frames per second for video capture.
    ffmpeg : str | None
        Explicit path to the ffmpeg binary. ``None`` means auto-detect.
    overwrite : bool
        Overwrite the output file if it already exists.
    json_output : bool
        Emit machine-readable JSON output from the CLI.
    """

    output: str | Path = "recording.mp4"
    monitor: Optional[int] = None
    window_title: Optional[str] = None
    window_handle: Optional[int] = None
    no_audio: bool = False
    audio_only: bool = False
    video_only: bool = False
    duration: Optional[float] = None
    fps: int = 30
    ffmpeg: Optional[str] = None
    overwrite: bool = False
    json_output: bool = False

    def __post_init__(self) -> None:
        self.output = Path(self.output)
        self.validate()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Raise :class:`ConfigError` on invalid / conflicting options."""
        # Mutual exclusion: target selection
        targets = sum([
            self.monitor is not None,
            self.window_title is not None,
            self.window_handle is not None,
        ])
        if targets > 1:
            raise ConfigError(
                "Only one of --monitor, --window-title, or --window-handle "
                "may be specified."
            )

        # Mutual exclusion: media mode
        if self.audio_only and self.video_only:
            raise ConfigError(
                "--audio-only and --video-only are mutually exclusive."
            )
        if self.audio_only and self.no_audio:
            raise ConfigError(
                "--audio-only and --no-audio are mutually exclusive."
            )

        # video-only implies no audio
        if self.video_only:
            self.no_audio = True

        # audio-only must not have a video target
        if self.audio_only and (
            self.monitor is not None
            or self.window_title is not None
            or self.window_handle is not None
        ):
            raise ConfigError(
                "--audio-only cannot be combined with a video capture target."
            )

        # Duration must be positive
        if self.duration is not None and self.duration <= 0:
            raise ConfigError("--duration must be a positive number.")

        # FPS must be positive
        if self.fps <= 0:
            raise ConfigError("--fps must be a positive integer.")

        # Output file collision
        if not self.overwrite and self.output.exists():
            raise ConfigError(
                f"Output file already exists: {self.output}. "
                "Use --overwrite to replace it."
            )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def capture_video(self) -> bool:
        """Whether this config requires video capture."""
        return not self.audio_only

    @property
    def capture_audio(self) -> bool:
        """Whether this config requires audio capture."""
        return not self.no_audio and not self.video_only

    @property
    def has_explicit_target(self) -> bool:
        """Whether the user chose a specific capture target."""
        return (
            self.monitor is not None
            or self.window_title is not None
            or self.window_handle is not None
        )
