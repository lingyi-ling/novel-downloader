#!/usr/bin/env python3
"""
novel_downloader.apps.web.components.navigation
-----------------------------------------------

A tiny NiceGUI component that renders the app's top navigation bar
"""

from typing import Any

from nicegui import ui
from nicegui.events import ClickEventArguments, ValueChangeEventArguments

from novel_downloader.infra.i18n import (
    available_languages,
    get_language,
    set_language,
    t,
)

from ..theme import install

_dark_state = {"value": False}


def _theme_props(is_dark: bool | None) -> str:
    icon = "dark_mode" if is_dark else "light_mode"
    return f"flat round dense icon={icon}"


def navbar(active: str) -> None:
    """
    Render the site-wide navigation header.

    :param active: Key of the current page to highlight.
    """
    install()
    dark = ui.dark_mode(value=_dark_state["value"])

    def toggle(e: ClickEventArguments) -> None:
        new_val = not dark.value
        dark.set_value(new_val)
        _dark_state["value"] = new_val
        theme_btn.props(_theme_props(new_val))

    def switch_language(e: ValueChangeEventArguments[Any]) -> None:
        if set_language(str(e.value)):
            ui.navigate.reload()

    with ui.header().classes("items-center justify-between gap-4"):
        with ui.row().classes("items-center gap-1"):
            ui.label(t("Novel Downloader")).classes("nd-brand q-mr-md")
            _nav_btn(t("Search"), "/", active == "search")
            _nav_btn(t("Download"), "/download", active == "download")
            _nav_btn(t("In Progress"), "/progress", active == "progress")
            _nav_btn(t("History"), "/history", active == "history")

        with ui.row().classes("items-center gap-2"):
            lang_sel = ui.select(
                available_languages(),
                value=get_language(),
                with_input=False,
                on_change=switch_language,
            ).props("dense outlined options-dense")
            lang_sel.classes("w-[132px]")
            lang_sel.tooltip(t("Language"))

            theme_btn = (
                ui.button(on_click=toggle)
                .props(_theme_props(dark.value))
                .classes("nd-icon-btn")
            )


def _nav_btn(label: str, path: str, is_active: bool) -> None:
    classes = "nd-nav-item" + (" nd-nav-item--active" if is_active else "")
    ui.button(label, on_click=lambda: ui.navigate.to(path)).props(
        "flat no-caps dense"
    ).classes(classes)
