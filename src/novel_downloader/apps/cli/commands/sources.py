#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.sources
------------------------------------------

Inspect the rule-driven ("book source") plugins: list what is available and
validate a rule file without touching any network.
"""

from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace
from pathlib import Path

from novel_downloader.apps.cli import ui
from novel_downloader.infra.i18n import t

from .base import Command


class SourcesCmd(Command):
    name = "sources"
    help = t("List and validate rule-based book sources.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        sub = parser.add_subparsers(dest="action")

        p_list = sub.add_parser("list", help=t("List available rule-based sources."))
        p_list.add_argument(
            "--check",
            action="store_true",
            help=t("Also verify that each source can be registered."),
        )

        p_check = sub.add_parser("check", help=t("Validate a single rule file."))
        p_check.add_argument("path", help=t("Path to a rule JSON file."))

    @classmethod
    def run(cls, args: Namespace) -> None:
        action = getattr(args, "action", None) or "list"
        if action == "check":
            cls._check(args.path)
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
                "yes" if rule.search is not None else "no",
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
            return f"FAIL: {e}"
        return "ok"

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
                search="yes" if rule.search is not None else "no",
            )
        )
