#!/usr/bin/env python3
"""
novel_downloader.apps.web.pages.download
----------------------------------------

"""

import asyncio

from nicegui import ui

from novel_downloader.apps.constants import DOWNLOAD_SUPPORT_SITES
from novel_downloader.infra.book_url_resolver import resolve_book_url
from novel_downloader.infra.i18n import t

from ..components import page_frame
from ..components.widgets import page_header
from ..services import manager, setup_dialog

_DEFAULT_SITE = "qidian"

#: Quiet period (in seconds) after the last keystroke before the URL is
#: parsed while the user is still typing or pasting.
_INPUT_PARSE_THROTTLE = 0.5

#: Only surface the parsing indicator once a resolve takes longer than this
#: (in seconds), so fast parses do not flash the banner.
_PARSE_FEEDBACK_DELAY = 0.3


@ui.page("/download")
def page_download() -> None:
    ui.page_title(f"{t('Download')} · {t('Novel Downloader')}")
    page_frame("download")
    setup_dialog()

    with ui.column().classes("nd-page w-full max-w-screen-lg min-w-[320px] mx-auto"):
        page_header(
            t("Download"),
            t(
                "Add books by pasting a detail-page link, or fill in the site "
                "and book ID"
            ),
        )

        with ui.card().props("flat").classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(t("Select input method")).classes("text-sm nd-muted")
                mode = ui.toggle(
                    {"url": t("Via URL"), "id": t("Site + ID")}, value="url"
                ).props("dense no-caps")

            ui.separator().classes("q-my-md")

            # --- URL mode controls ---
            with ui.column().classes("gap-2 w-full") as url_section:
                url_input = (
                    ui.input(t("Novel URL"))
                    .props("outlined dense clearable autocomplete=off")
                    .classes("w-full")
                )

                parsing_row = ui.row().classes(
                    "nd-parse-banner items-center gap-2 w-full"
                )
                with parsing_row:
                    ui.spinner(size="1rem", color="primary")
                    ui.label(t("Parsing link...")).classes("text-caption nd-muted")
                parsing_row.visible = False

                preview_row = ui.row().classes(
                    "nd-parse-banner items-center gap-2 w-full"
                )
                with preview_row:
                    ui.icon("check_circle").classes("text-positive")
                    site_badge = ui.label("").classes("nd-pill")
                    id_badge = ui.label("").classes("nd-pill")
                preview_row.visible = False

                ui.label(
                    t(
                        "Paste the full novel detail page link, the system will automatically parse the site and book ID"  # noqa: E501
                    )
                ).classes("text-caption nd-muted q-ml-sm")

            ui.separator().classes("q-my-md")

            # --- Site + ID controls ---
            with ui.column().classes("gap-2 w-full") as site_id_section:
                with ui.row().classes("items-start gap-2 w-full"):
                    site = (
                        ui.select(
                            DOWNLOAD_SUPPORT_SITES,
                            value=_DEFAULT_SITE,
                            label=t("Site"),
                            with_input=True,
                        )
                        .props("outlined dense")
                        .classes("w-full md:w-[40%]")
                    )
                    book_id = (
                        ui.input(t("Book ID"))
                        .props("outlined dense clearable autocomplete=off")
                        .classes("w-full md:w-[60%]")
                    )
                ui.label(
                    t(
                        "If you already know the site and book ID, you can enter them directly here"  # noqa: E501
                    )
                ).classes("text-caption nd-muted q-ml-sm")

            ui.separator().classes("q-my-md")

            # Shared actions
            with ui.row().classes("justify-end items-center gap-3 w-full"):
                clear_btn = (
                    ui.button(t("Clear")).props("flat no-caps").classes("nd-btn-ghost")
                )
                add_btn = ui.button(t("Add to download queue"), icon="add").props(
                    "unelevated no-caps color=primary"
                )

        # ---------- logic ----------

        def _apply_visibility() -> None:
            is_url = mode.value == "url"
            url_section.visible = is_url
            site_id_section.visible = not is_url
            if not is_url:
                parsing_row.visible = False
            preview_row.visible = (
                is_url and bool(site_badge.text) and bool(id_badge.text)
            )

        async def _resolve(raw: str) -> tuple[str | None, str | None]:
            try:
                # resolving may lazily load rule-based sources from disk
                info = await asyncio.to_thread(resolve_book_url, raw)
            except Exception:
                return None, None
            if not info:
                return None, None
            site_key = info["site_key"]
            bid = info.get("book_id")
            if not bid:
                return None, None
            return site_key, bid

        def _reset_preview() -> None:
            site_badge.text = ""
            id_badge.text = ""
            preview_row.visible = False

        def _set_parsing(active: bool) -> None:
            parsing_row.visible = active
            if active:
                _reset_preview()

        async def _resolve_with_feedback(raw: str) -> tuple[str | None, str | None]:
            """Resolve ``raw``, showing the parsing state only if it is slow."""
            task = asyncio.ensure_future(_resolve(raw))
            done, _ = await asyncio.wait({task}, timeout=_PARSE_FEEDBACK_DELAY)
            if done:
                return task.result()
            _set_parsing(True)
            try:
                return await task
            finally:
                _set_parsing(False)

        def _clear_all() -> None:
            mode.value = "url"
            url_input.value = ""
            book_id.value = ""
            site.value = _DEFAULT_SITE
            _reset_preview()
            parsing_row.visible = False
            _apply_visibility()

        mode.on_value_change(lambda _: _apply_visibility())
        _apply_visibility()

        async def _update_preview(raw: str) -> bool:
            site_key, bid = await _resolve_with_feedback(raw)
            if site_key and bid:
                site_display = DOWNLOAD_SUPPORT_SITES.get(site_key, site_key)
                site_badge.text = t("Site: {site}").format(site=site_display)
                id_badge.text = t("Book ID: {bid}").format(bid=bid)
                preview_row.visible = True
                return True
            _reset_preview()
            return False

        async def _preview_on_blur() -> None:
            raw = (url_input.value or "").strip()
            if not raw:
                _reset_preview()
                return
            if not await _update_preview(raw):
                ui.notify(
                    t("Parsing failed: The link may not be supported or is invalid"),
                    type="warning",
                )

        async def _preview_on_input() -> None:
            # Parsing while the user is still typing must stay silent: no toast
            # on failure, so a half-typed link never pops up an error.
            raw = (url_input.value or "").strip()
            if not raw:
                _reset_preview()
                return
            await _update_preview(raw)

        url_input.on("blur", _preview_on_blur)
        url_input.on(
            "update:model-value",
            _preview_on_input,
            args=[],
            throttle=_INPUT_PARSE_THROTTLE,
            leading_events=False,
            trailing_events=True,
        )

        async def _add_task_from_url() -> None:
            raw = (url_input.value or "").strip()
            if not raw:
                ui.notify(t("Please enter a novel URL"), type="warning")
                return
            site_key, bid = await _resolve(raw)
            if not site_key or not bid:
                ui.notify(
                    t("Unable to parse the URL, please check the link or site support"),
                    type="warning",
                )
                return
            site_display = DOWNLOAD_SUPPORT_SITES.get(site_key, site_key)
            title = f"{site_display} (id = {bid})"
            ui.notify(t("Task added: {title}").format(title=title))
            await manager.add_task(title=title, site=site_key, book_id=bid)

        async def _add_task_from_id() -> None:
            bid = (book_id.value or "").strip()
            if not bid:
                ui.notify(t("Please enter a Book ID"), type="warning")
                return
            site_key = str(site.value)
            site_display = DOWNLOAD_SUPPORT_SITES.get(site_key, site_key)
            title = f"{site_display} (id = {bid})"
            ui.notify(t("Task added: {title}").format(title=title))
            await manager.add_task(title=title, site=site_key, book_id=bid)

        async def add_task() -> None:
            # disable button to prevent duplicate submissions
            add_btn.props(remove="loading")
            add_btn.props("loading")
            add_btn.disable()
            try:
                if mode.value == "url":
                    await _add_task_from_url()
                else:
                    # safety net: a URL pasted into the ID field is handled on
                    # input already, but keep the check in case it slipped through
                    bid = (book_id.value or "").strip()
                    if bid.startswith(("http://", "https://")):
                        mode.value = "url"
                        _apply_visibility()
                        url_input.value = bid
                        await _add_task_from_url()
                    else:
                        await _add_task_from_id()
            finally:
                add_btn.enable()
                add_btn.props(remove="loading")

        async def _submit_from_url() -> None:
            # refresh the parse preview first so the banner matches the input
            raw = (url_input.value or "").strip()
            if raw:
                await _update_preview(raw)
            await add_task()

        async def _book_id_to_url() -> None:
            # Pasting a link into the ID field switches back to URL mode at once.
            raw = (book_id.value or "").strip()
            if not raw.startswith(("http://", "https://")):
                return
            book_id.value = ""
            url_input.value = raw
            mode.value = "url"
            _apply_visibility()
            await _update_preview(raw)

        book_id.on_value_change(_book_id_to_url)

        # enter key submits in both modes
        url_input.on("keydown.enter", lambda _: _submit_from_url())
        book_id.on("keydown.enter", lambda _: add_task())

        add_btn.on("click", add_task)
        clear_btn.on("click", lambda: _clear_all())

        # initial state
        _apply_visibility()
