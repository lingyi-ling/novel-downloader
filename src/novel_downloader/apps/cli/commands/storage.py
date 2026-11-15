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
mirroring the ``<site>/<book>`` layout. Every copy is re-read after it is
published and checked with the same integrity and schema rules as ``storage
verify``; a copy that fails is removed together with the rest of the set this
run produced, the command exits 1, and the source databases are left untouched.
``--no-verify`` skips that post-write check so a copy this build cannot open
(a database written by a newer build, for example) is still preserved. ``--keep
N`` prunes older backups of each book database down to the newest ``N`` after
the new, verified set has been published; only files this command itself names
(``chapter.<stage>.<UTC>.sqlite``, or ``chapter.<stage>.<UTC>.<n>.sqlite`` when
a same-second copy already exists) are ever considered, and the files written
by the current run are never removed. ``storage restore --from FILE|DIR``
validates every source (integrity check, schema version, required columns)
before atomically installing it; an existing database is preserved in a
``.pre-restore-*.bak`` sibling first and is never overwritten without
``--force`` (``--yes`` is kept as an alias). A directory source restores the
whole ``<site>/<book>`` set, keeping the newest copy of each stage and skipping
older duplicates; any failure rolls the already-installed group back. Targets
must stay inside the raw-data directory and are refused when a path component
is a symbolic link. ``--dry-run`` reports the plan without writing anything.
Both subcommands accept ``--json`` for a single-line report. A ``backup --json``
run that fails at runtime always prints that one line with ``ok: false``, a
stable ``error`` category (``not_database``, ``integrity_failed``,
``wal_damaged``, ``schema_unsupported``, ``sequence_exhausted``, ``io_error``,
...), a human-readable ``message`` and the affected ``source``; usage errors
still exit 2 with no JSON.

``storage verify`` walks the raw-data directory, opens every
``chapter.<stage>.sqlite`` read-only and reports whether it is healthy: SQLite's
integrity check, the schema version (older is fine, newer is not), the required
columns and a checksum walk of any ``-wal`` sidecar. Damaged files are only
reported -- unlike the download path, this command never copies, renames or
repairs them. Paths that live under the user's home directory are shortened to
``~`` so the report can be shared without leaking the local layout. Human mode
announces the scan and every database on stderr as it goes, and a Ctrl+C stops
the walk with a summary of what was checked so far; ``--json`` stays a single
line on stdout.
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
from typing import NoReturn

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
    stdin_is_interactive,
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
#: plus a UTC timestamp inserted before the extension. A ``.<n>`` sequence is
#: appended when another backup of the same database already occupies the
#: same-second name, so concurrent runs never collide.
_BACKUP_NAME_RE = re.compile(
    r"^chapter\.(?P<stage>[^./\\]+)\.(?P<stamp>\d{8}T\d{6}Z)(?:\.(?P<seq>\d+))?\.sqlite$"
)

#: Timestamp format embedded in backup and pre-restore file names.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: Same-second backups claimed per database before the run gives up. Each
#: collision takes the next ``.<n>`` suffix; a directory holding a thousand
#: copies of one database from a single second is not a supported workload.
_MAX_BACKUP_SEQUENCE = 1000


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


def _resolve_paths(*, non_interactive: bool | None = None) -> dict[str, Path]:
    """
    Resolve the four data directories from the current configuration.

    :param non_interactive: Whether a missing configuration may prompt.  The
        default follows stdin, so a piped/CI invocation never blocks here;
        ``--json`` callers pass ``True`` even on a terminal.
    """
    if non_interactive is None:
        non_interactive = not stdin_is_interactive()
    config_data = load_or_init_config(None, non_interactive=non_interactive)
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
                "Manage chapter databases; without a subcommand this command "
                "reports disk usage."
            ),
        )
        for subcmd in (StorageBackupCmd, StorageRestoreCmd, StorageVerifyCmd):
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

        stats = _collect(
            _resolve_paths(
                non_interactive=as_json or not stdin_is_interactive(),
            )
        )

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
    except BaseException:
        # A non-database file raises ``DatabaseError`` (not an
        # ``OperationalError``), and any other failure must not leak the
        # handle either: on Windows an open handle makes the caller's rollback
        # unlink fail, leaving a corrupt copy behind.
        conn.close()
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


@dataclass
class RetentionReport:
    """Outcome of one ``--keep`` pruning pass.

    ``candidates`` are the backups selected for removal, ``removed`` the ones
    that actually disappeared and ``failed`` the ``(path, error)`` pairs whose
    deletion raised. ``keep == 0`` means pruning is disabled.
    """

    keep: int
    dry_run: bool
    candidates: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "keep": self.keep,
            "dry_run": self.dry_run,
            "candidates": [str(path) for path in self.candidates],
            "removed": [str(path) for path in self.removed],
            "failed": [
                {"path": str(path), "error": error} for path, error in self.failed
            ],
        }

    @property
    def ok(self) -> bool:
        return not self.failed


#: Stable ``error`` identifiers emitted by ``storage backup --json`` when the
#: run fails. They are part of the machine contract: a consumer may branch on
#: ``error`` instead of parsing the human-readable ``message``.
BACKUP_ERROR_CATEGORIES: tuple[str, ...] = (
    "not_database",
    "integrity_failed",
    "wal_damaged",
    "schema_unsupported",
    "sequence_exhausted",
    "io_error",
    "database_error",
    "retention_failed",
    "config_error",
    "internal_error",
    "verification_failed",
)


def _classify_backup_error(exc: BaseException) -> str:
    """Map a backup failure to its stable ``error`` identifier.

    The order matters: :class:`ChapterStorageCorruptError` is also a
    :class:`sqlite3.DatabaseError`, and the corrupted-target case is already
    classified by the post-write check.
    """
    if isinstance(exc, ChapterStorageSchemaError):
        return "schema_unsupported"
    if isinstance(exc, ChapterStorageCorruptError):
        # Only ``_source_connection`` raises this for a WAL sidecar; the other
        # instance comes from ``inspect_chapter_database``'s integrity check.
        text = str(exc).lower()
        if "write-ahead log" in text:
            return "wal_damaged"
        return "integrity_failed"
    if isinstance(exc, FileExistsError):
        # Raised by ``_reserve_backup_target`` once every allowed name for the
        # current second is taken (it never overwrites a backup).
        return "sequence_exhausted"
    if isinstance(exc, sqlite3.DatabaseError):
        if "not a database" in str(exc).lower():
            return "not_database"
        return "database_error"
    if isinstance(exc, OSError):
        return "io_error"
    return "internal_error"


def _backup_failure_payload(
    *,
    category: str,
    message: str,
    source: Path | None,
    out_dir: Path,
    stamp: str | None,
    keep: int,
    dry_run: bool,
    verification_failed: bool = False,
    verification_error: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build the one-line ``--json`` report for a failed backup run.

    ``count`` and ``bytes`` are zero and ``files`` is empty because a failed
    run rolls back every file it published; the sources and pre-existing
    backups stay untouched. ``verified`` is false either way: no complete set
    survived to be checked. The success shape is unchanged.
    """
    payload: dict[str, object] = {
        "command": "storage backup",
        "ok": False,
        "error": category,
        "message": message,
        "source": str(source) if source is not None else None,
        "timestamp": stamp,
        "out": str(out_dir),
        "count": 0,
        "bytes": 0,
        "files": [],
        "verified": False,
        "verification_failed": verification_failed,
        "retention": RetentionReport(keep=keep, dry_run=dry_run).to_dict(),
    }
    if verification_error is not None:
        payload["verification_error"] = verification_error
    return payload


def _report_backup_failure(
    message: str,
    *,
    category: str,
    source: Path | None,
    out_dir: Path,
    stamp: str,
    keep: int,
    dry_run: bool,
    as_json: bool,
    verification_failed: bool = False,
    verification_error: dict[str, str] | None = None,
) -> NoReturn:
    """Emit the failure report (one JSON line when asked) and exit 1.

    ``--json`` keeps exactly one line on stdout; the human-readable ``message``
    still goes to stderr through :func:`fail`, so a monitoring script learns
    both the stable category and why the run failed.
    """
    if as_json:
        _emit_json(
            _backup_failure_payload(
                category=category,
                message=message,
                source=source,
                out_dir=out_dir,
                stamp=stamp,
                keep=keep,
                dry_run=dry_run,
                verification_failed=verification_failed,
                verification_error=verification_error,
            )
        )
    fail(message)


def _plan_retention(
    out_dir: Path,
    keep: int,
    *,
    protect: set[Path],
    failures: list[tuple[Path, str]],
) -> list[Path]:
    """Return the backups ``keep`` allows to be removed, oldest first.

    Only regular files named ``chapter.<stage>.<UTC>.sqlite`` (optionally
    ``chapter.<stage>.<UTC>.<n>.sqlite`` for a same-second copy) written
    directly by this command are considered; directories, symlinks and every
    other file are ignored. Backups are grouped by their containing book
    directory *and* stage, so a book holding both ``raw`` and ``cleaner``
    databases keeps ``N`` copies of each instead of one stage evicting the
    other. ``protect`` holds the resolved paths written by the current run:
    they are still counted for ordering but are never selected for removal (a
    skewed clock can therefore leave one extra file behind, never delete a
    fresh backup).

    Unreadable directories are appended to ``failures`` instead of aborting the
    scan, so one bad subtree cannot make pruning look successful.
    """

    groups: dict[tuple[Path, str], list[tuple[str, int, Path, Path]]] = {}

    def _onerror(exc: OSError) -> None:
        failing = Path(exc.filename) if exc.filename else out_dir
        failures.append((failing, str(exc)))

    for root, dirs, names in os.walk(out_dir, onerror=_onerror):
        # ``os.walk`` does not descend into directory symlinks by default; keep
        # that guarantee explicit even if the platform default ever changes.
        dirs[:] = [
            name for name in dirs if not os.path.islink(os.path.join(root, name))
        ]
        parent = Path(root)
        for name in names:
            match = _BACKUP_NAME_RE.match(name)
            if match is None:
                continue
            candidate = parent / name
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                resolved = candidate.resolve()
            except OSError as exc:
                failures.append((candidate, str(exc)))
                continue
            groups.setdefault((parent, match.group("stage")), []).append(
                (
                    match.group("stamp"),
                    int(match.group("seq") or 0),
                    candidate,
                    resolved,
                )
            )

    doomed: list[Path] = []
    for entries in groups.values():
        # Newest first: the fixed-width UTC stamp sorts lexicographically, and
        # the numeric sequence orders same-second copies (``.10`` after ``.9``).
        # A file without a sequence predates the suffix scheme and counts as
        # the first copy of that second. The path breaks any remaining tie
        # deterministically.
        entries.sort(
            key=lambda entry: (entry[0], entry[1], str(entry[2])), reverse=True
        )
        doomed.extend(
            path
            for _stamp, _seq, path, resolved in entries[keep:]
            if resolved not in protect
        )
    return sorted(doomed, key=str)


def _prune_old_backups(
    out_dir: Path,
    *,
    keep: int,
    dry_run: bool,
    protect: set[Path],
    as_json: bool,
) -> RetentionReport:
    """Apply the ``--keep`` policy and report what was (or would be) removed."""
    report = RetentionReport(keep=keep, dry_run=dry_run)
    if keep <= 0:
        return report

    report.candidates = _plan_retention(
        out_dir, keep, protect=protect, failures=report.failed
    )

    for path in report.candidates:
        if dry_run:
            if not as_json:
                ui.info(t("Would remove old backup {path}.").format(path=path))
            continue
        try:
            path.unlink()
        except OSError as exc:
            report.failed.append((path, str(exc)))
            ui.error(
                t("Failed to remove old backup {path}: {error}").format(
                    path=path, error=exc
                )
            )
        else:
            report.removed.append(path)
            if not as_json:
                ui.info(t("Removed old backup {path}.").format(path=path))

    if as_json:
        return report

    if dry_run and report.candidates:
        ui.info(
            t(
                "Retention: {count} old backup(s) would be removed, keeping the "
                "newest {keep} per book and stage."
            ).format(count=len(report.candidates), keep=keep)
        )
    elif report.removed:
        ui.success(
            t(
                "Retention: removed {count} old backup(s), keeping the newest "
                "{keep} per book and stage."
            ).format(count=len(report.removed), keep=keep)
        )
    return report


def _reserve_backup_target(target: Path) -> Path:
    """Atomically claim ``target``, or the first free ``.<n>`` sibling.

    Two backups of the same database can land in the same UTC second: two
    concurrent runs, a scheduled job overlapping a manual one, or a clock
    whose sub-second detail the fixed-width stamp cannot express. The name is
    claimed with ``O_CREAT|O_EXCL``, so racers can never pick the same file;
    the loser moves on to ``chapter.<stage>.<UTC>.1.sqlite``, then ``.2``, and
    so on. The returned path is an empty placeholder owned by the caller: the
    snapshot writer replaces it atomically and the caller removes it if the
    snapshot fails.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    base = target.stem  # chapter.<stage>.<UTC>
    suffix = target.suffix
    for index in range(0, _MAX_BACKUP_SEQUENCE + 1):
        candidate = (
            target if index == 0 else target.with_name(f"{base}.{index}{suffix}")
        )
        # A directory (or a file another process is still creating) cannot be
        # claimed; skip it without opening anything. This also covers Windows,
        # where opening an existing directory reports EACCES instead of EEXIST.
        if os.path.lexists(candidate):
            continue
        try:
            handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Lost the race to a concurrent run; take the next name.
            continue
        os.close(handle)
        return candidate

    # Every accepted name for this second is taken. Raising instead of exiting
    # lets the caller's cleanup drop the partial set it already published.
    raise FileExistsError(
        t("Backup target already exists, refusing to overwrite: {path}").format(
            path=target
        )
    )


class _BackupVerificationError(RuntimeError):
    """A published backup failed the post-write integrity/schema check.

    ``category`` is the stable machine identifier reported as ``error`` in
    ``--json`` output (``integrity_failed``, ``schema_unsupported``,
    ``wal_damaged``, ...). It defaults to ``verification_failed`` so a caller
    that only supplies a plain reason string keeps a defined identifier.
    """

    def __init__(
        self,
        source: Path,
        target: Path,
        reason: str,
        category: str = "verification_failed",
    ) -> None:
        super().__init__(reason)
        self.source = source
        self.target = target
        self.reason = reason
        self.category = category


def _report_verification_failure(
    exc: _BackupVerificationError,
    *,
    out_dir: Path,
    stamp: str,
    keep: int,
    dry_run: bool,
    as_json: bool,
) -> NoReturn:
    """Explain a failed post-write check and exit 1.

    The caller has already removed every file this run wrote, so ``count`` is
    zero: the failed set is not a usable backup. ``--json`` still prints one
    machine-readable line carrying the stable ``error`` category, the failing
    source, target and reason, so a monitoring script learns why the run failed
    instead of only its exit code.
    """
    message = t(
        "Backup verification failed for {target}: {reason}. No backup from "
        "this run was kept; the source databases are unchanged."
    ).format(target=exc.target, reason=exc.reason)
    _report_backup_failure(
        message,
        category=exc.category,
        source=exc.source,
        out_dir=out_dir,
        stamp=stamp,
        keep=keep,
        dry_run=dry_run,
        as_json=as_json,
        verification_failed=True,
        verification_error={
            "source": str(exc.source),
            "target": str(exc.target),
            "reason": exc.reason,
        },
    )


def _run_backup(
    raw_root: Path,
    out_dir: Path,
    *,
    as_json: bool,
    keep: int = 0,
    dry_run: bool = False,
    verify: bool = True,
) -> None:
    """Back up every chapter database below ``raw_root`` into ``out_dir``.

    Each published copy is re-read and validated with the same rules as
    ``storage verify`` unless ``verify`` is false. A failed check removes the
    whole set this run wrote (backups are only useful as a complete set) and
    exits 1; the source databases are never modified either way.
    """
    out_dir = out_dir.resolve()
    if out_dir.exists() and not out_dir.is_dir():
        fail(
            t("Backup destination is not a directory: {path}").format(path=out_dir),
            EXIT_USAGE,
        )

    # Fixed at the start so every failure report (including one raised while
    # the source tree is being discovered) carries the run's timestamp.
    stamp = _timestamp()

    root = raw_root.resolve()
    # Only skip the destination when it is a proper subtree of the source; if
    # the user backs up into raw_data itself, discovery runs first and the
    # timestamped names cannot match the source pattern anyway.
    skip = out_dir if out_dir != root and out_dir.is_relative_to(root) else None
    try:
        sources = _find_chapter_databases(root, skip=skip)
    except OSError as exc:
        # A source tree that cannot be walked is a runtime failure; ``--json``
        # must still produce its one machine-readable line.
        _report_backup_failure(
            t("Failed to back up {source}: {error}").format(source=root, error=exc),
            category="io_error",
            source=None,
            out_dir=out_dir,
            stamp=stamp,
            keep=keep,
            dry_run=dry_run,
            as_json=as_json,
        )

    plan: list[tuple[Path, Path]] = []
    for source in sources:
        match = _CHAPTER_DB_RE.match(source.name)
        if match is None:  # pragma: no cover - _find_chapter_databases filters
            continue
        relative = source.relative_to(root)
        target = (
            out_dir / relative.parent / f"chapter.{match.group('stage')}.{stamp}.sqlite"
        )
        plan.append((source, target))

    if not plan:
        # Nothing was published, so there is no fresh backup to protect and no
        # successful backup run to follow: leave existing backups untouched.
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
                    "verified": False,
                    "verification_failed": False,
                    "retention": RetentionReport(keep=keep, dry_run=dry_run).to_dict(),
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
        for source, base_target in plan:
            # Claim the name first: an existing same-second backup (from an
            # earlier or concurrent run) must be neither overwritten nor fatal.
            target = _reserve_backup_target(base_target)
            try:
                info = _write_snapshot(source, target)
                if verify:
                    reason = _verify_database(target)
                    if reason is not None:
                        raise _BackupVerificationError(
                            source,
                            target,
                            reason,
                            getattr(reason, "category", "verification_failed"),
                        )
            except BaseException:
                # The placeholder and any copy written for this source are
                # ours; a failure (including Ctrl+C during verification) must
                # not leave them behind for retention or a retry to count.
                _remove_database_files(target)
                raise
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
                    "verified": verify,
                }
            )
            if not as_json:
                ui.info(
                    t("Backed up {source} -> {target}.").format(
                        source=source, target=target
                    )
                )
    except _BackupVerificationError as exc:
        # A backup set is only useful as a whole; drop the files this run
        # already produced so a retry cannot mistake them for a full set.
        for path in created:
            _remove_database_files(path)
        _report_verification_failure(
            exc,
            out_dir=out_dir,
            stamp=stamp,
            keep=keep,
            dry_run=dry_run,
            as_json=as_json,
        )
    except BaseException as exc:
        # Same rollback for every other failure, including Ctrl+C: a run that
        # did not publish its complete set must not leave the files it did
        # write behind.  Interrupts are re-raised so the CLI aborter keeps its
        # usual contract; ordinary errors are reported and exit 1.
        for path in created:
            _remove_database_files(path)
        if not isinstance(exc, Exception):
            raise
        _report_backup_failure(
            t("Failed to back up {source}: {error}").format(source=source, error=exc),
            category=_classify_backup_error(exc),
            source=source,
            out_dir=out_dir,
            stamp=stamp,
            keep=keep,
            dry_run=dry_run,
            as_json=as_json,
        )

    protected: set[Path] = set()
    for path in created:
        with contextlib.suppress(OSError):
            protected.add(path.resolve())

    if as_json:
        retention = _prune_old_backups(
            out_dir,
            keep=keep,
            dry_run=dry_run,
            protect=protected,
            as_json=True,
        )
        _emit_json(
            {
                "command": "storage backup",
                "ok": retention.ok,
                "timestamp": stamp,
                "out": str(out_dir),
                "count": len(entries),
                "bytes": total_bytes,
                "files": entries,
                "verified": verify,
                "verification_failed": False,
                "retention": retention.to_dict(),
                **(
                    {
                        "error": "retention_failed",
                        "message": t(
                            "Retention failed: {count} old backup(s) could not "
                            "be removed; the new backups were kept."
                        ).format(count=len(retention.failed)),
                    }
                    if not retention.ok
                    else {}
                ),
            }
        )
    else:
        ui.success(
            t("Created {count} backup file(s) in {path}.").format(
                count=len(entries), path=out_dir
            )
        )
        retention = _prune_old_backups(
            out_dir,
            keep=keep,
            dry_run=dry_run,
            protect=protected,
            as_json=False,
        )

    # A backup that cannot prune is still a good backup; the files are kept and
    # the run reports failure so cron/scripts notice the growing directory.
    if not retention.ok:
        raise SystemExit(EXIT_FAILURE)


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


#: Stable ``error`` identifiers emitted by ``storage restore --json``. A
#: consumer may branch on ``error`` instead of parsing the localized message.
RESTORE_ERROR_CATEGORIES: tuple[str, ...] = (
    "source_missing",
    "no_backups",
    "not_database",
    "integrity_failed",
    "wal_damaged",
    "schema_unsupported",
    "database_error",
    "io_error",
    "config_error",
    "internal_error",
)


class _RestoreSourceError(RuntimeError):
    """A restore source failed validation before anything was written."""

    def __init__(self, message: str, category: str) -> None:
        super().__init__(message)
        self.category = category


def _classify_restore_error(exc: BaseException) -> str:
    """Map a restore failure to its stable ``error`` identifier."""
    if isinstance(exc, FileNotFoundError):
        return "source_missing"
    return _classify_backup_error(exc)


@dataclass
class RestorePlanItem:
    """One ``source -> target`` pair a restore will install.

    ``stage`` and ``stamp`` come from the backup file name; a file that does
    not carry the backup naming (a hand-copied ``.bak``) uses the stage of the
    explicit ``--to`` target and an empty stamp.
    """

    source: Path
    target: Path
    stage: str
    stamp: str = ""
    seq: int = 0


@dataclass
class InstalledRestore:
    """A target a run already replaced, plus how to roll it back.

    ``preserved`` is the ``.pre-restore-*.bak`` holding the original bytes, or
    ``None`` when the target did not exist before this run.
    """

    target: Path
    preserved: Path | None


def _validate_target_path(target: Path, raw_root: Path) -> Path:
    """Resolve and validate one database path a restore may write.

    The target must be named like a chapter database, live strictly inside the
    raw-data directory, and must not be (or sit behind) a symbolic link: the
    containment check is done on the resolved path, so a link inside the tree
    that points elsewhere would otherwise be approved and then written through.
    """
    if _CHAPTER_DB_RE.match(target.name) is None:
        fail(
            t(
                "Restore target must be named like chapter.<stage>.sqlite: {path}"
            ).format(path=target),
            EXIT_USAGE,
        )

    # Lexical absolute form: normalizes ``..`` without following links, so the
    # walk below still sees every symlink on the user's spelling.
    absolute = Path(os.path.abspath(target))
    root = raw_root.resolve()
    try:
        resolved = target.resolve()
    except OSError:
        resolved = absolute
    if resolved == root or not resolved.is_relative_to(root):
        fail(
            t("Restore target must be inside the raw data directory: {path}").format(
                path=target
            ),
            EXIT_USAGE,
        )

    probe = absolute
    # Both spellings of the root stop the walk, so a raw-data directory that is
    # itself reached through a link is not mistaken for a link inside it.
    stops = {root, Path(os.path.abspath(raw_root))}
    while True:
        # The walk stops at the raw-data root itself: that directory may
        # legitimately be a symlink (a volume mounted into the data tree), and
        # the containment check above already ran on its resolved form. Every
        # component *below* it must be a real directory, so a link planted in
        # ``raw_data/<site>/<book>`` cannot redirect the write.
        if probe in stops or probe.parent == probe:
            break
        if probe.is_symlink():
            fail(
                t("Refusing to restore through a symbolic link: {path}").format(
                    path=probe
                ),
                EXIT_USAGE,
            )
        probe = probe.parent

    if target.exists() and not target.is_file():
        fail(
            t("Restore target is not a database file: {path}").format(path=target),
            EXIT_USAGE,
        )
    return target


def _find_backup_sources(root: Path) -> list[tuple[Path, re.Match[str]]]:
    """Return every backup-named file under ``root``, sorted by path.

    Only regular files named ``chapter.<stage>.<UTC>[.<n>].sqlite`` are
    considered; a ``.pre-restore-*.bak``, a hand-renamed copy or a foreign file
    is ignored. Directory symlinks are never followed and file symlinks are
    skipped, so a link cannot pull foreign bytes into a restore. An unreadable
    subtree raises instead of silently producing a partial set.
    """
    errors: list[OSError] = []

    def _onerror(exc: OSError) -> None:
        errors.append(exc)

    found: list[tuple[Path, re.Match[str]]] = []
    for dirpath, dirnames, filenames in os.walk(
        root, onerror=_onerror, followlinks=False
    ):
        dirnames[:] = [
            name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in sorted(filenames):
            match = _BACKUP_NAME_RE.match(name)
            if match is None:
                continue
            candidate = Path(dirpath) / name
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
            except OSError:
                continue
            found.append((candidate, match))

    if errors:
        raise errors[0]
    found.sort(key=lambda pair: str(pair[0]))
    return found


def _plan_file_restore(
    source: Path, raw_root: Path, target_arg: str | None
) -> list[RestorePlanItem]:
    """Plan a single-database restore from one backup file."""
    match = _BACKUP_NAME_RE.match(source.name)
    if target_arg:
        target = expand_path(target_arg)
        target_match = _CHAPTER_DB_RE.match(target.name)
        if target_match is None:
            fail(
                t(
                    "Restore target must be named like chapter.<stage>.sqlite: {path}"
                ).format(path=target),
                EXIT_USAGE,
            )
        stage = target_match.group("stage")
    else:
        # Without ``--to`` the target comes from the backup name; a file outside
        # the backup scheme makes ``_derive_restore_target`` exit 2.
        target = _derive_restore_target(source, raw_root)
        if match is None:  # pragma: no cover - _derive_restore_target exits
            fail(
                t("Cannot derive a restore target from {path}; pass --to.").format(
                    path=source
                ),
                EXIT_USAGE,
            )
        stage = match.group("stage")

    return [
        RestorePlanItem(
            source=source,
            target=target,
            stage=stage,
            stamp=match.group("stamp") if match is not None else "",
            seq=int(match.group("seq") or 0) if match is not None else 0,
        )
    ]


def _plan_directory_restore(
    source_dir: Path, raw_root: Path, target_arg: str | None
) -> tuple[list[RestorePlanItem], int]:
    """Plan a set restore, returning ``(items, skipped)``.

    Backups are grouped by their book directory and stage; only the newest copy
    of each group is restored and the older ones are counted as ``skipped``.
    This keeps ``storage backup --keep`` history from restoring an older copy
    over a newer one in the same run.

    ``--to`` names either the raw-data root that receives the mirrored layout
    or, for a single-database set, one explicit database path.
    """
    found = _find_backup_sources(source_dir)

    single: Path | None = None
    root = raw_root
    if target_arg:
        candidate = expand_path(target_arg)
        if _CHAPTER_DB_RE.match(candidate.name) is not None:
            single = candidate
        else:
            root = candidate

    groups: dict[tuple[Path, str], list[RestorePlanItem]] = {}
    for path, match in found:
        target = single if single is not None else _derive_restore_target(path, root)
        groups.setdefault((path.parent, match.group("stage")), []).append(
            RestorePlanItem(
                source=path,
                target=target,
                stage=match.group("stage"),
                stamp=match.group("stamp"),
                seq=int(match.group("seq") or 0),
            )
        )

    items: list[RestorePlanItem] = []
    skipped = 0
    for entries in groups.values():
        # Newest first: fixed-width UTC stamp, then the same-second sequence.
        entries.sort(key=lambda item: (item.stamp, item.seq, str(item.source)))
        entries.reverse()
        items.append(entries[0])
        skipped += len(entries) - 1

    items.sort(key=lambda item: str(item.target))
    if single is not None and len(items) != 1:
        fail(
            t(
                "--to must name a directory when the backup set holds {count} "
                "database(s)."
            ).format(count=len(items)),
            EXIT_USAGE,
        )
    return items, skipped


def _validate_restore_plan(
    items: list[RestorePlanItem],
    *,
    raw_root: Path,
    source: Path,
) -> None:
    """Reject every structurally unsafe plan entry before anything is read.

    Only path safety is checked here -- containment inside ``raw_data``,
    symlinks, a file restored onto itself and a target inside the backup tree.
    The ``--force`` gate is a separate step (:func:`_check_overwrite_gate`) so a
    corrupt backup is always reported as such instead of being masked by a
    usage error.
    """
    source_root: Path | None = None
    if source.is_dir():
        with contextlib.suppress(OSError):
            source_root = source.resolve()

    for item in items:
        _validate_target_path(item.target, raw_root)
        if item.source.is_symlink():
            fail(
                t("Refusing to restore from a symbolic link: {path}").format(
                    path=item.source
                ),
                EXIT_USAGE,
            )
        try:
            resolved_source = item.source.resolve()
            resolved_target = item.target.resolve()
        except OSError:
            resolved_source = item.source
            resolved_target = item.target
        if resolved_source == resolved_target:
            fail(
                t("Refusing to restore a file onto itself: {path}").format(
                    path=item.target
                ),
                EXIT_USAGE,
            )
        if source_root is not None and resolved_target.is_relative_to(source_root):
            fail(
                t("Refusing to restore into the backup directory: {path}").format(
                    path=item.target
                ),
                EXIT_USAGE,
            )


def _check_overwrite_gate(items: list[RestorePlanItem], *, force: bool) -> None:
    """Exit 2 unless ``--force`` was given for every existing target."""
    if force:
        return
    for item in items:
        if item.target.exists():
            fail(
                t(
                    "Refusing to overwrite {path} without --force (the existing "
                    "database would be backed up first)."
                ).format(path=item.target),
                EXIT_USAGE,
            )


def _inspect_restore_source(source: Path, *, verify: bool) -> ChapterDatabaseInfo:
    """Validate a restore source read-only and return its schema/row count.

    With ``verify`` (the default) the source must pass the same strict checks
    as ``storage verify``: integrity, schema not newer than this build, and the
    required table/columns. ``--no-verify`` relaxes only the schema rules (a
    file written by a newer build stays restorable) while the integrity check
    is still enforced, so a physically damaged file is never installed.
    """
    try:
        with _source_connection(source) as conn:
            return inspect_chapter_database(
                conn,
                source=str(source),
                require_schema=verify,
                allow_newer=not verify,
            )
    except (
        ChapterStorageCorruptError,
        ChapterStorageSchemaError,
        sqlite3.Error,
        OSError,
    ) as exc:
        raise _RestoreSourceError(str(exc), _classify_restore_error(exc)) from exc


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


def _rollback_restore(installed: list[InstalledRestore]) -> list[str]:
    """Undo every target a failed run already replaced, newest first.

    A preserved ``.pre-restore-*.bak`` is moved back over the target; a target
    that did not exist before the run is removed. Sidecars written by the new
    file are dropped either way. Returns the ``(path: error)`` strings for the
    entries that could not be restored, so the caller can report a partial
    rollback instead of claiming success.
    """
    failures: list[str] = []
    for entry in reversed(installed):
        for suffix in ("-wal", "-shm", "-journal"):
            with contextlib.suppress(OSError):
                Path(f"{entry.target}{suffix}").unlink()
        if entry.preserved is None:
            with contextlib.suppress(FileNotFoundError):
                entry.target.unlink()
            continue
        try:
            os.replace(entry.preserved, entry.target)
        except OSError as exc:
            failures.append(f"{entry.target}: {exc}")
    return failures


def _restore_plan_payload(
    items: list[RestorePlanItem],
    *,
    source: Path,
    skipped: int,
    dry_run: bool,
) -> dict[str, object]:
    """Describe a restore plan for ``--dry-run`` and success reports."""
    files = [
        {
            "source": str(item.source),
            "target": str(item.target),
            "stage": item.stage,
        }
        for item in items
    ]
    return {
        "command": "storage restore",
        "ok": True,
        "from": str(source),
        "target": str(items[0].target) if len(items) == 1 else None,
        "restored": 0,
        "files": files,
        "skipped": skipped,
        "dry_run": dry_run,
        "error": None,
        "message": None,
    }


def _restore_failure_payload(
    *,
    category: str,
    message: str,
    source: Path,
    target: Path | None,
    dry_run: bool,
    skipped: int = 0,
) -> dict[str, object]:
    """Build the one-line ``--json`` report for a failed restore."""
    return {
        "command": "storage restore",
        "ok": False,
        "from": str(source),
        "target": str(target) if target is not None else None,
        "restored": 0,
        "files": [],
        "skipped": skipped,
        "dry_run": dry_run,
        "error": category,
        "message": message,
    }


def _report_restore_failure(
    message: str,
    *,
    category: str,
    source: Path,
    target: Path | None,
    dry_run: bool,
    as_json: bool,
    skipped: int = 0,
) -> NoReturn:
    """Emit the restore failure report (one JSON line when asked) and exit 1."""
    if as_json:
        _emit_json(
            _restore_failure_payload(
                category=category,
                message=message,
                source=source,
                target=target,
                dry_run=dry_run,
                skipped=skipped,
            )
        )
    fail(message)


def _run_restore(
    raw_root: Path,
    source: Path,
    target_arg: str | None,
    *,
    force: bool,
    as_json: bool,
    dry_run: bool = False,
    verify: bool = True,
) -> None:
    """Validate every source, then install the planned set with rollback.

    Nothing is written until the whole plan has been validated: a corrupt or
    unsupported file anywhere in a directory source aborts the run before the
    first target is touched. Once installation starts, each target is replaced
    atomically and the previous database is preserved in a
    ``.pre-restore-*.bak`` sibling; a failure (including Ctrl+C) rolls every
    already-installed target back and leaves no half-written group behind.
    """
    if not source.exists():
        _report_restore_failure(
            t("Restore source does not exist: {path}").format(path=source),
            category="source_missing",
            source=source,
            target=None,
            dry_run=dry_run,
            as_json=as_json,
        )
    if not source.is_file() and not source.is_dir():
        _report_restore_failure(
            t("Restore source does not exist: {path}").format(path=source),
            category="source_missing",
            source=source,
            target=None,
            dry_run=dry_run,
            as_json=as_json,
        )

    skipped = 0
    try:
        if source.is_dir():
            items, skipped = _plan_directory_restore(source, raw_root, target_arg)
        else:
            items = _plan_file_restore(source, raw_root, target_arg)
    except OSError as exc:
        _report_restore_failure(
            t("Failed to read {source}: {error}").format(source=source, error=exc),
            category="io_error",
            source=source,
            target=None,
            dry_run=dry_run,
            as_json=as_json,
        )

    if not items:
        _report_restore_failure(
            t("No backup files found in {path}.").format(path=source),
            category="no_backups",
            source=source,
            target=None,
            dry_run=dry_run,
            as_json=as_json,
        )

    _validate_restore_plan(items, raw_root=raw_root, source=source)

    # Validate every source before writing anything, so a bad file later in the
    # set cannot leave the earlier targets replaced.
    for item in items:
        try:
            _inspect_restore_source(item.source, verify=verify)
        except _RestoreSourceError as exc:
            _report_restore_failure(
                t("Backup file is not a valid chapter database: {error}").format(
                    error=exc
                ),
                category=exc.category,
                source=item.source,
                target=item.target,
                dry_run=dry_run,
                as_json=as_json,
                skipped=skipped,
            )

    if dry_run:
        if as_json:
            _emit_json(
                _restore_plan_payload(
                    items, source=source, skipped=skipped, dry_run=True
                )
            )
        else:
            for item in items:
                ui.info(
                    t("Would restore {source} to {target}.").format(
                        source=item.source, target=item.target
                    )
                )
            ui.info(
                t(
                    "Dry run: {count} database(s) would be restored from {source}."
                ).format(count=len(items), source=source)
            )
        return

    # Enforced after the dry-run return: a preview may describe an overwrite
    # without being authorised to perform one, and after source validation so a
    # corrupt backup is reported as corrupt rather than as a usage error.
    _check_overwrite_gate(items, force=force)

    stamp = _timestamp()
    installed: list[InstalledRestore] = []
    entries: list[dict[str, object]] = []
    try:
        for item in items:
            exists = item.target.exists()
            preserved: Path | None = None
            if exists:
                preserved = _preserve_existing(item.target, stamp)
                if not as_json:
                    ui.info(
                        t("Preserved the existing database as {path}.").format(
                            path=preserved
                        )
                    )
            # Recorded before the write so a failure part-way through still
            # restores this target (and drops the stray ``.bak``) on rollback.
            installed.append(InstalledRestore(item.target, preserved))
            info = _write_snapshot(item.source, item.target)
            # Sidecars belong to the replaced file. Their data was captured by
            # the online backup above; leaving them next to the new file would
            # let SQLite apply a foreign WAL or roll back a stale journal.
            for suffix in ("-wal", "-shm", "-journal"):
                with contextlib.suppress(OSError):
                    Path(f"{item.target}{suffix}").unlink()
            entries.append(
                {
                    "source": str(item.source),
                    "target": str(item.target),
                    "replaced_existing": exists,
                    "pre_restore_backup": str(preserved) if preserved else None,
                    "schema_version": info.schema_version,
                    "chapters": info.chapter_count,
                    "bytes": item.target.stat().st_size,
                    "sha256": _sha256(item.target),
                }
            )
            if not as_json and len(items) > 1:
                ui.info(
                    t("Restored {source} to {target}.").format(
                        source=item.source, target=item.target
                    )
                )
    except BaseException as exc:
        rollback_failures = _rollback_restore(installed)
        if rollback_failures:
            _report_restore_failure(
                t(
                    "Restore failed and could not be rolled back completely: {error}"
                ).format(error="; ".join(rollback_failures)),
                category="io_error",
                source=source,
                target=items[0].target,
                dry_run=dry_run,
                as_json=as_json,
                skipped=skipped,
            )
        if not isinstance(exc, Exception):
            # Ctrl+C and other interrupts keep the CLI's usual contract: the
            # group is rolled back, then the interrupt propagates.
            raise
        message = t("Failed to restore {source}: {error}").format(
            source=source, error=exc
        )
        _report_restore_failure(
            message,
            category=_classify_restore_error(exc),
            source=source,
            target=items[0].target if items else None,
            dry_run=dry_run,
            as_json=as_json,
            skipped=skipped,
        )

    if skipped and not as_json:
        ui.info(
            t("Skipped {count} older backup(s) in the backup set.").format(
                count=skipped
            )
        )

    if as_json:
        payload = _restore_plan_payload(
            items, source=source, skipped=skipped, dry_run=False
        )
        payload["restored"] = len(entries)
        payload["files"] = entries
        if len(entries) == 1:
            # Keep the single-file fields stable for existing consumers.
            payload.update(
                {
                    key: entries[0][key]
                    for key in (
                        "replaced_existing",
                        "pre_restore_backup",
                        "schema_version",
                        "chapters",
                        "bytes",
                        "sha256",
                    )
                }
            )
        _emit_json(payload)
    elif len(entries) == 1:
        ui.success(
            t("Restored {source} to {target}.").format(
                source=entries[0]["source"], target=entries[0]["target"]
            )
        )
    else:
        ui.success(
            t("Restored {count} database(s) from {source}.").format(
                count=len(entries), source=source
            )
        )


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _home_dir() -> str:
    """Return the user's home directory without a trailing separator."""
    try:
        home = os.path.expanduser("~")
    except (KeyError, RuntimeError):  # pragma: no cover - exotic environments
        return ""
    if not home or home == "~":
        return ""
    return home.rstrip("\\/")


def _redact_path(path: Path | str) -> str:
    """Shorten a path under the user's home directory to ``~/...``."""
    text = str(path)
    home = _home_dir()
    if not home:
        return text
    if os.path.normcase(text) == os.path.normcase(home):
        return "~"
    prefix = os.path.normcase(home) + os.sep
    if os.path.normcase(text).startswith(prefix):
        return "~" + text[len(home) :]
    return text


def _redact_home(text: str) -> str:
    """Replace the home-directory prefix inside an arbitrary message."""
    home = _home_dir()
    return text.replace(home, "~") if home else text


class _VerifyProblem(str):
    """A verification-failure reason that also carries its stable category.

    Subclassing :class:`str` keeps every existing consumer working unchanged:
    the value still formats into messages, JSON reports and equality checks
    exactly like the reason text. ``storage backup`` reads ``category`` to fill
    the machine-readable ``error`` field, falling back to
    ``verification_failed`` for a plain string (a monkeypatched or third-party
    provider of the reason).
    """

    category: str

    def __new__(cls, reason: str, category: str) -> _VerifyProblem:
        problem = str.__new__(cls, reason)
        problem.category = category
        return problem


def _verify_database(path: Path) -> _VerifyProblem | None:
    """Return ``None`` when ``path`` is a healthy chapter database.

    The file is only ever read: the WAL sidecar is checksummed before SQLite
    opens the main file, the connection is opened read-only, and a damaged
    database is neither copied nor renamed. A returned value is the reason the
    database cannot be used by this build, carrying the stable failure
    category used by ``storage backup --json``.
    """
    wal_problem = inspect_wal_damage(path)
    if wal_problem is not None:
        return _VerifyProblem(
            t("damaged write-ahead log: {problem}").format(problem=wal_problem),
            "wal_damaged",
        )

    try:
        with _source_connection(path) as conn:
            # ``_source_connection`` may fall back to a temporary snapshot for
            # a WAL database whose sidecars cannot be locked. Keep even that
            # connection read-only so verification can never write anything.
            conn.execute("PRAGMA query_only = ON")
            inspect_chapter_database(conn, source=_redact_path(path))
    except ChapterStorageSchemaError as exc:
        return _VerifyProblem(_redact_home(str(exc)), "schema_unsupported")
    except ChapterStorageCorruptError as exc:
        return _VerifyProblem(_redact_home(str(exc)), "integrity_failed")
    except sqlite3.Error as exc:
        text = _redact_home(str(exc) or type(exc).__name__)
        lowered = text.lower()
        if "not a database" in lowered or "not an sqlite" in lowered:
            category = "not_database"
        elif "malformed" in lowered or "corrupt" in lowered:
            category = "integrity_failed"
        else:
            category = "database_error"
        return _VerifyProblem(text, category)
    except OSError as exc:
        return _VerifyProblem(_redact_home(str(exc) or type(exc).__name__), "io_error")
    return None


def _run_verify(raw_root: Path, *, as_json: bool) -> None:
    """Check every chapter database below ``raw_root`` without modifying it.

    In human mode the scan and every database are announced on stderr before
    they are opened, so a large or slow library never looks hung. ``--json``
    deliberately prints nothing but the single final report on stdout.

    A Ctrl+C stops the walk after the database in flight and reports what was
    checked so far. The interrupt is then re-raised, so ``cli_main`` keeps its
    usual contract ("Aborted." on stderr, exit code 1); a partial report never
    exits 0.

    Two interrupt-specific guarantees hold whatever the stage:

    * an interrupt during discovery (before any database is opened) still
      produces the one ``--json`` line, with ``total`` 0 because the scan never
      produced a count;
    * every completed check is recorded with a single atomic append, so
      ``checked``, ``healthy``, ``corrupt`` and ``errors`` can never disagree
      with each other in a partial report.
    """
    if not as_json:
        ui.info(t("Scanning for chapter databases..."))
    databases: list[Path] = []
    interrupted = False
    try:
        databases = _find_chapter_databases(raw_root)
    except KeyboardInterrupt:
        # No database is in flight, so the walk has nothing to add: report a
        # zero total and let the run end as interrupted. A scripted consumer
        # must not receive an empty stdout for an interrupted ``--json`` run.
        interrupted = True

    total = len(databases)
    #: ``(display, reason)`` per completed check; ``reason`` is ``None`` for a
    #: healthy database. Counters are derived from this list afterwards so an
    #: interrupt can never leave ``corrupt`` disagreeing with ``errors``.
    records: list[tuple[str, _VerifyProblem | None]] = []
    try:
        for index, database in enumerate(databases, 1):
            display = _redact_path(database)
            if not as_json:
                ui.info(
                    t("Checking {index}/{total}: {path}").format(
                        index=index, total=total, path=display
                    )
                )
            reason = _verify_database(database)
            # One atomic append per completed check (a signal is only handled
            # between bytecodes, so the tuple is either fully recorded or the
            # database counts as still in flight). The old ``checked += 1``
            # followed by ``healthy += 1`` left a window where an interrupted
            # healthy database was reported as corrupt with no ``errors``
            # entry.
            records.append((display, reason))
            if not as_json:
                if reason is None:
                    ui.success(t("{path}: ok").format(path=display))
                else:
                    ui.error(
                        t("{path}: corrupt: {reason}").format(
                            path=display, reason=reason
                        )
                    )
    except KeyboardInterrupt:
        # Only completed records are counted, so the summary never claims an
        # interrupted database was healthy.
        interrupted = True

    checked = len(records)
    healthy = sum(1 for _display, reason in records if reason is None)
    errors: list[dict[str, str]] = [
        {"path": display, "reason": reason}
        for display, reason in records
        if reason is not None
    ]
    corrupt = len(errors)
    ok = not interrupted and corrupt == 0

    if as_json:
        payload: dict[str, object] = {
            "ok": ok,
            "total": total,
            "healthy": healthy,
            "corrupt": corrupt,
            "errors": errors,
        }
        # Only an interrupted run adds the marker; the normal payload keeps its
        # established shape for existing consumers.
        if interrupted:
            payload["interrupted"] = True
        _emit_json(payload)
    elif interrupted:
        # Checked before the empty-library case: an interrupt during discovery
        # leaves ``total`` at 0 but is not "nothing to verify".
        ui.warn(
            t(
                "Interrupted after checking {checked} of {total} database(s): "
                "{healthy} healthy, {corrupt} corrupt."
            ).format(
                checked=checked,
                total=total,
                healthy=healthy,
                corrupt=corrupt,
            )
        )
    elif total == 0:
        ui.info(t("No chapter databases found to verify."))
    else:
        summary = t(
            "Verified {total} database(s): {healthy} healthy, {corrupt} corrupt."
        ).format(total=total, healthy=healthy, corrupt=corrupt)
        if ok:
            ui.success(summary)
        else:
            ui.error(summary)

    if interrupted:
        # Let ``cli_main`` print "Aborted." and turn this into exit code 1,
        # exactly as for any other interrupted command.
        raise KeyboardInterrupt
    if not ok:
        raise SystemExit(EXIT_FAILURE)


class StorageVerifyCmd(Command):
    name = "verify"
    help = t("Verify every chapter database without modifying it.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print a single-line JSON report on stdout."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        as_json = bool(getattr(args, "as_json", False))
        raw_root = _resolve_paths(
            non_interactive=as_json or not stdin_is_interactive()
        )["raw"]
        _run_verify(raw_root, as_json=as_json)


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
        parser.add_argument(
            "--keep",
            type=int,
            default=0,
            metavar="N",
            help=t(
                "After a successful backup, delete all but the newest N backups "
                "of each book database (0 disables cleanup)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=t(
                "Report which old backups --keep would delete without deleting "
                "them; the new backup itself is still written."
            ),
        )
        parser.add_argument(
            "--no-verify",
            dest="verify",
            action="store_false",
            default=True,
            help=t(
                "Write the new backups without the post-write integrity and "
                "schema check that 'storage verify' performs."
            ),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        keep = int(getattr(args, "keep", 0) or 0)
        dry_run = bool(getattr(args, "dry_run", False))
        verify = bool(getattr(args, "verify", True))
        if keep < 0:
            fail(
                t("--keep must be 0 (disabled) or a positive integer."),
                EXIT_USAGE,
            )
        if dry_run and keep <= 0:
            fail(t("--dry-run requires --keep."), EXIT_USAGE)
        as_json = bool(getattr(args, "as_json", False))
        out_dir = expand_path(args.out).resolve()
        try:
            raw_root = _resolve_paths(
                non_interactive=as_json or not stdin_is_interactive()
            )["raw"]
        except SystemExit as exc:
            # ``_resolve_paths`` reports a missing/unparsable configuration on
            # stderr and exits 1. With ``--json`` the runtime-failure contract
            # still requires one machine-readable line on stdout; the usage
            # code and every other exit path are left untouched.
            if as_json and exc.code not in (None, 0, EXIT_USAGE):
                _emit_json(
                    _backup_failure_payload(
                        category="config_error",
                        message=t(
                            "Could not load the configuration; no backup was made."
                        ),
                        source=None,
                        out_dir=out_dir,
                        stamp=_timestamp(),
                        keep=keep,
                        dry_run=dry_run,
                    )
                )
            raise
        _run_backup(
            raw_root,
            out_dir,
            as_json=as_json,
            keep=keep,
            dry_run=dry_run,
            verify=verify,
        )


class StorageRestoreCmd(Command):
    name = "restore"
    help = t("Restore chapter databases from a backup file or directory.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--from",
            dest="source",
            required=True,
            help=t(
                "Backup file or backup directory to restore. A directory "
                "restores the newest copy of every stage it contains."
            ),
        )
        parser.add_argument(
            "--to",
            dest="target",
            help=t(
                "Database path or raw-data root to write (must be inside the "
                "raw data directory). Derived from the backup's site/book path "
                "when omitted."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=t("Report what would be restored without writing anything."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print a single-line JSON report on stdout."),
        )
        parser.add_argument(
            "--force",
            "-y",
            "--yes",
            dest="force",
            action="store_true",
            help=t(
                "Allow replacing existing databases (each one is backed up as "
                "a .pre-restore-*.bak first)."
            ),
        )
        parser.add_argument(
            "--no-verify",
            dest="verify",
            action="store_false",
            default=True,
            help=t(
                "Skip the strict schema check on the backup (the SQLite "
                "integrity check is still enforced)."
            ),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        as_json = bool(getattr(args, "as_json", False))
        source = expand_path(args.source)
        dry_run = bool(getattr(args, "dry_run", False))
        try:
            raw_root = _resolve_paths(
                non_interactive=as_json or not stdin_is_interactive()
            )["raw"]
        except SystemExit as exc:
            # ``_resolve_paths`` reports a missing configuration on stderr and
            # exits 1. ``--json`` still owes its consumer one machine-readable
            # line; usage errors (2) keep their established empty-stdout shape.
            if as_json and exc.code not in (None, 0, EXIT_USAGE):
                _emit_json(
                    _restore_failure_payload(
                        category="config_error",
                        message=t(
                            "Could not load the configuration; nothing was restored."
                        ),
                        source=source,
                        target=None,
                        dry_run=dry_run,
                    )
                )
            raise
        _run_restore(
            raw_root,
            source,
            args.target,
            force=bool(getattr(args, "force", False)),
            as_json=as_json,
            dry_run=dry_run,
            verify=bool(getattr(args, "verify", True)),
        )
