#!/usr/bin/env python3
"""Install or remove an opt-in local pre-commit secret-scan hook.

The hook runs ``scripts/scan_secrets.py --staged`` before every commit, so a
credential is caught on the developer's machine instead of in CI.  It is
intentionally **not** installed by default and the installer never touches
``core.hooksPath`` or any global Git configuration: it only writes
``<git-common-dir>/hooks/pre-commit`` inside the repository it is run from.

The generated hook carries :data:`MARKER`, which is how the installer
recognises its own file.  An existing hook from another tool is never
overwritten unless ``--force`` is given; forcing first copies it to
``pre-commit.bak`` (``.bak.1``, ``.bak.2``, ... if that name is taken).
``--uninstall`` removes only a hook that carries the marker.

Usage::

    python scripts/install_git_hooks.py
    python scripts/install_git_hooks.py --force
    python scripts/install_git_hooks.py --uninstall

Exit codes
----------
``0``
    The requested action completed (or there was nothing to do).
``1``
    The action was refused or failed; the hook was left untouched.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

#: Written into the generated hook and matched on uninstall/reinstall.  It is
#: deliberately specific so an unrelated hook cannot be mistaken for ours.
MARKER = "# novel-downloader: managed pre-commit secret scan"

HOOK_NAME = "pre-commit"

HOOK_TEMPLATE = """#!/bin/sh
{marker}
# Installed by scripts/install_git_hooks.py; remove it with:
#   python scripts/install_git_hooks.py --uninstall
# This local hook is opt-in. CI remains the authoritative secret-scan gate.

set -u

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

scan="$root/scripts/scan_secrets.py"
if [ ! -f "$scan" ]; then
    echo "pre-commit: $scan not found; skipping local secret scan" >&2
    exit 0
fi

if [ -x "$root/.venv/Scripts/python.exe" ]; then
    python_bin="$root/.venv/Scripts/python.exe"
elif [ -x "$root/.venv/bin/python" ]; then
    python_bin="$root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    python_bin=python3
elif command -v python >/dev/null 2>&1; then
    python_bin=python
else
    echo "pre-commit: no Python interpreter found; skipping local secret scan" >&2
    exit 0
fi

exec "$python_bin" "$scan" --staged
"""


class HookError(RuntimeError):
    """The requested action could not be completed safely."""


@dataclass(frozen=True)
class HookChange:
    """What an install/uninstall call did."""

    changed: bool
    message: str


def render_hook() -> str:
    """Return the hook script, with LF line endings for Git Bash."""
    return HOOK_TEMPLATE.replace("{marker}", MARKER)


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git {' '.join(args)} failed"
        raise HookError(detail)
    return result.stdout


def hooks_dir(root: Path) -> Path:
    """Return the repository's hooks directory.

    ``--git-common-dir`` is used instead of ``--git-path hooks`` so a
    ``core.hooksPath`` override cannot redirect the write outside this
    repository.  Linked worktrees resolve to the shared ``.git/hooks``.
    """
    raw = _git_output(root, "rev-parse", "--git-common-dir").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = root / common
    return common.resolve() / "hooks"


def _backup(path: Path) -> Path:
    """Copy *path* to the first free ``.bak``/``.bak.N`` name."""
    candidate = path.with_name(path.name + ".bak")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak.{counter}")
        counter += 1
    try:
        shutil.copy2(path, candidate)
    except OSError as exc:
        raise HookError(f"cannot back up {path}: {exc}") from exc
    return candidate


def _write_hook(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise HookError(f"cannot write {path}: {exc}") from exc
    with contextlib.suppress(OSError):
        # Best effort: Git for Windows runs hooks through sh even when the
        # executable bit is not represented on NTFS.
        path.chmod(0o755)


def _read_hook(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HookError(f"cannot read {path}: {exc}") from exc


def install(root: Path = DEFAULT_ROOT, *, force: bool = False) -> HookChange:
    """Install the managed hook under *root*.

    Raises :class:`HookError` when an unmanaged hook exists and *force* is
    false.  With *force* the existing hook is copied to a ``.bak`` file first.
    """
    directory = hooks_dir(root)
    hook = directory / HOOK_NAME
    content = render_hook()

    if hook.exists():
        existing = _read_hook(hook)
        if existing == content:
            return HookChange(False, f"already installed: {hook}")
        if MARKER not in existing:
            if not force:
                raise HookError(
                    f"{hook} exists and was not installed by this tool; "
                    "re-run with --force to back it up and replace it"
                )
            backup = _backup(hook)
            action = f"replaced {hook} (backup: {backup})"
        else:
            action = f"updated {hook}"
    else:
        action = f"installed {hook}"

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HookError(f"cannot create {directory}: {exc}") from exc
    _write_hook(hook, content)
    return HookChange(True, action)


def uninstall(root: Path = DEFAULT_ROOT) -> HookChange:
    """Remove the hook only when it carries :data:`MARKER`."""
    hook = hooks_dir(root) / HOOK_NAME
    if not hook.exists():
        return HookChange(False, f"nothing to remove: {hook} does not exist")
    if MARKER not in _read_hook(hook):
        raise HookError(f"{hook} was not installed by this tool; leaving it untouched")
    try:
        hook.unlink()
    except OSError as exc:
        raise HookError(f"cannot remove {hook}: {exc}") from exc
    return HookChange(True, f"removed {hook}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install an opt-in pre-commit hook that runs the local secret scan. "
            "CI remains the authoritative gate."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="back up an existing unmanaged pre-commit hook and replace it",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="remove the hook, but only if this tool installed it",
    )
    args = parser.parse_args(argv)

    if args.force and args.uninstall:
        print(
            "install-git-hooks: error: --force and --uninstall are mutually exclusive",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        if args.uninstall:
            change = uninstall(DEFAULT_ROOT)
        else:
            change = install(DEFAULT_ROOT, force=args.force)
    except HookError as exc:
        print(f"install-git-hooks: error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(change.message)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
