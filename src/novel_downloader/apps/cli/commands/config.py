#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.config
-----------------------------------------

"""

import errno
import json
import os
import stat
import tempfile
import tomllib
from argparse import ArgumentParser, Namespace, _SubParsersAction
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from novel_downloader import __version__
from novel_downloader.apps.cli import ui
from novel_downloader.infra.config import copy_default_config, validate_config
from novel_downloader.infra.config.file_io import MAX_CONFIG_FILE_BYTES
from novel_downloader.infra.config.validation import ConfigError
from novel_downloader.infra.i18n import available_languages, set_language, t
from novel_downloader.infra.paths import (
    DEFAULT_CONFIG_FILENAME,
    STATE_PATH,
    app_home,
)

from .base import (
    EXIT_FAILURE,
    EXIT_USAGE,
    Command,
    expand_path,
    fail,
    localize_help,
)

#: Suffix of the copy kept before ``config init --force`` replaces a file.
BACKUP_SUFFIX = ".bak"

#: Format version written into the export header. Bump only when the export
#: layout changes in a way importers must know about.
EXPORT_FORMAT_VERSION = 1

#: Root tables that make a TOML document a novel-downloader configuration.
_CONFIG_ROOT_TABLES = frozenset({"general", "sites", "plugins"})

#: Legacy JSON configuration the loader also accepts. An import always writes
#: TOML, so a JSON file is never replaced in place; a TOML file next to it is
#: created instead and wins the loader's priority order.
_ALTERNATE_CONFIG_FILENAME = "settings.json"


def _copy_file_atomic(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target`` via a same-directory temporary file.

    ``os.replace`` is atomic on every supported filesystem, and the temporary
    file lives next to ``target`` so the rename never crosses a device. A
    failure leaves both the source and any previous ``target`` byte-for-byte
    intact.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as out, source.open("rb") as inp:
            while chunk := inp.read(1024 * 1024):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, target)
    except BaseException:
        with suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


def _write_bytes_atomic(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` through a same-directory temporary file.

    The target directory is created when missing. ``os.replace`` is atomic, so
    a crash, a full disk or a read-only target leaves the previous file
    untouched; a failed write never leaves its temporary file behind.

    An existing target's permission bits are preserved (best effort), matching
    the behaviour of the configuration loader's own atomic writer. A new file
    keeps the private ``0600`` mode ``mkstemp`` assigns it, which is
    appropriate for an export that may carry credentials.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with suppress(OSError):
            mode = stat.S_IMODE(os.stat(target).st_mode)
            os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        with suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


def _emit_payload(payload: dict[str, object]) -> None:
    """Write a compact, single-line JSON document to stdout."""
    ui.emit(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _active_settings_path() -> Path | None:
    """Return the TOML settings file the loader would use, if one exists.

    Resolution matches :func:`novel_downloader.infra.config.load_config`: the
    current directory first, then :func:`app_home`. Only ``settings.toml`` is
    considered because the export/import format is TOML; a ``settings.json``
    in the same priority position is deliberately ignored rather than being
    rewritten as TOML behind the loader's back.
    """
    for base in (Path.cwd(), app_home()):
        candidate = base / DEFAULT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _import_target_path() -> Path:
    """Return the TOML file an import replaces.

    The target is chosen so the imported file always wins the loader's
    priority order (current directory before :func:`app_home`, TOML before
    JSON):

    * an existing local or app-home ``settings.toml`` is updated in place;
    * when only a legacy ``settings.json`` exists at a location, a TOML file
      next to it is created instead -- never written into the JSON file;
    * otherwise a new local ``settings.toml`` is created, mirroring ``config
      init``.

    Nothing is written here -- the path is only resolved.
    """
    local = Path.cwd() / DEFAULT_CONFIG_FILENAME
    if local.exists() or local.is_symlink():
        return local

    # A local JSON config outranks an app-home TOML config. Creating the TOML
    # file locally (instead of updating app home) is what makes the import
    # visible to the loader.
    local_json = Path.cwd() / _ALTERNATE_CONFIG_FILENAME
    if local_json.exists() or local_json.is_symlink():
        return local

    home = app_home() / DEFAULT_CONFIG_FILENAME
    if home.exists() or home.is_symlink():
        return home

    home_json = app_home() / _ALTERNATE_CONFIG_FILENAME
    if home_json.exists() or home_json.is_symlink():
        return home

    return local


def _read_toml_config(path: Path) -> tuple[str, dict[str, Any]]:
    """Read, parse and schema-check a TOML configuration file.

    Returns the decoded text (BOM removed) together with the parsed mapping.
    The checks run before callers write anything, so a malformed or
    unrepresentative file can never replace a working configuration.

    Error messages never include file contents: TOML syntax errors are
    reported by line/column, and validator messages name paths and problem
    classes only. That matters because a real configuration may contain proxy
    credentials or session cookies.

    :raises ValueError: On an unreadable/oversized file, invalid UTF-8,
        invalid TOML, schema errors, or a document without any known root
        table.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(
            t("Could not read {path}: {err}").format(
                path=path, err=exc.strerror or type(exc).__name__
            )
        ) from exc
    if size > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            t("Configuration file is too large: {path} ({size} bytes).").format(
                path=path, size=size
            )
        )

    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_FILE_BYTES + 1)
    except OSError as exc:
        raise ValueError(
            t("Could not read {path}: {err}").format(
                path=path, err=exc.strerror or type(exc).__name__
            )
        ) from exc
    if len(raw) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            t("Configuration file is too large: {path} ({size} bytes).").format(
                path=path, size=len(raw)
            )
        )

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            t(
                "Configuration file is not valid UTF-8: {path} "
                "(byte offset {position})."
            ).format(path=path, position=exc.start)
        ) from exc

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            t("Invalid TOML in {path}: {err}").format(path=path, err=exc)
        ) from exc

    if not _CONFIG_ROOT_TABLES.intersection(data):
        raise ValueError(
            t(
                "Not a novel-downloader configuration: {path} has no "
                "[general], [sites] or [plugins] table."
            ).format(path=path)
        )

    try:
        problems = validate_config(data)
    except ConfigError as exc:
        raise ValueError(
            t("Configuration validation failed: {err}").format(err=exc)
        ) from exc
    errors = [problem for problem in problems if problem.level == "error"]
    if errors:
        detail = "; ".join(
            f"{problem.path}: {problem.message}" for problem in errors[:5]
        )
        if len(errors) > 5:
            detail += f"; ... ({len(errors) - 5} more)"
        raise ValueError(
            t("Configuration validation failed: {detail}").format(detail=detail)
        )

    return text, data


def _export_header() -> bytes:
    """Build the version/timestamp comment block prepended to an export.

    The timestamp is UTC and second-precision; it records *when* the backup
    was taken without making the output depend on the machine's time zone.
    """
    exported_at = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    lines = [
        "# novel-downloader configuration export",
        f"# export-format: {EXPORT_FORMAT_VERSION}",
        f"# app-version: {__version__}",
        f"# exported-at: {exported_at}",
        "# note: may contain credentials (for example a proxy password); "
        "keep this file private.",
        "",
        "",
    ]
    return ("\n".join(lines)).encode("utf-8")


class ConfigCmd(Command):
    name = "config"
    help = t("Manage application configuration and settings")

    @classmethod
    def register(cls, subparsers: "_SubParsersAction[ArgumentParser]") -> None:
        parser = subparsers.add_parser(
            cls.name, help=cls.help, description=cls.help, add_help=False
        )
        localize_help(parser)
        sub = parser.add_subparsers(
            dest="subcommand", required=True, title=t("Subcommands")
        )

        for subcmd in (
            ConfigInitCmd,
            ConfigExportCmd,
            ConfigImportCmd,
            ConfigSetLangCmd,
        ):
            subcmd.register(sub)

    @classmethod
    def run(cls, args: Namespace) -> None:
        raise NotImplementedError("ConfigCmd should not be executed directly")


class ConfigInitCmd(Command):
    name = "init"
    help = t("Initialize default configuration files in the current directory.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--force",
            action="store_true",
            help=t("Force overwrite if the file already exists."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        target_path = Path.cwd() / DEFAULT_CONFIG_FILENAME
        backup_path = target_path.with_name(target_path.name + BACKUP_SUFFIX)
        force = bool(getattr(args, "force", False))
        as_json = bool(getattr(args, "as_json", False))

        def emit(
            status: str,
            *,
            backup: Path | None = None,
            error: str | None = None,
        ) -> None:
            payload: dict[str, object] = {
                "command": "config init",
                "path": str(target_path),
                "status": status,
                "backup": str(backup) if backup is not None else None,
                "force": force,
            }
            if error is not None:
                payload["error"] = error
            ui.emit(json.dumps(payload, ensure_ascii=False))

        def abort(message: str, error: str) -> NoReturn:
            if as_json:
                emit("error", error=error)
            else:
                ui.error(message)
            raise SystemExit(EXIT_FAILURE)

        # ``is_dir`` follows a symlink: a link to a directory must not be
        # replaced by a regular file.
        if target_path.is_dir():
            directory_error = IsADirectoryError(
                errno.EISDIR, os.strerror(errno.EISDIR), str(target_path)
            )
            abort(
                t("Failed to copy {filename}: {err}").format(
                    filename=DEFAULT_CONFIG_FILENAME, err=directory_error
                ),
                str(directory_error),
            )

        # ``exists`` is False for a dangling symlink; ``is_symlink`` still
        # lets us treat it as occupied instead of silently claiming to have
        # created a new file.
        exists = target_path.exists() or target_path.is_symlink()

        if exists and not force:
            # The default is deliberately non-destructive and never prompts:
            # replacing the file would throw away hand-written comments. A
            # script re-running init must stay quiet and exit successfully.
            message = t("{target} already exists (pass --force to overwrite)").format(
                target=DEFAULT_CONFIG_FILENAME
            )
            if as_json:
                emit("skipped")
            else:
                ui.warn(message)
            return

        used_backup: Path | None = None

        # Windows cannot ``os.replace`` over a file carrying the read-only
        # attribute, and the failed attempt would leave a read-only temporary
        # file behind. Report it before touching anything; POSIX replaces a
        # read-only file normally when its directory is writable.
        if (
            force
            and exists
            and target_path.exists()
            and os.name == "nt"
            and not os.access(target_path, os.W_OK)
        ):
            read_only_error = PermissionError(
                errno.EACCES, os.strerror(errno.EACCES), str(target_path)
            )
            abort(
                t("Failed to copy {filename}: {err}").format(
                    filename=DEFAULT_CONFIG_FILENAME, err=read_only_error
                ),
                str(read_only_error),
            )

        if force and exists:
            if not as_json:
                ui.warn(
                    t("Overwriting existing file: {filename}").format(
                        filename=DEFAULT_CONFIG_FILENAME
                    )
                )
            # A dangling symlink has no readable content to preserve; the link
            # itself is replaced below without following it.
            if target_path.exists():
                try:
                    _copy_file_atomic(target_path, backup_path)
                except Exception as exc:
                    abort(
                        t("Could not write {path}: {err}").format(
                            path=backup_path, err=exc
                        ),
                        str(exc),
                    )
                used_backup = backup_path
                if not as_json:
                    ui.info(t("Copied: {filename}").format(filename=backup_path.name))

        try:
            copy_default_config(target_path)
        except Exception as exc:
            abort(
                t("Failed to copy {filename}: {err}").format(
                    filename=DEFAULT_CONFIG_FILENAME, err=exc
                ),
                str(exc),
            )

        status = "overwritten" if exists else "created"
        if as_json:
            emit(status, backup=used_backup)
        else:
            ui.success(t("Copied: {filename}").format(filename=DEFAULT_CONFIG_FILENAME))


class ConfigExportCmd(Command):
    name = "export"
    help = t("Export the active configuration to a backup TOML file.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--out",
            required=True,
            metavar="FILE",
            help=t("Destination file for the exported configuration."),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=t(
                "Overwrite an existing destination; its previous contents are "
                "kept with a .bak suffix."
            ),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        out_path = expand_path(args.out).absolute()
        force = bool(getattr(args, "force", False))
        as_json = bool(getattr(args, "as_json", False))
        backup_path = out_path.with_name(out_path.name + BACKUP_SUFFIX)
        source_path = _active_settings_path()

        def emit(
            status: str,
            *,
            backup: Path | None = None,
            error: str | None = None,
        ) -> None:
            payload: dict[str, object] = {
                "command": "config export",
                "path": str(out_path),
                "source": str(source_path) if source_path is not None else None,
                "status": status,
                "backup": str(backup) if backup is not None else None,
                "force": force,
            }
            if error is not None:
                payload["error"] = error
            _emit_payload(payload)

        def abort(message: str, error: str) -> NoReturn:
            if as_json:
                emit("error", error=error)
            else:
                ui.error(message)
            raise SystemExit(EXIT_FAILURE)

        if out_path.is_dir():
            directory_error = IsADirectoryError(
                errno.EISDIR, os.strerror(errno.EISDIR), str(out_path)
            )
            abort(
                t("Could not write {path}: {err}").format(
                    path=out_path, err=directory_error
                ),
                str(directory_error),
            )

        if source_path is None:
            abort(
                t(
                    "No configuration file found to export. "
                    "Run 'novel-cli config init' first."
                ),
                "no configuration file found",
            )

        # Validate before touching the destination: an export must contain a
        # configuration the loader would actually accept.
        try:
            text, _ = _read_toml_config(source_path)
        except ValueError as exc:
            abort(
                t("Could not export {path}: {err}").format(path=source_path, err=exc),
                str(exc),
            )

        # ``exists`` is False for a dangling symlink; ``is_symlink`` still
        # treats it as occupied so the default stays non-destructive.
        exists = out_path.exists() or out_path.is_symlink()
        if exists and not force:
            abort(
                t("{path} already exists (pass --force to overwrite)").format(
                    path=out_path
                ),
                "destination already exists",
            )

        used_backup: Path | None = None
        if force and exists:
            if out_path.exists():
                try:
                    _copy_file_atomic(out_path, backup_path)
                except Exception as exc:
                    abort(
                        t("Could not write {path}: {err}").format(
                            path=backup_path, err=exc
                        ),
                        str(exc),
                    )
                used_backup = backup_path
            if not as_json:
                ui.warn(
                    t("Overwriting existing file: {filename}").format(
                        filename=out_path.name
                    )
                )

        try:
            _write_bytes_atomic(out_path, _export_header() + text.encode("utf-8"))
        except Exception as exc:
            abort(
                t("Could not write {path}: {err}").format(path=out_path, err=exc),
                str(exc),
            )

        status = "overwritten" if exists else "created"
        if as_json:
            emit(status, backup=used_backup)
            return

        if used_backup is not None:
            ui.info(t("Backed up previous file: {path}").format(path=used_backup))
        ui.success(t("Exported configuration: {path}").format(path=out_path))
        # The values are copied verbatim; this is the one place a warning is
        # warranted because the exported file may hold proxy credentials or
        # session cookies.
        ui.info(
            t(
                "The exported file may contain credentials "
                "(for example a proxy password). Keep it private."
            )
        )


class ConfigImportCmd(Command):
    name = "import"
    help = t("Import a TOML configuration file into the active settings.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--from",
            dest="source",
            required=True,
            metavar="FILE",
            help=t("Configuration file to import."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        source_path = expand_path(args.source).absolute()
        target_path = _import_target_path().absolute()
        as_json = bool(getattr(args, "as_json", False))
        backup_path = target_path.with_name(target_path.name + BACKUP_SUFFIX)

        def emit(
            status: str,
            *,
            backup: Path | None = None,
            error: str | None = None,
        ) -> None:
            payload: dict[str, object] = {
                "command": "config import",
                "path": str(target_path),
                "source": str(source_path),
                "status": status,
                "backup": str(backup) if backup is not None else None,
            }
            if error is not None:
                payload["error"] = error
            _emit_payload(payload)

        def abort(message: str, error: str) -> NoReturn:
            if as_json:
                emit("error", error=error)
            else:
                ui.error(message)
            raise SystemExit(EXIT_FAILURE)

        if not source_path.is_file():
            abort(
                t("Not a regular file: {path}").format(path=source_path),
                "source is not a regular file",
            )
        if target_path.is_dir():
            directory_error = IsADirectoryError(
                errno.EISDIR, os.strerror(errno.EISDIR), str(target_path)
            )
            abort(
                t("Could not write {path}: {err}").format(
                    path=target_path, err=directory_error
                ),
                str(directory_error),
            )

        # Parse + schema-check before any write. A rejected file therefore
        # leaves the active configuration byte-for-byte intact.
        try:
            text, _ = _read_toml_config(source_path)
        except ValueError as exc:
            abort(
                t("Could not import {path}: {err}").format(path=source_path, err=exc),
                str(exc),
            )

        used_backup: Path | None = None
        if target_path.exists():
            try:
                _copy_file_atomic(target_path, backup_path)
            except Exception as exc:
                abort(
                    t("Could not write {path}: {err}").format(
                        path=backup_path, err=exc
                    ),
                    str(exc),
                )
            used_backup = backup_path

        try:
            _write_bytes_atomic(target_path, text.encode("utf-8"))
        except Exception as exc:
            abort(
                t("Could not write {path}: {err}").format(path=target_path, err=exc),
                str(exc),
            )

        if as_json:
            emit("imported", backup=used_backup)
            return

        if used_backup is not None:
            ui.info(t("Backed up previous file: {path}").format(path=used_backup))
        ui.success(t("Imported configuration: {path}").format(path=target_path))


class ConfigSetLangCmd(Command):
    name = "set-lang"
    help = t("Set the interface language.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        languages = ", ".join(sorted(available_languages()))
        parser.add_argument(
            "lang",
            help=t("Language code (choices: {choices}; e.g. zh, en).").format(
                choices=languages
            ),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        try:
            code = set_language(args.lang)
        except Exception as exc:
            # The preference is persisted in the app-home state file. A
            # read-only directory, a full disk or a broken state file must be
            # reported like any other configuration write failure instead of
            # escaping as a traceback.
            fail(
                t("Could not write {path}: {err}").format(path=STATE_PATH, err=exc),
                EXIT_FAILURE,
            )
        if not code:
            langs = ", ".join(sorted(available_languages()))
            ui.error(
                t("Unsupported language: {lang}. Available: {langs}").format(
                    lang=args.lang, langs=langs
                )
            )
            raise SystemExit(EXIT_USAGE)
        ui.success(t("Language switched to {lang}").format(lang=code))
