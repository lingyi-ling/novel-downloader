#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.storage
------------------------------------------

Report how much disk space the application's data directories use and manage
safe backups of the chapter databases.

Categories are the same directories the ``clean`` command operates on:
``raw_data``, ``cache``, ``downloads`` and ``logs``. Each bucket also reports
the free space of the volume it lives on and its largest immediate
subdirectories (typically the site keys under ``raw_data``), so a runaway site
is visible at a glance. ``--json`` adds a machine-readable view of the same
data plus the ``reclaimable`` totals: what ``clean cache`` + ``clean logs``
would free. ``--clean`` removes the existing directories after a single
confirmation; it is the bulk equivalent of ``clean book --all``,
``clean cache`` and ``clean logs``.

``storage backup --out DIR`` takes a consistent SQLite online backup of every
``chapter.<stage>.sqlite`` file under the raw-data directory, including data
still sitting in a WAL, and writes one timestamped copy per database while
mirroring the ``<site>/<book>`` layout. ``storage restore --from FILE``
validates the file (integrity check, schema version, required columns) before
atomically replacing the target database; an existing database is preserved
in a ``.pre-restore-*.bak`` sibling first and is never overwritten without
``--yes``. Both subcommands accept ``--json`` for a single-line report.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from argparse import ArgumentParser, Namespace, _SubParsersAction
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from novel_downloader.apps.cli import ui
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t
from novel_downloader.infra.persistence.chapter_storage import (
    ChapterDatabaseInfo,
    ChapterStorageCorruptError,
    ChapterStorageSchemaError,
    inspect_chapter_database,
    inspect_wal_damage,
)
from novel_downloader.libs.filesystem import (
    RemovalItem,
    plan_removal,
    remove_path_safely,
)

from .base import (
    EXIT_FAILURE,
    EXIT_USAGE,
    Command,
    emit_removal_preview,
    expand_path,
    fail,
    is_safe_path_component,
    localize_help,
)
from .base import format_size as format_size  # re-exported for doctor/update

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

#: The stage databases written by the download/process pipeline
#: (``chapter.raw.sqlite``, ``chapter.cleaner.sqlite``, ...).
_CHAPTER_DB_RE = re.compile(r"^chapter\.(?P<stage>[^./\\]+)\.sqlite$")

#: Backup names produced by :class:`StorageBackupCmd`: the stage database name
#: plus a UTC timestamp inserted before the extension.
_BACKUP_NAME_RE = re.compile(
    r"^chapter\.(?P<stage>[^./\\]+)\.(?P<stamp>\d{8}T\d{6}Z)\.sqlite$"
)

#: Timestamp format embedded in backup and pre-restore file names.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


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


def _effective_targets(stats: list[DirStat]) -> list[DirStat]:
    """
    Collapse configured directories into the set that must actually be removed.

    Configurations may overlap (``downloads`` inside ``raw_data``, the same
    path used for two categories, ...). Removing a parent already deletes its
    descendants, so attempting the child afterwards fails with "path not
    found" and turns an otherwise successful ``--clean`` into exit code 1.
    Duplicates are collapsed and any entry nested under another target is
    dropped; the surviving targets are returned in category order.
    """
    unique: list[DirStat] = []
    seen: set[Path] = set()
    for stat in stats:
        if stat.path in seen:
            continue
        seen.add(stat.path)
        unique.append(stat)

    return [
        stat
        for stat in unique
        if not any(
            other.path != stat.path and stat.path.is_relative_to(other.path)
            for other in unique
        )
    ]


def _preview_clean(stats: list[DirStat], *, as_json: bool) -> None:
    """Report what :func:`_clean` would remove without touching the disk."""
    items: list[RemovalItem] = []
    for stat in _effective_targets(stats):
        if not stat.path.exists():
            continue
        try:
            # Same root/allow_root arguments as the real removal, so a path
            # the deletion helper would reject is an error here too.
            item = plan_removal(stat.path, root=stat.path.parent, allow_root=True)
        except (OSError, ValueError) as exc:
            fail(
                t("Failed to inspect {desc}: {error}").format(
                    desc=stat.path, error=exc
                ),
                EXIT_FAILURE,
            )
        if item is not None:
            items.append(item)
    emit_removal_preview(
        command="storage --clean",
        items=items,
        as_json=as_json,
    )


def _clean(stats: list[DirStat], *, assume_yes: bool) -> None:
    """Remove every existing data directory after one confirmation."""
    existing = [stat for stat in _effective_targets(stats) if stat.path.exists()]
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
        # A path may have disappeared with an earlier (parent) removal, or a
        # concurrent process may have cleaned it; that is not a failure.
        if not stat.path.exists():
            continue
        try:
            # Each bucket is itself the allowed root: `storage --clean` is an
            # explicit "remove this configured directory" request.  The helper
            # still refuses filesystem roots/home/cwd and never follows links.
            remove_path_safely(stat.path, root=stat.path.parent, allow_root=True)
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
    def register(cls, subparsers: _SubParsersAction[ArgumentParser]) -> None:
        """Register the legacy flag interface plus ``backup``/``restore``.

        The subparsers are intentionally optional: ``storage --json`` and
        ``storage --clean`` predate them and must keep working unchanged.
        """
        parser = subparsers.add_parser(
            cls.name,
            help=cls.help,
            description=cls.help,
            add_help=False,
        )
        localize_help(parser)
        sub = parser.add_subparsers(
            dest="subcommand",
            title=t("Subcommands"),
            description=t(
                "Manage chapter database backups; without a subcommand this "
                "command reports disk usage."
            ),
        )
        for subcmd in (StorageBackupCmd, StorageRestoreCmd):
            subcmd.register(sub)
        cls.add_arguments(parser)
        parser.set_defaults(func=cls.run)

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
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=t("Show what --clean would remove without deleting anything."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        as_json = bool(getattr(args, "as_json", False))
        clean = bool(getattr(args, "clean", False))
        dry_run = bool(getattr(args, "dry_run", False))

        if dry_run and not clean:
            fail(t("--dry-run requires --clean."), EXIT_USAGE)
        if as_json and clean and not dry_run:
            fail(t("--json and --clean cannot be combined."), EXIT_USAGE)

        stats = _collect(_resolve_paths())

        if dry_run:
            _preview_clean(stats, as_json=as_json)
            return

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


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    """Return the UTC timestamp embedded in backup file names."""
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)


def _sha256(path: Path) -> str:
    """Hash a file without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_database_files(path: Path) -> None:
    """Remove a database and any sidecars, ignoring missing/permission errors."""
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        with contextlib.suppress(OSError):
            candidate.unlink()


def _needs_snapshot_copy(exc: sqlite3.OperationalError) -> bool:
    """True when SQLite could not open the file read-only (e.g. WAL sidecars).

    A locked database is *not* retried this way: the online backup API is the
    correct tool there, and copying a file another process is writing could
    produce a torn snapshot.
    """
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "unable to open",
            "readonly",
            "read-only",
            "attempt to write",
        )
    )


@contextlib.contextmanager
def _source_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` read-only for backup/inspection.

    Read-only connections are the safe default: a backup or restore must never
    modify its source.  A WAL database whose ``-shm`` file cannot be created
    (a file copied without its sidecars, a read-only directory, ...) cannot be
    opened this way at all; in that case the main file and any sidecars are
    copied to a private temporary directory and the copy is opened instead.
    The source bytes are still never written to.

    ``path`` may be relative (``storage restore --from ./backups/...``); a
    file URI requires an absolute path, so resolve it first.  Without this a
    relative ``--from`` reached ``Path.as_uri()`` and escaped as a traceback.
    """
    resolved = path.expanduser().resolve()
    problem = inspect_wal_damage(resolved)
    if problem is not None:
        raise ChapterStorageCorruptError(
            f"'{path}' has a damaged write-ahead log ({problem}). Nothing was "
            "read from or written to it; recover the sidecar before retrying."
        )

    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA schema_version").fetchone()
    except sqlite3.OperationalError as exc:
        conn.close()
        if not _needs_snapshot_copy(exc):
            raise
    else:
        try:
            yield conn
        finally:
            conn.close()
        return

    with tempfile.TemporaryDirectory(prefix="noveldl-snapshot-") as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite"
        shutil.copy2(path, snapshot)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(f"{snapshot}{suffix}"))
        # ``copy2`` preserves a read-only attribute; the snapshot itself must
        # be writable so SQLite can recover the WAL into it.
        with contextlib.suppress(OSError):
            snapshot.chmod(0o600)
        copy_conn = sqlite3.connect(str(snapshot), timeout=5.0)
        try:
            yield copy_conn
        finally:
            copy_conn.close()


def _write_snapshot(source: Path, target: Path) -> ChapterDatabaseInfo:
    """Write a consistent, verified SQLite copy of ``source`` to ``target``.

    The copy is built by the online backup API (so committed WAL frames are
    included), forced out of WAL mode so the result is a self-contained single
    file, re-opened read-only and validated, then atomically moved into place.
    On any failure the temporary file is removed and ``target`` is untouched.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    temp = Path(raw_temp)

    try:
        with _source_connection(source) as src:
            info = inspect_chapter_database(
                src,
                source=str(source),
                require_schema=False,
                allow_newer=True,
            )
            dest = sqlite3.connect(str(temp), timeout=5.0)
            try:
                src.backup(dest)
                dest.commit()
                dest.execute("PRAGMA journal_mode = DELETE").fetchone()
            finally:
                dest.close()

        with contextlib.suppress(OSError):
            temp.chmod(0o600)
        # Verify the bytes that are about to be published, not just the source.
        with _source_connection(temp) as check:
            inspect_chapter_database(
                check,
                source=str(temp),
                require_schema=False,
                allow_newer=True,
            )
        with temp.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        _remove_database_files(temp)
        raise

    return info


def _find_chapter_databases(root: Path, *, skip: Path | None = None) -> list[Path]:
    """Return every ``chapter.<stage>.sqlite`` under ``root``, sorted.

    Symlinks are ignored so a link pointing outside the data directory cannot
    pull foreign data into a backup.  ``skip`` excludes a directory tree (the
    backup destination when it lives inside ``raw_data``).
    """
    if not root.is_dir():
        return []

    found: list[Path] = []
    for candidate in sorted(root.rglob("chapter.*.sqlite")):
        if not _CHAPTER_DB_RE.match(candidate.name):
            continue
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if skip is not None:
            with contextlib.suppress(OSError, ValueError):
                if candidate.resolve().is_relative_to(skip):
                    continue
        found.append(candidate)
    return found


def _emit_json(payload: dict[str, object]) -> None:
    """Write one compact JSON line to stdout."""
    ui.emit(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _run_backup(raw_root: Path, out_dir: Path, *, as_json: bool) -> None:
    """Back up every chapter database below ``raw_root`` into ``out_dir``."""
    out_dir = out_dir.resolve()
    if out_dir.exists() and not out_dir.is_dir():
        fail(
            t("Backup destination is not a directory: {path}").format(path=out_dir),
            EXIT_USAGE,
        )

    root = raw_root.resolve()
    # Only skip the destination when it is a proper subtree of the source; if
    # the user backs up into raw_data itself, discovery runs first and the
    # timestamped names cannot match the source pattern anyway.
    skip = out_dir if out_dir != root and out_dir.is_relative_to(root) else None
    sources = _find_chapter_databases(root, skip=skip)

    stamp = _timestamp()
    plan: list[tuple[Path, Path]] = []
    for source in sources:
        match = _CHAPTER_DB_RE.match(source.name)
        if match is None:  # pragma: no cover - _find_chapter_databases filters
            continue
        relative = source.relative_to(root)
        target = (
            out_dir / relative.parent / f"chapter.{match.group('stage')}.{stamp}.sqlite"
        )
        if target.exists():
            fail(
                t("Backup target already exists, refusing to overwrite: {path}").format(
                    path=target
                ),
                EXIT_FAILURE,
            )
        plan.append((source, target))

    if not plan:
        if as_json:
            _emit_json(
                {
                    "command": "storage backup",
                    "ok": True,
                    "timestamp": stamp,
                    "out": str(out_dir),
                    "count": 0,
                    "bytes": 0,
                    "files": [],
                }
            )
        else:
            ui.info(t("No chapter databases found to back up."))
        return

    entries: list[dict[str, object]] = []
    created: list[Path] = []
    total_bytes = 0
    source = plan[0][0]
    try:
        for source, target in plan:
            info = _write_snapshot(source, target)
            created.append(target)
            size = target.stat().st_size
            total_bytes += size
            entries.append(
                {
                    "source": str(source),
                    "target": str(target),
                    "bytes": size,
                    "sha256": _sha256(target),
                    "schema_version": info.schema_version,
                    "chapters": info.chapter_count,
                }
            )
            if not as_json:
                ui.info(
                    t("Backed up {source} -> {target}.").format(
                        source=source, target=target
                    )
                )
    except Exception as exc:
        # A backup set is only useful as a whole; drop the files this run
        # already produced so a retry cannot mistake them for a full set.
        for path in created:
            _remove_database_files(path)
        fail(t("Failed to back up {source}: {error}").format(source=source, error=exc))

    if as_json:
        _emit_json(
            {
                "command": "storage backup",
                "ok": True,
                "timestamp": stamp,
                "out": str(out_dir),
                "count": len(entries),
                "bytes": total_bytes,
                "files": entries,
            }
        )
    else:
        ui.success(
            t("Created {count} backup file(s) in {path}.").format(
                count=len(entries), path=out_dir
            )
        )


def _derive_restore_target(source: Path, raw_root: Path) -> Path:
    """Derive ``raw_data/<site>/<book>/chapter.<stage>.sqlite`` from a backup.

    Backups mirror the original layout, so the last two parent directories are
    the site and book IDs. A file copied out of that layout needs ``--to``.
    """
    match = _BACKUP_NAME_RE.match(source.name)
    if match is None:
        fail(
            t("Cannot derive a restore target from {path}; pass --to.").format(
                path=source
            ),
            EXIT_USAGE,
        )

    book_dir = source.parent
    site_dir = book_dir.parent
    if (
        not is_safe_path_component(book_dir.name)
        or not is_safe_path_component(site_dir.name)
        or site_dir == site_dir.parent
    ):
        fail(
            t("Cannot derive a restore target from {path}; pass --to.").format(
                path=source
            ),
            EXIT_USAGE,
        )

    return (
        raw_root
        / site_dir.name
        / book_dir.name
        / f"chapter.{match.group('stage')}.sqlite"
    )


def _validated_restore_target(
    source: Path, raw_root: Path, target_arg: str | None
) -> Path:
    """Resolve and validate the database path a restore may write."""
    target = (
        expand_path(target_arg)
        if target_arg
        else _derive_restore_target(source, raw_root)
    )

    if _CHAPTER_DB_RE.match(target.name) is None:
        fail(
            t(
                "Restore target must be named like chapter.<stage>.sqlite: {path}"
            ).format(path=target),
            EXIT_USAGE,
        )

    root = raw_root.resolve()
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    if resolved == root or not resolved.is_relative_to(root):
        fail(
            t("Restore target must be inside the raw data directory: {path}").format(
                path=target
            ),
            EXIT_USAGE,
        )
    return target


def _preserve_existing(target: Path, stamp: str) -> Path:
    """Save the database a restore is about to replace.

    Uses the online backup API when the file is a readable database. A corrupt
    file cannot be opened by SQLite at all; it is copied byte-for-byte (with
    its sidecars) instead, so the user still has the original data.
    """
    base = target.with_name(f"{target.name}.pre-restore-{stamp}.bak")
    backup = base
    counter = 1
    while backup.exists():
        backup = base.with_name(f"{base.name}-{counter}")
        counter += 1

    try:
        _write_snapshot(target, backup)
        return backup
    except Exception:
        _remove_database_files(backup)

    try:
        shutil.copy2(target, backup)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(f"{backup}{suffix}"))
    except OSError:
        _remove_database_files(backup)
        raise
    return backup


def _run_restore(
    raw_root: Path,
    source: Path,
    target_arg: str | None,
    *,
    assume_yes: bool,
    as_json: bool,
) -> None:
    """Validate ``source`` and atomically install it as the target database."""
    if not source.is_file():
        fail(t("Restore source does not exist: {path}").format(path=source))

    target = _validated_restore_target(source, raw_root, target_arg)

    exists = target.exists()
    if exists and not target.is_file():
        fail(t("Restore target is not a database file: {path}").format(path=target))
    if exists and source.resolve() == target.resolve():
        fail(
            t("Refusing to restore a file onto itself: {path}").format(path=target),
            EXIT_USAGE,
        )
    if exists and not assume_yes:
        fail(
            t(
                "Refusing to overwrite {path} without --yes "
                "(the existing database would be backed up first)."
            ).format(path=target),
            EXIT_USAGE,
        )

    try:
        with _source_connection(source) as conn:
            info = inspect_chapter_database(conn, source=str(source))
    except (
        ChapterStorageCorruptError,
        ChapterStorageSchemaError,
        sqlite3.Error,
        OSError,
    ) as exc:
        fail(
            t("Backup file is not a valid chapter database: {error}").format(error=exc)
        )

    preserved: Path | None = None
    if exists:
        try:
            preserved = _preserve_existing(target, _timestamp())
        except Exception as exc:
            fail(
                t(
                    "Failed to back up the existing database; nothing was "
                    "changed: {error}"
                ).format(error=exc)
            )
        if not as_json:
            ui.info(
                t("Preserved the existing database as {path}.").format(path=preserved)
            )

    try:
        _write_snapshot(source, target)
    except Exception as exc:
        fail(t("Failed to restore {source}: {error}").format(source=source, error=exc))

    # Sidecars belong to the replaced file. Their data was captured by the
    # online backup above; leaving them next to the new file would let SQLite
    # try to apply a foreign WAL or roll back a stale journal.
    for suffix in ("-wal", "-shm", "-journal"):
        with contextlib.suppress(OSError):
            Path(f"{target}{suffix}").unlink()

    size = target.stat().st_size
    if as_json:
        _emit_json(
            {
                "command": "storage restore",
                "ok": True,
                "from": str(source),
                "target": str(target),
                "replaced_existing": exists,
                "pre_restore_backup": str(preserved) if preserved else None,
                "schema_version": info.schema_version,
                "chapters": info.chapter_count,
                "bytes": size,
                "sha256": _sha256(target),
            }
        )
    else:
        ui.success(
            t("Restored {source} to {target}.").format(source=source, target=target)
        )


class StorageBackupCmd(Command):
    name = "backup"
    help = t("Back up every chapter database to a directory.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--out",
            required=True,
            help=t("Directory that receives the timestamped backup files."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print a single-line JSON report on stdout."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        raw_root = _resolve_paths()["raw"]
        _run_backup(
            raw_root,
            expand_path(args.out),
            as_json=bool(getattr(args, "as_json", False)),
        )


class StorageRestoreCmd(Command):
    name = "restore"
    help = t("Restore a chapter database from a backup file.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--from",
            dest="source",
            required=True,
            help=t("Backup file to restore."),
        )
        parser.add_argument(
            "--to",
            dest="target",
            help=t(
                "Database path to write (must be inside the raw data directory). "
                "Derived from the backup's site/book path when omitted."
            ),
        )
        parser.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help=t("Allow replacing an existing database (it is backed up first)."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print a single-line JSON report on stdout."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        raw_root = _resolve_paths()["raw"]
        _run_restore(
            raw_root,
            expand_path(args.source),
            args.target,
            assume_yes=bool(getattr(args, "yes", False)),
            as_json=bool(getattr(args, "as_json", False)),
        )
