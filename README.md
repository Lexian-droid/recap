# recap

Headless screen and audio capture library and CLI for Windows.

## Features

- Record an entire monitor (GDI BitBlt) or a single window (PrintWindow)
- Record system audio via WASAPI loopback
- Per-application audio capture via WASAPI process loopback (Windows 10 2004+)
- Video-only, audio-only, or audio+video modes
- Crop support with configurable size and anchor position
- CLI tool (`recap`) and importable Python library
- Rust native backend (PyO3) for performance-critical capture paths, with automatic fallback to pure-Python
- Uses FFmpeg for encoding/muxing only

## Requirements

- **Windows 10** version 2004 (build 19041) or later for per-application audio capture
- **Python** 3.10–3.13
- **FFmpeg** on PATH or specified via `--ffmpeg`
- **Rust toolchain** (only if building from source)

## Installation

### From PyPI (prebuilt wheels)

```bash
pip install recap-capture
```

### From source (editable)

Requires [maturin](https://www.maturin.rs/) and the Rust toolchain:

```bash
pip install maturin
git clone https://github.com/Lexian-droid/recap.git
cd recap
maturin develop --release
pip install -e .
```

## Architecture

The project has a hybrid Python/Rust structure:

```
recap/              Python package (CLI, config, orchestration)
  _native.py        Bridge to Rust — sets NATIVE_AVAILABLE flag
  cli.py            CLI entry point
  config.py         RecordingConfig dataclass
  recorder.py       Orchestrates capture threads + FFmpeg
  video.py          Python GDI video capture (fallback)
  audio.py          Python WASAPI audio capture (fallback)
  discovery.py      Python monitor/window/device enumeration (fallback)
  ffmpeg.py         FFmpeg discovery and process wiring
  exceptions.py     Exception hierarchy

rust_core/          Rust native backend (PyO3/maturin)
  src/lib.rs        PyO3 module registration
  src/video.rs      GDI BitBlt / PrintWindow video capture
  src/audio.rs      WASAPI loopback + process loopback audio capture
  src/discovery.rs  Monitor/window/audio device enumeration
```

When the Rust extension (`recap._rust_core`) is available, `VideoCapture`, `AudioCapture`, and all discovery functions are automatically replaced with their native implementations. If the extension is missing (e.g. no wheel for your platform), everything falls back to the pure-Python backends transparently.

### Window-specific recording

When a window is targeted via `--window-title` or `--window-handle`:

- **Video** is captured using `PrintWindow` on that window's HWND
- **Audio** is captured using WASAPI process loopback, isolating only audio from that window's process tree

If the target process cannot be resolved, audio falls back to desktop-wide loopback with a warning.

## Releasing

GitHub Actions will publish the package to PyPI when you push a tag that starts with `v` (e.g. `v0.4.1`). Wheels are built on `windows-latest` for Python 3.10–3.13 using `maturin-action`.

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

# Record a specific window (video + window-specific audio)
recap record --window-title "Notepad" --output notepad.mp4

# Record video only
recap record --video-only --output silent.mp4

# Record a 1280x720 crop from the top-left of the selected source
recap record --crop-size 1280x720 --crop-position top-left --output cropped.mp4

# Record a centered 1280x720 crop from a specific window
recap record --window-title "Notepad" --crop-size 1280x720 --crop-position middle --output notepad-cropped.mp4

# Record audio only
recap record --audio-only --output audio.wav

# Record for 30 seconds
recap record --duration 30 --output clip.mp4
```

### Crop positions

`--crop-position` supports:

- `top-left`, `top-middle`, `top-right`
- `middle-left`, `middle`, `middle-right`
- `bottom-left`, `bottom-middle`, `bottom-right`

Center aliases are also accepted (`center`, `top-center`, `middle-center`, etc.).

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

### Window-specific recording

```python
from recap import Recorder, RecordingConfig

config = RecordingConfig(
    output="discord.mp4",
    window_title="Discord",
)
recorder = Recorder(config)
recorder.start()
# captures only the Discord window's video and audio
recorder.stop()
recorder.wait()
```

## License

MIT
