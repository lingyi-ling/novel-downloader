#!/usr/bin/env python3
"""
novel_downloader.apps.cli.main
------------------------------

Unified CLI entry point. Parses arguments and delegates to parser or interactive.
"""

import argparse
import os

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

    parser = argparse.ArgumentParser(description=t("Novel Downloader CLI tool."))
    parser.add_argument(
        "-v", "--version", action="version", version=f"NovelDownloader {__version__}"
    )
    parser.add_argument(
        "--lang",
        help=t(
            "Override the interface language for this run "
            "(e.g. en, zh). Place it before the subcommand."
        ),
    )

    subparsers = parser.add_subparsers(dest="command")

    for cmd in commands:
        cmd.register(subparsers)

    args = parser.parse_args()

    if args.command is None:
        from novel_downloader.apps.cli.interactive import start_interactive

        start_interactive()
        return

    if hasattr(args, "func"):
        try:
            args.func(args)
        except KeyboardInterrupt as err:
            print("\n" + t("Aborted."))
            raise SystemExit(1) from err
    else:
        parser.print_help()


if __name__ == "__main__":
    cli_main()
