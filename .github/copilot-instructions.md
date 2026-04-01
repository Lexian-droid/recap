# Project Guidelines

## Code Style
- Follow existing Python style in `recap/`: type hints, dataclasses for config/data containers, and focused module responsibilities.
- Keep changes small and localized; avoid broad refactors unless explicitly requested.
- Preserve the existing exception model in `recap/exceptions.py` and raise specific `RecapError` subclasses for user-facing failures.

## Architecture
- Treat `recap/recorder.py` as the orchestration boundary: capture threads + FFmpeg process lifecycle live there.
- The project has a hybrid Python/Rust structure. Performance-critical capture paths have a Rust native backend compiled via PyO3/maturin into `recap/_rust_core.pyd`. All Python modules fall back to pure-Python implementations when the native extension is unavailable.
- `recap/_native.py` is the bridge: it imports from `recap._rust_core` and sets `NATIVE_AVAILABLE`. Do not bypass this — always check `NATIVE_AVAILABLE` before delegating.
- Keep capture backends separated by concern:
  - `recap/video.py`: video frame capture (GDI BitBlt for monitors, PrintWindow for windows); delegates to `NativeVideoCapture` when native is available
  - `recap/audio.py`: WASAPI loopback audio capture; delegates to `NativeAudioCapture` when native is available
  - `recap/ffmpeg.py`: FFmpeg discovery/validation and process wiring
  - `recap/discovery.py`: monitor/window/device enumeration; delegates to native functions when available
- Rust sources live in `rust_core/src/`:
  - `lib.rs`: PyO3 module registration
  - `video.rs`: GDI BitBlt / PrintWindow video capture
  - `audio.rs`: WASAPI system-wide loopback + `ActivateAudioInterfaceAsync` process loopback
  - `discovery.rs`: monitor/window/audio device enumeration
- CLI behavior and exit codes are centralized in `recap/cli.py`; maintain backward-compatible flags and command semantics.
- When recording a window, `recorder.py` resolves the window's PID and passes `process_id` to `AudioCapture`, enabling per-application WASAPI loopback. If PID resolution fails it warns and falls back to system-wide loopback.

## Build and Test
- Build the Rust extension: `maturin develop --release` (requires Rust toolchain and maturin)
- Install Python package in editable mode: `pip install -e .`
- After any change to `rust_core/`, run `cargo check` first, then `maturin develop --release` to rebuild the extension
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
- Rust/windows crate notes:
  - `windows` crate v0.61 — `#[windows::core::implement]` is available without any extra feature flag
  - `Ref<'_, T>` does not auto-deref to COM methods; use `.ok()?` or `.unwrap()` to get `&T`
  - When constructing a `PROPVARIANT` with `VT_BLOB` pointing to stack-allocated data, wrap it in `ManuallyDrop` to prevent `PropVariantClear` from calling `CoTaskMemFree` on the stack pointer
  - WASAPI process-loopback `IAudioClient` (obtained via `ActivateAudioInterfaceAsync`) still requires `AUDCLNT_STREAMFLAGS_LOOPBACK` in `Initialize` and does not support `GetMixFormat` — always obtain the mix format from the default render endpoint first

## Documentation
- Use `README.md` as the source of truth for user-facing install and CLI examples; update it when behavior or flags change.