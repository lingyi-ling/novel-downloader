#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.storage
------------------------------------------

Report how much disk space the application's data directories use.

Categories are the same directories the ``clean`` command operates on:
``raw_data``, ``cache``, ``downloads`` and ``logs``. ``--clean`` removes the
existing directories after a single confirmation; it is the bulk equivalent of
``clean book --all``, ``clean cache`` and ``clean logs``.
"""

from __future__ import annotations

import json
import os
import shutil
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
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


@dataclass
class DirStat:
    """Recursive size/file count of one data directory."""

    key: str
    label: str
    path: Path
    files: int = 0
    bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "path": str(self.path),
            "files": self.files,
            "bytes": self.bytes,
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


def _scan(path: Path) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for ``path``, tolerating errors."""
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


def _collect(paths: dict[str, Path]) -> list[DirStat]:
    """Build one ``DirStat`` per category in :data:`CATEGORIES` order."""
    stats: list[DirStat] = []
    for key, label in CATEGORIES:
        files, total = _scan(paths[key])
        stats.append(
            DirStat(key=key, label=label, path=paths[key], files=files, bytes=total)
        )
    return stats


def _format_bytes(count: int) -> str:
    """Format a byte count with a binary unit suffix."""
    if count < 1024:
        return f"{count} B"
    value = float(count)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PiB"


def _render(stats: list[DirStat]) -> None:
    """Print the usage table."""
    rows = [
        [stat.label, str(stat.path), str(stat.files), _format_bytes(stat.bytes)]
        for stat in stats
    ]
    total_files = sum(stat.files for stat in stats)
    total_bytes = sum(stat.bytes for stat in stats)
    rows.append([t("Total"), "", str(total_files), _format_bytes(total_bytes)])
    ui.render_table(
        t("Storage usage"),
        [t("Directory"), t("Path"), t("Files"), t("Size")],
        rows,
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
            payload = {stat.key: stat.to_dict() for stat in stats}
            payload["total"] = {
                "files": sum(stat.files for stat in stats),
                "bytes": sum(stat.bytes for stat in stats),
            }
            ui.emit(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        _render(stats)

        if clean:
            _clean(stats, assume_yes=bool(getattr(args, "yes", False)))
