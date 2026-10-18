#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.sources
------------------------------------------

Inspect the rule-driven ("book source") plugins: list what is available and
validate a rule file without touching any network.
"""

from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace, _SubParsersAction
from pathlib import Path

from novel_downloader.apps.cli import ui
from novel_downloader.infra.i18n import t

from .base import Command, localize_help


class SourcesCmd(Command):
    name = "sources"
    help = t("List, validate, install and remove rule-based book sources.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        sub = parser.add_subparsers(dest="action", title=t("Subcommands"))

        p_list = cls._add_subparser(
            sub, "list", t("List available rule-based sources.")
        )
        p_list.add_argument(
            "--check",
            action="store_true",
            help=t("Also verify that each source can be registered."),
        )

        p_check = cls._add_subparser(sub, "check", t("Validate a single rule file."))
        p_check.add_argument("path", help=t("Path to a rule JSON file."))

        p_add = cls._add_subparser(
            sub, "add", t("Install a rule file into your user rules directory.")
        )
        p_add.add_argument("path", help=t("Path to a rule JSON file."))
        p_add.add_argument(
            "--force", action="store_true", help=t("Overwrite an existing rule file.")
        )

        p_remove = cls._add_subparser(
            sub, "remove", t("Remove a user-installed rule (built-ins are kept).")
        )
        p_remove.add_argument("site_key", help=t("site_key of the rule to remove."))

    @staticmethod
    def _add_subparser(
        sub: _SubParsersAction[ArgumentParser],
        name: str,
        help_text: str,
    ) -> ArgumentParser:
        """Create a subcommand parser with a localized help action."""
        parser = sub.add_parser(
            name, help=help_text, description=help_text, add_help=False
        )
        localize_help(parser)
        return parser

    @classmethod
    def run(cls, args: Namespace) -> None:
        action = getattr(args, "action", None) or "list"
        if action == "check":
            cls._check(args.path)
        elif action == "add":
            cls._add(args.path, force=bool(getattr(args, "force", False)))
        elif action == "remove":
            cls._remove(args.site_key)
        else:
            cls._list(check=bool(getattr(args, "check", False)))

    # ------------------------------------------------------------------ #
    @classmethod
    def _list(cls, *, check: bool) -> None:
        from novel_downloader.plugins.rule import iter_rules

        rules = sorted(iter_rules(), key=lambda item: item[0])
        if not rules:
            ui.warn(t("No rule-based sources found."))
            return

        columns = [
            t("Site Key"),
            t("Name"),
            t("Search"),
            t("Hosts"),
            t("Origin"),
        ]
        if check:
            columns.append(t("Status"))

        rows: list[list[str]] = []
        for key, rule, meta in rules:
            row = [
                key,
                rule.display_name,
                t("yes") if rule.search is not None else t("no"),
                ", ".join(rule.hosts) or "-",
                meta.get("origin", "-"),
            ]
            if check:
                row.append(cls._probe(key, rule.search is not None))
            rows.append(row)

        ui.render_table(t("Rule-based sources"), columns, rows)

    @classmethod
    def _probe(cls, key: str, has_search: bool) -> str:
        from novel_downloader.plugins import registrar
        from novel_downloader.schemas import FetcherConfig, ParserConfig

        try:
            registrar.get_fetcher(key, FetcherConfig())
            registrar.get_parser(key, ParserConfig())
            if has_search:
                registrar.get_searcher_class(key)
        except Exception as e:
            return t("FAIL: {err}").format(err=e)
        return t("ok")

    # ------------------------------------------------------------------ #
    @classmethod
    def _check(cls, path_str: str) -> None:
        from novel_downloader.plugins.rule.model import RuleError, parse_rule

        path = Path(path_str)
        if not path.is_file():
            ui.error(t("Rule file not found: {path}").format(path=path_str))
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            ui.error(t("Invalid JSON in {path}: {err}").format(path=path_str, err=e))
            return

        try:
            rule = parse_rule(data, source=path.name)
        except RuleError as e:
            ui.error(t("Invalid rule in {path}: {err}").format(path=path_str, err=e))
            return

        ui.success(
            t("Rule OK: {name} (site_key={key}, search={search})").format(
                name=rule.display_name,
                key=rule.site_key,
                search=t("yes") if rule.search is not None else t("no"),
            )
        )

    # ------------------------------------------------------------------ #
    @classmethod
    def _add(cls, path_str: str, *, force: bool) -> None:
        from novel_downloader.plugins.rule import RuleError, install_rule

        path = Path(path_str)
        if not path.is_file():
            ui.error(t("Rule file not found: {path}").format(path=path_str))
            return

        try:
            target = install_rule(path, force=force)
        except RuleError as e:
            ui.error(t("Could not install {path}: {err}").format(path=path_str, err=e))
            return

        ui.success(t("Installed rule to {target}").format(target=target))

    @classmethod
    def _remove(cls, site_key: str) -> None:
        from novel_downloader.plugins.rule import RuleError, remove_rule

        try:
            target = remove_rule(site_key)
        except RuleError as e:
            ui.error(str(e))
            return

        ui.success(
            t("Removed rule '{key}' from {target}").format(key=site_key, target=target)
        )
