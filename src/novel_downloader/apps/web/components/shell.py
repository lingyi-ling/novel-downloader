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

from novel_downloader import __version__
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
    ("music", "music_note", "/music"),
)


def _nav_label(key: str) -> str:
    """Translate a nav key lazily so language switches re-render labels."""
    labels = {
        "search": t("Search"),
        "download": t("Download"),
        "progress": t("In Progress"),
        "history": t("History"),
        "music": t("Music"),
    }
    return labels[key]


def _theme_props(is_dark: bool | None) -> str:
    icon = "dark_mode" if is_dark else "light_mode"
    return f"flat round dense icon={icon}"


def _aria_props(label: str) -> str:
    """
    Build a quote-safe ``aria-label`` prop string.

    NiceGUI tokenizes prop strings itself, so an unescaped double quote in
    ``label`` (as a translation may contain) would silently truncate the value.

    :param label: Accessible name to expose on the element.
    :return: A prop string such as ``aria-label="Open navigation"``.
    """
    escaped = label.replace("\\", "\\\\").replace('"', '\\"')
    return 'aria-label="' + escaped + '"'


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

        with (
            ui.column()
            .classes("gap-1 w-full q-px-sm")
            .props(f"role=navigation {_aria_props(t('Main navigation'))}")
        ):
            for key, icon, path in _NAV_ITEMS:
                _side_item(icon, _nav_label(key), path, key == active)

        ui.element("div").classes("grow")

        ui.label(f"v{__version__}").classes("nd-side-tagline q-px-sm")

    with ui.header().classes("nd-topbar items-center gap-2"):
        ui.button(icon="menu", on_click=drawer.toggle).props(
            f"flat round dense {_aria_props(t('Open navigation'))}"
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

        theme_label = t("Toggle theme")
        theme_btn = (
            ui.button(on_click=toggle)
            .props(_theme_props(dark.value))
            .props(_aria_props(theme_label))
            .classes("nd-icon-btn")
        )
        theme_btn.tooltip(theme_label)


def _side_item(icon: str, label: str, path: str, is_active: bool) -> None:
    classes = "nd-side-item" + (" nd-side-item--active" if is_active else "")
    ui.button(label, icon=icon, on_click=lambda: ui.navigate.to(path)).props(
        "flat no-caps"
    ).classes(classes)
