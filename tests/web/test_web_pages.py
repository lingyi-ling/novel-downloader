#!/usr/bin/env python3
"""
tests.web.test_web_pages
------------------------

In-process page regression tests for the NiceGUI web UI.

Every route is rendered inside NiceGUI's official ``user_simulation`` context:
the ``@ui.page`` functions are imported (which registers the routes) and then
executed against the in-process ASGI application, driven through an ``httpx``
``ASGITransport`` client.  No TCP port is opened and no server process is
started, so these tests stay fast and host independent.

The assertions are deliberately structural -- presence of the shell, the five
navigation entries, toolbars and page headings, the mutual visibility of the
download input sections, and one rendered history row -- rather than styling
or exact-copy based, so restyling the pages does not break them.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from nicegui import ui
from nicegui.testing import User, user_simulation

from novel_downloader.infra.i18n import t

_PAGES_MODULE = "novel_downloader.apps.web.pages"
_WEB_PREFIX = "novel_downloader.apps.web"

#: The five routes the shell is expected to expose.
_ROUTES = ("/", "/download", "/progress", "/history", "/music")

#: Navigation keys shared with ``components.shell``.
_NAV_KEYS = ("search", "download", "progress", "history", "music")

#: Set once a transient import failure has already consumed its single retry.
_retry_used = False


def _purge_web_modules() -> None:
    """Drop cached web modules so the next import rebuilds them from scratch."""
    for name in [
        n
        for n in list(sys.modules)
        if n == _WEB_PREFIX or n.startswith(_WEB_PREFIX + ".")
    ]:
        sys.modules.pop(name, None)


def _import_pages() -> None:
    """
    Import the page package so its ``@ui.page`` routes are registered.

    Any cached web modules are dropped first: if another test module (or an
    earlier simulation) already imported them, the ``@ui.page`` decorators
    would not run again and every route would 404.

    Parallel edits to the page modules can momentarily leave them
    unimportable; wait once and retry before letting the error surface.
    """
    global _retry_used
    _purge_web_modules()
    try:
        importlib.import_module(_PAGES_MODULE)
    except Exception:
        if _retry_used:
            raise
        _retry_used = True
        time.sleep(30)
        _purge_web_modules()
        importlib.import_module(_PAGES_MODULE)


@contextlib.asynccontextmanager
async def page_user() -> AsyncIterator[User]:
    """
    Yield a simulated :class:`User` with the web UI pages registered.

    The routes must be re-imported for every test because NiceGUI's
    simulation context resets the global element/route state on exit.
    """
    async with user_simulation() as user:
        _import_pages()
        yield user


def _elements(
    user: User,
    *,
    kind: type[ui.element] | None = None,
    content: str | None = None,
) -> list[Any]:
    """Return the visible elements matching the given filter (empty if none)."""
    try:
        if kind is None:
            return list(user.find(content=content).elements)
        return list(user.find(kind=kind, content=content).elements)
    except AssertionError:
        return []


def _count(
    user: User,
    *,
    kind: type[ui.element] | None = None,
    content: str | None = None,
) -> int:
    """Return how many visible elements match the given filter."""
    return len(_elements(user, kind=kind, content=content))


def _drawer_elements(
    user: User,
    *,
    kind: type[ui.element] | None = None,
    content: str | None = None,
) -> list[Any]:
    """Return elements matching the filter, scoped to the navigation drawer."""
    with user.scope(kind=ui.left_drawer):
        return _elements(user, kind=kind, content=content)


def _body_elements(
    user: User,
    *,
    kind: type[ui.element] | None = None,
    content: str | None = None,
) -> list[Any]:
    """Return matching elements that are *not* part of the navigation drawer."""
    every = set(_elements(user, kind=kind, content=content))
    return list(every - set(_drawer_elements(user, kind=kind, content=content)))


def _nav_labels() -> set[str]:
    """Return the translated labels of the five expected navigation items."""
    from novel_downloader.apps.web.components.shell import _nav_label

    return {_nav_label(key) for key in _NAV_KEYS}


# ------------------------------------
# shell / navigation (all five routes)
# ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _ROUTES)
async def test_route_renders_shell(route: str) -> None:
    async with page_user() as user:
        await user.open(route)

        # top bar and navigation drawer exist
        await user.should_see(kind=ui.header)
        await user.should_see(kind=ui.left_drawer)

        # the drawer holds exactly the five i18n navigation entries
        nav_labels = _nav_labels()
        assert len(nav_labels) == 5
        assert all(label.strip() for label in nav_labels)
        nav_buttons = _drawer_elements(user, kind=ui.button)
        nav_texts = {btn.text for btn in nav_buttons}
        missing = nav_labels - nav_texts
        assert nav_labels <= nav_texts, f"missing nav items on {route}: {missing}"
        assert len(nav_buttons) == len(_NAV_KEYS)

        # the page body renders its heading block (title + subtitle)
        body_labels = {
            lbl.text.strip()
            for lbl in _body_elements(user, kind=ui.label)
            if isinstance(lbl.text, str) and lbl.text.strip()
        }
        assert len(body_labels) >= 2, f"no heading block found on {route}"


# ------------------------------------
# search page
# ------------------------------------


@pytest.mark.asyncio
async def test_search_page_controls_and_empty_state() -> None:
    async with page_user() as user:
        await user.open("/")

        # query field plus search / stop buttons live in the page body
        assert _count(user, kind=ui.input) >= 1
        buttons = _body_elements(user, kind=ui.button)
        texts = {btn.text for btn in buttons}
        icons = {btn.props.get("icon") for btn in buttons}
        assert t("Search") in texts or "search" in icons
        assert t("Stop") in texts or "stop" in icons

        # nothing has been searched yet: no result cards are rendered
        assert _count(user, kind=ui.image) == 0
        assert _count(user, kind=ui.pagination) == 0


# ------------------------------------
# download page
# ------------------------------------


@pytest.mark.asyncio
async def test_download_page_toggle_swaps_input_sections() -> None:
    async with page_user() as user:
        await user.open("/download")

        toggles = _elements(user, kind=ui.toggle)
        assert toggles, "input-method toggle is missing"

        # "URL" mode shows one input section ...
        before = {element.id for element in _elements(user, kind=ui.input)}
        assert before, "no input is visible in the initial mode"

        # ... switching the mode swaps which section is visible
        client = user.client
        assert client is not None
        with client:
            toggles[0].value = "id"

        after = {element.id for element in _elements(user, kind=ui.input)}
        assert after, "no input is visible after switching mode"
        assert before != after, "input sections are not mutually exclusive"


# ------------------------------------
# progress page
# ------------------------------------


@pytest.mark.asyncio
async def test_progress_page_has_two_sections() -> None:
    async with page_user() as user:
        await user.open("/progress")

        assert _count(user, kind=ui.card) >= 2
        assert _count(user, content=t("Running / Queued")) >= 1
        assert _count(user, content=t("Completed / Cancelled / Failed")) >= 1


# ------------------------------------
# history page
# ------------------------------------


@pytest.mark.asyncio
async def test_history_page_toolbar_and_file_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "exports"
    out.mkdir()
    (out / "novel_one.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "settings.json").write_text(
        json.dumps({"general": {"output_dir": str(out)}}), encoding="utf-8"
    )

    async with page_user() as user:
        # ``history._output_dir()`` resolves the export directory at render time
        monkeypatch.chdir(tmp_path)
        await user.open("/history")

        # toolbar: type filter, filename filter and a refresh action
        assert _count(user, kind=ui.toggle) >= 1
        assert _count(user, kind=ui.input) >= 1
        buttons = _body_elements(user, kind=ui.button)
        assert any(
            btn.props.get("icon") == "refresh" or btn.text == t("Refresh")
            for btn in buttons
        ), "refresh button is missing"

        # the single fake export shows up as a file row
        assert _count(user, content="novel_one.txt") >= 1


# ------------------------------------
# music page
# ------------------------------------


@pytest.mark.asyncio
async def test_music_page_title_and_platform_select() -> None:
    async with page_user() as user:
        from novel_downloader.apps.web.pages import music

        if not music._MUSIC_AVAILABLE:
            pytest.skip("music extras are not installed")

        await user.open("/music")

        # the heading block (title + subtitle) is rendered in the page body
        body_labels = {
            lbl.text.strip()
            for lbl in _body_elements(user, kind=ui.label)
            if isinstance(lbl.text, str) and lbl.text.strip()
        }
        assert t("Music") in body_labels

        # the platform dropdown is present (it carries one option per provider)
        selects = _body_elements(user, kind=ui.select)
        assert any(len(getattr(sel, "options", {}) or {}) > 10 for sel in selects), (
            "platform selector is missing"
        )
