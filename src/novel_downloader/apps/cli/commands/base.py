#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.base
---------------------------------------

"""

import os
from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace, _SubParsersAction
from pathlib import Path
from typing import Any, NoReturn

from novel_downloader.infra.i18n import t
from novel_downloader.schemas import BookConfig

__all__ = [
    "EXIT_FAILURE",
    "EXIT_SUCCESS",
    "EXIT_USAGE",
    "Command",
    "expand_config_paths",
    "expand_path",
    "fail",
    "localize_help",
    "parse_book_args",
    "require_safe_book_id",
    "require_safe_site",
]

#: Exit code for a successful run.
EXIT_SUCCESS = 0
#: Exit code for a run that failed at runtime (network, site, config, file...).
EXIT_FAILURE = 1
#: Exit code for a command-line usage error (missing/conflicting arguments, ...).
EXIT_USAGE = 2


def fail(message: str, code: int = EXIT_FAILURE) -> NoReturn:
    """
    Report a message on stderr and terminate with a non-zero exit code.

    Centralizes the "print an error and exit non-zero" pattern so every
    command uses the same stream (stderr, keeping stdout machine readable) and
    the same codes (see :data:`EXIT_USAGE`, :data:`EXIT_FAILURE`).

    :param message: Human-readable error message.
    :param code: Exit code to terminate with; defaults to ``EXIT_FAILURE``.
    """
    # Imported lazily: ``ui`` imports ``i18n`` only, but keeping this local
    # avoids any chance of an import cycle at module load time.
    from novel_downloader.apps.cli import ui

    ui.error(message)
    raise SystemExit(code)


def expand_path(value: str) -> Path:
    """
    Turn a user-supplied path into a ``Path`` with ``~``/env vars expanded.

    Windows shells expand neither ``~`` nor POSIX ``$VAR`` (variables are
    written ``%VAR%`` there), so an unexpanded ``--config ~/settings.toml``
    used to be taken literally -- and ``load_or_init_config`` would then offer
    to create a directory literally named ``~``. Expanding at the CLI boundary
    keeps every command's path options working the same way on both platforms.

    :param value: Raw command-line path.
    :return: The same path with user/environment parts resolved.
    """
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


#: ``general`` keys that hold directories consumed by the client/exporter.
_CONFIG_DIR_KEYS: tuple[str, ...] = ("output_dir", "raw_data_dir", "cache_dir")


def _needs_path_expansion(value: str) -> bool:
    """True when ``value`` can contain a user home or environment reference."""
    # ``~`` (POSIX/Windows home), ``$VAR``/``${VAR}`` (POSIX) and ``%VAR%``
    # (Windows). Plain relative paths are left byte-for-byte alone so the
    # configured spelling survives into logs and error messages.
    return "~" in value or "$" in value or "%" in value


def expand_config_paths(config: dict[str, Any]) -> dict[str, Any]:
    """
    Expand ``~`` and environment variables in the configured directories.

    ``ConfigAdapter`` passes ``general.output_dir``, ``general.raw_data_dir``,
    ``general.cache_dir`` and ``general.debug.log_dir`` to the client/exporter
    as literal strings, while every path *option* (``--config``, ``batch
    --out``, ``--not-found``, ...) goes through :func:`expand_path`. A config
    value such as ``~/books`` would therefore create a literal ``~`` directory
    under the working directory on Windows (whose shell expands neither form)
    instead of writing to the user's home -- and ``batch --out ~/books`` would
    land somewhere else again.

    The config is expanded once, at the CLI boundary, so config-driven and
    flag-driven directories mean the same thing. Values without a ``~``/env
    marker are returned untouched.

    :param config: Loaded configuration mapping.
    :return: The same mapping (a copy when something changed); the input is
        never mutated.
    """
    general = config.get("general")
    if not isinstance(general, dict):
        return config

    updated = dict(general)
    changed = False

    for key in _CONFIG_DIR_KEYS:
        value = updated.get(key)
        if isinstance(value, str) and value.strip() and _needs_path_expansion(value):
            expanded = str(expand_path(value))
            if expanded != value:
                updated[key] = expanded
                changed = True

    debug = updated.get("debug")
    if isinstance(debug, dict):
        log_dir = debug.get("log_dir")
        if (
            isinstance(log_dir, str)
            and log_dir.strip()
            and _needs_path_expansion(log_dir)
        ):
            expanded = str(expand_path(log_dir))
            if expanded != log_dir:
                updated["debug"] = {**debug, "log_dir": expanded}
                changed = True

    if not changed:
        return config
    return {**config, "general": updated}


def _is_path_component(value: str) -> bool:
    """
    True when ``value`` is usable as a single directory/file name.

    Site keys and book IDs are joined onto the raw-data/output roots, so a
    value containing a separator, a drive/stream colon or a ``.``/``..``
    segment would step outside the directory the user selected. Real keys are
    plain identifiers (``b520``, ``8_8187``), so an escaping value is a bad
    argument rather than a runtime condition.
    """
    text = str(value)
    path = Path(text)
    return not (
        not text.strip()
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or ":" in text
        or path.is_absolute()
        or path.name != text
        or any(ord(char) < 32 for char in text)
    )


def require_safe_site(site: str) -> str:
    """
    Return ``site`` when it can be used as one path component, else exit 2.

    :param site: Raw site key (from ``--site`` or a URL/not-found header).
    :return: The unchanged key when it is safe to join onto a base directory.
    """
    if not _is_path_component(site):
        fail(t("Unsupported site: {site!r}").format(site=str(site)), EXIT_USAGE)
    return site


def require_safe_book_id(book_id: str) -> str:
    """
    Return ``book_id`` when it can be used as one path component, else exit 2.

    :param book_id: Raw book ID (from the command line or a provider).
    :return: The unchanged ID when it is safe to join onto a base directory.
    """
    if not _is_path_component(book_id):
        # "Invalid input." is an existing catalogued string; the offending
        # value is raw diagnostic text appended as a detail.
        fail(f"{t('Invalid input.')} (book_id={str(book_id)!r})", EXIT_USAGE)
    return book_id


def localize_help(parser: ArgumentParser) -> None:
    """
    Add a localized ``-h`` / ``--help`` action to ``parser``.

    Parsers must be created with ``add_help=False`` so the built-in (English)
    ``-h`` action can be replaced by this one.

    :param parser: Parser to add the help action to.
    """
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help=t("Show this help message and exit."),
    )


def parse_book_args(
    book_ids: list[str],
    start_id: str | None,
    end_id: str | None,
) -> list[BookConfig]:
    """
    Convert CLI ``book_ids`` / ``--start`` / ``--end`` into ``BookConfig`` items.

    ``--start`` / ``--end`` only ever apply to the first book, matching the
    documented CLI contract.

    :param book_ids: Book IDs (or URLs) in command-line order.
    :param start_id: Start chapter ID, applied to the first book only.
    :param end_id: End chapter ID, applied to the first book only.
    :return: One ``BookConfig`` per book ID (empty list when none were given).
    """
    if not book_ids:
        return []

    result: list[BookConfig] = [
        BookConfig(
            book_id=book_ids[0],
            start_id=start_id,
            end_id=end_id,
        )
    ]

    result.extend(BookConfig(book_id=book_id) for book_id in book_ids[1:])

    return result


class Command(ABC):
    name: str
    help: str

    @classmethod
    def register(cls, subparsers: "_SubParsersAction[ArgumentParser]") -> None:
        parser = subparsers.add_parser(
            cls.name,
            help=cls.help,
            # Repeat the one-line summary as the subcommand description so
            # ``<command> --help`` explains itself instead of starting blank.
            description=cls.help,
            add_help=False,
        )
        localize_help(parser)
        cls.add_arguments(parser)
        parser.set_defaults(func=cls.run)

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        return

    @classmethod
    @abstractmethod
    def run(cls, args: Namespace) -> None:
        pass
