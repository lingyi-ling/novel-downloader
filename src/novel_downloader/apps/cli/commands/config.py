#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.config
-----------------------------------------

"""

import errno
import json
import os
import tempfile
from argparse import ArgumentParser, Namespace, _SubParsersAction
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

from novel_downloader.apps.cli import ui
from novel_downloader.infra.config import copy_default_config
from novel_downloader.infra.i18n import available_languages, set_language, t
from novel_downloader.infra.paths import DEFAULT_CONFIG_FILENAME, STATE_PATH

from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail, localize_help

#: Suffix of the copy kept before ``config init --force`` replaces a file.
BACKUP_SUFFIX = ".bak"


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

        for subcmd in (ConfigInitCmd, ConfigSetLangCmd):
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
