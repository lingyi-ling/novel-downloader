#!/usr/bin/env python3
"""
novel_downloader.apps.web.pages.history
---------------------------------------

"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from nicegui import ui
from nicegui.events import KeyEventArguments, ValueChangeEventArguments

from novel_downloader.apps.common.history_files import (
    _DEFAULT_ORDER,
    FileItem,
    SortKey,
    SortOrder,
    TypeFilter,
    _apply_filter,
    _apply_sort,
    _format_size,
    _scan_files,
)
from novel_downloader.infra.i18n import t

from ..components import page_frame
from ..components.widgets import empty_state, icon_tile, page_header
from ..services import setup_dialog

_PAGE_SIZE = 20
_PAGER_WIDTH = 9

# ``FileItem`` and the helpers above are imported from the shared module so
# that existing imports (``pages.history.FileItem`` etc.) keep working.


def _render_file_row(item: FileItem) -> None:
    """Render a single file row with icon, meta, and a Download button."""
    url = f"/downloads/{quote(item.name)}?v={int(item.mtime)}"
    human_mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.mtime))

    with (
        ui.card().props("flat").classes("w-full"),
        ui.row().classes("items-center gap-4 w-full no-wrap"),
    ):
        icon_tile("menu_book" if item.ext == "epub" else "description")

        # Meta & actions
        with ui.column().classes("min-w-0 grow gap-1"):
            with ui.row().classes("items-center gap-2 min-w-0 w-full no-wrap"):
                ui.label(item.name).classes("text-base font-medium break-words")
                ui.label(item.ext).classes("nd-pill shrink-0")
            ui.label(
                f"{t('Modified Time')}: {human_mtime} · {_format_size(item.size)}"
            ).classes("text-xs nd-muted")

        ui.button(
            t("Download"),
            icon="download",
            on_click=lambda e, url=url: ui.download(url),
        ).props("flat no-caps").classes("nd-btn-ghost shrink-0")


@ui.page("/history")
def page_history() -> None:
    ui.page_title(f"{t('Download History')} · {t('Novel Downloader')}")
    page_frame("history")
    setup_dialog()

    # reactive state
    page: int = 1
    type_filter: TypeFilter = "All"
    name_query: str = ""
    sort_by: SortKey = "mtime"
    sort_order: SortOrder = "desc"
    syncing_sort_order: bool = False

    # cached lists
    all_items: list[FileItem] = []
    sorted_items: list[FileItem] = []

    # centered, responsive container
    with ui.column().classes("nd-page w-full max-w-screen-lg min-w-[320px] mx-auto"):
        page_header(
            t("Download History"),
            t("Browse and re-download exported files"),
        )

        # Toolbar (filters & sorting)
        with (
            ui.card().props("flat").classes("nd-toolbar w-full"),
            ui.row().classes("items-center gap-3 w-full flex-wrap"),
        ):
            ui.label(t("Type")).classes("text-sm nd-muted")
            type_sel = ui.toggle(
                {"All": t("All"), "txt": "txt", "epub": "epub"},
                value=type_filter,
            ).props("dense no-caps")

            name_filter_input = (
                ui.input(placeholder=t("Filter by filename"))
                .props("outlined dense clearable")
                .classes("w-[200px]")
            )

            ui.element("div").classes("grow")

            sort_key_sel = (
                ui.select(
                    {
                        "name": t("Filename"),
                        "mtime": t("Modified Time"),
                        "size": t("File Size"),
                    },
                    value=sort_by,
                    with_input=False,
                )
                .props("dense outlined")
                .classes("w-[160px]")
            )

            sort_order_sel = ui.toggle(
                {"asc": t("Ascending"), "desc": t("Descending")}, value=sort_order
            ).props("dense no-caps")

            def _on_refresh() -> None:
                _reload()
                _refresh()

            ui.button(
                t("Refresh"),
                icon="refresh",
                on_click=_on_refresh,
            ).props("flat no-caps").classes("nd-btn-ghost")

        # status line (counts)
        status_area = ui.row().classes("items-center gap-2 w-full text-sm nd-muted")

        # list + pager
        list_area = ui.column().classes("w-full gap-3")
        pager_area = ui.row().classes("justify-center my-2 w-full")

        def _reload() -> None:
            """Scan filesystem and refresh full list."""
            nonlocal all_items
            all_items = _scan_files()
            _recompute_from_cache()

        def _recompute_from_cache() -> None:
            """Recompute filtered+sorted items from cached all_items."""
            nonlocal sorted_items, page
            filtered = _apply_filter(all_items, type_filter, name_query)
            sorted_items = _apply_sort(filtered, sort_by, sort_order)
            page = 1

        def _refresh_status(total: int, filtered_total: int, total_size: int) -> None:
            status_area.clear()
            with status_area:
                if filtered_total == total:
                    count_text = t("Total {total} files").format(total=total)
                else:
                    count_text = t(
                        "Total {total} files · After filter {filtered} files"
                    ).format(total=total, filtered=filtered_total)
                size_text = t("Total size {size}").format(size=_format_size(total_size))
                ui.label(f"{count_text} · {size_text}")

        def _refresh() -> None:
            nonlocal page
            total_all = len(all_items)
            total = len(sorted_items)
            total_size = sum(it.size for it in sorted_items)
            total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

            # clamp page
            page = min(max(page, 1), total_pages)

            start = (page - 1) * _PAGE_SIZE
            end = min(start + _PAGE_SIZE, total)
            current_slice = sorted_items[start:end]

            _refresh_status(total_all, total, total_size)

            list_area.clear()
            with list_area:
                if not current_slice:
                    has_filter = type_filter != "All" or bool(name_query)
                    if has_filter and total_all > 0:
                        empty_state("folder_open", t("No files match the filter"))
                    else:
                        empty_state("folder_open", t("No files"))
                else:
                    for item in current_slice:
                        _render_file_row(item)

            pager_area.clear()
            if total_pages > 1:

                def _on_page_change(e: ValueChangeEventArguments[Any]) -> None:
                    nonlocal page
                    try:
                        page = int(e.value)
                    except Exception:
                        page = 1
                    _refresh()

                with pager_area:
                    ui.pagination(
                        1,  # min
                        total_pages,  # max
                        direction_links=True,
                        value=page,
                        on_change=_on_page_change,
                    ).props(f"max-pages={_PAGER_WIDTH} boundary-numbers ellipses")

        # Handlers
        def _on_type_change(e: ValueChangeEventArguments[Any]) -> None:
            nonlocal type_filter
            type_filter = e.value
            _recompute_from_cache()
            _refresh()

        def _on_name_query_change(e: ValueChangeEventArguments[Any]) -> None:
            nonlocal name_query
            value = e.value
            name_query = value.strip() if isinstance(value, str) else ""
            _recompute_from_cache()
            _refresh()

        def _on_sort_key_change(e: ValueChangeEventArguments[Any]) -> None:
            nonlocal sort_by, sort_order, syncing_sort_order
            sort_by = e.value
            # Switching the key resets the direction to that key's default.
            default_order = _DEFAULT_ORDER.get(sort_by, "desc")
            if sort_order != default_order:
                sort_order = default_order
                # Keep the toggle in sync without re-entering its handler.
                syncing_sort_order = True
                try:
                    sort_order_sel.value = default_order
                finally:
                    syncing_sort_order = False
            _recompute_from_cache()
            _refresh()

        def _on_sort_order_change(e: ValueChangeEventArguments[Any]) -> None:
            nonlocal sort_order, sorted_items, page
            if syncing_sort_order:
                return
            new_order = e.value
            # Optimization: if only order changes, reverse current list
            if sort_order != new_order and len(sorted_items) > 1:
                sorted_items.reverse()
                sort_order = new_order
                page = 1
                _refresh()
            else:
                sort_order = new_order
                _recompute_from_cache()
                _refresh()

        type_sel.on_value_change(_on_type_change)
        name_filter_input.on_value_change(_on_name_query_change)
        sort_key_sel.on_value_change(_on_sort_key_change)
        sort_order_sel.on_value_change(_on_sort_order_change)

        # Keyboard: left/right to change page (keyup only)
        def _on_key(e: KeyEventArguments) -> None:
            if not e.action.keyup:
                return
            nonlocal page
            total = len(sorted_items)
            total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

            if e.key == "ArrowLeft" and page > 1:
                page -= 1
                _refresh()
            elif e.key == "ArrowRight" and page < total_pages:
                page += 1
                _refresh()

        ui.keyboard(on_key=_on_key)

        # Initial render
        _reload()
        _refresh()
