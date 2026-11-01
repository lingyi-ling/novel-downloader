#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.storage
------------------------------------------

Report how much disk space the application's data directories use.

Categories are the same directories the ``clean`` command operates on:
``raw_data``, ``cache``, ``downloads`` and ``logs``. Each bucket also reports
the free space of the volume it lives on and its largest immediate
subdirectories (typically the site keys under ``raw_data``), so a runaway site
is visible at a glance. ``--json`` adds a machine-readable view of the same
data plus the ``reclaimable`` totals: what ``clean cache`` + ``clean logs``
would free. ``--clean`` removes the existing directories after a single
confirmation; it is the bulk equivalent of ``clean book --all``,
``clean cache`` and ``clean logs``.
"""

from __future__ import annotations

import json
import os
import shutil
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass, field
from pathlib import Path

from novel_downloader.apps.cli import ui
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t

from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail

#: ``(key, label)`` pairs in display order; keys are stable for ``--json``.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("raw", t("Raw data")),
    ("cache", t("Cache")),
    ("downloads", t("Downloads")),
    ("logs", t("Logs")),
)

#: Categories fully reclaimable through existing ``clean`` subcommands; used
#: for the ``reclaimable`` totals in ``--json`` and the hint after the table.
RECLAIMABLE_CATEGORIES: tuple[str, ...] = ("cache", "logs")

#: Largest immediate subdirectories listed per bucket, largest first.
TOP_SUBDIRECTORIES = 5

#: Subdirectory rows rendered per bucket in the human table.
_TOP_SUBDIRECTORY_ROWS = 3

#: Tree glyph prefixing subdirectory rows in the table.
_SUBDIRECTORY_PREFIX = "  └ "


@dataclass
class SubStat:
    """Recursive size/file count of one immediate subdirectory."""

    name: str
    files: int = 0
    bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "files": self.files,
            "bytes": self.bytes,
        }


@dataclass
class DirStat:
    """Recursive size/file count of one data directory.

    ``free_bytes`` is the free space of the volume holding the directory
    (``None`` when it cannot be determined); ``top`` lists the largest
    immediate subdirectories.
    """

    key: str
    label: str
    path: Path
    files: int = 0
    bytes: int = 0
    free_bytes: int | None = None
    top: list[SubStat] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "path": str(self.path),
            "files": self.files,
            "bytes": self.bytes,
            "free_bytes": self.free_bytes,
            "top": [sub.to_dict() for sub in self.top],
        }


def _resolve_paths() -> dict[str, Path]:
    """Resolve the four data directories from the current configuration."""
    config_data = load_or_init_config(None)
    if config_data is None:
        # ``load_or_init_config`` already reported the reason on stderr.
        raise SystemExit(EXIT_FAILURE)

    adapter = ConfigAdapter(config=config_data)
    return {
        "raw": adapter.get_raw_data_dir(),
        "cache": adapter.get_cache_dir(),
        "downloads": adapter.get_output_dir(),
        "logs": adapter.get_log_dir(),
    }


def scan_tree(path: Path) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for ``path``, tolerating errors.

    Shared with ``doctor`` (cache-directory check) so both commands agree on
    how a directory is measured. Missing directories and unreadable entries
    count as zero; symlinks are not followed (``os.walk`` default).
    """
    if not path.is_dir():
        return 0, 0

    files = 0
    total = 0
    for root, _dirs, names in os.walk(path, onerror=lambda _err: None):
        for name in names:
            candidate = Path(root) / name
            try:
                stat = candidate.stat()
            except OSError:
                continue
            files += 1
            total += stat.st_size
    return files, total


def format_size(count: int) -> str:
    """Format a byte count with a binary unit suffix."""
    if count < 1024:
        return f"{count} B"
    value = float(count)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PiB"


def _top_subdirectories(path: Path, limit: int) -> list[SubStat]:
    """Return the ``limit`` largest immediate subdirectories of ``path``."""
    if limit <= 0 or not path.is_dir():
        return []
    subs: list[SubStat] = []
    try:
        children = list(path.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        files, total = scan_tree(child)
        if files or total:
            subs.append(SubStat(name=child.name, files=files, bytes=total))
    subs.sort(key=lambda sub: (sub.bytes, sub.files), reverse=True)
    return subs[:limit]


def disk_free(path: Path) -> int | None:
    """Return the free bytes of the volume holding ``path``.

    The nearest existing ancestor is probed so a not-yet-created data
    directory still reports the free space of the drive it will live on.
    ``None`` is returned when the platform call fails. Shared with ``doctor``
    (disk-space check) so both commands agree on how free space is measured.
    """
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _collect(paths: dict[str, Path]) -> list[DirStat]:
    """Build one ``DirStat`` per category in :data:`CATEGORIES` order."""
    stats: list[DirStat] = []
    for key, label in CATEGORIES:
        path = paths[key]
        files, total = scan_tree(path)
        stats.append(
            DirStat(
                key=key,
                label=label,
                path=path,
                files=files,
                bytes=total,
                free_bytes=disk_free(path),
                top=_top_subdirectories(path, TOP_SUBDIRECTORIES),
            )
        )
    return stats


def _reclaimable(stats: list[DirStat]) -> tuple[int, int]:
    """Return ``(bytes, files)`` reclaimable through the ``clean`` command."""
    by_key = {stat.key: stat for stat in stats}
    total_bytes = 0
    total_files = 0
    for key in RECLAIMABLE_CATEGORIES:
        stat = by_key.get(key)
        if stat is not None:
            total_bytes += stat.bytes
            total_files += stat.files
    return total_bytes, total_files


def _render(stats: list[DirStat]) -> None:
    """Print the usage table with the largest subdirectories and totals."""
    rows: list[list[str]] = []
    for stat in stats:
        free = "" if stat.free_bytes is None else format_size(stat.free_bytes)
        rows.append(
            [
                stat.label,
                str(stat.path),
                str(stat.files),
                format_size(stat.bytes),
                free,
            ]
        )
        for sub in stat.top[:_TOP_SUBDIRECTORY_ROWS]:
            rows.append(
                [
                    f"{_SUBDIRECTORY_PREFIX}{sub.name}",
                    "",
                    str(sub.files),
                    format_size(sub.bytes),
                    "",
                ]
            )
    total_files = sum(stat.files for stat in stats)
    total_bytes = sum(stat.bytes for stat in stats)
    rows.append([t("Total"), "", str(total_files), format_size(total_bytes), ""])
    ui.render_table(
        t("Storage usage"),
        [t("Directory"), t("Path"), t("Files"), t("Size"), t("Free")],
        rows,
    )

    reclaimable_bytes, _reclaimable_files = _reclaimable(stats)
    if reclaimable_bytes > 0:
        ui.info(
            t("Reclaimable with 'clean cache' and 'clean logs': {size}").format(
                size=format_size(reclaimable_bytes)
            )
        )


def _clean(stats: list[DirStat], *, assume_yes: bool) -> None:
    """Remove every existing data directory after one confirmation."""
    existing = [stat for stat in stats if stat.path.exists()]
    if not existing:
        ui.info(t("Nothing to clean."))
        return

    for stat in existing:
        ui.info(t("{label}: {path}").format(label=stat.label, path=stat.path))

    if not assume_yes:
        question = t("Remove all data in these {count} director(ies)?").format(
            count=len(existing)
        )
        if not ui.confirm(question, default=False):
            ui.warn(t("Cancelled."))
            return

    failed = False
    for stat in existing:
        try:
            shutil.rmtree(stat.path)
            ui.success(t("Successfully removed {desc}.").format(desc=stat.path))
        except Exception as exc:
            failed = True
            ui.error(
                t("Failed to remove {desc}: {error}").format(desc=stat.path, error=exc)
            )

    if failed:
        raise SystemExit(EXIT_FAILURE)


class StorageCmd(Command):
    name = "storage"
    help = t("Show disk usage of the application's data directories.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )
        parser.add_argument(
            "--clean",
            action="store_true",
            help=t("Remove the data directories listed by this command."),
        )
        parser.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help=t("Skip the confirmation prompt used by --clean."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        as_json = bool(getattr(args, "as_json", False))
        clean = bool(getattr(args, "clean", False))

        if as_json and clean:
            fail(t("--json and --clean cannot be combined."), EXIT_USAGE)

        stats = _collect(_resolve_paths())

        if as_json:
            reclaimable_bytes, reclaimable_files = _reclaimable(stats)
            payload = {stat.key: stat.to_dict() for stat in stats}
            payload["total"] = {
                "files": sum(stat.files for stat in stats),
                "bytes": sum(stat.bytes for stat in stats),
                "reclaimable_bytes": reclaimable_bytes,
                "reclaimable_files": reclaimable_files,
            }
            ui.emit(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        _render(stats)

        if clean:
            _clean(stats, assume_yes=bool(getattr(args, "yes", False)))
