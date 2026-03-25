"""recap – recording configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from recap.exceptions import ConfigError


_CROP_POSITION_ALIASES: dict[str, str] = {
    "top-left": "top-left",
    "top-middle": "top-middle",
    "top-center": "top-middle",
    "top-right": "top-right",
    "middle-left": "middle-left",
    "center-left": "middle-left",
    "middle": "middle",
    "middle-middle": "middle",
    "middle-center": "middle",
    "center": "middle",
    "middle-right": "middle-right",
    "center-right": "middle-right",
    "bottom-left": "bottom-left",
    "bottom-middle": "bottom-middle",
    "bottom-center": "bottom-middle",
    "bottom-right": "bottom-right",
}


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
    crop_width : int | None
        Crop width in pixels. Must be provided together with ``crop_height``.
    crop_height : int | None
        Crop height in pixels. Must be provided together with ``crop_width``.
    crop_position : str
        Crop anchor position (for example ``top-left`` or ``middle``).
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
    crop_width: Optional[int] = None
    crop_height: Optional[int] = None
    crop_position: str = "middle"

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

        # Crop validation
        crop_dim_count = sum(
            value is not None for value in (self.crop_width, self.crop_height)
        )
        if crop_dim_count == 1:
            raise ConfigError(
                "Crop size requires both width and height (for example "
                "--crop-size 1280x720)."
            )
        if self.crop_width is not None and self.crop_height is not None:
            if self.crop_width <= 0 or self.crop_height <= 0:
                raise ConfigError("Crop width and height must be positive.")
            if not self.capture_video:
                raise ConfigError("Crop options require video capture.")

        normalized_position = _normalize_crop_position(self.crop_position)
        if normalized_position is None:
            allowed = ", ".join(sorted(set(_CROP_POSITION_ALIASES.values())))
            raise ConfigError(
                "Invalid crop position. Expected one of: "
                f"{allowed}."
            )
        self.crop_position = normalized_position

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

    @property
    def has_crop(self) -> bool:
        """Whether a crop region is configured."""
        return self.crop_width is not None and self.crop_height is not None

    def build_crop_filter(self, source_width: int, source_height: int) -> str:
        """Build an FFmpeg ``crop=`` filter string for this config."""
        if not self.has_crop:
            raise ConfigError("Crop filter requested but crop is not configured.")

        crop_width = int(self.crop_width)
        crop_height = int(self.crop_height)
        if crop_width > source_width or crop_height > source_height:
            raise ConfigError(
                "Crop size "
                f"{crop_width}x{crop_height} exceeds capture size "
                f"{source_width}x{source_height}."
            )

        vertical, horizontal = _split_crop_position(self.crop_position)
        if vertical == "top":
            y = 0
        elif vertical == "middle":
            y = (source_height - crop_height) // 2
        else:
            y = source_height - crop_height

        if horizontal == "left":
            x = 0
        elif horizontal == "middle":
            x = (source_width - crop_width) // 2
        else:
            x = source_width - crop_width

        return f"crop={crop_width}:{crop_height}:{x}:{y}"


def _normalize_crop_position(value: str) -> Optional[str]:
    key = value.strip().lower().replace("_", "-")
    return _CROP_POSITION_ALIASES.get(key)


def _split_crop_position(position: str) -> tuple[str, str]:
    if position == "middle":
        return "middle", "middle"
    vertical, horizontal = position.split("-", maxsplit=1)
    return vertical, horizontal
