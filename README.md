# recap

Cross-platform headless screen and audio capture library and CLI.

## Features

- **Cross-platform**: Windows, macOS, and Linux
- Record an entire monitor or a single window
- Record system audio (loopback capture)
- Per-application audio capture (Windows only via WASAPI process loopback)
- Video-only, audio-only, or audio+video modes
- Crop support with configurable size and anchor position
- CLI tool (`recap`) and importable Python library
- Rust native backend (PyO3) for Windows, with automatic fallback to pure-Python
- Uses FFmpeg for encoding/muxing only

## Platform Support

| Feature | Windows | macOS | Linux |
|---|---|---|---|
| Monitor capture | ✓ GDI BitBlt | ✓ CoreGraphics | ✓ X11 XGetImage |
| Window capture | ✓ PrintWindow | ✓ CGWindowListCreateImage | ✓ X11 XGetImage |
| System audio | ✓ WASAPI loopback | ⚠ Requires BlackHole/Soundflower | ✓ PulseAudio monitor |
| Per-app audio | ✓ WASAPI process loopback | ✗ Not supported | ✗ Not supported |
| Rust backend | ✓ | — | — |
| HW encoding | NVENC / QSV / AMF | VideoToolbox / NVENC | NVENC / VAAPI / QSV |

### macOS Notes

- **Screen Recording permission** must be granted in System Settings → Privacy & Security → Screen Recording.
- **System audio capture** requires a virtual audio loopback device such as [BlackHole](https://github.com/ExistentialAudio/BlackHole). Install BlackHole, then set it as your system output (or use a Multi-Output Device to hear audio simultaneously). Recap auto-detects BlackHole/Soundflower when available.
- Per-application audio capture is not supported; system-wide audio is captured instead.

### Linux Notes

- Video capture requires **X11** (XWayland is acceptable). Native Wayland capture is not yet supported. If running under Wayland, ensure `DISPLAY` is set for XWayland.
- Audio capture uses **PulseAudio/PipeWire** loopback (`default.monitor`). Ensure `pactl` is available (`pulseaudio-utils` or `pipewire-pulse`).
- Per-application audio capture is not directly supported; system-wide audio is captured instead.

## Requirements

- **Python** 3.10–3.13
- **FFmpeg** on PATH or specified via `--ffmpeg`
- **Rust toolchain** (only if building from source on Windows)

### Platform-specific requirements

- **Windows**: Windows 10 version 2004+ for per-app audio capture
- **macOS**: macOS 11+ recommended; Screen Recording permission required
- **Linux**: X11 or XWayland; PulseAudio or PipeWire; `xrandr` for multi-monitor detection

## Installation

### From PyPI (prebuilt wheels)

```bash
pip install recap-capture
```

### From source (editable)

Requires [maturin](https://www.maturin.rs/) and the Rust toolchain (for the Windows native backend):

```bash
pip install maturin
git clone https://github.com/Lexian-droid/recap.git
cd recap
maturin develop --release   # builds native backend (Windows only)
pip install -e .
```

On macOS/Linux, skip the `maturin develop` step if you don't need the Rust backend — the pure-Python implementations are used automatically.

## Architecture

The project has a hybrid Python/Rust structure with a platform abstraction layer:

```
recap/              Python package (CLI, config, orchestration)
  __init__.py       Public API and version
  _native.py        Bridge to Rust — sets NATIVE_AVAILABLE flag
  cli.py            CLI entry point
  config.py         RecordingConfig dataclass
  recorder.py       Orchestrates capture threads + FFmpeg
  video.py          Platform-dispatching video capture
  audio.py          Platform-dispatching audio capture
  discovery.py      Platform-dispatching monitor/window/device enumeration
  ffmpeg.py         FFmpeg discovery and process wiring
  exceptions.py     Exception hierarchy
  platforms/        Platform abstraction layer
    __init__.py     Platform detection utilities
    macos/          macOS backends (CoreGraphics + FFmpeg avfoundation)
    linux/          Linux backends (X11 + FFmpeg pulse/alsa)

rust_core/          Rust native backend (PyO3/maturin, Windows only)
  src/lib.rs        PyO3 module registration
  src/video.rs      GDI BitBlt / PrintWindow video capture
  src/audio.rs      WASAPI loopback + process loopback audio capture
  src/discovery.rs  Monitor/window/audio device enumeration
```

### Platform dispatch

The `video.py`, `audio.py`, and `discovery.py` modules contain the Windows implementation at the top level. On macOS or Linux, platform-specific implementations are imported from `recap/platforms/` and replace the module-level names. This means the public API (`from recap import VideoCapture`) works identically regardless of platform.

On Windows, the Rust extension (`recap._rust_core`) can further replace the Python backends with native implementations for maximum performance.

### Window-specific recording

When a window is targeted via `--window-title` or `--window-handle`:

- **Video** is captured using the platform's native window capture API
- **Audio** on Windows uses WASAPI process loopback to isolate audio from that process. On macOS/Linux, system-wide audio is captured instead (with a warning).

## Releasing

GitHub Actions will publish the package to PyPI when you push a tag that starts with `v` (e.g. `v0.5.0`). Wheels are built for Python 3.10–3.13. The Rust native backend is compiled for Windows; macOS and Linux use pure-Python backends.

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

# Record a specific window (video + window-specific audio on Windows)
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
# captures the Discord window's video (and per-app audio on Windows)
recorder.stop()
recorder.wait()
```

## License

MIT
