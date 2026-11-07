#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.browse
-----------------------------------------

Browse what a site publishes without knowing a keyword: its rankings and its
category tree. Only sites whose searcher implements ``rank``/``categories``
(番茄 is the first) answer; every other site reports that browsing is
unsupported instead of failing with an ``AttributeError``.
"""

from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from novel_downloader.apps.cli import prompts, ui
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t
from novel_downloader.plugins import registrar

from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail

#: ``--gender`` spellings mapped onto the site's numeric audience code.
_GENDERS = {"male": "1", "female": "2", "1": "1", "2": "2"}


class BrowseCmd(Command):
    name = "browse"
    help = t("Browse rankings and categories on a site.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("site", help=t("Site key to browse (e.g. fanqienovel)"))
        parser.add_argument(
            "--categories",
            action="store_true",
            help=t("List the site's browse categories and exit."),
        )
        parser.add_argument(
            "--category",
            metavar="ID|NAME",
            help=t("List the books of one category (id or name)."),
        )
        parser.add_argument(
            "--gender",
            default="male",
            choices=sorted(_GENDERS),
            help=t("Audience for category browsing (default: male)."),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            metavar="N",
            help=t("Number of entries to show (default: 20)."),
        )
        parser.add_argument(
            "--config", type=str, help=t("Path to the configuration file")
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print the entries as JSON on stdout and exit."),
        )
        parser.add_argument(
            "--first",
            action="store_true",
            help=t("Download the top entry without asking (non-interactive)."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        site: str = args.site
        gender = _GENDERS[args.gender]
        limit = max(1, int(args.limit))
        as_json = bool(args.as_json)

        config_path: Path | None = Path(args.config) if args.config else None
        config_data = load_or_init_config(config_path)
        if config_data is None:
            raise SystemExit(EXIT_FAILURE)
        adapter = ConfigAdapter(config=config_data)
        ui.setup_logging(
            log_dir=adapter.get_log_dir(),
            console_level=adapter.get_log_level(),
            console=not as_json,
        )

        try:
            searcher_cls = registrar.get_searcher_class(site)
        except ValueError as exc:
            fail(str(exc), EXIT_USAGE)

        if args.categories and args.category is not None:
            fail(t("--categories and --category cannot be combined."), EXIT_USAGE)

        needed = (
            "categories"
            if args.categories
            else "category_books"
            if args.category is not None
            else "rank"
        )
        if not callable(getattr(searcher_cls, needed, None)):
            fail(
                t("'{site}' does not support browsing.").format(site=site),
                EXIT_USAGE,
            )

        import asyncio

        async def _run() -> Any:
            import aiohttp

            from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS

            timeout_cfg = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(
                timeout=timeout_cfg, headers=DEFAULT_USER_HEADERS
            ) as session:
                searcher: Any = searcher_cls(session)
                if args.categories:
                    return await searcher.categories()
                if args.category is not None:
                    category_id = await _resolve_category(
                        searcher, args.category, gender
                    )
                    return await searcher.category_books(
                        category_id, gender=gender, limit=limit
                    )
                return await searcher.rank(limit)

        try:
            payload = asyncio.run(_run())
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the user
            fail(t("Could not read '{site}': {err}").format(site=site, err=exc))

        if args.categories:
            _print_categories(site, payload, as_json=as_json)
            return

        results = list(payload or [])
        if as_json:
            ui.emit(json.dumps(results, ensure_ascii=False, indent=2))
            return
        if not results:
            ui.warn(t("No entries returned by '{site}'.").format(site=site))
            return

        ui.info(
            t("Found {count} entr(y/ies) on '{site}'.").format(
                count=len(results), site=site
            )
        )
        chosen = results[0] if args.first else prompts.select_search_result(results)
        if chosen is None:
            return

        from .download import DownloadCmd

        DownloadCmd.run(
            Namespace(
                site=site,
                book_ids=[chosen["book_id"]],
                config=args.config,
                start=None,
                end=None,
                no_export=False,
                format=None,
                as_json=False,
                strict=False,
            )
        )


async def _resolve_category(searcher: Any, value: str, gender: str) -> str:
    """Turn a category name into its id, keeping numeric ids untouched."""
    text = str(value).strip()
    if text.isdigit():
        return text

    groups = await searcher.categories()
    # Prefer the requested audience, then the other one: a handful of names
    # (科幻末世) exist in both lists with different ids. The category tree is
    # keyed by audience name ("male" / "female"), not by the numeric code.
    order = ["male", "female"] if gender == "1" else ["female", "male"]
    for audience in order:
        for entry in groups.get(audience) or []:
            if entry.get("name") == text:
                return str(entry["id"])
    raise ValueError(t("Category '{name}' was not found.").format(name=text))


def _print_categories(site: str, groups: Any, *, as_json: bool) -> None:
    """Print the category tree, grouped by audience."""
    if as_json:
        ui.emit(json.dumps(groups, ensure_ascii=False, indent=2))
        return
    groups = groups or {}
    if not groups:
        ui.warn(t("No browse categories returned by '{site}'.").format(site=site))
        return
    for audience, entries in groups.items():
        label = {"male": t("Male"), "female": t("Female")}.get(audience, audience)
        ui.info(t("{name} ({count})").format(name=label, count=len(entries)))
        for entry in entries:
            ui.emit(f"  {entry['id']}\t{entry['name']}")
