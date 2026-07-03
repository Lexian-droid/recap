"""Release workflow helper for local validation and publishing.

This module provides a small CLI so package release steps are repeatable
without memorizing commands.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "test-local":
        return _cmd_test_local(args)
    if args.command == "build":
        return _cmd_build(args)
    if args.command == "check-dist":
        return _cmd_check_dist(args)
    if args.command == "publish-testpypi":
        return _cmd_publish(args, repository="testpypi")
    if args.command == "publish-pypi":
        return _cmd_publish(args, repository="pypi")

    parser.print_help()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recap-release",
        description="Local release helper for recap-capture.",
    )
    sub = parser.add_subparsers(dest="command")

    test_local = sub.add_parser(
        "test-local",
        help="Install editable package and run basic CLI smoke tests.",
    )
    test_local.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip pip install -e . step.",
    )

    sub.add_parser(
        "build",
        help="Build sdist and wheel into dist/.",
    )

    sub.add_parser(
        "check-dist",
        help="Run twine metadata checks on dist artifacts.",
    )

    publish_test = sub.add_parser(
        "publish-testpypi",
        help="Upload dist artifacts to TestPyPI.",
    )
    publish_test.add_argument(
        "--skip-check",
        action="store_true",
        help="Skip twine check before upload.",
    )

    publish_pypi = sub.add_parser(
        "publish-pypi",
        help="Upload dist artifacts to PyPI.",
    )
    publish_pypi.add_argument(
        "--skip-check",
        action="store_true",
        help="Skip twine check before upload.",
    )

    return parser


def _cmd_test_local(args: argparse.Namespace) -> int:
    root = _repo_root()
    if not args.skip_install:
        _run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=root)

    _run([sys.executable, "-m", "recap.cli", "version"], cwd=root)
    _run([sys.executable, "-m", "recap.cli", "doctor"], cwd=root)
    _run([sys.executable, "-m", "recap.cli", "monitors"], cwd=root)
    _run([sys.executable, "-m", "recap.cli", "devices"], cwd=root)
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    root = _repo_root()
    _run([sys.executable, "-m", "pip", "install", "build"], cwd=root)
    _run([sys.executable, "-m", "build"], cwd=root)
    return 0


def _cmd_check_dist(args: argparse.Namespace) -> int:
    root = _repo_root()
    _ensure_dist_exists(root)
    _run([sys.executable, "-m", "pip", "install", "twine"], cwd=root)
    _run([sys.executable, "-m", "twine", "check", "dist/*"], cwd=root)
    return 0


def _cmd_publish(args: argparse.Namespace, repository: str) -> int:
    root = _repo_root()
    _ensure_dist_exists(root)
    _run([sys.executable, "-m", "pip", "install", "twine"], cwd=root)

    if not args.skip_check:
        _run([sys.executable, "-m", "twine", "check", "dist/*"], cwd=root)

    cmd = [sys.executable, "-m", "twine", "upload"]
    if repository == "testpypi":
        cmd.extend(["--repository", "testpypi"])
    cmd.append("dist/*")
    _run(cmd, cwd=root)
    return 0


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_dist_exists(root: Path) -> None:
    dist_dir = root / "dist"
    if not dist_dir.exists() or not any(dist_dir.iterdir()):
        raise RuntimeError("dist/ is empty. Run 'recap-release build' first.")


def _run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(cwd))


if __name__ == "__main__":
    raise SystemExit(main())
