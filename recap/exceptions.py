"""recap – exceptions."""


class RecapError(Exception):
    """Base exception for all recap errors."""


class FFmpegNotFoundError(RecapError):
    """FFmpeg binary could not be located."""


class FFmpegError(RecapError):
    """FFmpeg process returned a non-zero exit code."""


class CaptureError(RecapError):
    """An error occurred during capture."""


class AudioCaptureError(CaptureError):
    """An error occurred during audio capture."""


class VideoCaptureError(CaptureError):
    """An error occurred during video capture."""


class ConfigError(RecapError):
    """Invalid or conflicting configuration."""


class EnvironmentError(RecapError):
    """The runtime environment is missing required components."""
