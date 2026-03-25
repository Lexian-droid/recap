# recap

Headless screen and audio capture library and CLI for Windows.

## Features

- Record an entire monitor via Windows Graphics Capture (WGC)
- Record a single window via WGC (real window capture, not desktop crop)
- Record system audio via WASAPI loopback
- Video-only, audio-only, or audio+video modes
- CLI tool (`recap`) and importable Python library
- Uses FFmpeg for encoding/muxing only

## Installation

```bash
pip install -e .
```

FFmpeg must be available on PATH or specified via `--ffmpeg`.

## Releasing

GitHub Actions will publish the package to PyPI when you push a tag that starts with `v`, for example `v0.1.1`.

Before the first release, configure PyPI Trusted Publishing for this repository and approve the `pypi` environment in GitHub.

## CLI Usage

```bash
# Check environment
recap doctor

# List available capture targets
recap monitors
recap windows
recap devices

# Record primary monitor with audio
recap record --output recording.mp4

# Record a specific window
recap record --window-title "Notepad" --output notepad.mp4

# Record video only
recap record --video-only --output silent.mp4

# Record audio only
recap record --audio-only --output audio.wav

# Record for 30 seconds
recap record --duration 30 --output clip.mp4
```

## Library Usage

```python
from recap import Recorder, RecordingConfig

config = RecordingConfig(output="recording.mp4")
recorder = Recorder(config)
recorder.start()
# ... do work ...
recorder.stop()
recorder.wait()
```

## License

MIT
