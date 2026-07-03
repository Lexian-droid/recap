# Project Guidelines

## Code Style
- Follow existing Python style in `recap/`: type hints, dataclasses for config/data containers, and focused module responsibilities.
- Keep changes small and localized; avoid broad refactors unless explicitly requested.
- Preserve the existing exception model in `recap/exceptions.py` and raise specific `RecapError` subclasses for user-facing failures.

## Architecture
- Treat `recap/recorder.py` as the orchestration boundary: capture threads + FFmpeg process lifecycle live there.
- The project uses a pure-Python platform abstraction layer. Windows capture, discovery, and audio logic stay in the main modules, with macOS/Linux implementations imported from `recap/platforms/`.
- Platform dispatch happens in `recap/video.py`, `recap/audio.py`, and `recap/discovery.py`. Each contains the Windows implementation inline, with platform dispatch at the bottom importing from `recap/platforms/macos/` or `recap/platforms/linux/` on non-Windows.
- `recap/platforms/__init__.py` provides platform detection (`is_windows()`, `is_macos()`, `is_linux()`) and `subprocess_flags()` for cross-platform subprocess calls.
- Keep capture backends separated by concern:
  - `recap/video.py`: video frame capture; dispatches to platform-specific implementation
  - `recap/audio.py`: audio capture; dispatches to platform-specific implementation
  - `recap/ffmpeg.py`: FFmpeg discovery/validation and process wiring (cross-platform)
  - `recap/discovery.py`: monitor/window/device enumeration; dispatches to platform-specific implementation
- Platform-specific backends live in `recap/platforms/`:
  - `recap/platforms/macos/`: CoreGraphics video, FFmpeg avfoundation audio, CG/CF discovery
  - `recap/platforms/linux/`: X11 video, FFmpeg pulse/alsa audio, xrandr/X11 discovery
- CLI behavior and exit codes are centralized in `recap/cli.py`; maintain backward-compatible flags and command semantics.
- When recording a window, `recorder.py` resolves the window's PID and passes `process_id` to `AudioCapture`, enabling per-application WASAPI loopback on Windows. On macOS/Linux, per-process audio is not supported and falls back to system-wide capture with a warning.

## Build and Test
- Install Python package in editable mode: `pip install -e .`
- Primary manual validation commands:
  - `recap doctor`
  - `recap monitors`
  - `recap windows`
  - `recap devices`
  - `recap record --duration 3 --output quick-test.mp4`
- If tests are added or modified, run `pytest` (project declares pytest in optional `dev` dependencies).

## Conventions
- This project is cross-platform: Windows, macOS, and Linux. Use the platform abstraction in `recap/platforms/` for OS-specific code.
- Windows-specific code (GDI, WASAPI, Win32) stays in the main modules (`video.py`, `audio.py`, `discovery.py`).
- macOS-specific code uses CoreGraphics/CoreFoundation via ctypes and FFmpeg avfoundation.
- Linux-specific code uses X11 via ctypes, xrandr for monitors, and FFmpeg pulse/alsa for audio.
- Use `recap.platforms.subprocess_flags()` instead of hardcoding `creationflags=subprocess.CREATE_NO_WINDOW`.
- Keep FFmpeg handling robust: support explicit `--ffmpeg` path and environment/path discovery behavior already implemented in `recap/ffmpeg.py`.
- Respect capture mode validation in `RecordingConfig` (`video-only`, `audio-only`, and audio flags must remain mutually consistent).
- Preserve DPI-awareness and high-DPI correctness assumptions used during capture initialization.
## Documentation
- Use `README.md` as the source of truth for user-facing install and CLI examples; update it when behavior or flags change.