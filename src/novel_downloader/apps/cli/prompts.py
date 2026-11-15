#!/usr/bin/env python3
"""
novel_downloader.apps.cli.prompts
---------------------------------
"""

__all__ = [
    "select_books",
    "select_main_action",
    "select_search_result",
    "select_site",
]

from collections.abc import Sequence
from pathlib import Path
from typing import cast

from novel_downloader.apps.cli import ui
from novel_downloader.apps.constants import DOWNLOAD_SUPPORT_SITES, SEARCH_SUPPORT_SITES
from novel_downloader.infra.i18n import t
from novel_downloader.plugins.search import GroupedSearchResult
from novel_downloader.schemas import SearchResult


def select_main_action() -> str:
    """
    Display the main interactive menu and return user's choice.
    """
    ui.render_table(
        t("Main Menu"),
        [t("#"), t("Action")],
        [
            ["1", t("Search for novels")],
            ["2", t("Download novel (by URL or Site/Book ID)")],
            ["3", t("Export previously downloaded novels")],
            ["", t("Exit")],
        ],
    )

    choice = ui.prompt_choice(
        t("Select an action"),
        ["1", "2", "3"],
    )
    return choice


def select_site(raw_dir: Path, per_page: int = 10) -> str | None:
    if not raw_dir.exists():
        ui.error(t("Raw data directory does not exist: {p}").format(p=str(raw_dir)))
        return None

    site_dirs = sorted([p for p in raw_dir.iterdir() if p.is_dir()])
    if not site_dirs:
        ui.warn(t("No sites found under {p}.").format(p=str(raw_dir)))
        return None

    all_rows = [
        [str(i), DOWNLOAD_SUPPORT_SITES.get(p.name, p.name), p.name]
        for i, p in enumerate(site_dirs, 1)
    ]

    total = len(all_rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = 1

    while True:
        start = (page - 1) * per_page + 1
        end = min(page * per_page, total)
        page_rows = all_rows[start - 1 : end]

        ui.render_table(
            t("Available Sites · Page {page}/{total}").format(
                page=page, total=total_pages
            ),
            [t("#"), t("Site Name"), t("Site Key")],
            page_rows,
        )

        numeric_choices = [str(i) for i in range(start, end + 1)]
        nav_choices = []
        if page < total_pages:
            nav_choices.append("n")
        if page > 1:
            nav_choices.append("p")

        choice = ui.prompt_choice(
            t(
                "Enter a number, 'n' for next, 'p' for previous (press Enter to cancel)"  # noqa: E501
            ),
            numeric_choices + nav_choices,
        )

        if choice == "":
            return None
        if choice == "n" and page < total_pages:
            page += 1
            continue
        if choice == "p" and page > 1:
            page -= 1
            continue
        if choice in numeric_choices:
            idx = int(choice)
            return site_dirs[idx - 1].name


def select_books(raw_dir: Path, site: str, per_page: int = 10) -> list[str]:
    import json

    site_dir = raw_dir / site
    if not site_dir.exists():
        ui.error(t("Site directory does not exist: {p}").format(p=str(site_dir)))
        return []

    book_dirs = sorted([p for p in site_dir.iterdir() if p.is_dir()])
    if not book_dirs:
        ui.warn(t("No books found under site '{site}'.").format(site=site))
        return []

    all_rows = []
    for i, p in enumerate(book_dirs, 1):
        book_id = p.name
        book_name, author = "", ""
        info_path = p / "book_info.raw.json"
        if info_path.exists():
            try:
                with info_path.open("r", encoding="utf-8") as f:
                    meta = json.load(f)
                    book_name = str(meta.get("book_name", "") or "")
                    author = str(meta.get("author", "") or "")
            except Exception:
                ui.warn(t("Failed to read metadata for {bid}").format(bid=book_id))
        all_rows.append([str(i), book_id, book_name, author])

    total = len(all_rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = 1

    while True:
        start = (page - 1) * per_page + 1
        end = min(page * per_page, total)
        page_rows = all_rows[start - 1 : end]

        ui.render_table(
            t("Available Books for {site} · Page {page}/{total}").format(
                site=site, page=page, total=total_pages
            ),
            [t("#"), t("Book ID"), t("Title"), t("Author")],
            page_rows,
        )

        # Free-form input (not ui.prompt_choice): a constrained prompt would
        # reject the advertised multi-select "1,3,5" answers, leaving the
        # comma branch below unreachable for real users.
        choice = (
            ui.prompt(
                t(
                    "Enter numbers (e.g. 1,3,5), 'a' for all, 'n' next, 'p' previous (Enter to cancel)"  # noqa: E501
                )
            )
            .strip()
            .lower()
        )

        if choice == "":
            return []
        if choice == "a":
            return [p.name for p in book_dirs]
        if choice == "n" and page < total_pages:
            page += 1
            continue
        if choice == "p" and page > 1:
            page -= 1
            continue
        if choice in {"n", "p"}:
            # Navigation entered where it does not apply (e.g. "n" on the
            # last page): ignore and re-display the current page.
            continue
        tokens = [s.strip() for s in choice.split(",") if s.strip()]
        # isdecimal(), not isdigit(): digit look-alikes such as "²" pass
        # isdigit() but int() would then raise ValueError here.
        if tokens and all(s.isdecimal() for s in tokens):
            idxs = sorted({int(s) for s in tokens})
            if any(i < 1 or i > len(book_dirs) for i in idxs):
                ui.warn(t("One or more indices out of range."))
                continue
            return [book_dirs[i - 1].name for i in idxs]
        ui.warn(t("Invalid input."))


#: Human labels for ``match_tier`` 0..4, indexed by the tier.  The msgids are
#: already in the catalog for the desktop badge, so the CLI reuses the same
#: wording instead of introducing a second vocabulary for the same value.
_MATCH_TIER_MSGS = (
    "No match",
    "Partial",
    "Contains",
    "Prefix",
    "Exact",
)

#: Title-bar notes for the query-intent verdict.  The same msgids label the
#: desktop chip, so both fronts describe the ordering with one vocabulary.
_QUERY_INTENT_MSGS = {
    "title": "Title match",
    "author": "Author match",
}


def _query_intent(results: Sequence[SearchResult]) -> str:
    """
    Return the title-bar note for the ranked list, or ``""``.

    Reads the explicit ``query_intent`` verdict :func:`search` attaches to
    final rows.  Streamed previews, older cache snapshots and ambiguous
    verdicts carry none; conflicting values are also ignored rather than
    guessed, so the title bar only explains an order the ranking really used.
    """
    from novel_downloader.plugins.search import query_intent_of

    intent = query_intent_of(results)
    return t(_QUERY_INTENT_MSGS[intent]) if intent is not None else ""


def _match_suffix(result: SearchResult) -> str:
    """
    The ranking telemetry suffix for one search-result row, or ``""``.

    ``rank_score`` (0..1) and ``match_tier`` (0..4) are added by the ranked
    aggregate call only: streamed previews and rows restored from an older
    cache carry neither.  A row without both usable values therefore renders
    exactly as before -- no placeholder, no dash -- so the table stays
    meaningful for those sources.  Both values are validated like the desktop
    badge does, which also rejects ``bool`` (a subclass of ``int``) and
    non-finite scores that would otherwise format as ``nan``/``inf``.
    """
    tier = result.get("match_tier")
    if isinstance(tier, bool) or not isinstance(tier, int) or not 0 <= tier <= 4:
        return ""
    score = result.get("rank_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return ""
    score = float(score)
    if not 0.0 <= score <= 1.0:
        return ""
    return f"{t(_MATCH_TIER_MSGS[tier])} {score:.2f}"


def _title_cell(result: SearchResult) -> str:
    """The ``Title`` cell, annotated with ranking telemetry when present."""
    title = result["title"]
    suffix = _match_suffix(result)
    return f"{title} · {suffix}" if suffix else title


def _site_cell(result: SearchResult) -> str:
    """
    The ``Site Name`` cell of one search-result row.

    A merged (grouped) entry -- ``search --group`` -- lists every source site
    of the book, best first, with the best source starred (``*``); a flat
    entry keeps its single site label.  Selecting the row downloads from the
    best source, whose ``site``/``book_id`` the entry itself carries.
    """
    sources = cast("GroupedSearchResult", result).get("sources") or []
    if not sources:
        return SEARCH_SUPPORT_SITES.get(result["site"], result["site"])
    labels: list[str] = []
    for index, site in enumerate(sources):
        label = SEARCH_SUPPORT_SITES.get(site, site)
        labels.append(f"*{label}" if index == 0 else label)
    return " · ".join(labels)


def select_search_result(
    results: Sequence[SearchResult],
    per_page: int = 10,
) -> SearchResult | None:
    """
    Show results in pages and let user select by global index.
    """
    if not results:
        ui.warn(t("No results found."))
        return None

    total = len(results)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = 1
    intent_note = _query_intent(results)

    columns = [
        t("#"),
        t("Title"),
        t("Author"),
        t("Latest"),
        t("Updated"),
        t("Site Name"),
        t("Book ID"),
    ]
    all_rows = [
        [
            str(i),
            _title_cell(r),
            r["author"],
            r["latest_chapter"],
            r["update_date"],
            _site_cell(r),
            r["book_id"],
        ]
        for i, r in enumerate(results, 1)
    ]

    while True:
        start = (page - 1) * per_page + 1
        end = min(page * per_page, total)

        page_rows = all_rows[start - 1 : end]

        title = t("Search Results · Page {page}/{total_pages}").format(
            page=page, total_pages=total_pages
        )
        if intent_note:
            title = f"{title} · {intent_note}"
        ui.render_table(
            title,
            columns,
            page_rows,
        )

        numeric_choices = [str(i) for i in range(start, end + 1)]
        nav_choices = []
        if page < total_pages:
            nav_choices.append("n")
        if page > 1:
            nav_choices.append("p")

        choice = ui.prompt_choice(
            t(
                "Enter a number, 'n' for next, 'p' for previous (press Enter to cancel)"  # noqa: E501
            ),
            numeric_choices + nav_choices,
        )

        if choice == "":
            return None  # cancel
        if choice == "n" and page < total_pages:
            page += 1
            continue
        if choice == "p" and page > 1:
            page -= 1
            continue
        if choice in numeric_choices:
            idx = int(choice)
            return results[idx - 1]
