"""recap – command-line interface.

Entry-point: ``recap`` console command (installed via ``console_scripts``).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import textwrap
import threading
from pathlib import Path

from recap import __version__

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_ENV = 3


def main(argv: list[str] | None = None) -> int:
    """CLI entry-point.  Returns an exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    # Dispatch
    handler = {
        "record": _cmd_record,
        "monitors": _cmd_monitors,
        "windows": _cmd_windows,
        "devices": _cmd_devices,
        "doctor": _cmd_doctor,
        "version": _cmd_version,
    }.get(args.command)

    if handler is None:
        parser.print_help()
        return EXIT_ERROR

    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        _print_error(str(exc), args)
        return EXIT_ERROR


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recap",
        description="Headless screen and audio capture for Windows.",
    )
    sub = parser.add_subparsers(dest="command")

    # ── record ────────────────────────────────────────────────────
    rec = sub.add_parser("record", help="Start a recording session.")
    rec.add_argument(
        "--output", "-o",
        default="recording.mp4",
        help="Output file path (default: recording.mp4).",
    )
    rec.add_argument(
        "--monitor", "-m",
        type=int,
        default=None,
        help="Monitor index to capture (0-based).",
    )
    rec.add_argument(
        "--window-title",
        default=None,
        help="Capture the first visible window whose title contains this string.",
    )
    rec.add_argument(
        "--window-handle",
        type=lambda x: int(x, 0),
        default=None,
        help="Capture the window with this HWND (decimal or hex).",
    )
    rec.add_argument(
        "--window-capture-mode",
        default="printwindow",
        metavar="MODE",
        help=(
            "Window capture backend: printwindow (default) or screen. "
            "Use 'screen' if certain apps (for example browsers) freeze "
            "when unfocused."
        ),
    )
    rec.add_argument(
        "--no-audio",
        action="store_true",
        help="Do not capture audio.",
    )
    rec.add_argument(
        "--audio-only",
        action="store_true",
        help="Capture audio only (no video).",
    )
    rec.add_argument(
        "--video-only",
        action="store_true",
        help="Capture video only (no audio).",
    )
    rec.add_argument(
        "--duration", "-d",
        type=float,
        default=None,
        help="Recording duration in seconds (default: until stopped).",
    )
    rec.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Frames per second (default: 30).",
    )
    rec.add_argument(
        "--crop-size",
        type=_parse_crop_size,
        default=None,
        metavar="WIDTHxHEIGHT",
        help=(
            "Crop video to WIDTHxHEIGHT (example: 1280x720). "
            "Applies to monitor and window capture."
        ),
    )
    rec.add_argument(
        "--crop-position",
        default="middle",
        metavar="POSITION",
        help=(
            "Crop anchor position: top-left, top-middle, top-right, "
            "middle-left, middle, middle-right, bottom-left, "
            "bottom-middle, bottom-right (default: middle)."
        ),
    )
    rec.add_argument(
        "--ffmpeg",
        default=None,
        help="Path to the ffmpeg binary.",
    )
    rec.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output file if it already exists.",
    )
    rec.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON output.",
    )

    # ── discovery commands ────────────────────────────────────────
    mon = sub.add_parser("monitors", help="List available monitors.")
    mon.add_argument("--json", action="store_true", dest="json_output")

    win = sub.add_parser("windows", help="List visible windows.")
    win.add_argument("--json", action="store_true", dest="json_output")

    dev = sub.add_parser("devices", help="List audio output devices.")
    dev.add_argument("--json", action="store_true", dest="json_output")

    # ── doctor ────────────────────────────────────────────────────
    doc = sub.add_parser("doctor", help="Check the runtime environment.")
    doc.add_argument("--json", action="store_true", dest="json_output")
    doc.add_argument("--ffmpeg", default=None, help="Path to the ffmpeg binary.")

    # ── version ───────────────────────────────────────────────────
    sub.add_parser("version", help="Print version and exit.")

    return parser


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------

def _cmd_version(args: argparse.Namespace) -> int:
    print(f"recap {__version__}")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    from recap.ffmpeg import validate_environment

    diag = validate_environment(getattr(args, "ffmpeg", None))

    if getattr(args, "json_output", False):
        print(json.dumps(diag, indent=2, default=str))
        return EXIT_OK

    py = diag["python"]
    print(f"Python:   {py['version'].split()[0]}  ({py['executable']})")
    print(f"Platform: {py['platform']}")
    print(f"Windows:  {'yes' if diag['windows'] else 'NO'}")

    if diag["ffmpeg"]:
        ff = diag["ffmpeg"]
        print(f"FFmpeg:   {ff['version']}  ({ff['path']})")
    else:
        print("FFmpeg:   NOT FOUND")

    if diag["issues"]:
        print()
        print("Issues:")
        for issue in diag["issues"]:
            print(f"  ✗ {issue}")
        return EXIT_ENV

    print()
    print("Everything looks good ✓")
    return EXIT_OK


def _cmd_monitors(args: argparse.Namespace) -> int:
    from recap.discovery import list_monitors

    monitors = list_monitors()

    if getattr(args, "json_output", False):
        print(json.dumps([m.as_dict() for m in monitors], indent=2))
        return EXIT_OK

    if not monitors:
        print("No monitors found.")
        return EXIT_OK

    for m in monitors:
        primary = " (primary)" if m.is_primary else ""
        print(f"  [{m.index}] {m.name}  {m.width}x{m.height}  @ ({m.x},{m.y}){primary}")

    return EXIT_OK


def _cmd_windows(args: argparse.Namespace) -> int:
    from recap.discovery import list_windows

    windows = list_windows()

    if getattr(args, "json_output", False):
        print(json.dumps([w.as_dict() for w in windows], indent=2))
        return EXIT_OK

    if not windows:
        print("No visible windows found.")
        return EXIT_OK

    for w in windows:
        title = w.title[:60] + ("..." if len(w.title) > 60 else "")
        print(f"  [{w.handle:#010x}] {title}")

    return EXIT_OK


def _cmd_devices(args: argparse.Namespace) -> int:
    from recap.discovery import list_audio_devices

    devices = list_audio_devices()

    if getattr(args, "json_output", False):
        print(json.dumps([d.as_dict() for d in devices], indent=2))
        return EXIT_OK

    if not devices:
        print("No audio devices found.")
        return EXIT_OK

    for d in devices:
        default = " (default)" if d.is_default else ""
        print(f"  {d.name}{default}")

    return EXIT_OK


def _cmd_record(args: argparse.Namespace) -> int:
    from recap.config import RecordingConfig
    from recap.exceptions import ConfigError, RecapError
    from recap.recorder import Recorder

    crop_width = None
    crop_height = None
    if args.crop_size is not None:
        crop_width, crop_height = args.crop_size

    try:
        config = RecordingConfig(
            output=args.output,
            monitor=args.monitor,
            window_title=args.window_title,
            window_handle=args.window_handle,
            no_audio=args.no_audio,
            audio_only=args.audio_only,
            video_only=args.video_only,
            duration=args.duration,
            fps=args.fps,
            crop_width=crop_width,
            crop_height=crop_height,
            crop_position=args.crop_position,
            window_capture_mode=args.window_capture_mode,
            ffmpeg=args.ffmpeg,
            overwrite=args.overwrite,
            json_output=args.json_output,
        )
    except ConfigError as exc:
        _print_error(str(exc), args)
        return EXIT_CONFIG

    recorder = Recorder(config)

    # Graceful stop via SIGINT / SIGBREAK / named event
    stop_event = threading.Event()

    def _signal_stop(*_a):
        if not stop_event.is_set():
            stop_event.set()
            recorder.stop()

    signal.signal(signal.SIGINT, _signal_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_stop)

    # Also create a named Win32 event so parent processes can stop us
    _create_stop_event(stop_event, recorder)

    try:
        recorder.start()
    except RecapError as exc:
        _print_error(str(exc), args)
        return EXIT_ERROR

    if not args.json_output:
        if config.duration:
            print(f"Recording for {config.duration}s -> {config.output}")
        else:
            print(f"Recording -> {config.output}  (send signal or set event to stop)")

    # Block until stopped
    rc = recorder.wait()

    if args.json_output:
        result = {
            "output": str(config.output),
            "state": recorder.state.value,
            "exit_code": rc,
        }
        if recorder.error:
            result["error"] = str(recorder.error)
        print(json.dumps(result, indent=2, default=str))
    else:
        if rc == 0:
            print(f"Done. Saved to {config.output}")
        else:
            _print_error(f"FFmpeg exited with code {rc}", args)

    return rc


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _print_error(msg: str, args: argparse.Namespace) -> None:
    if getattr(args, "json_output", False):
        print(json.dumps({"error": msg}), file=sys.stderr)
    else:
        print(f"Error: {msg}", file=sys.stderr)


def _parse_crop_size(value: str) -> tuple[int, int]:
    cleaned = value.strip().lower().replace(" ", "")
    width_text, sep, height_text = cleaned.partition("x")
    if sep != "x":
        raise argparse.ArgumentTypeError(
            "Crop size must use WIDTHxHEIGHT format (example: 1280x720)."
        )
    try:
        width = int(width_text)
        height = int(height_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Crop size width and height must be integers."
        ) from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(
            "Crop size width and height must be positive."
        )
    return width, height


def _create_stop_event(
    stop_event: threading.Event,
    recorder,
) -> None:
    """Create a Win32 named event ``recap_stop_{pid}`` that external
    processes can signal to trigger a graceful stop.
    """
    import os

    pid = os.getpid()
    event_name = f"recap_stop_{pid}"

    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateEventW(None, True, False, event_name)
        if not handle:
            return

        def _watcher():
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
            if not stop_event.is_set():
                stop_event.set()
                recorder.stop()
            kernel32.CloseHandle(handle)

        t = threading.Thread(target=_watcher, daemon=True, name="recap-stop-event")
        t.start()
    except Exception:
        pass


import ctypes  # noqa: E402 – needed by _create_stop_event


if __name__ == "__main__":
    sys.exit(main())
