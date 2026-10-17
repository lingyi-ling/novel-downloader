#!/usr/bin/env python3
"""
novel_downloader.apps.web.pages.search
--------------------------------------

Search UI with a settings dropdown, persistent state, and paginated results.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import aclosing, suppress
from functools import partial
from typing import Any

from nicegui import ui
from nicegui.elements.number import Number
from nicegui.events import KeyEventArguments, ValueChangeEventArguments

from novel_downloader.apps.constants import SEARCH_SUPPORT_SITES
from novel_downloader.apps.login import LOGIN_SITES
from novel_downloader.infra.i18n import t
from novel_downloader.plugins.ranking import default_order, sort_results
from novel_downloader.plugins.search import search_stream
from novel_downloader.schemas import SearchResult

from ..components import page_frame
from ..components.widgets import empty_state, page_header, skeleton_card
from ..services import manager, setup_dialog

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_SITE_LIMIT = 30
_PAGE_SIZE = 20
_PAGER_WIDTH = 9

_STATE: dict[str, dict[str, Any]] = {}


def _get_state() -> dict[str, Any]:
    cid = ui.context.client.id
    if cid not in _STATE:
        _STATE[cid] = {
            "query": "",
            "sites": None,  # list[str] | None (None => search all)
            "per_site_limit": _DEFAULT_SITE_LIMIT,
            "timeout": _DEFAULT_TIMEOUT,
            "results": [],  # list[SearchResult]
            "page": 1,
            "page_size": _PAGE_SIZE,
            "search_task": None,  # asyncio.Task | None
            "searching": False,  # bool
            "searched": False,  # bool (True once a search has been started)
            "started_at": 0.0,  # float (monotonic timestamp of last search start)
            "sort_key": "quality",  # quality | priority | site | title | author
            "sort_order": "desc",  # asc | desc
        }
    return _STATE[cid]


def _cleanup_state() -> None:
    cid = ui.context.client.id
    # cancel any in-flight search task to avoid leaks
    st = _STATE.get(cid)
    if st and isinstance(st.get("search_task"), asyncio.Task):
        task: asyncio.Task[Any] = st["search_task"]
        if not task.done():
            task.cancel()
    _STATE.pop(cid, None)


def _coerce_timeout(inp: Number) -> float:
    v = inp.value
    try:
        v = float(v)
        if v <= 0:
            raise ValueError
    except (TypeError, ValueError):
        ui.notify(t("Timeout must be > 0 seconds, reset to 10.0"), type="warning")
        v = _DEFAULT_TIMEOUT
    inp.set_value(v)
    inp.sanitize()
    return float(v)


def _coerce_psl(inp: Number) -> int:
    v = inp.value
    try:
        v = int(v)
        if v <= 0:
            raise ValueError
    except (TypeError, ValueError):
        ui.notify(
            t("Per-site limit must be a positive integer, reset to 30"), type="warning"
        )
        v = _DEFAULT_SITE_LIMIT
    inp.set_value(v)
    inp.sanitize()
    return int(v)


def _render_placeholder_cover() -> None:
    with ui.element("div").classes("nd-cover-empty"):
        ui.icon("menu_book").classes("text-2xl")


def _site_label(site_key: str) -> str:
    return SEARCH_SUPPORT_SITES.get(site_key, site_key)


def _spawn_login_window(site: str) -> subprocess.Popen[bytes] | None:
    """
    Launch the interactive login window in a separate process.

    pywebview must own its process' main thread, so it cannot share the server
    process; a child process keeps the UI responsive meanwhile.
    """
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "novel_downloader.apps.login.main", site]
        )
    except OSError:
        logger.exception("Failed to open the login window for %s", site)
        return None


def _apply_sort(state: dict[str, Any]) -> None:
    """Rank state['results'] in place according to sort_key/order."""
    key = state.get("sort_key") or "quality"
    order = state.get("sort_order") or default_order(key)
    sort_results(
        state["results"],
        by=key,
        order=order,
        keyword=str(state.get("query") or ""),
        site_labels=SEARCH_SUPPORT_SITES,
    )


def _render_result_row(r: SearchResult) -> None:
    with (
        ui.card().props("flat").classes("w-full"),
        ui.row().classes("items-start justify-between w-full gap-4"),
    ):
        cover = (r.get("cover_url") or "").strip()
        if cover.startswith(("http://", "https://")):
            ui.image(cover).classes("nd-cover")
        else:
            _render_placeholder_cover()

        with ui.column().classes("gap-1 grow"):
            ui.link(r["title"], r["book_url"], new_tab=True).classes(
                "nd-link text-base"
            )
            ui.label(
                t("{author} · {words} · Updated at {date}").format(
                    author=r["author"], words=r["word_count"], date=r["update_date"]
                )
            ).classes("text-xs nd-muted")
            if r["latest_chapter"].strip():
                ui.label(r["latest_chapter"]).classes("text-sm nd-muted")

            with ui.row().classes("items-center gap-2"):
                ui.label(_site_label(r["site"])).classes("nd-pill")
                ui.label(f"ID: {r['book_id']}").classes("text-xs nd-muted")

        dl_btn = ui.button(t("Download"), icon="download").props(
            "unelevated no-caps color=primary"
        )

        async def _add_task() -> None:
            title = r["title"]
            # guard against double submission while the task is being queued
            dl_btn.disable()
            dl_btn.props("loading")
            try:
                ui.notify(t("Task added: {title}").format(title=title))
                await manager.add_task(
                    title=title, site=r["site"], book_id=r["book_id"]
                )
            finally:
                dl_btn.props(remove="loading")
                dl_btn.enable()

        dl_btn.on_click(_add_task)


def _build_settings_dropdown(
    state: dict[str, Any],
) -> tuple[Callable[[], list[str] | None], Callable[[], int], Callable[[], float]]:
    """
    Create settings button + anchored menu with initial values from state.

    Returns a tuple of getter functions:
      * get_sites(): list of site keys, or None if none selected
      * get_psl(): per-site limit (int)
      * get_timeout(): timeout (float)
    """
    site_cbs: dict[str, Any] = {}

    settings_btn = (
        ui.button(t("Settings"), icon="settings")
        .props("flat no-caps")
        .classes("nd-btn-ghost")
    )
    with settings_btn:
        menu = ui.menu().props("no-parent-event")
        with menu:
            ui.label(t("Site Selection")).classes("text-sm nd-muted q-mb-xs")

            with ui.row().classes("gap-2"):

                def _select_all() -> None:
                    for cb in site_cbs.values():
                        cb.set_value(True)

                def _clear_all() -> None:
                    for cb in site_cbs.values():
                        cb.set_value(False)

                ui.button(t("Select All"), on_click=_select_all).props(
                    "flat no-caps dense"
                )
                ui.button(t("Clear"), on_click=_clear_all).props("flat no-caps dense")

            ui.separator()

            with (
                ui.scroll_area().classes("w-[300px] max-h-[260px] q-mt-xs"),
                ui.column().classes("gap-1"),
            ):
                selected = set(state.get("sites") or [])
                for key, label in SEARCH_SUPPORT_SITES.items():
                    site_cbs[key] = ui.checkbox(label, value=(key in selected))

            ui.separator()
            ui.label(t("Advanced Settings")).classes("text-sm nd-muted q-mt-sm")

            psl_in = (
                ui.number(
                    t("Per-site Limit"),
                    value=state["per_site_limit"],
                    min=1,
                    step=1,
                )
                .without_auto_validation()
                .classes("w-[180px]")
            )
            timeout_in = (
                ui.number(
                    t("Timeout (seconds)"),
                    value=state["timeout"],
                    format="%.1f",
                    min=0.1,
                    step=0.1,
                )
                .without_auto_validation()
                .classes("w-[180px]")
            )

    settings_btn.on("click", lambda: menu.open())

    def _get_sites() -> list[str] | None:
        chosen = [k for k, cb in site_cbs.items() if bool(cb.value)]
        return chosen or None

    def _get_psl() -> int:
        val = _coerce_psl(psl_in)
        state["per_site_limit"] = val
        return val

    def _get_timeout() -> float:
        val = _coerce_timeout(timeout_in)
        state["timeout"] = val
        return val

    return _get_sites, _get_psl, _get_timeout


@ui.page("/")
def page_search() -> None:
    page_frame("search")
    setup_dialog()

    state = _get_state()

    # ---------- Outer container ----------
    with ui.column().classes("nd-page w-full max-w-screen-lg min-w-[320px] mx-auto"):
        page_header(
            t("Search"),
            t("Search across supported sites and queue downloads in one click"),
        )

        # ---------- Sticky toolbar ----------
        with (
            ui.card().props("flat").classes("nd-toolbar w-full"),
            ui.column().classes("w-full gap-3"),
        ):
            with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                get_sites, get_psl, get_timeout = _build_settings_dropdown(state)

                query_in = (
                    ui.input(t("Enter keyword"), value=state["query"])
                    .props("outlined dense clearable autocomplete=off")
                    .classes("min-w-[260px] grow")
                )

                search_btn = ui.button(t("Search"), icon="search").props(
                    "unelevated no-caps color=primary"
                )
                stop_btn = (
                    ui.button(
                        t("Stop"),
                        icon="stop",
                        on_click=lambda: _cancel_current(state),
                    )
                    .props("flat no-caps")
                    .classes("nd-btn-ghost")
                )
                stop_btn.disable()  # nothing to stop until a search runs

            with ui.row().classes(
                "items-center justify-between w-full gap-2 flex-wrap"
            ):

                async def _open_login(site: str) -> None:
                    proc = _spawn_login_window(site)
                    if proc is None:
                        ui.notify(
                            t("Could not open the login window."), type="negative"
                        )
                        return
                    ui.notify(t("Login window opened; complete the sign-in there."))
                    code = await asyncio.to_thread(proc.wait)
                    if code == 0:
                        ui.notify(
                            t("Login successful; session cookies saved."),
                            type="positive",
                        )
                    else:
                        ui.notify(t("Login was not completed."), type="warning")

                # sites that need a signed-in session
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    for site_key in sorted(LOGIN_SITES):
                        ui.button(
                            t("Log in to {site}").format(site=_site_label(site_key)),
                            on_click=partial(_open_login, site_key),
                        ).props("flat no-caps dense").classes("nd-btn-ghost")

                # sort controls
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    sort_key_labels = {
                        "quality": t("Quality"),
                        "priority": t("Priority"),
                        "site": t("Site"),
                        "title": t("Title"),
                        "author": t("Author"),
                        "words": t("Word count"),
                        "update": t("Update time"),
                    }
                    sort_key_sel = (
                        ui.select(
                            sort_key_labels,
                            value=state["sort_key"],
                            label=t("Sort"),
                            with_input=False,
                        )
                        .props("outlined dense")
                        .classes("w-[180px]")
                    )

                    sort_order_sel = ui.toggle(
                        {"asc": t("Ascending"), "desc": t("Descending")},
                        value=state["sort_order"],
                    ).props("dense no-caps")

                    def _on_sort_change(_: Any = None) -> None:
                        new_key = sort_key_sel.value or "quality"
                        if new_key != state["sort_key"]:
                            # Switching keys resets to the natural direction.
                            state["sort_key"] = new_key
                            state["sort_order"] = default_order(new_key)
                            sort_order_sel.set_value(state["sort_order"])
                        else:
                            state["sort_key"] = new_key
                            state["sort_order"] = sort_order_sel.value or default_order(
                                new_key
                            )
                        _apply_sort(state)
                        state["page"] = 1
                        render_results.refresh()

                    sort_key_sel.on_value_change(_on_sort_change)
                    sort_order_sel.on_value_change(_on_sort_change)

        # ---------- Status ----------
        status_area = ui.row().classes(
            "items-center gap-2 my-2 w-full text-sm nd-muted"
        )

        # ---------- Results + pager ----------
        list_area = ui.column().classes("w-full gap-3")
        pager_area = ui.row().classes("items-center justify-center w-full q-mt-md")

    # applied disabled state of the Stop button, to avoid redundant toggling
    stop_disabled = True

    @ui.refreshable
    def render_status() -> None:
        nonlocal stop_disabled
        want_disabled = not bool(state.get("searching"))
        if want_disabled != stop_disabled:
            stop_disabled = want_disabled
            if want_disabled:
                stop_btn.disable()
            else:
                stop_btn.enable()

        status_area.clear()
        with status_area:
            searching = bool(state.get("searching"))
            if searching:
                ui.icon("hourglass_top").classes("nd-muted")
                ui.label(t("Searching (results will appear progressively)...")).classes(
                    "text-caption"
                )
            total = len(state.get("results") or [])
            if searching or total > 0:
                started_at = float(state.get("started_at") or 0.0)
                seconds = time.monotonic() - started_at if started_at > 0.0 else 0.0
                ui.label(
                    t("Retrieved {total} results in {seconds}s").format(
                        total=total, seconds=f"{seconds:.1f}"
                    )
                ).classes("text-caption")

    @ui.refreshable
    def render_results() -> None:
        list_area.clear()
        pager_area.clear()

        results: list[SearchResult] = state["results"]
        total = len(results)
        page_size = int(state["page_size"])
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(int(state["page"]), total_pages))
        state["page"] = page

        start = (page - 1) * page_size
        end = min(total, start + page_size)
        current = results[start:end]

        if total > 0:
            tip = t("Total {total} results (page {page}/{pages})").format(
                total=total, page=page, pages=total_pages
            )
            with list_area:
                ui.label(tip).classes("text-sm text-caption")
                for r in current:
                    _render_result_row(r)
        elif state.get("searching"):
            with list_area:
                for _ in range(3):
                    skeleton_card()
        elif state.get("searched"):
            # a search has run to completion but matched nothing
            with list_area:
                empty_state("search_off", t("No books found for this keyword"))
        else:
            with list_area:
                empty_state("search", t("Enter a keyword to start searching"))

        if total_pages > 1:

            def _on_page_change(e: ValueChangeEventArguments[Any]) -> None:
                try:
                    state["page"] = int(e.value or 1)
                except Exception:
                    state["page"] = 1
                render_results.refresh()

            with pager_area:
                ui.pagination(
                    1,  # min
                    total_pages,  # max
                    direction_links=True,
                    value=page,
                    on_change=_on_page_change,
                ).props(f"max-pages={_PAGER_WIDTH} boundary-numbers ellipses")

    def _cancel_current(st: dict[str, Any]) -> None:
        task: asyncio.Task[Any] | None = st.get("search_task")
        if task and not task.done():
            task.cancel()
        st["search_task"] = None
        st["searching"] = False
        render_status.refresh()

    async def _run_stream(
        q: str, sites: list[str] | None, per_site_limit: int, timeout_val: float
    ) -> None:
        try:
            # show loading state
            state["searching"] = True
            search_btn.props("loading")
            render_status.refresh()

            # clear existing results and reset pagination
            state["results"] = []
            state["page"] = 1
            render_results.refresh()

            async with aclosing(
                search_stream(
                    keyword=q,
                    sites=sites,
                    per_site_limit=per_site_limit,
                    timeout=timeout_val,
                )
            ) as stream:
                async for chunk in stream:
                    if not chunk:
                        continue
                    state["results"].extend(chunk)
                    _apply_sort(state)
                    render_status.refresh()
                    render_results.refresh()

        except asyncio.CancelledError:
            # cancellation when user starts a new search or presses Stop
            pass
        finally:
            state["searching"] = False
            search_btn.props(remove="loading")
            render_status.refresh()

    async def do_search() -> None:
        q = (query_in.value or "").strip()
        if not q:
            ui.notify(t("Please enter a keyword"), type="warning")
            return

        # Cancel any previous search
        _cancel_current(state)

        # persist current settings
        state["query"] = q
        state["sites"] = get_sites()
        per_site_limit = get_psl()
        timeout_val = get_timeout()

        # mark that a search has run and record its start time
        state["searched"] = True
        state["started_at"] = time.monotonic()

        # kick off streaming search as a background task
        task = asyncio.create_task(
            _run_stream(q, state["sites"], per_site_limit, timeout_val)
        )
        state["search_task"] = task

    def _on_key(e: KeyEventArguments) -> None:
        if not e.action.keyup:
            return

        total = len(state["results"])
        if total == 0:
            return
        page_size = int(state["page_size"])
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = state["page"]

        if e.key == "ArrowLeft" and page > 1:
            state["page"] -= 1
            render_results.refresh()
        elif e.key == "ArrowRight" and page < total_pages:
            state["page"] += 1
            render_results.refresh()

    search_btn.on("click", do_search)
    query_in.on("keydown.enter", do_search)
    ui.keyboard(on_key=_on_key)

    # initial render
    render_status()
    render_results()

    # clean up state on disconnect to avoid leaks
    with suppress(Exception):
        ui.context.client.on_disconnect(_cleanup_state)
