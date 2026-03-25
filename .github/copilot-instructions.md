# Project Guidelines

## Code Style
- Follow existing Python style in `recap/`: type hints, dataclasses for config/data containers, and focused module responsibilities.
- Keep changes small and localized; avoid broad refactors unless explicitly requested.
- Preserve the existing exception model in `recap/exceptions.py` and raise specific `RecapError` subclasses for user-facing failures.

## Architecture
- Treat `recap/recorder.py` as the orchestration boundary: capture threads + FFmpeg process lifecycle live there.
- Keep capture backends separated by concern:
  - `recap/video.py`: video frame capture
  - `recap/audio.py`: WASAPI loopback audio capture
  - `recap/ffmpeg.py`: FFmpeg discovery/validation and process wiring
  - `recap/discovery.py`: monitor/window/device enumeration
- CLI behavior and exit codes are centralized in `recap/cli.py`; maintain backward-compatible flags and command semantics.

## Build and Test
- Install in editable mode: `pip install -e .`
- Primary manual validation commands:
  - `recap doctor`
  - `recap monitors`
  - `recap windows`
  - `recap devices`
  - `recap record --duration 3 --output quick-test.mp4`
- If tests are added or modified, run `pytest` (project declares pytest in optional `dev` dependencies).

## Conventions
- This project is Windows-focused. Prefer Windows-native APIs/patterns already used in the codebase; avoid introducing cross-platform abstractions unless requested.
- Keep FFmpeg handling robust: support explicit `--ffmpeg` path and environment/path discovery behavior already implemented in `recap/ffmpeg.py`.
- Respect capture mode validation in `RecordingConfig` (`video-only`, `audio-only`, and audio flags must remain mutually consistent).
- Preserve DPI-awareness and high-DPI correctness assumptions used during capture initialization.

## Documentation
- Use `README.md` as the source of truth for user-facing install and CLI examples; update it when behavior or flags change.