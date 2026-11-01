#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.sources
------------------------------------------

Inspect the rule-driven ("book source") plugins: list what is available and
validate a rule file.  Listing and validation are offline.

``list --check`` verifies that each source can be constructed
(fetcher/parser/searcher); it never touches the network, so it stays the
fast, CI-safe command it has always been.  Network reachability is a
separate, explicit opt-in: ``list --probe`` performs a bounded, concurrent
probe (see :mod:`novel_downloader.plugins.utils.site_probe`) and adds
``Latency``/``HTTP`` columns.  ``--by-latency`` implies ``--probe``.

Design note -- bare ``sources`` defaults to ``list`` while ``config``,
``clean`` and ``follow`` exit 2 without a subcommand. This is intentional,
not drift: those containers only have state-changing actions (no safe
default to guess), while ``sources`` has a read-only, idempotent one whose
loss on a bare invocation would serve nobody (the ``git remote`` /
``pip list`` convention). ``sources`` bare printing the table is also the
documented contract in the e2e suite; changing it to exit 2 would break
existing users and scripts for no safety gain.

Exit code note -- ``--check`` exits non-zero only when a source cannot be
instantiated, exactly as before the probe existed.  ``--probe`` (and
``--by-latency``, which implies it) never changes the exit code on its own:
network unreachability is reported (``-`` latency, blank status) but is not
fatal, because a large share of book sources are unreachable from any single
machine.  When ``--probe`` is used without ``--check`` the command therefore
always exits 0.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import tempfile
from argparse import ArgumentParser, Namespace, _SubParsersAction
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.parse import urlsplit

from novel_downloader.apps.cli import ui
from novel_downloader.infra.i18n import t
from novel_downloader.plugins.utils.site_probe import (
    ProbeResult,
    probe_sites,
    sort_by_latency,
)

from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail, localize_help

#: Hard limit for a remotely fetched rule file (1 MiB).
_MAX_RULE_BYTES = 1024 * 1024

#: Timeout for the remote rule download, in seconds.
_RULE_TIMEOUT = 15.0

#: Default per-site timeout for the ``sources list --probe`` network probe.
_PROBE_TIMEOUT = 3.0

#: Default number of simultaneous network probes.
_PROBE_CONCURRENCY = 8

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


def _probe_target(rule: Any) -> str | None:
    """
    Return the URL used to health-check *rule*, if it has one.

    ``base_url`` is the source's canonical origin; rules that only declare
    ``hosts`` are probed over HTTPS on their first host.

    :param rule: A rule object (``base_url`` / ``hosts`` are read defensively
        so lightweight test doubles work too).
    :return: Absolute HTTP(S) URL, or ``None`` when the rule declares neither.
    """
    base = str(getattr(rule, "base_url", "") or "").strip()
    if base:
        return base
    hosts = getattr(rule, "hosts", ()) or ()
    if hosts:
        return f"https://{hosts[0]}"
    return None


def _run_probes(
    items: list[tuple[str, str]],
    *,
    timeout: float,
    concurrency: int,
) -> dict[str, ProbeResult]:
    """Run the network probes once and index the results by ``site_key``."""
    results = asyncio.run(probe_sites(items, timeout=timeout, concurrency=concurrency))
    return {result.site_key: result for result in results}


def _format_latency(result: ProbeResult | None) -> str:
    """Render the ``Latency`` cell (``123 ms`` or ``-``)."""
    if result is None or result.latency_ms is None:
        return "-"
    return f"{result.latency_ms:.0f} ms"


def _format_http(result: ProbeResult | None) -> str:
    """Render the ``HTTP`` cell (the status code, or ``-``)."""
    if result is None or result.status is None:
        return "-"
    return str(result.status)


def _order_by_latency(
    rules: list[tuple[str, Any, dict[str, str]]],
    probes: dict[str, ProbeResult],
) -> list[tuple[str, Any, dict[str, str]]]:
    """Order *rules* by measured latency, unreachable rows last."""
    ordered = sort_by_latency(
        [probes[key] for key, _rule, _meta in rules if key in probes]
    )
    rank = {result.site_key: index for index, result in enumerate(ordered)}
    # Rows without a probe result (no URL, or instantiation failed) have no
    # rank and sort after every measured row; ``site_key`` keeps ties stable.
    return sorted(
        rules,
        key=lambda item: (rank.get(item[0], len(rank)), item[0]),
    )


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
            "--probe",
            action="store_true",
            help=(
                "Also probe each source over the network and report latency "
                "and HTTP status (opt-in; --check stays offline)."
            ),
        )
        p_list.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )
        p_list.add_argument(
            "--by-latency",
            action="store_true",
            help=(
                "Sort sources by network latency (unreachable last); implies --probe."
            ),
        )
        p_list.add_argument(
            "--probe-timeout",
            metavar="SECONDS",
            type=float,
            default=_PROBE_TIMEOUT,
            help="Per-site network probe timeout in seconds (default: 3.0).",
        )
        p_list.add_argument(
            "--probe-concurrency",
            metavar="N",
            type=int,
            default=_PROBE_CONCURRENCY,
            help="Maximum simultaneous network probes (default: 8).",
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
        # Bare ``sources`` intentionally falls through to the read-only list
        # action (see the module docstring for the rationale); config/clean/
        # follow instead require an explicit subcommand because none of their
        # actions is a safe default.
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
                probe=bool(getattr(args, "probe", False)),
                as_json=bool(getattr(args, "as_json", False)),
                by_latency=bool(getattr(args, "by_latency", False)),
                probe_timeout=float(getattr(args, "probe_timeout", _PROBE_TIMEOUT)),
                probe_concurrency=int(
                    getattr(args, "probe_concurrency", _PROBE_CONCURRENCY)
                ),
            )

    # ------------------------------------------------------------------ #
    @classmethod
    def _list(
        cls,
        *,
        check: bool = False,
        probe: bool = False,
        as_json: bool = False,
        by_latency: bool = False,
        probe_timeout: float = _PROBE_TIMEOUT,
        probe_concurrency: int = _PROBE_CONCURRENCY,
    ) -> None:
        from novel_downloader.plugins.rule import iter_rules

        # Ordering by latency is only meaningful after probing, so the flag
        # implies ``--probe`` (never ``--check``: it must not silently start
        # validating registrations the user did not ask about).
        if by_latency:
            probe = True

        # A probe is the only thing here that touches the network, so bad
        # probe knobs are a usage error -- but only when a probe will run.
        # ``Semaphore(0)`` and a non-finite timeout would otherwise deadlock
        # or hang the command.
        if probe:
            if not math.isfinite(probe_timeout) or probe_timeout <= 0:
                fail(
                    f"--probe-timeout must be a positive number of seconds "
                    f"(got {probe_timeout}).",
                    EXIT_USAGE,
                )
            if probe_concurrency < 1:
                fail(
                    f"--probe-concurrency must be at least 1 "
                    f"(got {probe_concurrency}).",
                    EXIT_USAGE,
                )

        rules = sorted(iter_rules(), key=lambda item: item[0])

        # 1) The original, offline "can this source be instantiated?" check.
        #    ``--check`` stops here: no network I/O, ever.
        statuses: dict[str, str] = {}
        probes: dict[str, ProbeResult] = {}
        if check:
            for key, rule, _meta in rules:
                statuses[key] = cls._probe(key, rule.search is not None)

        # 2) The explicit, opt-in network probe.  When ``--check`` is also
        #    requested, only sources that instantiated cleanly are probed: a
        #    broken registration is already reported by the Status column, and
        #    probing it would just add a second failure.
        if probe:
            targets = [
                (key, target)
                for key, rule, _meta in rules
                if (not check or statuses.get(key) == t("ok"))
                and (target := _probe_target(rule))
            ]
            if targets:
                probes = _run_probes(
                    targets,
                    timeout=probe_timeout,
                    concurrency=probe_concurrency,
                )

        ordered = _order_by_latency(rules, probes) if by_latency else rules

        if as_json:
            payload: list[dict[str, object]] = []
            failed = False
            for key, rule, meta in ordered:
                status: str | None = None
                result = probes.get(key)
                if check:
                    status = statuses.get(key)
                    if status != t("ok"):
                        failed = True
                item: dict[str, object] = {
                    "site_key": key,
                    "name": rule.display_name,
                    "search": rule.search is not None,
                    "hosts": list(rule.hosts),
                    "origin": meta.get("origin", "-"),
                    "status": status,
                }
                # Network fields only exist when a probe ran.  Omitting them
                # otherwise keeps the offline JSON payload byte-compatible
                # with pre-probe consumers.
                if probe:
                    item["latency_ms"] = (
                        result.latency_ms if result is not None else None
                    )
                    item["http_status"] = result.status if result is not None else None
                payload.append(item)
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
        if probe:
            columns.extend(["Latency", "HTTP"])

        rows: list[list[str]] = []
        check_failed = False
        for key, rule, meta in ordered:
            row = [
                key,
                rule.display_name,
                t("yes") if rule.search is not None else t("no"),
                ", ".join(rule.hosts) or "-",
                meta.get("origin", "-"),
            ]
            if check:
                status = statuses[key]
                if status != t("ok"):
                    check_failed = True
                row.append(status)
            if probe:
                result = probes.get(key)
                row.extend([_format_latency(result), _format_http(result)])
            rows.append(row)

        ui.render_table(t("Rule-based sources"), columns, rows)

        # Exit-code contract: a source that cannot be instantiated fails the
        # command (and only ``--check`` can discover that).  Network
        # reachability is reported in the Latency/HTTP columns but is not
        # fatal -- dozens of book sources are unreachable from any given
        # machine, and making that a hard failure would break the documented
        # offline ``--check`` contract.
        if check_failed:
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
            # A path the user typed that does not exist is a usage error
            # (bad argument), not a runtime failure -- align with the
            # missing-argument contract (exit 2) shared by the rest of the CLI.
            fail(t("Rule file not found: {path}").format(path=path_str), EXIT_USAGE)
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
            # Same contract as ``sources check``: a nonexistent path is a
            # usage error (exit 2), not a runtime failure.
            fail(t("Rule file not found: {path}").format(path=path_str), EXIT_USAGE)

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
