#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.login
----------------------------------------

Open a login window for a site and persist its session cookies, so that
sites which require an authenticated session (e.g. fanqienovel) can be
searched without pasting cookies by hand.
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from novel_downloader.apps.cli import ui
from novel_downloader.infra.i18n import t

from .base import EXIT_FAILURE, EXIT_USAGE, Command


class LoginCmd(Command):
    name = "login"
    help = t("Open a login window for a site and save its session cookies.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "site",
            nargs="?",
            default="fanqienovel",
            help=t("Site key to log in to (default: fanqienovel)."),
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=300.0,
            metavar="SECONDS",
            help=t("Seconds to wait for the login to complete (default: 300)."),
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help=t("List sites that support an interactive login."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        from novel_downloader.apps.login import LOGIN_SITES, run_login, site_cache_dir

        if getattr(args, "list", False):
            for key in sorted(LOGIN_SITES):
                ui.emit(key)
            return

        site = args.site
        if site not in LOGIN_SITES:
            ui.error(
                t("Site '{site}' does not support an interactive login.").format(
                    site=site
                )
            )
            # Invalid argument: exit 2, like ``config set-lang`` for an
            # unsupported language.
            raise SystemExit(EXIT_USAGE)

        if run_login(site, timeout=float(args.timeout)):
            ui.success(
                t("Session cookies saved to {path}").format(path=site_cache_dir(site))
            )
        else:
            ui.error(t("Login was not completed; no cookies were saved."))
            # Documented contract: a login that was not completed exits non-zero.
            raise SystemExit(EXIT_FAILURE)
