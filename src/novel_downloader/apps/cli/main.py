#!/usr/bin/env python3
"""
novel_downloader.apps.cli.main
------------------------------

Unified CLI entry point. Parses arguments and delegates to parser or interactive.
"""

import argparse
import os
import sys

from novel_downloader import __version__
from novel_downloader.infra.i18n import LANG_ENV_VAR, normalize_language, t


def _preparse_language(argv: list[str] | None = None) -> str:
    """
    Apply the global ``--lang`` option before command modules are imported.

    ``Command`` subclasses resolve their localized ``help`` strings at import
    time, so the language has to be known before ``commands`` is imported.

    :param argv: Argument list to scan; defaults to ``sys.argv[1:]``.
    :return: The normalized language code that was applied, or ``""``.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang")
    pre_args, _ = pre.parse_known_args(argv)

    code = normalize_language(pre_args.lang)
    if code:
        os.environ[LANG_ENV_VAR] = code
    return code


def cli_main() -> None:
    _preparse_language()

    # Imported lazily so localized ``help`` strings pick up ``--lang``.
    from novel_downloader.apps.cli.commands import commands
    from novel_downloader.apps.cli.commands.batch import BatchCmd

    parser = argparse.ArgumentParser(
        prog="novel-cli",
        description=t("Novel Downloader CLI tool."),
        epilog=t(
            "Examples:\n"
            "  %(prog)s download https://example.com/book/123/\n"
            "  %(prog)s download --site b520 8_8187\n"
            "  %(prog)s search --site b520 --limit 5 KEYWORD\n"
            "  %(prog)s export --format epub 88888\n"
            "\n"
            "Run '%(prog)s COMMAND --help' for command-specific options."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help=t("Show this help message and exit."),
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"NovelDownloader {__version__}",
        help=t("Show program's version number and exit."),
    )
    parser.add_argument(
        "--lang",
        help=t(
            "Override the interface language for this run "
            "(e.g. en, zh). Place it before the subcommand."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", title=t("Commands"))

    # ``batch`` is appended here rather than in ``commands/__init__.py`` so the
    # change stays in the entry point. Appending to the shared list (instead of
    # building a local one) also lets ``novel-cli completion`` -- which reads
    # that list -- offer the new subcommand. The guard keeps repeated
    # ``cli_main()`` calls (tests, embedders) from registering it twice.
    if BatchCmd not in commands:
        commands.append(BatchCmd)
    for cmd in commands:
        cmd.register(subparsers)

    args = parser.parse_args()

    if args.command is None:
        from novel_downloader.apps.cli.interactive import start_interactive

        try:
            start_interactive()
        except KeyboardInterrupt as err:
            # Same contract as the command path above: "Aborted." on stderr,
            # exit code 1 -- never a raw traceback out of interactive mode.
            print("\n" + t("Aborted."), file=sys.stderr)
            raise SystemExit(1) from err
        return

    if hasattr(args, "func"):
        try:
            args.func(args)
        except KeyboardInterrupt as err:
            # Diagnostics belong on stderr: stdout stays machine readable
            # (``search --json``, ``login --list``, ...).
            print("\n" + t("Aborted."), file=sys.stderr)
            raise SystemExit(1) from err
    else:
        parser.print_help()


if __name__ == "__main__":
    cli_main()
