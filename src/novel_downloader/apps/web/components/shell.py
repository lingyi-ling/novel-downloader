#!/usr/bin/env python3
"""
novel_downloader.apps.web.components.shell
-------------------------------------------

The application shell rendered on every page: a slim top bar plus a left
navigation drawer. Below the drawer's breakpoint the drawer turns into an
overlay opened from the top bar's menu button.
"""

from __future__ import annotations

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

_NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("search", "search", "/"),
    ("download", "download", "/download"),
    ("progress", "cloud_download", "/progress"),
    ("history", "history", "/history"),
)


def _nav_label(key: str) -> str:
    """Translate a nav key lazily so language switches re-render labels."""
    labels = {
        "search": t("Search"),
        "download": t("Download"),
        "progress": t("In Progress"),
        "history": t("History"),
    }
    return labels[key]


def _theme_props(is_dark: bool | None) -> str:
    icon = "dark_mode" if is_dark else "light_mode"
    return f"flat round dense icon={icon}"


def page_frame(active: str) -> None:
    """
    Render the shared shell: top bar and navigation drawer.

    :param active: Key of the current page to highlight in the drawer.
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

    drawer = ui.left_drawer().props("width=250 breakpoint=1024").classes("nd-drawer")
    with drawer, ui.column().classes("nd-side w-full"):
        with ui.column().classes("gap-0 q-px-sm"):
            ui.label(t("Novel Downloader")).classes("nd-brand")
            ui.label(t("Local-first novel downloader")).classes("nd-side-tagline")

        ui.separator().classes("q-my-md")

        with ui.column().classes("gap-1 w-full q-px-sm"):
            for key, icon, path in _NAV_ITEMS:
                _side_item(icon, _nav_label(key), path, key == active)

        ui.element("div").classes("grow")

    with ui.header().classes("nd-topbar items-center gap-2"):
        ui.button(icon="menu", on_click=drawer.toggle).props(
            "flat round dense"
        ).classes("nd-icon-btn lt-lg")
        ui.element("div").classes("grow")

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


def _side_item(icon: str, label: str, path: str, is_active: bool) -> None:
    classes = "nd-side-item" + (" nd-side-item--active" if is_active else "")
    ui.button(label, icon=icon, on_click=lambda: ui.navigate.to(path)).props(
        "flat no-caps"
    ).classes(classes)
