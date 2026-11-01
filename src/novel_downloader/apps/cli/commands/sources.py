#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.sources
------------------------------------------

Inspect the rule-driven ("book source") plugins: list what is available and
validate a rule file without touching any network.
"""

from __future__ import annotations

import json
import re
import tempfile
from argparse import ArgumentParser, Namespace, _SubParsersAction
from pathlib import Path
from urllib import request as urllib_request
from urllib.parse import urlsplit

from novel_downloader.apps.cli import ui
from novel_downloader.infra.i18n import t

from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail, localize_help

#: Hard limit for a remotely fetched rule file (1 MiB).
_MAX_RULE_BYTES = 1024 * 1024

#: Timeout for the remote rule download, in seconds.
_RULE_TIMEOUT = 15.0

#: User agent sent when fetching a rule (some hosts reject the default).
_RULE_USER_AGENT = "novel-cli/rule-downloader"

_ILLEGAL_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class _RuleDownloadError(Exception):
    """Raised when a remote rule cannot be fetched or is too large."""


def _validate_rule_url(url: str) -> str:
    """
    Validate and normalize a remote rule URL.

    Only ``http`` and ``https`` are accepted; anything else (``file:``,
    ``ftp:``, a bare path, ...) is a usage error.

    :raises ValueError: When the URL is not an absolute HTTP(S) URL.
    """
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(url)
    return url.strip()


def _download_rule(
    url: str,
    *,
    timeout: float = _RULE_TIMEOUT,
    max_bytes: int = _MAX_RULE_BYTES,
) -> bytes:
    """
    Download a rule JSON document with a hard size bound.

    :param url: Absolute HTTP(S) URL.
    :param timeout: Socket timeout in seconds.
    :param max_bytes: Maximum accepted response size.
    :raises ValueError: When ``url`` is not an HTTP(S) URL.
    :raises _RuleDownloadError: When the response is empty/too large.
    :raises OSError: When the network request fails.
    """
    target = _validate_rule_url(url)
    request = urllib_request.Request(
        target,
        headers={"User-Agent": _RULE_USER_AGENT},
    )
    with urllib_request.urlopen(request, timeout=timeout) as response:
        data: bytes = response.read(max_bytes + 1)

    if len(data) > max_bytes:
        raise _RuleDownloadError(
            t("The remote rule is larger than {limit} bytes.").format(limit=max_bytes)
        )
    if not data.strip():
        raise _RuleDownloadError(t("The remote rule is empty."))
    return data


def _decode_rule(data: bytes) -> str:
    """Decode a downloaded rule as UTF-8 (accepting a BOM)."""
    return data.decode("utf-8-sig")


def _rule_name(name: str | None, url: str) -> str:
    """Pick a safe file stem for an installed remote rule."""
    raw = (name or "").strip()
    if not raw:
        raw = urlsplit(url).path.rsplit("/", 1)[-1]
        if raw.lower().endswith(".json"):
            raw = raw[:-5]
    if raw.lower().endswith(".json"):
        raw = raw[:-5]
    stem = _ILLEGAL_NAME.sub("_", raw).strip().rstrip(". ")
    return stem[:120]


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
        p_list.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )

        p_check = cls._add_subparser(sub, "check", t("Validate a single rule file."))
        p_check.add_argument("path", help=t("Path to a rule JSON file."))

        p_add = cls._add_subparser(
            sub, "add", t("Install a rule file into your user rules directory.")
        )
        p_add.add_argument(
            "path",
            nargs="?",
            help=t("Path to a local rule JSON file."),
        )
        p_add.add_argument(
            "--url",
            metavar="URL",
            help=t("Download the rule JSON from this HTTP(S) URL instead."),
        )
        p_add.add_argument(
            "--name",
            metavar="NAME",
            help=t("File name (without .json) to install a remote rule as."),
        )
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
            cls._add(
                args.path,
                url=args.url,
                name=args.name,
                force=bool(getattr(args, "force", False)),
            )
        elif action == "remove":
            cls._remove(args.site_key)
        else:
            cls._list(
                check=bool(getattr(args, "check", False)),
                as_json=bool(getattr(args, "as_json", False)),
            )

    # ------------------------------------------------------------------ #
    @classmethod
    def _list(cls, *, check: bool, as_json: bool = False) -> None:
        from novel_downloader.plugins.rule import iter_rules

        rules = sorted(iter_rules(), key=lambda item: item[0])

        if as_json:
            payload: list[dict[str, object]] = []
            failed = False
            for key, rule, meta in rules:
                status: str | None = None
                if check:
                    status = cls._probe(key, rule.search is not None)
                    if status != t("ok"):
                        failed = True
                payload.append(
                    {
                        "site_key": key,
                        "name": rule.display_name,
                        "search": rule.search is not None,
                        "hosts": list(rule.hosts),
                        "origin": meta.get("origin", "-"),
                        "status": status,
                    }
                )
            ui.emit(json.dumps(payload, ensure_ascii=False, indent=2))
            if failed:
                raise SystemExit(EXIT_FAILURE)
            return

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
        probe_failed = False
        for key, rule, meta in rules:
            row = [
                key,
                rule.display_name,
                t("yes") if rule.search is not None else t("no"),
                ", ".join(rule.hosts) or "-",
                meta.get("origin", "-"),
            ]
            if check:
                status = cls._probe(key, rule.search is not None)
                if status != t("ok"):
                    probe_failed = True
                row.append(status)
            rows.append(row)

        ui.render_table(t("Rule-based sources"), columns, rows)

        if probe_failed:
            raise SystemExit(EXIT_FAILURE)

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
        from novel_downloader.plugins.rule.model import (
            RuleError,
            loads_rule_json,
            parse_rule,
        )

        path = Path(path_str)
        if not path.is_file():
            fail(t("Rule file not found: {path}").format(path=path_str))
        try:
            data = loads_rule_json(
                path.read_text(encoding="utf-8-sig"), source=path.name
            )
        except RuleError as e:
            fail(t("Invalid JSON in {path}: {err}").format(path=path_str, err=e))

        try:
            rule = parse_rule(data, source=path.name)
        except RuleError as e:
            fail(t("Invalid rule in {path}: {err}").format(path=path_str, err=e))

        ui.success(
            t("Rule OK: {name} (site_key={key}, search={search})").format(
                name=rule.display_name,
                key=rule.site_key,
                search=t("yes") if rule.search is not None else t("no"),
            )
        )

    # ------------------------------------------------------------------ #
    @classmethod
    def _add(
        cls,
        path_str: str | None,
        *,
        url: str | None = None,
        name: str | None = None,
        force: bool,
    ) -> None:
        if bool(path_str) == bool(url):
            fail(t("Provide exactly one of PATH or --url."), EXIT_USAGE)
        if name and not url:
            fail(t("--name can only be used together with --url."), EXIT_USAGE)

        if url:
            cls._add_remote(url, name=name, force=force)
            return

        from novel_downloader.plugins.rule import RuleError, install_rule

        assert path_str is not None  # narrowed by the check above
        path = Path(path_str)
        if not path.is_file():
            fail(t("Rule file not found: {path}").format(path=path_str))

        try:
            target = install_rule(path, force=force)
        except RuleError as e:
            fail(t("Could not install {path}: {err}").format(path=path_str, err=e))

        ui.success(t("Installed rule to {target}").format(target=target))

    @classmethod
    def _add_remote(cls, url: str, *, name: str | None, force: bool) -> None:
        """Download, validate and install a rule hosted at ``url``."""
        from novel_downloader.plugins.rule import RuleError, install_rule
        from novel_downloader.plugins.rule.model import loads_rule_json, parse_rule

        try:
            raw = _download_rule(url)
        except ValueError:
            fail(
                t("Unsupported rule URL: {url}").format(url=url),
                EXIT_USAGE,
            )
        except _RuleDownloadError as e:
            fail(str(e))
        except Exception as e:
            fail(t("Could not download {url}: {err}").format(url=url, err=e))

        try:
            text = _decode_rule(raw)
        except UnicodeDecodeError as e:
            fail(t("The remote rule is not valid UTF-8: {err}").format(err=e))

        source = _rule_name(name, url) or "remote-rule"
        try:
            payload = loads_rule_json(text, source=source)
        except RuleError as e:
            fail(t("Invalid JSON in {path}: {err}").format(path=url, err=e))

        try:
            rule = parse_rule(payload, source=source)
        except RuleError as e:
            fail(t("Invalid rule in {path}: {err}").format(path=url, err=e))

        stem = _rule_name(name, url) or rule.site_key
        with tempfile.TemporaryDirectory(prefix="novel-rule-") as tmp:
            temp_path = Path(tmp) / f"{stem}.json"
            temp_path.write_text(text, encoding="utf-8")
            try:
                target = install_rule(temp_path, force=force)
            except RuleError as e:
                fail(t("Could not install {path}: {err}").format(path=url, err=e))

        ui.success(t("Installed rule to {target}").format(target=target))

    @classmethod
    def _remove(cls, site_key: str) -> None:
        from novel_downloader.plugins.rule import RuleError, remove_rule

        try:
            target = remove_rule(site_key)
        except RuleError as e:
            fail(str(e))

        ui.success(
            t("Removed rule '{key}' from {target}").format(key=site_key, target=target)
        )
