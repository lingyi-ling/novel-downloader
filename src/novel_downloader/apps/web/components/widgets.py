#!/usr/bin/env python3
"""
novel_downloader.apps.web.components.widgets
---------------------------------------------

Small shared UI building blocks used across the pages.
"""

from __future__ import annotations

from nicegui import ui

from novel_downloader.infra.i18n import t

from ..models import Status


def status_pill(status: Status) -> None:
    """Render a colored pill describing a task status."""
    labels = {
        Status.QUEUED: t("Queued"),
        Status.RUNNING: t("Downloading"),
        Status.PROCESSING: t("Processing"),
        Status.EXPORTING: t("Exporting"),
        Status.COMPLETED: t("Completed"),
        Status.CANCELLED: t("Cancelled"),
        Status.FAILED: t("Failed"),
    }
    ui.label(labels[status]).classes(f"nd-pill nd-pill--{status.value}")


def page_header(title: str, subtitle: str) -> None:
    """Render the standard page heading: serif title plus muted subtitle.

    The title is exposed as a level-1 heading so assistive technology can
    navigate to the page's main heading.
    """
    with ui.column().classes("nd-page-head w-full"):
        ui.label(title).classes("nd-h1").props("role=heading aria-level=1")
        ui.label(subtitle).classes("nd-sub")


def empty_state(icon: str, text: str) -> None:
    """Render a centered hint block for empty lists.

    The leading icon is purely decorative, so it is hidden from assistive
    technology; ``text`` carries the meaning.
    """
    with ui.column().classes("nd-empty w-full"):
        with (
            ui.element("div")
            .classes("nd-icon-tile nd-icon-tile--lg")
            .props("aria-hidden=true")
        ):
            ui.icon(icon)
        ui.label(text).classes("nd-sub")


def skeleton_card() -> None:
    """Render one shimmering placeholder card."""
    with ui.card().props("flat").classes("nd-skeleton w-full h-[120px]"):
        pass


def icon_tile(icon: str) -> None:
    """Render a rounded square tile holding an icon.

    The tile is decorative: callers always render the meaning as adjacent
    text, so it is hidden from assistive technology.
    """
    with ui.element("div").classes("nd-icon-tile").props("aria-hidden=true"):
        ui.icon(icon)
