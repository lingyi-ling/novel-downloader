#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.ui
--------------------------------

CustomTkinter user interface for the native desktop client.

Layout
------

A fixed-width sidebar (logo, navigation, theme switch, version) on the left
and a content area on the right.  The content area stacks the five pages
(Search, Download, History, Music, Follow) on top of each other and raises
the active one, replacing the old ``ttk.Notebook`` with a custom navigation
whose selected item is highlighted in the accent color.

The lists are card lists: every row of the old ``ttk.Treeview`` tables is now
a rounded ``CTkFrame`` card with its own actions, rendered inside a
``CTkScrollableFrame``.

Threading contract
------------------
* Every network/engine call runs outside the UI thread.
* Async engine calls use ``asyncio.run`` in their own worker thread, except
  download-task manager calls which go through :class:`AsyncEngine` (a
  persistent loop thread; see :mod:`.engine`).
* Worker threads never touch Tk.  They push messages to :attr:`DesktopApp.events`
  and the UI thread drains that queue in ``_poll`` every 200 ms.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections.abc import Callable, Coroutine, Iterable
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, TypeVar

import customtkinter as ctk

from novel_downloader import __version__
from novel_downloader.apps.common.history_files import (
    FileItem,
    _apply_sort,
    _output_dir,
    _scan_files,
)
from novel_downloader.apps.constants import SEARCH_SUPPORT_SITES
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.core.task_manager import manager
from novel_downloader.apps.core.watchlist import (
    DEFAULT_CHECK_TIMEOUT,
    CheckResult,
    WatchEntry,
    WatchlistStore,
    check_all,
)
from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import app_home
from novel_downloader.plugins.search import search
from novel_downloader.schemas import SearchResult

from . import theme as palette
from .engine import AsyncEngine
from .formatting import (
    FILTER_ALL,
    FOLLOW_ADDED,
    FOLLOW_ALREADY,
    FOLLOW_INVALID,
    TASK_FILTER_CODES,
    ExportChoice,
    SearchMenuState,
    TaskMenuState,
    can_follow_search_result,
    filter_tasks,
    follow_feedback_message,
    follow_row,
    format_error,
    format_task_progress,
    health_rows,
    health_summary,
    history_row,
    music_row,
    parse_sites,
    retry_params,
    search_menu_state,
    status_label,
    task_detail_text,
    task_export_choices,
    task_filter_code,
    task_filter_label,
    task_menu_state,
    task_output_actions,
)
from .notifications import (
    TaskNotification,
    TaskOutcome,
    detect_task_notifications,
    flatten_task_snapshot,
    play_notification_sound,
    snapshot_task_states,
    task_event_status_message,
)
from .preferences import (
    DEFAULT_API_BASE,
    MUSIC_LEVELS,
    MusicSettings,
    SearchState,
    environment_summary,
    level_code,
    level_display,
    music_settings_from_mapping,
    music_settings_to_mapping,
    remember_search,
    resolve_output_dir,
)
from .window_state import (
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    load_window_geometry,
    parse_geometry,
    save_window_geometry,
    serialize_geometry,
    window_state_file,
)

__all__ = ["DesktopApp", "run_app"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

_POLL_MS = 200
_SNAPSHOT_SECONDS = 0.5
_SEARCH_PER_SITE = 5
_SEARCH_TIMEOUT = 10.0
_MUSIC_SEARCH_LIMIT = 30
_MUSIC_WORKERS = 3

#: Geometry used when no valid window state was stored yet.
_DEFAULT_GEOMETRY = "1200x780"

#: How long the status bar keeps a task-notification highlight (ms).
_NOTIFY_STATUS_MS = 5000

#: Status-bar highlight colors as ``(light, dark)`` text-color pairs, shared
#: with the notification module's tests.
_STATUS_HIGHLIGHT_COMPLETED = palette.STATUS_OK_TEXT
_STATUS_HIGHLIGHT_FAILED = palette.STATUS_FAIL_TEXT
_STATUS_DEFAULT_TEXT = palette.TEXT

#: Per-platform budget for the health check; each platform gets one bounded
#: search (see ``apps.music.providers.health_check``).
_MUSIC_HEALTH_TIMEOUT = 15.0

#: Outcome kinds posted by the follow-check worker for each book.
_FOLLOW_ITEM_KINDS = ("first", "updated", "up_to_date", "not_found", "error")

#: Sidebar width in pixels; the content area takes the rest.
_SIDEBAR_WIDTH = 200

#: Tk ``Control`` modifier mask (``event.state``) for ctrl-click multi-select.
_CONTROL_MASK = 0x0004

#: Page order used by the sidebar navigation and the Ctrl+1..5 shortcuts.
_PAGE_KEYS = ("search", "download", "history", "music", "follow")

#: Sidebar icon shown in front of each navigation label.
_PAGE_ICONS = {
    "search": "⌕",
    "download": "⇩",
    "history": "↺",
    "music": "♪",
    "follow": "♥",
}

#: Font family for the whole UI (falls back to the theme default elsewhere).
_FONT_FAMILY = "Segoe UI" if sys.platform.startswith("win") else "Roboto"

#: Progress-bar easing: steps and per-step delay (ms) for the smooth fill.
_PROGRESS_STEPS = 8
_PROGRESS_STEP_MS = 16

#: Page transition: fade steps and per-step delay (ms).
_FADE_STEPS = 6
_FADE_STEP_MS = 16

#: How strongly the incoming page is tinted at the start of the fade.
_FADE_TINT = 0.25

#: Delay (ms) before a ``CTkToplevel`` dialog claims the modal grab; the
#: Windows titlebar setup re-maps the window shortly after creation.
_MODAL_GRAB_DELAY_MS = 200

#: Ctrl+1..5 raise the pages in this order (kept from the notebook era).
_PAGE_ORDER = ("search", "download", "history", "music", "follow")

Event = tuple[str, Any]


async def _snapshot_tasks() -> dict[str, list[DownloadTask]]:
    """Read a task snapshot from the background loop thread."""
    return manager.snapshot()


# ---------------------------------------------------------------------------
# Custom theme
# ---------------------------------------------------------------------------
def apply_custom_theme() -> None:
    """
    Write the desktop palette into CustomTkinter's active theme.

    Must run before any widget is created.  Every color is written as a
    ``(light, dark)`` pair so widget re-skinning on appearance-mode changes is
    handled by CustomTkinter itself.
    """
    widget_theme = ctk.ThemeManager.theme
    widget_theme["CTk"]["fg_color"] = list(palette.BG)
    widget_theme["CTkToplevel"]["fg_color"] = list(palette.BG)

    frame = widget_theme["CTkFrame"]
    frame["corner_radius"] = 10
    frame["fg_color"] = list(palette.BG)
    frame["top_fg_color"] = list(palette.CARD)
    frame["border_color"] = list(palette.BORDER)

    button = widget_theme["CTkButton"]
    button["corner_radius"] = 8
    button["border_width"] = 1
    button["fg_color"] = list(palette.CARD)
    button["hover_color"] = list(palette.HOVER)
    button["border_color"] = list(palette.BORDER)
    button["text_color"] = list(palette.TEXT)
    button["text_color_disabled"] = list(palette.MUTED)

    widget_theme["CTkLabel"]["text_color"] = list(palette.TEXT)

    entry = widget_theme["CTkEntry"]
    entry["corner_radius"] = 8
    entry["fg_color"] = list(palette.FIELD)
    entry["border_color"] = list(palette.BORDER)
    entry["text_color"] = list(palette.TEXT)
    entry["placeholder_text_color"] = list(palette.MUTED)

    checkbox = widget_theme["CTkCheckBox"]
    checkbox["fg_color"] = list(palette.ACCENT)
    checkbox["hover_color"] = list(palette.ACCENT_HOVER)
    checkbox["border_color"] = list(palette.BORDER)
    checkbox["text_color"] = list(palette.TEXT)

    switch = widget_theme["CTkSwitch"]
    switch["fg_color"] = list(palette.TRACK)
    switch["progress_color"] = list(palette.ACCENT)
    switch["text_color"] = list(palette.TEXT)

    progress = widget_theme["CTkProgressBar"]
    progress["fg_color"] = list(palette.TRACK)
    progress["progress_color"] = list(palette.ACCENT)

    option = widget_theme["CTkOptionMenu"]
    option["corner_radius"] = 8
    option["fg_color"] = list(palette.FIELD)
    option["button_color"] = list(palette.HOVER)
    option["button_hover_color"] = list(palette.BORDER)
    option["text_color"] = list(palette.TEXT)
    option["text_color_disabled"] = list(palette.MUTED)

    textbox = widget_theme["CTkTextbox"]
    textbox["corner_radius"] = 8
    textbox["fg_color"] = list(palette.FIELD)
    textbox["border_color"] = list(palette.BORDER)
    textbox["text_color"] = list(palette.TEXT)

    widget_theme["CTkScrollableFrame"]["label_fg_color"] = list(palette.SIDEBAR)

    dropdown = widget_theme["DropdownMenu"]
    dropdown["fg_color"] = list(palette.CARD)
    dropdown["hover_color"] = list(palette.HOVER)
    dropdown["text_color"] = list(palette.TEXT)

    widget_theme["CTkFont"]["family"] = _FONT_FAMILY


# ---------------------------------------------------------------------------
# Card selection model
# ---------------------------------------------------------------------------
class CardSelector:
    """
    Selection bookkeeping for a card list rendered from a sequence.

    The old ``ttk.Treeview`` managed row selection internally; card lists keep
    it here instead.  ``on_change`` is invoked after every mutation so the
    owning page can repaint the affected cards and refresh its action buttons.
    """

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._selected: set[int] = set()
        self._on_change = on_change

    @property
    def selection(self) -> list[int]:
        """The selected card indices, in ascending order."""
        return sorted(self._selected)

    def is_selected(self, index: int) -> bool:
        """Whether one card index is selected."""
        return index in self._selected

    def click(self, index: int, additive: bool = False) -> None:
        """
        Apply a user click on card ``index``.

        A plain click selects only that card; a ctrl-click (``additive``)
        toggles it without losing the rest.
        """
        if additive:
            if index in self._selected:
                self._selected.discard(index)
            else:
                self._selected.add(index)
        else:
            self._selected = {index}
        self._notify()

    def set_selection(self, indices: Iterable[int]) -> None:
        """Replace the selection with ``indices`` (used by tests/scripts)."""
        self._selected = {int(index) for index in indices}
        self._notify()

    def clear(self) -> None:
        """Drop the whole selection."""
        if not self._selected:
            return
        self._selected = set()
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()


# ---------------------------------------------------------------------------
# Card widgets
# ---------------------------------------------------------------------------
class InfoCard:
    """
    One selectable card row: bold title, muted meta line, optional buttons.

    Used for search results, history files, music tracks, follow entries and
    platform health rows.  Selection highlight, click/ctrl-click, double-click
    and right-click behaviour are shared by all lists.
    """

    def __init__(
        self,
        master: Any,
        app: DesktopApp,
        *,
        title: str,
        meta: str = "",
        buttons: Iterable[tuple[str, Callable[[], None]]] = (),
        on_click: Callable[[tk.Event[Any]], None] | None = None,
        on_double_click: Callable[[], None] | None = None,
        on_right_click: Callable[[tk.Event[Any]], None] | None = None,
    ) -> None:
        self.app = app
        self._selected = False
        self.frame = ctk.CTkFrame(
            master,
            corner_radius=10,
            fg_color=palette.CARD,
            border_width=1,
            border_color=palette.BORDER,
        )
        body = ctk.CTkFrame(self.frame, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=(12, 4), pady=8)
        self.title_label = ctk.CTkLabel(
            body,
            text=title,
            anchor="w",
            justify="left",
            font=app.font_bold,
            fg_color="transparent",
        )
        self.title_label.pack(fill="x")
        self.meta_label = ctk.CTkLabel(
            body,
            text=meta,
            anchor="w",
            justify="left",
            font=app.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        )
        if meta:
            self.meta_label.pack(fill="x")

        if buttons:
            actions = ctk.CTkFrame(self.frame, fg_color="transparent")
            actions.pack(side="right", fill="y", padx=(4, 8), pady=6)
            for text, command in buttons:
                ctk.CTkButton(
                    actions,
                    text=text,
                    width=64,
                    height=26,
                    corner_radius=7,
                    font=app.font_small,
                    command=command,
                ).pack(side="left", padx=(4, 0))

        # Click targets: the card body and its labels (buttons keep their own
        # clicks and never toggle the selection).
        click_targets: tuple[Any, ...] = (self.frame, body, self.title_label)
        if meta:
            click_targets += (self.meta_label,)
        for target in click_targets:
            target.bind("<Button-1>", self._handle_click)
            if on_double_click is not None:
                target.bind("<Double-Button-1>", lambda _e: on_double_click())
            if on_right_click is not None:
                target.bind("<Button-3>", self._handle_right_click)
                target.bind("<Button-2>", self._handle_right_click)

        self._on_click = on_click
        self._on_right_click = on_right_click

    # -- event plumbing -------------------------------------------------
    def _handle_click(self, event: tk.Event[Any]) -> None:
        if self._on_click is not None:
            self._on_click(event)

    def _handle_right_click(self, event: tk.Event[Any]) -> None:
        if self._on_right_click is not None:
            self._on_right_click(event)

    # -- appearance ------------------------------------------------------
    def set_selected(self, selected: bool) -> None:
        """Repaint the card for the selected (accent tint) or idle state."""
        if selected == self._selected:
            return
        self._selected = selected
        if selected:
            self.frame.configure(
                fg_color=palette.ACCENT_SOFT, border_color=palette.ACCENT
            )
        else:
            self.frame.configure(fg_color=palette.CARD, border_color=palette.BORDER)

    def destroy(self) -> None:
        """Remove the card from the list."""
        self.frame.destroy()


class TaskCard:
    """
    One download-task card with a live status badge and progress bar.

    Cards are created once per task id and updated in place by the 0.5 s
    snapshot poll, so the progress bars keep animating smoothly instead of
    being rebuilt twice a second.
    """

    def __init__(self, master: Any, app: DesktopApp, task_id: str) -> None:
        self.app = app
        self.task_id = task_id
        self._selected = False
        self._title_text = ""
        self._site_text = ""
        self._status_key = ""
        self._progress_text = ""
        self._progress_fraction: float | None = None
        self._error_text: str | None = None

        self.frame = ctk.CTkFrame(
            master,
            corner_radius=10,
            fg_color=palette.CARD,
            border_width=1,
            border_color=palette.BORDER,
        )

        head = ctk.CTkFrame(self.frame, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 0))
        self.badge = ctk.CTkLabel(
            head,
            text="",
            corner_radius=6,
            font=app.font_badge,
            fg_color=palette.BADGE_QUEUED[0],
            text_color=palette.BADGE_QUEUED[1],
        )
        self.badge.pack(side="left")
        self.title_label = ctk.CTkLabel(
            head,
            text="",
            anchor="w",
            font=app.font_bold,
            fg_color="transparent",
        )
        self.title_label.pack(side="left", padx=(8, 0))
        self.site_label = ctk.CTkLabel(
            head,
            text="",
            anchor="e",
            font=app.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        )
        self.site_label.pack(side="right")

        body = ctk.CTkFrame(self.frame, fg_color="transparent")
        body.pack(fill="x", padx=12, pady=(6, 8))
        self.progress_bar = ctk.CTkProgressBar(
            body, height=8, corner_radius=1000, progress_color=palette.ACCENT
        )
        self.progress_bar.set(0)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.progress_label = ctk.CTkLabel(
            body,
            text="",
            anchor="e",
            width=120,
            font=app.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        )
        self.progress_label.pack(side="left", padx=(10, 0))

        self.error_label = ctk.CTkLabel(
            self.frame,
            text="",
            anchor="w",
            justify="left",
            wraplength=900,
            font=app.font_small,
            text_color=palette.STATUS_FAIL_TEXT,
            fg_color="transparent",
        )
        # Hidden until the task actually carries an error.
        self.error_label.pack_forget()

        click_targets = (
            self.frame,
            head,
            self.badge,
            self.title_label,
            self.site_label,
            body,
            self.progress_label,
        )
        for target in click_targets:
            target.bind("<Button-1>", self._handle_click)
            target.bind("<Double-Button-1>", self._handle_double_click)
            target.bind("<Button-3>", self._handle_right_click)
            target.bind("<Button-2>", self._handle_right_click)

    # -- event plumbing -------------------------------------------------
    def _handle_click(self, event: tk.Event[Any]) -> None:
        self.app._on_task_card_click(self.task_id, event)

    def _handle_double_click(self, _event: tk.Event[Any]) -> None:
        self.app._on_task_card_double_click(self.task_id)

    def _handle_right_click(self, event: tk.Event[Any]) -> None:
        self.app._on_task_card_right_click(self.task_id, event)

    # -- state -----------------------------------------------------------
    def update(self, task: DownloadTask) -> None:
        """Refresh badge, progress and error from a fresh task snapshot."""
        title = str(getattr(task, "title", "") or "")
        if title != self._title_text:
            self._title_text = title
            self.title_label.configure(text=title)
        site = str(getattr(task, "site", "") or "")
        if site != self._site_text:
            self._site_text = site
            self.site_label.configure(text=site)

        status_value = str(getattr(task.status, "value", task.status))
        status_text = status_label(task.status)
        if status_value != self._status_key:
            self._status_key = status_value
            background, foreground = palette.BADGES.get(
                status_value, palette.BADGE_QUEUED
            )
            self.badge.configure(
                text=status_text, fg_color=background, text_color=foreground
            )
            self.progress_bar.configure(progress_color=self._bar_color(status_value))

        progress_text = format_task_progress(task.chapters_done, task.chapters_total)
        if progress_text != self._progress_text:
            self._progress_text = progress_text
            self.progress_label.configure(text=progress_text)

        fraction = max(0.0, min(1.0, float(task.progress())))
        if fraction != self._progress_fraction:
            self._progress_fraction = fraction
            self.app._animate_progress(self.progress_bar, fraction)

        error = str(task.error or "")
        if error != self._error_text:
            self._error_text = error
            if error:
                self.error_label.configure(text=f"{t('Error')}: {error}")
                self.error_label.pack(fill="x", padx=12, pady=(0, 8))
            else:
                self.error_label.pack_forget()

    @staticmethod
    def _bar_color(status_value: str) -> palette.Color:
        """Progress-bar fill color for a task status."""
        if status_value == "completed":
            return ("#2F6B34", "#8FCE9A")
        if status_value == "failed":
            return ("#B3392B", "#F0918A")
        return palette.ACCENT

    def set_selected(self, selected: bool) -> None:
        """Repaint the card for the selected or idle state."""
        if selected == self._selected:
            return
        self._selected = selected
        if selected:
            self.frame.configure(
                fg_color=palette.ACCENT_SOFT, border_color=palette.ACCENT
            )
        else:
            self.frame.configure(fg_color=palette.CARD, border_color=palette.BORDER)

    def destroy(self) -> None:
        """Remove the card from the list."""
        self.frame.destroy()


class DesktopApp:
    """Build and drive the desktop window."""

    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.events: queue.Queue[Event] = queue.Queue()
        self.engine = AsyncEngine()
        self._closing = False

        self._status_var = tk.StringVar(value=t("Ready"))
        # Task-completion notifications (the Android TaskEvents pattern): the
        # ``task_id -> status`` baseline of the previous snapshot, plus the
        # status-bar highlight bookkeeping.
        self._previous_task_states: dict[str, str] = {}
        self._status_label: ctk.CTkLabel | None = None
        self._status_highlight_after: str | None = None
        self._search_results: list[SearchResult] = []
        self._history_items: list[FileItem] = []
        self._snapshot_future: Future[dict[str, list[DownloadTask]]] | None = None
        self._last_snapshot = 0.0

        # Search widgets / state.
        self.search_var = tk.StringVar()
        self.sites_var = tk.StringVar()
        self.search_entry: ctk.CTkEntry | None = None
        self.search_button: ctk.CTkButton | None = None
        self.search_last_button: ctk.CTkButton | None = None
        self.search_list: ctk.CTkScrollableFrame | None = None
        self._search_cards: dict[int, InfoCard] = {}
        # Follow ("追更") button on the search page: acts on the selected cards.
        self.search_follow_button: ctk.CTkButton | None = None
        # (site, book_id) pairs whose follow-add is currently running on a
        # worker thread; re-triggering those rows is a no-op until the worker
        # reports back, so a double click cannot queue the same book twice.
        self._search_follow_pending: set[tuple[str, str]] = set()
        # Right-click context menu for the search-result cards; built lazily
        # like the task menu and refilled for every popup.
        self._search_menu: tk.Menu | None = None
        self._last_search: SearchState | None = None
        self._search_selector = CardSelector(self._repaint_search_cards)

        # Download widgets.
        self.task_list: ctk.CTkScrollableFrame | None = None
        self._task_cards: dict[str, TaskCard] = {}
        self._task_headers: dict[str, ctk.CTkLabel] = {}
        self._task_empty_label: ctk.CTkLabel | None = None
        self.retry_button: ctk.CTkButton | None = None
        self.open_file_button: ctk.CTkButton | None = None
        self.open_folder_button: ctk.CTkButton | None = None
        self.task_detail_var = tk.StringVar(
            value=t("Select a task to view its details.")
        )
        self._tasks_by_id: dict[str, DownloadTask] = {}
        self._selected_task_id: str | None = None
        self._task_menu: tk.Menu | None = None
        self._task_file_menu: tk.Menu | None = None
        self._file_menu_choices: tuple[ExportChoice, ...] | None = None
        self._last_tasks_snapshot: dict[str, list[DownloadTask]] = {}
        # Retry bookkeeping: ids currently queued for a retry and ids whose
        # retry already succeeded (the source row then stays disabled).
        self._retry_pending: set[str] = set()
        self._retried_ids: set[str] = set()
        self.task_filter_var = tk.StringVar(value=task_filter_label(FILTER_ALL))
        self.task_keyword_var = tk.StringVar()
        self.task_filter_menu: ctk.CTkOptionMenu | None = None
        self.task_keyword_entry: ctk.CTkEntry | None = None
        # Per-progress-bar easing chains (bar widget -> pending after id).
        self._progress_animations: dict[Any, str] = {}

        # Status bar.
        self._env_var = tk.StringVar(value="")

        # History widgets.
        self.history_list: ctk.CTkScrollableFrame | None = None
        self._history_cards: dict[int, InfoCard] = {}
        self.history_dir_var = tk.StringVar(value="")
        self._history_selector = CardSelector(self._repaint_history_cards)

        # Music widgets / state.
        self._music_loaded = False
        self._music_engine: dict[str, Any] | None = None
        self._music_error = ""
        self._music_ready = False
        self._music_display_to_key: dict[str, str] = {}
        self._music_providers: dict[str, Any] = {}
        self._music_provider_lock = threading.Lock()
        self._music_tracks: list[Any] = []
        self._music_track_cards: dict[int, InfoCard] = {}
        self._music_stop = threading.Event()
        self._music_selector = CardSelector(self._repaint_track_cards)
        self.music_platform_var = tk.StringVar()
        self.music_query_var = tk.StringVar()
        self.music_note_var = tk.StringVar(value="")
        self.music_summary_var = tk.StringVar(value="")
        self.music_out_dir_var: tk.StringVar | None = None
        self.music_level_var: tk.StringVar | None = None
        self.music_embed_tags_var: tk.BooleanVar | None = None
        self.music_embed_cover_var: tk.BooleanVar | None = None
        self.music_save_lyric_var: tk.BooleanVar | None = None
        self.music_split_folder_var: tk.BooleanVar | None = None
        self.music_skip_existing_var: tk.BooleanVar | None = None
        self.music_platform_menu: ctk.CTkOptionMenu | None = None
        self.music_level_menu: ctk.CTkOptionMenu | None = None
        self.music_search_button: ctk.CTkButton | None = None
        self.music_download_button: ctk.CTkButton | None = None
        self.music_stop_button: ctk.CTkButton | None = None
        self.music_list: ctk.CTkScrollableFrame | None = None
        self.music_progress: ctk.CTkProgressBar | None = None
        self.music_log: ctk.CTkTextbox | None = None
        # Platform health check ("检查平台") widgets / state.
        self.music_health_button: ctk.CTkButton | None = None
        self.music_health_list: ctk.CTkScrollableFrame | None = None
        self._music_health_cards: list[InfoCard] = []
        self.music_health_summary_var = tk.StringVar(value="")
        self._music_health_running = False
        self._music_health_results: dict[str, dict[str, Any]] = {}

        # Follow ("追更") widgets / state.  The store only resolves the file
        # path; every read/write happens on a worker thread, never on Tk.
        # The lock serializes this process's read-modify-write cycles (the
        # store itself is lock-free and atomic only at the os.replace step).
        self._watch_store = WatchlistStore()
        self._watch_lock = threading.Lock()
        self._follow_entries: list[WatchEntry] = []
        self._follow_checking = False
        self._follow_counts: dict[str, int] = {}
        # One-shot status message ("added" / "already followed") raised by a
        # search-page follow request; the next follow-list refresh shows it
        # instead of the generic entry count.
        self._follow_status_after_reload: str | None = None
        self.follow_list: ctk.CTkScrollableFrame | None = None
        self._follow_cards: dict[int, InfoCard] = {}
        self._follow_selector = CardSelector(self._repaint_follow_cards)
        self.follow_add_button: ctk.CTkButton | None = None
        self.follow_remove_button: ctk.CTkButton | None = None
        self.follow_check_button: ctk.CTkButton | None = None
        self.follow_download_button: ctk.CTkButton | None = None

        # Layout / navigation state (built in _build_ui).
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._page_index = 0
        self._nav_buttons: list[ctk.CTkButton] = []
        self._page_fade_after: dict[int, str] = {}
        self._dark_mode_var: tk.StringVar | None = None
        # Fonts are created once the root exists.
        self.font_title: ctk.CTkFont | None = None
        self.font_bold: ctk.CTkFont | None = None
        self.font_body: ctk.CTkFont | None = None
        self.font_small: ctk.CTkFont | None = None
        self.font_badge: ctk.CTkFont | None = None
        self.font_nav: ctk.CTkFont | None = None
        self.font_logo: ctk.CTkFont | None = None

        # Widgets read the theme at creation time; make sure the custom
        # palette is active even when a plain CTk root was created without
        # going through run_app() (tests, embedders).  Idempotent.
        apply_custom_theme()
        self._build_ui()
        self._register_shortcuts()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(_POLL_MS, self._poll)
        self._refresh_history(initial=True)
        self._reload_follow_async()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title(f"{t('Novel Downloader')} v{__version__}")
        self._restore_window_geometry()
        self.root.minsize(760, 480)
        self._create_fonts()

        # Status bar first: packed at the bottom it spans the whole width.
        self._build_status_bar()

        sidebar = ctk.CTkFrame(
            self.root, width=_SIDEBAR_WIDTH, corner_radius=0, fg_color=palette.SIDEBAR
        )
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)

        content = ctk.CTkFrame(self.root, fg_color="transparent", corner_radius=0)
        content.pack(side="left", fill="both", expand=True)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        builders = {
            "search": self._build_search_page,
            "download": self._build_download_page,
            "history": self._build_history_page,
            "music": self._build_music_page,
            "follow": self._build_follow_page,
        }
        for _index, key in enumerate(_PAGE_ORDER):
            page = ctk.CTkFrame(content, fg_color="transparent", corner_radius=0)
            page.grid(row=0, column=0, sticky="nsew", padx=16, pady=(12, 8))
            builders[key](page)
            self._pages[key] = page

        self._show_page(0, animate=False)

    def _create_fonts(self) -> None:
        """Create the shared fonts (requires an existing root window)."""
        self.font_title = ctk.CTkFont(family=_FONT_FAMILY, size=18, weight="bold")
        self.font_bold = ctk.CTkFont(family=_FONT_FAMILY, size=13, weight="bold")
        self.font_body = ctk.CTkFont(family=_FONT_FAMILY, size=13)
        self.font_small = ctk.CTkFont(family=_FONT_FAMILY, size=11)
        self.font_badge = ctk.CTkFont(family=_FONT_FAMILY, size=10, weight="bold")
        self.font_nav = ctk.CTkFont(family=_FONT_FAMILY, size=13)
        self.font_logo = ctk.CTkFont(family=_FONT_FAMILY, size=15, weight="bold")

    def _build_status_bar(self) -> None:
        status_bar = ctk.CTkFrame(
            self.root, corner_radius=0, fg_color=palette.SIDEBAR, height=32
        )
        status_bar.pack(side="bottom", fill="x")
        status_label = ctk.CTkLabel(
            status_bar,
            textvariable=self._status_var,
            anchor="w",
            justify="left",
            fg_color="transparent",
            text_color=palette.TEXT,
        )
        status_label.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=4)
        self._status_label = status_label
        self._env_var.set(environment_summary(DEFAULT_API_BASE, app_home()))
        ctk.CTkLabel(
            status_bar,
            textvariable=self._env_var,
            anchor="e",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(side="right", padx=(6, 12), pady=4)

    def _build_sidebar(self, sidebar: ctk.CTkFrame) -> None:
        # Logo: a rounded accent mark next to the application name.
        logo = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo.pack(fill="x", padx=16, pady=(18, 14))
        mark = ctk.CTkFrame(
            logo, width=34, height=34, corner_radius=10, fg_color=palette.ACCENT
        )
        mark.pack(side="left")
        mark.pack_propagate(False)
        ctk.CTkLabel(
            mark,
            text="N",
            font=ctk.CTkFont(family=_FONT_FAMILY, size=16, weight="bold"),
            text_color=("#FFFFFF", "#FFFFFF"),
            fg_color="transparent",
        ).pack(expand=True)
        ctk.CTkLabel(
            logo,
            text=t("Novel Downloader"),
            anchor="w",
            font=self.font_logo,
            fg_color="transparent",
        ).pack(side="left", padx=(10, 0))

        nav = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav.pack(fill="x", padx=10)
        for key in _PAGE_ORDER:
            label = t(key.capitalize())
            button = ctk.CTkButton(
                nav,
                text=f"{_PAGE_ICONS[key]}  {label}",
                anchor="w",
                height=36,
                corner_radius=10,
                fg_color="transparent",
                hover_color=palette.HOVER,
                text_color=palette.TEXT,
                font=self.font_nav,
                command=partial(self._show_page, _PAGE_ORDER.index(key)),
            )
            button.pack(fill="x", pady=2)
            self._nav_buttons.append(button)

        # Footer: theme switch, shortcuts, version.
        ctk.CTkFrame(sidebar, fg_color="transparent").pack(fill="both", expand=True)
        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.pack(fill="x", padx=14, pady=(0, 14))

        self._dark_mode_var = tk.StringVar(
            value="dark" if ctk.get_appearance_mode().lower() == "dark" else "light"
        )
        ctk.CTkSwitch(
            footer,
            text=t("Dark mode"),
            variable=self._dark_mode_var,
            onvalue="dark",
            offvalue="light",
            command=self._on_theme_toggle,
            font=self.font_small,
        ).pack(anchor="w", pady=(0, 8))

        ctk.CTkButton(
            footer,
            text=f"⌨  {t('Shortcuts')}",
            anchor="w",
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=palette.HOVER,
            text_color=palette.MUTED,
            font=self.font_small,
            command=self._show_keyboard_shortcuts,
        ).pack(fill="x", pady=(0, 2))

        ctk.CTkLabel(
            footer,
            text=f"v{__version__}",
            anchor="w",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(anchor="w")

    # ------------------------------------------------------------------
    # Page scaffolding
    # ------------------------------------------------------------------
    def _page_header(self, page: ctk.CTkFrame, title: str) -> None:
        """Render the page title at the top of a page."""
        ctk.CTkLabel(
            page,
            text=title,
            anchor="w",
            font=self.font_title,
            fg_color="transparent",
        ).pack(fill="x", pady=(0, 10))

    def _show_page(self, index: int, *_args: Any, animate: bool = True) -> None:
        """Raise page ``index`` and highlight its navigation button."""
        if not 0 <= index < len(_PAGE_ORDER):
            return
        self._page_index = index
        for position, button in enumerate(self._nav_buttons):
            self._style_nav_button(button, selected=(position == index))
        page = self._pages[_PAGE_ORDER[index]]
        page.tkraise()
        if animate:
            self._start_page_fade(page)

    def _style_nav_button(self, button: ctk.CTkButton, *, selected: bool) -> None:
        """Apply the selected (accent tint) or idle sidebar style."""
        if selected:
            button.configure(
                fg_color=palette.ACCENT_SOFT,
                hover_color=palette.ACCENT_SOFT,
                text_color=palette.ACCENT,
            )
        else:
            button.configure(
                fg_color="transparent",
                hover_color=palette.HOVER,
                text_color=palette.TEXT,
            )

    def _start_page_fade(self, page: ctk.CTkFrame) -> None:
        """Fade a page in with a brief accent-tinted transition."""
        previous = self._page_fade_after.pop(id(page), None)
        if previous is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(previous)

        mode = ctk.get_appearance_mode().lower()
        background = palette.select(palette.BG, mode)
        accent = palette.select(palette.ACCENT, mode)
        start = palette.mix(background, accent, _FADE_TINT)

        def step(remaining: int) -> None:
            try:
                if remaining <= 0:
                    page.configure(fg_color=palette.BG)
                    self._page_fade_after.pop(id(page), None)
                    return
                ratio = 1.0 - remaining / _FADE_STEPS
                page.configure(fg_color=palette.mix(start, background, ratio))
                after_id = self.root.after(_FADE_STEP_MS, partial(step, remaining - 1))
            except tk.TclError:
                self._page_fade_after.pop(id(page), None)
                return
            self._page_fade_after[id(page)] = after_id

        step(_FADE_STEPS)

    def _on_theme_toggle(self) -> None:
        """Apply the dark-mode switch position as the appearance mode."""
        assert self._dark_mode_var is not None
        ctk.set_appearance_mode(self._dark_mode_var.get())

    # ------------------------------------------------------------------
    # Search page
    # ------------------------------------------------------------------
    def _build_search_page(self, page: ctk.CTkFrame) -> None:
        self._page_header(page, t("Search"))

        form = ctk.CTkFrame(page, fg_color="transparent")
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)

        self.search_entry = ctk.CTkEntry(
            form,
            textvariable=self.search_var,
            placeholder_text=t("Enter a keyword to search."),
            height=34,
            font=self.font_body,
        )
        self.search_entry.grid(row=0, column=0, sticky="ew", pady=2)
        self.search_entry.bind("<Return>", lambda _event: self._start_search())
        self.search_button = ctk.CTkButton(
            form,
            text=t("Search"),
            width=96,
            height=34,
            fg_color=palette.ACCENT,
            hover_color=palette.ACCENT_HOVER,
            text_color=("#FFFFFF", "#FFFFFF"),
            command=self._start_search,
        )
        self.search_button.grid(row=0, column=1, padx=(8, 0), pady=2)

        sites_row = ctk.CTkFrame(form, fg_color="transparent")
        sites_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        sites_row.columnconfigure(0, weight=1)
        ctk.CTkEntry(
            sites_row,
            textvariable=self.sites_var,
            placeholder_text=t("Sites (comma separated, empty = all)"),
            height=30,
            font=self.font_small,
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            sites_row,
            text=t("e.g. qidian, b520, syosetu"),
            anchor="e",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=0, column=1, padx=(10, 0))

        self.search_list = ctk.CTkScrollableFrame(page, fg_color=palette.BG)
        self.search_list.pack(fill="both", expand=True, pady=(10, 6))

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x")
        self.search_last_button = ctk.CTkButton(
            actions,
            text=t("Restore last search"),
            state="disabled",
            command=self._restore_last_search,
        )
        self.search_last_button.pack(side="left")
        ctk.CTkButton(
            actions,
            text=t("Add to download queue"),
            command=self._add_selected_results,
        ).pack(side="left", padx=(6, 0))
        self.search_follow_button = ctk.CTkButton(
            actions,
            text=t("Follow"),
            state="disabled",
            command=self._follow_selected_results,
        )
        self.search_follow_button.pack(side="left", padx=(6, 0))

    def _make_search_card(self, index: int, result: SearchResult) -> InfoCard:
        assert self.search_list is not None
        title, author, site, word_count, updated = (
            str(result.get("title", "")),
            str(result.get("author", "")),
            str(result.get("site", "")),
            str(result.get("word_count", "")),
            str(result.get("update_date", "")),
        )
        meta_parts = [
            part
            for part in (
                f"{t('Author')}: {author}" if author else "",
                f"{t('Site')}: {site}" if site else "",
                f"{t('Word count')}: {word_count}" if word_count else "",
                f"{t('Updated')}: {updated}" if updated else "",
            )
            if part
        ]
        return InfoCard(
            self.search_list,
            self,
            title=title or f"{site}/{result.get('book_id', '')}",
            meta="   ·   ".join(meta_parts),
            buttons=(
                (t("Download"), partial(self._queue_search_result, index)),
                (t("Follow"), partial(self._follow_search_result, index)),
                (t("Open in browser"), partial(self._open_result_url, index)),
            ),
            on_click=partial(self._on_card_click, self._search_selector, index),
            on_double_click=partial(self._queue_search_result, index),
            on_right_click=partial(self._on_search_card_right_click, index),
        )

    def _render_search_results(self, payload: tuple[str, list[SearchResult]]) -> None:
        keyword, results = payload
        self._search_results = results
        self._search_selector.clear()
        for card in self._search_cards.values():
            card.destroy()
        self._search_cards = {}
        if self.search_list is not None:
            for index, result in enumerate(results):
                card = self._make_search_card(index, result)
                self._search_cards[index] = card
                card.frame.pack(fill="x", padx=2, pady=(0, 6))
        if self.search_button is not None:
            self.search_button.configure(state="normal")
        self._update_search_follow_button()
        self._set_status(
            t("Found {count} result(s) for '{keyword}'.").format(
                count=len(results), keyword=keyword
            )
        )

    def _on_card_click(
        self, selector: CardSelector, index: int, event: tk.Event[Any]
    ) -> None:
        """Shared card click: plain select, ctrl-click toggles."""
        selector.click(index, additive=bool(int(event.state) & _CONTROL_MASK))

    def _repaint_search_cards(self) -> None:
        for index, card in self._search_cards.items():
            card.set_selected(self._search_selector.is_selected(index))
        self._update_search_follow_button()

    def _on_search_card_right_click(self, index: int, event: tk.Event[Any]) -> None:
        """Pop the row context menu, acting on the selected search results."""
        if not self._search_selector.is_selected(index):
            self._search_selector.click(index)
        self._show_search_menu(event.x_root, event.y_root)

    def _show_search_menu(self, x_root: int, y_root: int) -> None:
        state: SearchMenuState = search_menu_state(self._selected_search_results())
        menu = self._search_menu
        if menu is None:
            menu = tk.Menu(self.root, tearoff=0)
            self._search_menu = menu
        self._rebuild_search_menu(menu, state)
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _rebuild_search_menu(self, menu: tk.Menu, state: SearchMenuState) -> tk.Menu:
        """(Re)fill the search context menu to match ``state``."""
        menu.delete(0, "end")
        menu.add_command(
            label=t("Add to download queue"),
            command=self._add_selected_results,
            state="normal" if state.can_add_to_queue else "disabled",
        )
        menu.add_command(
            label=t("Follow"),
            command=self._follow_selected_results,
            state="normal" if state.can_follow else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label=t("Open in browser"),
            command=self._open_selected_result_urls,
            state="normal" if state.can_open_url else "disabled",
        )
        return menu

    def _selected_search_results(self) -> list[SearchResult]:
        """Return the results behind the selected cards (empty when none)."""
        chosen: list[SearchResult] = []
        for index in self._search_selector.selection:
            try:
                chosen.append(self._search_results[index])
            except IndexError:
                continue
        return chosen

    def _queue_search_result(self, index: int) -> None:
        """Queue one search result for download (card button/double-click)."""
        try:
            result = self._search_results[index]
        except IndexError:
            return
        site = str(result.get("site", ""))
        book_id = str(result.get("book_id", ""))
        if not site or not book_id:
            self._set_status(t("Select one or more search results first."))
            return
        self._submit(
            manager.add_task(
                title=str(result.get("title", "")), site=site, book_id=book_id
            ),
            lambda task: t("Task added: {title}").format(title=task.title),
        )

    def _add_selected_results(self) -> None:
        selection = self._search_selector.selection
        if not selection:
            self._set_status(t("Select one or more search results first."))
            return

        queued = 0
        for index in selection:
            try:
                result = self._search_results[index]
            except IndexError:
                continue
            site = str(result.get("site", ""))
            book_id = str(result.get("book_id", ""))
            if not site or not book_id:
                continue
            title = str(result.get("title", ""))
            self._submit(
                manager.add_task(title=title, site=site, book_id=book_id),
                lambda task: t("Task added: {title}").format(title=task.title),
            )
            queued += 1

        if queued:
            self._set_status(t("Queued {count} task(s).").format(count=queued))

    def _open_result_url(self, index: int) -> None:
        """Open one result's book page in a browser (card button)."""
        try:
            result = self._search_results[index]
        except IndexError:
            return
        url = str(result.get("book_url", "")).strip()
        if not url:
            self._set_status(t("Select one or more search results first."))
            return
        webbrowser.open(url)

    def _open_selected_result_urls(self) -> None:
        """Open the book page of every selected search result in a browser."""
        urls: list[str] = []
        for result in self._selected_search_results():
            url = str(result.get("book_url", "")).strip()
            if url:
                urls.append(url)
        if not urls:
            self._set_status(t("Select one or more search results first."))
            return
        for url in urls:
            webbrowser.open(url)

    # ------------------------------------------------------------------
    # Follow ("追更") from the search page
    # ------------------------------------------------------------------
    def _update_search_follow_button(self) -> None:
        """Enable the follow button while a followable row is selected."""
        if self._search_follow_pending:
            # An add is still writing to the store; keep the button down.
            self._set_button_enabled(self.search_follow_button, False)
            return
        allowed = any(
            can_follow_search_result(
                result.get("site", ""),
                result.get("book_id", ""),
                result.get("title", ""),
            )
            for result in self._selected_search_results()
        )
        self._set_button_enabled(self.search_follow_button, allowed)

    def _follow_search_result(self, index: int) -> None:
        """Follow one search result (card button)."""
        try:
            result = self._search_results[index]
        except IndexError:
            return
        site = str(result.get("site", ""))
        book_id = str(result.get("book_id", ""))
        title = str(result.get("title", ""))
        if not can_follow_search_result(site, book_id, title):
            self._set_status(follow_feedback_message(FOLLOW_INVALID))
            return
        self._queue_follow_targets([(site, book_id, title)])

    def _follow_selected_results(self) -> None:
        """Add the selected search results to the shared follow list."""
        targets: list[tuple[str, str, str]] = []
        invalid = False
        for result in self._selected_search_results():
            site = str(result.get("site", ""))
            book_id = str(result.get("book_id", ""))
            title = str(result.get("title", ""))
            if not can_follow_search_result(site, book_id, title):
                invalid = True
                continue
            key = (site.strip(), book_id.strip())
            if key in self._search_follow_pending:
                continue  # an add for this row is already in flight
            targets.append((site, book_id, title))

        if not targets:
            if invalid:
                self._set_status(follow_feedback_message(FOLLOW_INVALID))
            return

        self._queue_follow_targets(targets)

    def _queue_follow_targets(self, targets: list[tuple[str, str, str]]) -> None:
        """Mark the targets pending and start the store-writing worker."""
        for site, book_id, _title in targets:
            self._search_follow_pending.add((site.strip(), book_id.strip()))
        self._set_button_enabled(self.search_follow_button, False)
        self._set_status(
            t("Adding {count} result(s) to the follow list...").format(
                count=len(targets)
            )
        )
        threading.Thread(
            target=self._search_follow_worker,
            args=(targets,),
            name="desktop-search-follow",
            daemon=True,
        ).start()

    def _search_follow_worker(self, targets: list[tuple[str, str, str]]) -> None:
        """Persist every target on the shared store; one failure stops none."""
        for site, book_id, title in targets:
            try:
                with self._watch_lock:
                    _entry, created = self._watch_store.add(
                        site, book_id, title=title or None
                    )
            except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
                self.events.put(("error", format_error(exc)))
                self.events.put(("search_follow_done", (site, book_id, title, None)))
                continue
            self.events.put(("search_follow_done", (site, book_id, title, created)))
        self.events.put(("search_follow_finished", None))

    def _on_search_follow_done(
        self, payload: tuple[str, str, str, bool | None]
    ) -> None:
        """Release the row's pending marker and remember its feedback line."""
        site, book_id, title, created = payload
        self._search_follow_pending.discard((site.strip(), book_id.strip()))
        if created is None:
            return  # the failure was already reported through the error event
        message = follow_feedback_message(
            FOLLOW_ADDED if created else FOLLOW_ALREADY,
            site=site,
            book_id=book_id,
            title=title,
        )
        # Point the user at the Follow page, which the reload below refreshes.
        self._follow_status_after_reload = f"{message} ({t('Follow')})"

    def _on_search_follow_finished(self, _payload: object) -> None:
        """Re-arm the button and refresh the follow page from the store."""
        self._update_search_follow_button()
        self._reload_follow_async()

    # ------------------------------------------------------------------
    # Download page
    # ------------------------------------------------------------------
    def _build_download_page(self, page: ctk.CTkFrame) -> None:
        self._page_header(page, t("Download"))

        filters = ctk.CTkFrame(page, fg_color="transparent")
        filters.pack(fill="x")
        ctk.CTkLabel(
            filters,
            text=t("Status"),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(side="left")
        self.task_filter_menu = ctk.CTkOptionMenu(
            filters,
            variable=self.task_filter_var,
            values=[task_filter_label(code) for code in TASK_FILTER_CODES],
            width=130,
            height=30,
            font=self.font_small,
            command=self._on_task_filter_changed,
        )
        self.task_filter_menu.pack(side="left", padx=(6, 12))
        self.task_keyword_entry = ctk.CTkEntry(
            filters,
            textvariable=self.task_keyword_var,
            placeholder_text=t("Keyword"),
            width=220,
            height=30,
            font=self.font_small,
        )
        self.task_keyword_entry.pack(side="left")
        self.task_keyword_var.trace_add("write", self._on_task_keyword_changed)
        ctk.CTkLabel(
            filters,
            text=t("Filters are applied locally."),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(side="left", padx=8)

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x", pady=(8, 4))
        ctk.CTkButton(
            actions,
            text=t("Cancel selected task"),
            command=self._cancel_selected_task,
        ).pack(side="left")
        self.retry_button = ctk.CTkButton(
            actions,
            text=t("Retry"),
            state="disabled",
            command=self._retry_selected_task,
        )
        self.retry_button.pack(side="left", padx=(6, 0))
        self.open_file_button = ctk.CTkButton(
            actions,
            text=t("Open file"),
            state="disabled",
            command=self._popup_task_file_menu,
        )
        self.open_file_button.pack(side="left", padx=(6, 0))
        # The exported-file dropdown is a plain tk.Menu popped up below the
        # button (CustomTkinter has no native context menu); it is rebuilt
        # whenever the selected task changes.
        self._task_file_menu = tk.Menu(self.open_file_button, tearoff=0)
        self.open_folder_button = ctk.CTkButton(
            actions,
            text=t("Open folder"),
            state="disabled",
            command=self._open_selected_task_folder,
        )
        self.open_folder_button.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            actions,
            text=t("Progress refreshes automatically."),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(side="left", padx=8)

        self.task_list = ctk.CTkScrollableFrame(page, fg_color=palette.BG)
        self.task_list.pack(fill="both", expand=True, pady=(2, 6))

        detail = ctk.CTkLabel(
            page,
            textvariable=self.task_detail_var,
            anchor="w",
            justify="left",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        )
        detail.pack(fill="x", pady=(2, 0))
        detail.bind("<Button-3>", self._on_task_detail_right_click)
        detail.bind(
            "<Configure>",
            lambda event: detail.configure(wraplength=max(200, event.width - 16)),
        )

    def _task_header(self, key: str) -> ctk.CTkLabel:
        """Lazily create the group header label for a task section."""
        header = self._task_headers.get(key)
        if header is None:
            assert self.task_list is not None
            header = ctk.CTkLabel(
                self.task_list,
                text="",
                anchor="w",
                font=self.font_small,
                text_color=palette.MUTED,
                fg_color="transparent",
            )
            self._task_headers[key] = header
        return header

    def _render_tasks(self, snapshot: dict[str, list[DownloadTask]]) -> None:
        assert self.task_list is not None

        self._last_tasks_snapshot = snapshot
        selected = self._selected_task_id
        status_filter = task_filter_code(self.task_filter_var.get())
        keyword = self.task_keyword_var.get()
        filter_active = status_filter != FILTER_ALL or bool(keyword.strip())

        sections = (
            ("running", t("Running"), snapshot.get("running", [])),
            ("pending", t("Queued"), snapshot.get("pending", [])),
            ("completed", t("Completed"), snapshot.get("completed", [])),
        )

        desired_keys: list[str] = []
        desired_tasks: dict[str, DownloadTask] = {}
        header_counts: dict[str, int] = {}
        section_labels: dict[str, str] = {}
        for key, label, tasks in sections:
            visible = filter_tasks(tasks, status=status_filter, keyword=keyword)
            section_labels[key] = label
            if filter_active and not visible:
                continue
            header_counts[key] = len(visible)
            desired_keys.append(f"header:{key}")
            for task in visible:
                desired_keys.append(task.task_id)
                desired_tasks[task.task_id] = task

        # Drop cards for tasks that are no longer visible.
        for task_id in list(self._task_cards):
            if task_id not in desired_tasks:
                self._task_cards.pop(task_id).destroy()

        # Repack the remaining cards (and headers) in snapshot order; packing
        # an already-placed widget moves it, so the order is corrected without
        # rebuilding anything.
        packed_headers: set[str] = set()
        for card_key in desired_keys:
            if card_key.startswith("header:"):
                key = card_key.split(":", 1)[1]
                header = self._task_header(key)
                count = header_counts.get(key, 0)
                section_label = section_labels.get(key, key)
                header.configure(text=f"{section_label} ({count})")
                header.pack(fill="x", padx=2, pady=(4, 4))
                packed_headers.add(key)
                continue
            task = desired_tasks[card_key]
            card = self._task_cards.get(card_key)
            if card is None:
                card = TaskCard(self.task_list, self, card_key)
                self._task_cards[card_key] = card
            card.update(task)
            card.frame.pack(fill="x", padx=2, pady=(0, 6))

        for key, header in self._task_headers.items():
            if key not in packed_headers:
                header.pack_forget()

        # Empty state.
        if not desired_tasks:
            message = (
                t("No tasks match the filter.")
                if filter_active
                else t("No tasks yet. Search for a book and add it to the queue.")
            )
            if self._task_empty_label is None:
                self._task_empty_label = ctk.CTkLabel(
                    self.task_list,
                    text="",
                    anchor="w",
                    justify="left",
                    font=self.font_small,
                    text_color=palette.MUTED,
                    fg_color="transparent",
                )
            self._task_empty_label.configure(text=message)
            self._task_empty_label.pack(fill="x", padx=2, pady=8)
        elif self._task_empty_label is not None:
            self._task_empty_label.pack_forget()

        self._tasks_by_id = desired_tasks
        for task_id, card in self._task_cards.items():
            card.set_selected(task_id == selected)

        self._update_task_controls()
        self._notify_task_transitions(snapshot)

    def _on_task_filter_changed(self, _value: str | None = None) -> None:
        self._render_tasks(self._last_tasks_snapshot)

    def _on_task_keyword_changed(self, *_args: str) -> None:
        self._render_tasks(self._last_tasks_snapshot)

    def _selected_task(self) -> DownloadTask | None:
        if self._selected_task_id is None:
            return None
        return self._tasks_by_id.get(self._selected_task_id)

    def _on_task_card_click(self, task_id: str, _event: tk.Event[Any]) -> None:
        self._selected_task_id = task_id
        for card_id, card in self._task_cards.items():
            card.set_selected(card_id == task_id)
        self._update_task_controls()

    def _on_task_card_double_click(self, task_id: str) -> None:
        self._selected_task_id = task_id
        for card_id, card in self._task_cards.items():
            card.set_selected(card_id == task_id)
        self._update_task_controls()
        task = self._selected_task()
        if task is not None:
            self._show_task_details(task)

    def _on_task_card_right_click(self, task_id: str, event: tk.Event[Any]) -> None:
        self._selected_task_id = task_id
        for card_id, card in self._task_cards.items():
            card.set_selected(card_id == task_id)
        self._update_task_controls()
        self._show_task_menu(event.x_root, event.y_root)

    def _on_task_detail_right_click(self, event: tk.Event[Any]) -> None:
        if self._selected_task() is not None:
            self._show_task_menu(event.x_root, event.y_root)

    def _retry_blocked(self, task: DownloadTask | None) -> bool:
        """Whether a retry was already requested for this task row."""
        if task is None:
            return False
        return task.task_id in self._retry_pending or task.task_id in self._retried_ids

    def _update_task_controls(self) -> None:
        task = self._selected_task()
        actions = task_output_actions(task)
        self._set_button_enabled(
            self.retry_button,
            retry_params(task) is not None and not self._retry_blocked(task),
        )
        self._set_button_enabled(self.open_file_button, actions.can_open_file)
        self._set_button_enabled(self.open_folder_button, actions.can_open_folder)
        self._rebuild_task_file_menu(task_export_choices(task))
        self.task_detail_var.set(task_detail_text(task))

    def _rebuild_task_file_menu(self, choices: tuple[ExportChoice, ...]) -> None:
        """List every exported file of the selected task in the open menu."""
        menu = self._task_file_menu
        if menu is None:
            menu = tk.Menu(self.root, tearoff=0)
            self._task_file_menu = menu
        if choices == self._file_menu_choices:
            # Avoid mutating an open menu on every 500 ms snapshot refresh.
            return
        self._file_menu_choices = choices
        menu.delete(0, "end")
        for choice in choices:
            menu.add_command(
                label=choice.label,
                command=partial(self._open_path, choice.path),
            )
        if not choices:
            menu.add_command(
                label=t("No exported file for the selected task."),
                state="disabled",
            )

    def _popup_task_file_menu(self) -> None:
        """Open the exported-file menu right below the Open file button."""
        button = self.open_file_button
        menu = self._task_file_menu
        if button is None or menu is None or self._selected_task() is None:
            return
        self._rebuild_task_file_menu(task_export_choices(self._selected_task()))
        try:
            menu.tk_popup(
                button.winfo_rootx(), button.winfo_rooty() + button.winfo_height()
            )
        finally:
            menu.grab_release()

    def _show_task_menu(self, x_root: int, y_root: int) -> None:
        task = self._selected_task()
        if task is None:
            return
        state: TaskMenuState = task_menu_state(
            task, retry_blocked=self._retry_blocked(task)
        )

        menu = self._task_menu
        if menu is None:
            menu = tk.Menu(self.root, tearoff=0)
            self._task_menu = menu
        menu.delete(0, "end")

        menu.add_command(
            label=t("Retry"),
            command=self._retry_selected_task,
            state="normal" if state.can_retry else "disabled",
        )
        if len(state.choices) > 1:
            submenu = tk.Menu(menu, tearoff=0)
            for choice in state.choices:
                submenu.add_command(
                    label=choice.label,
                    command=partial(self._open_path, choice.path),
                )
            menu.add_cascade(
                label=t("Open file"),
                menu=submenu,
                state="normal" if state.can_open_file else "disabled",
            )
        else:
            menu.add_command(
                label=t("Open file"),
                command=self._open_selected_task_file,
                state="normal" if state.can_open_file else "disabled",
            )
        menu.add_command(
            label=t("Open folder"),
            command=self._open_selected_task_folder,
            state="normal" if state.can_open_folder else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label=t("Copy path"),
            command=self._copy_selected_task_path,
            state="normal" if state.can_copy_path else "disabled",
        )
        menu.add_command(
            label=t("Copy error"),
            command=self._copy_selected_task_error,
            state="normal" if state.can_copy_error else "disabled",
        )
        menu.add_command(
            label=t("Show details"),
            command=self._show_selected_task_details,
            state="normal" if state.can_show_details else "disabled",
        )
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _open_selected_task_file(self) -> None:
        actions = task_output_actions(self._selected_task())
        if actions.file_path is None:
            self._set_status(t("No exported file for the selected task."))
            return
        self._open_path(actions.file_path)

    def _copy_selected_task_path(self) -> None:
        choices = task_export_choices(self._selected_task())
        if not choices:
            self._set_status(t("No exported file for the selected task."))
            return
        self._copy_text(str(choices[0].path))

    def _open_selected_task_folder(self) -> None:
        actions = task_output_actions(self._selected_task())
        if actions.folder_path is None:
            self._set_status(t("No exported folder for the selected task."))
            return
        self._open_path(actions.folder_path)

    def _copy_selected_task_error(self) -> None:
        task = self._selected_task()
        error = (task.error or "").strip() if task is not None else ""
        if not error:
            self._set_status(t("The selected task has no error message."))
            return
        self._copy_text(error)

    def _copy_text(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError as exc:
            self._show_error(
                t("Could not copy to the clipboard: {error}").format(error=exc)
            )
            return
        self._set_status(t("Copied to the clipboard."))

    def _show_selected_task_details(self) -> None:
        task = self._selected_task()
        if task is not None:
            self._show_task_details(task)

    def _show_task_details(self, task: DownloadTask) -> None:
        top = ctk.CTkToplevel(self.root)
        top.title(t("Task details"))
        top.transient(self.root)
        top.geometry("700x360")
        top.bind("<Escape>", lambda _event: top.destroy())

        text = ctk.CTkTextbox(top, wrap="word", font=self.font_body)
        text.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        text.insert("1.0", task_detail_text(task))
        text.configure(state="disabled")

        buttons = ctk.CTkFrame(top, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        detail = task_detail_text(task)
        ctk.CTkButton(
            buttons,
            text=t("Copy"),
            command=lambda: self._copy_text(detail),
        ).pack(side="left")
        ctk.CTkButton(buttons, text=t("Close"), command=top.destroy).pack(side="right")

        self._make_modal(top)

    def _animate_progress(self, bar: ctk.CTkProgressBar, target: float) -> None:
        """
        Ease a progress bar toward ``target`` over a few frames.

        The 0.5 s snapshot poll would otherwise jump between values; a short
        after-chain keeps the fill visibly smooth (the card widget is keyed by
        identity, and a destroyed bar simply ends its own chain).
        """
        pending = self._progress_animations.pop(bar, None)
        if pending is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(pending)
        if target <= 0.0 or target >= 1.0:
            with contextlib.suppress(tk.TclError):
                bar.set(target)
            return
        start = bar.get()

        def step(remaining: int) -> None:
            try:
                ratio = 1.0 - remaining / _PROGRESS_STEPS
                bar.set(start + (target - start) * ratio)
            except tk.TclError:
                self._progress_animations.pop(bar, None)
                return
            if remaining <= 0:
                self._progress_animations.pop(bar, None)
                return
            self._progress_animations[bar] = self.root.after(
                _PROGRESS_STEP_MS, partial(step, remaining - 1)
            )

        step(_PROGRESS_STEPS)

    def _cancel_selected_task(self) -> None:
        task_id = self._selected_task_id
        if task_id is None:
            self._set_status(t("Select a task to cancel."))
            return
        short_id = task_id[:8]
        self._submit(
            manager.cancel_task(task_id),
            lambda ok: (
                t("Task {task} cancelled").format(task=short_id)
                if ok
                else t("Task {task} could not be cancelled").format(task=short_id)
            ),
        )

    def _retry_selected_task(self) -> None:
        """Re-queue a failed or cancelled task with its original identifiers."""
        task = self._selected_task()
        params = retry_params(task)
        if task is None or params is None:
            self._set_status(t("Select a failed or cancelled task first."))
            return
        if self._retry_blocked(task):
            return

        source_id = task.task_id
        title = params.title or params.book_id
        self._retry_pending.add(source_id)
        self._update_task_controls()
        self._set_status(t("Retrying '{title}'...").format(title=title))

        try:
            coro = manager.add_task(
                title=params.title, site=params.site, book_id=params.book_id
            )
            future = self.engine.submit(coro)
        except Exception as exc:  # noqa: BLE001 - loop startup failure
            coro.close()
            self._retry_pending.discard(source_id)
            self._update_task_controls()
            self._show_error(format_error(exc))
            return

        def on_done(done: Future[DownloadTask]) -> None:
            try:
                done.result()
            except Exception as exc:  # noqa: BLE001 - reported in the UI thread
                self.events.put(("retry_done", (source_id, format_error(exc), title)))
            else:
                self.events.put(("retry_done", (source_id, None, title)))

        future.add_done_callback(on_done)

    def _retry_finished(self, payload: tuple[str, str | None, str]) -> None:
        source_id, error, title = payload
        self._retry_pending.discard(source_id)
        if error is None:
            # Keep the source row disabled so a second click cannot enqueue the
            # same book twice; the new task has its own row.
            self._retried_ids.add(source_id)
            self._set_status(t("Task added: {title}").format(title=title))
        else:
            self._show_error(error)
        self._update_task_controls()

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def _build_history_page(self, page: ctk.CTkFrame) -> None:
        self._page_header(page, t("History"))

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(
            actions, text=t("Refresh"), command=self._refresh_history_action
        ).pack(side="left")
        ctk.CTkButton(
            actions,
            text=t("Open containing folder"),
            command=self._open_history_folder,
        ).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            actions,
            textvariable=self.history_dir_var,
            anchor="w",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(side="left", padx=8)

        self.history_list = ctk.CTkScrollableFrame(page, fg_color=palette.BG)
        self.history_list.pack(fill="both", expand=True, pady=(4, 0))

    def _refresh_history_action(self) -> None:
        self._refresh_history()

    def _refresh_history(self, initial: bool = False) -> None:
        items: list[FileItem] = []
        error: str | None = None
        try:
            output_dir = _output_dir()
            items = _apply_sort(_scan_files(), "mtime", "desc")
            self.history_dir_var.set(
                t("Output directory: {path}").format(path=output_dir)
            )
        except Exception as exc:  # noqa: BLE001 - configuration/IO failure
            error = t("Could not scan the output directory: {error}").format(
                error=format_error(exc)
            )
            logger.exception("Failed to scan the output directory")

        self._history_items = items
        self._history_selector.clear()
        for card in self._history_cards.values():
            card.destroy()
        self._history_cards = {}
        if self.history_list is not None:
            for index, item in enumerate(items):
                name, size, modified = history_row(item)
                card = InfoCard(
                    self.history_list,
                    self,
                    title=name,
                    meta=f"{size}   ·   {t('Modified')}: {modified}",
                    buttons=((t("Open"), partial(self._open_history_file, index)),),
                    on_click=partial(
                        self._on_card_click, self._history_selector, index
                    ),
                    on_double_click=partial(self._open_history_file, index),
                )
                self._history_cards[index] = card
                card.frame.pack(fill="x", padx=2, pady=(0, 6))

        if error is not None:
            if initial:
                self._set_status(error)
            else:
                self._show_error(error)
        elif not items:
            self._set_status(t("No exported files found."))
        else:
            self._set_status(
                t("Found {count} exported file(s).").format(count=len(items))
            )

    def _repaint_history_cards(self) -> None:
        for index, card in self._history_cards.items():
            card.set_selected(self._history_selector.is_selected(index))

    def _selected_history_index(self) -> int | None:
        selection = self._history_selector.selection
        return selection[0] if selection else None

    def _open_history_folder(self) -> None:
        index = self._selected_history_index()
        if index is None:
            self._set_status(t("Select a file first."))
            return
        self._open_path(self._history_items[index].path.parent)

    def _open_history_file(self, index: int) -> None:
        try:
            item = self._history_items[index]
        except IndexError:
            return
        self._open_path(item.path)

    def _open_path(self, path: Path) -> None:
        try:
            startfile = getattr(os, "startfile", None)
            if callable(startfile):
                startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            self._show_error(
                t("Could not open {path}: {error}").format(path=path, error=exc)
            )

    # ------------------------------------------------------------------
    # Music (optional engine)
    # ------------------------------------------------------------------
    def _build_music_page(self, page: ctk.CTkFrame) -> None:
        self._page_header(page, t("Music"))
        engine = self._load_music_engine()
        if engine is None:
            ctk.CTkLabel(
                page,
                text=(
                    t(
                        "Install the music extras to enable this tab: "
                        'pip install "novel-downloader[music]"'
                    )
                ),
                anchor="w",
                justify="left",
                wraplength=760,
                fg_color="transparent",
            ).pack(anchor="w", pady=(8, 4))
            if self._music_error:
                ctk.CTkLabel(
                    page,
                    text=t("Import error: {error}").format(error=self._music_error),
                    anchor="w",
                    justify="left",
                    wraplength=760,
                    text_color=palette.STATUS_FAIL_TEXT,
                    fg_color="transparent",
                ).pack(anchor="w")
            return

        try:
            labels: list[tuple[str, str]] = list(engine["available_labels"]())
        except Exception as exc:  # noqa: BLE001 - optional engine registry
            self._music_error = format_error(exc)
            ctk.CTkLabel(
                page,
                text=t("Could not load music platforms: {error}").format(
                    error=self._music_error
                ),
                anchor="w",
                justify="left",
                wraplength=760,
                text_color=palette.STATUS_FAIL_TEXT,
                fg_color="transparent",
            ).pack(anchor="w", pady=8)
            return

        if not labels:
            ctk.CTkLabel(
                page,
                text=t("No music platforms are available."),
                fg_color="transparent",
            ).pack(anchor="w", pady=8)
            return

        self._music_ready = True
        display_to_key = {display: key for key, display in labels}
        self._music_display_to_key = display_to_key
        default_key = (
            "netease" if any(key == "netease" for key, _ in labels) else labels[0][0]
        )
        default_display = next(display for key, display in labels if key == default_key)
        self.music_platform_var.set(default_display)

        form = ctk.CTkFrame(page, fg_color="transparent")
        form.pack(fill="x")
        form.columnconfigure(4, weight=1)

        ctk.CTkLabel(
            form,
            text=t("Platform"),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        self.music_platform_menu = ctk.CTkOptionMenu(
            form,
            variable=self.music_platform_var,
            values=[display for _, display in labels],
            width=200,
            height=30,
            font=self.font_small,
            command=self._refresh_music_note,
        )
        self.music_platform_menu.grid(row=0, column=1, sticky="w", pady=2)

        ctk.CTkLabel(
            form,
            text=t("Keyword"),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=0, column=2, sticky="w", padx=(16, 6), pady=2)
        music_entry = ctk.CTkEntry(
            form, textvariable=self.music_query_var, height=30, font=self.font_body
        )
        music_entry.grid(row=0, column=3, columnspan=2, sticky="ew", pady=2)
        music_entry.bind("<Return>", lambda _event: self._start_music_search())
        self.music_search_button = ctk.CTkButton(
            form,
            text=t("Search"),
            width=90,
            height=30,
            fg_color=palette.ACCENT,
            hover_color=palette.ACCENT_HOVER,
            text_color=("#FFFFFF", "#FFFFFF"),
            command=self._start_music_search,
        )
        self.music_search_button.grid(row=0, column=5, padx=(8, 0), pady=2)

        # The selected platform's note (credentials / network requirements)
        # sits right below the dropdown so it is seen before searching.
        ctk.CTkLabel(
            form,
            textvariable=self.music_note_var,
            anchor="w",
            justify="left",
            wraplength=520,
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 2))

        # Download settings, persisted through ``apps.music.settings``.
        default_dir = str(engine["default_download_dir"]())
        current = music_settings_from_mapping(engine["load_settings"](), default_dir)
        self.music_out_dir_var = tk.StringVar(value=current.out_dir)
        self.music_level_var = tk.StringVar(value=level_display(current.level))
        self.music_embed_tags_var = tk.BooleanVar(value=current.embed_tags)
        self.music_embed_cover_var = tk.BooleanVar(value=current.embed_cover)
        self.music_save_lyric_var = tk.BooleanVar(value=current.save_lyric)
        self.music_split_folder_var = tk.BooleanVar(value=current.split_folder)
        self.music_skip_existing_var = tk.BooleanVar(value=current.skip_existing)

        settings_frame = ctk.CTkFrame(
            page,
            corner_radius=10,
            fg_color=palette.CARD,
            border_width=1,
            border_color=palette.BORDER,
        )
        settings_frame.pack(fill="x", pady=(8, 4))
        settings_frame.columnconfigure(1, weight=1)
        ctk.CTkLabel(
            settings_frame,
            text=t("Download settings"),
            anchor="w",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(8, 2))

        ctk.CTkLabel(
            settings_frame,
            text=t("Output folder"),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=1, column=0, sticky="w", padx=(12, 6), pady=2)
        out_entry = ctk.CTkEntry(
            settings_frame,
            textvariable=self.music_out_dir_var,
            height=30,
            font=self.font_small,
        )
        out_entry.grid(row=1, column=1, sticky="ew", pady=2)
        out_entry.bind("<FocusOut>", lambda _event: self._save_music_settings())
        out_entry.bind("<Return>", lambda _event: self._save_music_settings())
        ctk.CTkButton(
            settings_frame,
            text=t("Browse..."),
            width=90,
            height=30,
            command=self._browse_music_dir,
        ).grid(row=1, column=2, padx=(8, 12), pady=2)

        ctk.CTkLabel(
            settings_frame,
            text=t("Audio quality"),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=2, column=0, sticky="w", padx=(12, 6), pady=2)
        self.music_level_menu = ctk.CTkOptionMenu(
            settings_frame,
            variable=self.music_level_var,
            values=[level_display(level) for level in MUSIC_LEVELS],
            width=170,
            height=30,
            font=self.font_small,
            command=self._on_music_level_selected,
        )
        self.music_level_menu.grid(row=2, column=1, sticky="w", pady=2)

        checks = ctk.CTkFrame(settings_frame, fg_color="transparent")
        checks.grid(row=3, column=0, columnspan=3, sticky="w", padx=12, pady=(4, 10))
        check_options = (
            (t("Save lyrics"), self.music_save_lyric_var),
            (t("Save cover"), self.music_embed_cover_var),
            (t("Embed tags"), self.music_embed_tags_var),
            (t("Split into folders"), self.music_split_folder_var),
            (t("Skip existing"), self.music_skip_existing_var),
        )
        for label, variable in check_options:
            ctk.CTkCheckBox(
                checks,
                text=label,
                variable=variable,
                command=self._save_music_settings,
                font=self.font_small,
            ).pack(side="left", padx=(0, 12))

        self.music_list = ctk.CTkScrollableFrame(page, fg_color=palette.BG)
        self.music_list.pack(fill="both", expand=True, pady=(4, 4))

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x")
        self.music_download_button = ctk.CTkButton(
            actions,
            text=t("Download selected"),
            fg_color=palette.ACCENT,
            hover_color=palette.ACCENT_HOVER,
            text_color=("#FFFFFF", "#FFFFFF"),
            command=self._start_music_download,
        )
        self.music_download_button.pack(side="left")
        self.music_stop_button = ctk.CTkButton(
            actions,
            text=t("Stop"),
            state="disabled",
            command=self._stop_music_download,
        )
        self.music_stop_button.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            actions,
            textvariable=self.music_summary_var,
            anchor="w",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(side="left", padx=8)

        self.music_progress = ctk.CTkProgressBar(page, height=8)
        self.music_progress.set(0)
        self.music_progress.pack(fill="x", pady=(6, 4))

        self.music_log = ctk.CTkTextbox(page, height=110, font=self.font_small)
        self.music_log.pack(fill="x", pady=(0, 4))
        self.music_log.configure(state="disabled")

        # Platform health check ("检查平台"): one bounded search per platform,
        # run serially on a worker thread, results listed as they arrive.
        health_frame = ctk.CTkFrame(
            page,
            corner_radius=10,
            fg_color=palette.CARD,
            border_width=1,
            border_color=palette.BORDER,
        )
        health_frame.pack(fill="x", pady=(4, 0))

        ctk.CTkLabel(
            health_frame,
            text=t("Platform health"),
            anchor="w",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(fill="x", padx=12, pady=(8, 0))
        health_actions = ctk.CTkFrame(health_frame, fg_color="transparent")
        health_actions.pack(fill="x", padx=12)
        self.music_health_button = ctk.CTkButton(
            health_actions,
            text=t("Check platforms"),
            command=self._start_music_health_check,
        )
        self.music_health_button.pack(side="left")
        ctk.CTkLabel(
            health_actions,
            textvariable=self.music_health_summary_var,
            anchor="w",
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(side="left", padx=8)

        self.music_health_list = ctk.CTkScrollableFrame(
            health_frame, height=120, fg_color=palette.CARD
        )
        self.music_health_list.pack(fill="x", padx=8, pady=(4, 8))

        self._refresh_music_note()

    def _load_music_engine(self) -> dict[str, Any] | None:
        if self._music_loaded:
            return self._music_engine
        self._music_loaded = True

        try:
            # Probe the optional extras up-front so the page can explain what
            # is missing instead of failing in the middle of a download.
            import mutagen  # noqa: F401
            import qrcode  # noqa: F401
            import websocket  # noqa: F401

            from novel_downloader.apps.music import paths as music_paths
            from novel_downloader.apps.music import settings as music_settings
            from novel_downloader.apps.music.downloader import (
                DownloadOptions,
                download_many,
                human,
            )
            from novel_downloader.apps.music.providers import (
                available_labels,
                create,
                health_check,
                platform_note,
            )

            self._music_engine = {
                "available_labels": available_labels,
                "create": create,
                "health_check": health_check,
                "platform_note": platform_note,
                "DownloadOptions": DownloadOptions,
                "download_many": download_many,
                "human": human,
                "load_settings": music_settings.load_settings,
                "save_settings": music_settings.save_settings,
                "default_download_dir": music_paths.default_download_dir,
            }
        except Exception as exc:  # noqa: BLE001 - optional dependency guard
            self._music_error = format_error(exc)
            self._music_engine = None

        return self._music_engine

    def _music_selected_key(self) -> str | None:
        display = self.music_platform_var.get()
        return self._music_display_to_key.get(display)

    def _on_music_level_selected(self, _value: str | None = None) -> None:
        self._save_music_settings()

    def _browse_music_dir(self) -> None:
        engine = self._music_engine
        var = self.music_out_dir_var
        if engine is None or var is None:
            return
        initial = resolve_output_dir(var.get(), engine["default_download_dir"]())
        chosen = filedialog.askdirectory(
            parent=self.root,
            title=t("Choose the music output folder"),
            initialdir=initial,
            mustexist=False,
        )
        if chosen:
            var.set(chosen)
            self._save_music_settings()

    def _current_music_settings(self) -> MusicSettings | None:
        engine = self._music_engine
        if engine is None:
            return None
        default_dir = str(engine["default_download_dir"]())
        return music_settings_from_mapping(
            {
                "out_dir": self.music_out_dir_var.get()
                if self.music_out_dir_var is not None
                else "",
                "level": level_code(self.music_level_var.get())
                if self.music_level_var is not None
                else "",
                "embed_tags": self.music_embed_tags_var.get()
                if self.music_embed_tags_var is not None
                else True,
                "embed_cover": self.music_embed_cover_var.get()
                if self.music_embed_cover_var is not None
                else True,
                "save_lyric": self.music_save_lyric_var.get()
                if self.music_save_lyric_var is not None
                else True,
                "split_folder": self.music_split_folder_var.get()
                if self.music_split_folder_var is not None
                else True,
                "skip_existing": self.music_skip_existing_var.get()
                if self.music_skip_existing_var is not None
                else True,
            },
            default_dir,
        )

    def _save_music_settings(self, settings: MusicSettings | None = None) -> None:
        engine = self._music_engine
        current = settings or self._current_music_settings()
        if engine is None or current is None:
            return
        try:
            engine["save_settings"](music_settings_to_mapping(current))
        except Exception as exc:  # noqa: BLE001 - settings IO is best effort
            logger.exception("Failed to save the music settings")
            self._set_status(
                t("Could not save music settings: {error}").format(
                    error=format_error(exc)
                )
            )

    def _refresh_music_note(self, _value: str | None = None) -> None:
        engine = self._music_engine
        key = self._music_selected_key()
        if engine is None or not key:
            self.music_note_var.set("")
            return
        try:
            note = str(engine["platform_note"](key) or "")
        except Exception:  # noqa: BLE001 - notes are advisory only
            note = ""
        self.music_note_var.set(note)

    # ------------------------------------------------------------------
    # Music platform health check ("检查平台")
    # ------------------------------------------------------------------
    def _start_music_health_check(self) -> None:
        """Check every registered platform once, serially, off the UI thread."""
        engine = self._music_engine
        if not self._music_ready or engine is None:
            return
        if self._music_health_running:
            return
        try:
            platforms = [key for key, _ in engine["available_labels"]()]
        except Exception as exc:  # noqa: BLE001 - registry failure is not fatal
            self._show_error(
                t("Could not load music platforms: {error}").format(
                    error=format_error(exc)
                )
            )
            return
        if not platforms:
            self._set_status(t("No music platforms are available."))
            return

        keyword = self.music_query_var.get().strip() or "test"
        self._music_health_running = True
        self._music_health_results = {}
        for card in self._music_health_cards:
            card.destroy()
        self._music_health_cards = []
        if self.music_health_button is not None:
            self.music_health_button.configure(state="disabled")
        self._set_status(
            t("Checking {count} platform(s)...").format(count=len(platforms))
        )
        threading.Thread(
            target=self._music_health_worker,
            args=(platforms, keyword),
            name="desktop-music-health",
            daemon=True,
        ).start()

    def _music_health_worker(self, platforms: list[str], keyword: str) -> None:
        """Run one bounded ``health_check`` per platform, then report done."""
        engine = self._music_engine
        if engine is None:
            return
        check = engine["health_check"]
        for platform in platforms:
            try:
                result = dict(check(platform, keyword, timeout=_MUSIC_HEALTH_TIMEOUT))
            except Exception as exc:  # noqa: BLE001 - per-platform, not fatal
                result = {"ok": False, "elapsed_ms": 0, "error": format_error(exc)}
            self.events.put(("music_health_item", (platform, result)))
        self.events.put(("music_health_done", None))

    def _on_music_health_item(self, payload: tuple[str, dict[str, Any]]) -> None:
        """Record one platform's result and re-render the sorted table."""
        platform, result = payload
        self._music_health_results[platform] = dict(result)
        self._render_music_health_rows()

    def _render_music_health_rows(self) -> None:
        if self.music_health_list is None:
            return
        for card in self._music_health_cards:
            card.destroy()
        self._music_health_cards = []
        for row in health_rows(self._music_health_results):
            platform, ok_label, elapsed, error = row
            meta = f"{ok_label}   ·   {elapsed} {t('Elapsed (ms)')}"
            if error:
                meta = f"{meta}   ·   {error}"
            card = InfoCard(
                self.music_health_list,
                self,
                title=platform,
                meta=meta,
            )
            self._music_health_cards.append(card)
            card.frame.pack(fill="x", padx=2, pady=(0, 4))

    def _music_health_finished(self, _payload: object) -> None:
        self._music_health_running = False
        self._render_music_health_rows()
        summary = health_summary(self._music_health_results)
        self.music_health_summary_var.set(summary)
        self._set_status(summary)
        if self.music_health_button is not None:
            self.music_health_button.configure(state="normal")

    def _music_provider(self, key: str) -> Any:
        with self._music_provider_lock:
            provider = self._music_providers.get(key)
            if provider is not None:
                return provider
            engine = self._music_engine
            if engine is None:
                raise RuntimeError("The music engine is not available")
            provider = engine["create"](key)
            self._music_providers[key] = provider
            return provider

    def _start_music_search(self) -> None:
        if not self._music_ready:
            return
        keyword = self.music_query_var.get().strip()
        key = self._music_selected_key()
        if not keyword:
            self._set_status(t("Enter a music keyword first."))
            return
        if not key:
            self._set_status(t("Select a music platform first."))
            return

        if self.music_search_button is not None:
            self.music_search_button.configure(state="disabled")
        self._set_status(
            t("Searching music for '{keyword}'...").format(keyword=keyword)
        )
        threading.Thread(
            target=self._music_search_worker,
            args=(key, keyword),
            name="desktop-music-search",
            daemon=True,
        ).start()

    def _music_search_worker(self, key: str, keyword: str) -> None:
        try:
            tracks, total = self._music_provider(key).search(
                keyword, limit=_MUSIC_SEARCH_LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
            self.events.put(("music_search_error", format_error(exc)))
            return
        count = int(total) if total else len(tracks)
        self.events.put(("music_results", (list(tracks), count)))

    def _music_search_failed(self, message: str) -> None:
        if self.music_search_button is not None:
            self.music_search_button.configure(state="normal")
        self._show_error(t("Music search failed: {error}").format(error=message))

    def _render_music_results(self, payload: tuple[list[Any], int]) -> None:
        tracks, total = payload
        self._music_tracks = tracks
        self._music_selector.clear()
        for card in self._music_track_cards.values():
            card.destroy()
        self._music_track_cards = {}
        if self.music_list is not None:
            for index, track in enumerate(tracks):
                name, artist, album, duration = music_row(track)
                meta_parts = [part for part in (artist, album) if part]
                meta = "   ·   ".join(meta_parts)
                if duration != "-":
                    meta = f"{meta}   ·   {duration}" if meta else duration
                card = InfoCard(
                    self.music_list,
                    self,
                    title=name or "-",
                    meta=meta,
                    on_click=partial(self._on_card_click, self._music_selector, index),
                )
                self._music_track_cards[index] = card
                card.frame.pack(fill="x", padx=2, pady=(0, 6))
        if self.music_search_button is not None:
            self.music_search_button.configure(state="normal")
        self._set_status(t("Found {count} track(s).").format(count=total))

    def _repaint_track_cards(self) -> None:
        for index, card in self._music_track_cards.items():
            card.set_selected(self._music_selector.is_selected(index))

    def _start_music_download(self) -> None:
        engine = self._music_engine
        if not self._music_ready or engine is None:
            return

        selection = self._music_selector.selection
        if not selection:
            self._set_status(t("Select one or more tracks first."))
            return
        key = self._music_selected_key()
        if not key:
            self._set_status(t("Select a music platform first."))
            return

        tracks: list[Any] = []
        for index in selection:
            try:
                tracks.append(self._music_tracks[index])
            except IndexError:
                continue
        if not tracks:
            return

        settings = self._current_music_settings()
        if settings is None:
            return
        self._save_music_settings(settings)

        try:
            options = engine["DownloadOptions"](
                out_dir=settings.out_dir,
                level=settings.level,
                embed_tags=settings.embed_tags,
                embed_cover=settings.embed_cover,
                save_lyric=settings.save_lyric,
                split_folder=settings.split_folder,
                skip_existing=settings.skip_existing,
            )
        except Exception as exc:  # noqa: BLE001 - settings/engine failure
            self._show_error(
                t("Could not prepare the music download: {error}").format(
                    error=format_error(exc)
                )
            )
            return
        out_dir = settings.out_dir

        self._music_stop.clear()
        if self.music_progress is not None:
            self.music_progress.set(0)
        self.music_summary_var.set(
            t("Downloading {count} track(s) to {dir}...").format(
                count=len(tracks), dir=out_dir
            )
        )
        self._set_status(t("Downloading music..."))
        if self.music_download_button is not None:
            self.music_download_button.configure(state="disabled")
        if self.music_search_button is not None:
            self.music_search_button.configure(state="disabled")
        if self.music_stop_button is not None:
            self.music_stop_button.configure(state="normal")
        self._clear_music_log()

        threading.Thread(
            target=self._music_download_worker,
            args=(key, tracks, options),
            name="desktop-music-download",
            daemon=True,
        ).start()

    def _music_download_worker(self, key: str, tracks: list[Any], options: Any) -> None:
        engine = self._music_engine
        if engine is None:
            return
        total = len(tracks)

        def on_tick(fraction: float, done: int) -> None:
            self.events.put(("music_progress", (float(fraction), int(done), total)))

        def on_result(_index: int, track: Any, result: Any) -> None:
            self.events.put(("music_result", (track, result)))

        try:
            provider = self._music_provider(key)
            ok, skip, fail, failed_pairs, _failed_tracks = engine["download_many"](
                provider,
                tracks,
                options,
                workers=_MUSIC_WORKERS,
                on_result=on_result,
                on_tick=on_tick,
                should_stop=self._music_stop.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
            self.events.put(("music_download_error", format_error(exc)))
            return

        self.events.put(
            ("music_download_done", (int(ok), int(skip), int(fail), list(failed_pairs)))
        )

    def _stop_music_download(self) -> None:
        self._music_stop.set()
        self._set_status(t("Stopping music download..."))

    def _update_music_progress(self, payload: tuple[float, int, int]) -> None:
        fraction, done, total = payload
        if self.music_progress is not None:
            value = max(0.0, min(1.0, fraction))
            self._animate_progress(self.music_progress, value)
        self.music_summary_var.set(
            t("{done}/{total} track(s)").format(done=done, total=total)
        )

    def _append_music_result(self, payload: tuple[Any, Any]) -> None:
        track, result = payload
        status = str(getattr(result, "status", "fail"))
        engine = self._music_engine
        if status == "ok":
            size = 0
            if engine is not None:
                size = int(getattr(result, "bytes_", 0) or 0)
                detail = str(engine["human"](size))
            else:
                detail = ""
            label = t("OK")
        elif status == "skip":
            label = t("Skipped")
            detail = str(getattr(result, "reason", "") or "")
        else:
            label = t("Failed")
            detail = str(getattr(result, "reason", "") or "")
        name = f"{getattr(track, 'name', '')} - {getattr(track, 'artists', '')}"
        self._music_log_write(f"{name}: {label} {detail}\n")

    def _music_download_finished(
        self, payload: tuple[int, int, int, list[tuple[str, str]]]
    ) -> None:
        ok, skip, fail, failed_pairs = payload
        if self.music_progress is not None:
            self.music_progress.set(1.0)
        self.music_summary_var.set(
            t("Done: {ok} succeeded, {skip} skipped, {fail} failed").format(
                ok=ok, skip=skip, fail=fail
            )
        )
        self._set_status(
            t(
                "Music download finished: {ok} succeeded, {skip} skipped, {fail} failed"
            ).format(ok=ok, skip=skip, fail=fail)
        )
        for name, reason in failed_pairs:
            self._music_log_write(f"{name}: {t('Failed')} {reason}\n")
        self._finish_music_controls()

    def _music_download_failed(self, message: str) -> None:
        self.music_summary_var.set(t("Download stopped: {error}").format(error=message))
        self._finish_music_controls()
        self._show_error(t("Download failed: {error}").format(error=message))

    def _finish_music_controls(self) -> None:
        if self.music_download_button is not None:
            self.music_download_button.configure(state="normal")
        if self.music_search_button is not None:
            self.music_search_button.configure(state="normal")
        if self.music_stop_button is not None:
            self.music_stop_button.configure(state="disabled")

    def _clear_music_log(self) -> None:
        log = self.music_log
        if log is None:
            return
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")

    def _music_log_write(self, text: str) -> None:
        log = self.music_log
        if log is None:
            return
        log.configure(state="normal")
        log.insert("end", text)
        log.see("end")
        log.configure(state="disabled")

    # ------------------------------------------------------------------
    # Follow page ("追更")
    # ------------------------------------------------------------------
    def _build_follow_page(self, page: ctk.CTkFrame) -> None:
        self._page_header(page, t("Follow"))

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x", pady=(0, 4))
        self.follow_add_button = ctk.CTkButton(
            actions,
            text=t("Add"),
            fg_color=palette.ACCENT,
            hover_color=palette.ACCENT_HOVER,
            text_color=("#FFFFFF", "#FFFFFF"),
            command=self._open_follow_add_dialog,
        )
        self.follow_add_button.pack(side="left")
        self.follow_remove_button = ctk.CTkButton(
            actions, text=t("Remove"), command=self._remove_follow_selected
        )
        self.follow_remove_button.pack(side="left", padx=(6, 0))
        self.follow_check_button = ctk.CTkButton(
            actions, text=t("Check updates"), command=self._start_follow_check
        )
        self.follow_check_button.pack(side="left", padx=(6, 0))
        self.follow_download_button = ctk.CTkButton(
            actions,
            text=t("Download selected"),
            command=self._download_follow_selection,
        )
        self.follow_download_button.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            actions,
            text=t("Nothing selected downloads every followed book."),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).pack(side="left", padx=8)

        self.follow_list = ctk.CTkScrollableFrame(page, fg_color=palette.BG)
        self.follow_list.pack(fill="both", expand=True, pady=(4, 0))

    def _reload_follow_async(self) -> None:
        """Read the watchlist file on a worker thread and post the result."""
        threading.Thread(
            target=self._follow_load_worker,
            name="desktop-follow-load",
            daemon=True,
        ).start()

    def _follow_load_worker(self) -> None:
        try:
            entries = self._watch_store.entries()
        except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
            logger.exception("Failed to read the watchlist")
            self.events.put(("follow_load_error", format_error(exc)))
            return
        self.events.put(("follow_entries", list(entries)))

    def _make_follow_card(self, index: int, entry: WatchEntry) -> InfoCard:
        assert self.follow_list is not None
        site, book_id, title, last_chapter, updated = follow_row(entry)
        meta_parts = [
            part
            for part in (
                f"{t('Site')}: {site}" if site else "",
                f"{t('Book ID')}: {book_id}" if book_id else "",
                f"{t('Updated')}: {updated}" if updated else "",
            )
            if part
        ]
        meta = "   ·   ".join(meta_parts)
        if last_chapter:
            meta = f"{t('Latest chapter')}: {last_chapter}" + (
                f"\n{meta}" if meta else ""
            )
        return InfoCard(
            self.follow_list,
            self,
            title=title or f"{site}/{book_id}",
            meta=meta,
            on_click=partial(self._on_card_click, self._follow_selector, index),
        )

    def _render_follow_entries(self, entries: list[WatchEntry]) -> None:
        self._follow_entries = list(entries)
        self._follow_selector.clear()
        for card in self._follow_cards.values():
            card.destroy()
        self._follow_cards = {}
        if self.follow_list is not None:
            for index, entry in enumerate(self._follow_entries):
                card = self._make_follow_card(index, entry)
                self._follow_cards[index] = card
                card.frame.pack(fill="x", padx=2, pady=(0, 6))
        if self._follow_entries:
            self._set_status(
                t("Found {count} followed book(s).").format(
                    count=len(self._follow_entries)
                )
            )
        else:
            self._set_status(t("No followed books."))
        # A follow request raised on the search page owns the status line: its
        # outcome is more specific than the generic count rendered above.
        message = self._follow_status_after_reload
        if message is not None:
            self._follow_status_after_reload = None
            self._set_status(message)

    def _repaint_follow_cards(self) -> None:
        for index, card in self._follow_cards.items():
            card.set_selected(self._follow_selector.is_selected(index))

    def _follow_selected(self) -> list[WatchEntry]:
        """Return the entries behind the selected cards (empty when none)."""
        chosen: list[WatchEntry] = []
        for index in self._follow_selector.selection:
            try:
                chosen.append(self._follow_entries[index])
            except IndexError:
                continue
        return chosen

    def _set_follow_buttons(self, enabled: bool) -> None:
        for button in (
            self.follow_add_button,
            self.follow_remove_button,
            self.follow_check_button,
            self.follow_download_button,
        ):
            self._set_button_enabled(button, enabled)

    def _open_follow_add_dialog(self) -> None:
        """Show the modal add form and queue the entry on confirmation."""
        top = ctk.CTkToplevel(self.root)
        top.title(t("Add a book to the follow list."))
        top.transient(self.root)
        top.resizable(False, False)
        top.bind("<Escape>", lambda _event: top.destroy())

        form = ctk.CTkFrame(top, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        form.columnconfigure(1, weight=1)

        site_var = tk.StringVar()
        book_var = tk.StringVar()
        title_var = tk.StringVar()

        ctk.CTkLabel(
            form,
            text=t("Site"),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        site_entry = ctk.CTkEntry(
            form, textvariable=site_var, width=260, font=self.font_body
        )
        site_entry.grid(row=0, column=1, sticky="ew", pady=4)

        ctk.CTkLabel(
            form,
            text=t("Book ID"),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ctk.CTkEntry(form, textvariable=book_var, width=260, font=self.font_body).grid(
            row=1, column=1, sticky="ew", pady=4
        )

        ctk.CTkLabel(
            form,
            text=t("Title"),
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ctk.CTkEntry(form, textvariable=title_var, width=260, font=self.font_body).grid(
            row=2, column=1, sticky="ew", pady=4
        )

        ctk.CTkLabel(
            form,
            text=t("Display title used when checking for new chapters."),
            anchor="w",
            justify="left",
            wraplength=280,
            font=self.font_small,
            text_color=palette.MUTED,
            fg_color="transparent",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 6))

        confirmed: list[tuple[str, str, str]] = []

        def confirm() -> None:
            site = site_var.get().strip()
            book_id = book_var.get().strip()
            title = title_var.get().strip()
            if not site or not book_id:
                self._set_status(t("Site and Book ID are required."))
                return
            if not title and not messagebox.askokcancel(
                t("Confirm"),
                t(
                    "Without a title the update check cannot find this book "
                    "by name. Add it anyway?"
                ),
                parent=top,
            ):
                return
            confirmed.append((site, book_id, title))
            top.destroy()

        buttons = ctk.CTkFrame(top, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkButton(
            buttons,
            text=t("Add"),
            fg_color=palette.ACCENT,
            hover_color=palette.ACCENT_HOVER,
            text_color=("#FFFFFF", "#FFFFFF"),
            command=confirm,
        ).pack(side="right")
        ctk.CTkButton(buttons, text=t("Cancel"), command=top.destroy).pack(
            side="right", padx=(0, 8)
        )

        self._make_modal(top, focus=site_entry)
        top.wait_window()

        if confirmed:
            self._follow_add(*confirmed[0])

    @staticmethod
    def _make_modal(top: ctk.CTkToplevel, focus: Any | None = None) -> None:
        """
        Make a freshly built ``CTkToplevel`` modal.

        ``grab_set`` needs the window to be viewable; on Windows the CTk
        titlebar setup re-maps the window shortly after creation, so the grab
        is (re-)applied with a small delay instead of failing silently.
        """

        def grab() -> None:
            with contextlib.suppress(tk.TclError):
                top.grab_set()
            if focus is not None:
                with contextlib.suppress(tk.TclError):
                    focus.focus_set()

        top.after(_MODAL_GRAB_DELAY_MS, grab)

    def _follow_add(self, site: str, book_id: str, title: str) -> None:
        threading.Thread(
            target=self._follow_add_worker,
            args=(site, book_id, title),
            name="desktop-follow-add",
            daemon=True,
        ).start()

    def _follow_add_worker(self, site: str, book_id: str, title: str) -> None:
        try:
            with self._watch_lock:
                self._watch_store.add(site, book_id, title=title or None)
        except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
            self.events.put(("error", format_error(exc)))
            return
        self.events.put(
            (
                "status",
                t("Added to the follow list: {site}/{book_id}").format(
                    site=site, book_id=book_id
                ),
            )
        )
        self._reload_follow_async()

    def _remove_follow_selected(self) -> None:
        targets = self._follow_selected()
        if not targets:
            self._set_status(t("Select one or more followed books first."))
            return
        threading.Thread(
            target=self._follow_remove_worker,
            args=(targets,),
            name="desktop-follow-remove",
            daemon=True,
        ).start()

    def _follow_remove_worker(self, targets: list[WatchEntry]) -> None:
        removed = 0
        for entry in targets:
            try:
                with self._watch_lock:
                    if not self._watch_store.remove(entry.site, entry.book_id):
                        continue
            except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
                self.events.put(("error", format_error(exc)))
                continue
            removed += 1
            self.events.put(
                (
                    "status",
                    t("Removed from the follow list: {site}/{book_id}").format(
                        site=entry.site, book_id=entry.book_id
                    ),
                )
            )
        if removed:
            self._reload_follow_async()

    def _start_follow_check(self) -> None:
        if self._follow_checking:
            return
        entries = list(self._follow_entries)
        if not entries:
            self._set_status(t("No followed books to check."))
            return
        self._follow_checking = True
        self._follow_counts = {}
        self._set_follow_buttons(enabled=False)
        self._set_status(
            t("Checking {count} followed book(s)...").format(count=len(entries))
        )
        threading.Thread(
            target=self._follow_check_worker,
            name="desktop-follow-check",
            daemon=True,
        ).start()

    def _follow_check_worker(self) -> None:
        """
        Check the followed books with bounded concurrency.

        The shared :func:`check_all` sweep searches several books at once,
        persists every new chapter through the thread-safe store and reports
        each result as it completes; one failing book never stops the rest.
        """
        try:
            asyncio.run(
                check_all(
                    store=self._watch_store,
                    timeout=DEFAULT_CHECK_TIMEOUT,
                    progress_cb=self._follow_report_result,
                )
            )
        except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
            logger.exception("Failed to run the follow check")
            self.events.put(("error", format_error(exc)))
        finally:
            try:
                refreshed = self._watch_store.entries()
            except Exception:  # noqa: BLE001 - refresh is best effort
                logger.exception("Failed to reload the watchlist after the check")
            else:
                self.events.put(("follow_entries", list(refreshed)))
            self.events.put(("follow_check_done", None))

    def _follow_report_result(self, result: CheckResult) -> None:
        """Post one finished check to the UI thread (in completion order)."""
        entry = result.entry
        if result.error is not None:
            self.events.put(
                ("follow_item", (entry.site, entry.book_id, "error", result.error))
            )
            return
        if not result.latest:
            self.events.put(
                ("follow_item", (entry.site, entry.book_id, "not_found", ""))
            )
            return
        if not result.changed:
            self.events.put(
                (
                    "follow_item",
                    (entry.site, entry.book_id, "up_to_date", result.latest),
                )
            )
            return
        kind = "first" if not entry.last_chapter else "updated"
        self.events.put(
            ("follow_item", (entry.site, entry.book_id, kind, result.latest))
        )

    def _on_follow_item(self, payload: tuple[str, str, str, str]) -> None:
        """Report one book's check outcome and count it for the summary."""
        site, book_id, kind, detail = payload
        if kind not in _FOLLOW_ITEM_KINDS:
            return
        self._follow_counts[kind] = self._follow_counts.get(kind, 0) + 1
        label = f"{site}/{book_id}"
        if kind == "updated":
            message = t("New chapter for {book}: {latest}").format(
                book=label, latest=detail
            )
        elif kind == "first":
            message = t("First chapter recorded for {book}: {latest}").format(
                book=label, latest=detail
            )
        elif kind == "up_to_date":
            message = t("Up to date: {book} ({latest})").format(
                book=label, latest=detail
            )
        elif kind == "not_found":
            message = t("No result found for {book}.").format(book=label)
        else:
            message = t("Check failed for {book}: {error}").format(
                book=label, error=detail
            )
        self._set_status(message)

    def _follow_check_finished(self, _payload: object) -> None:
        self._follow_checking = False
        self._set_follow_buttons(enabled=True)
        counts = self._follow_counts
        self._set_status(
            t(
                "Follow check finished: {updated} updated, {uptodate} up to date, "
                "{missing} not found, {failed} failed."
            ).format(
                updated=counts.get("updated", 0) + counts.get("first", 0),
                uptodate=counts.get("up_to_date", 0),
                missing=counts.get("not_found", 0),
                failed=counts.get("error", 0),
            )
        )

    def _download_follow_selection(self) -> None:
        """Queue the selected books (or the whole list) for download."""
        entries = self._follow_selected() or list(self._follow_entries)
        if not entries:
            self._set_status(t("Select one or more followed books first."))
            return

        self._set_status(
            t("Queueing {count} followed book(s)...").format(count=len(entries))
        )
        for entry in entries:
            title = entry.title or entry.book_id
            coro = manager.add_task(
                title=entry.title, site=entry.site, book_id=entry.book_id
            )
            try:
                future = self.engine.submit(coro)
            except Exception as exc:  # noqa: BLE001 - loop startup failure
                coro.close()
                self.events.put(
                    (
                        "error",
                        t("Could not queue '{title}': {error}").format(
                            title=title, error=format_error(exc)
                        ),
                    )
                )
                continue
            future.add_done_callback(partial(self._follow_download_finished, title))

    def _follow_download_finished(self, title: str, done: Future[DownloadTask]) -> None:
        try:
            task = done.result()
        except Exception as exc:  # noqa: BLE001 - reported in the UI thread
            self.events.put(
                (
                    "error",
                    t("Could not queue '{title}': {error}").format(
                        title=title, error=format_error(exc)
                    ),
                )
            )
            return
        self.events.put(("status", t("Task added: {title}").format(title=task.title)))

    # ------------------------------------------------------------------
    # Polling and events
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        if self._closing:
            return

        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(kind, payload)
            except Exception as exc:  # noqa: BLE001 - never kill the poll loop
                logger.exception("Failed to handle desktop event %s", kind)
                self._show_error(format_error(exc))

        try:
            self._refresh_tasks_if_due()
        except Exception as exc:  # noqa: BLE001 - keep the UI alive
            logger.exception("Failed to refresh task snapshots")
            self._set_status(format_error(exc))

        if not self._closing:
            self.root.after(_POLL_MS, self._poll)

    def _refresh_tasks_if_due(self) -> None:
        """Poll the task manager snapshot at most every 0.5 seconds."""
        future = self._snapshot_future
        if future is not None:
            if not future.done():
                return
            self._snapshot_future = None
            try:
                snapshot = future.result()
            except Exception as exc:  # noqa: BLE001 - reported but not fatal
                logger.exception("Task snapshot failed")
                self._set_status(
                    t("Could not refresh tasks: {error}").format(
                        error=format_error(exc)
                    )
                )
                return
            self._render_tasks(snapshot)
            return

        now = time.monotonic()
        if now - self._last_snapshot < _SNAPSHOT_SECONDS:
            return
        self._last_snapshot = now
        coro = _snapshot_tasks()
        try:
            self._snapshot_future = self.engine.submit(coro)
        except Exception:  # noqa: BLE001 - loop failure is reported by status
            coro.close()
            logger.exception("Could not schedule a task snapshot")

    def _handle_event(self, kind: str, payload: Any) -> None:
        if kind == "status":
            self._set_status(str(payload))
        elif kind == "error":
            self._show_error(str(payload))
        elif kind == "search_results":
            self._render_search_results(payload)
        elif kind == "search_error":
            self._search_failed(str(payload))
        elif kind == "retry_done":
            self._retry_finished(payload)
        elif kind == "music_search_error":
            self._music_search_failed(str(payload))
        elif kind == "music_results":
            self._render_music_results(payload)
        elif kind == "music_progress":
            self._update_music_progress(payload)
        elif kind == "music_result":
            self._append_music_result(payload)
        elif kind == "music_download_done":
            self._music_download_finished(payload)
        elif kind == "music_download_error":
            self._music_download_failed(str(payload))
        elif kind == "music_health_item":
            self._on_music_health_item(payload)
        elif kind == "music_health_done":
            self._music_health_finished(payload)
        elif kind == "follow_entries":
            self._render_follow_entries(payload)
        elif kind == "follow_load_error":
            logger.error("Watchlist load failed: %s", payload)
            # The reload that should have shown the follow feedback failed;
            # drop it so a later refresh cannot resurrect a stale message.
            self._follow_status_after_reload = None
            self._set_status(
                t("Could not load the follow list: {error}").format(error=payload)
            )
        elif kind == "follow_item":
            self._on_follow_item(payload)
        elif kind == "follow_check_done":
            self._follow_check_finished(payload)
        elif kind == "search_follow_done":
            self._on_search_follow_done(payload)
        elif kind == "search_follow_finished":
            self._on_search_follow_finished(payload)
        else:
            logger.debug("Ignoring unknown desktop event: %s", kind)

    def _set_status(self, message: str) -> None:
        # Any new status message ends a pending notification highlight.
        self._reset_status_highlight()
        self._status_var.set(message)

    def _reset_status_highlight(self) -> None:
        """Drop a pending status-bar highlight (no-op when none is active)."""
        after_id = self._status_highlight_after
        if after_id is None:
            return
        self._status_highlight_after = None
        with contextlib.suppress(tk.TclError):
            self.root.after_cancel(after_id)
        label = self._status_label
        if label is not None:
            with contextlib.suppress(tk.TclError):
                label.configure(text_color=_STATUS_DEFAULT_TEXT)

    def _highlight_status(self, message: str, color: palette.Color) -> None:
        """Show ``message`` with a colored status bar for a few seconds."""
        self._set_status(message)
        label = self._status_label
        if label is None:
            return
        with contextlib.suppress(tk.TclError):
            label.configure(text_color=color)
        self._status_highlight_after = self.root.after(
            _NOTIFY_STATUS_MS, self._reset_status_highlight
        )

    # ------------------------------------------------------------------
    # Task-completion notifications
    # ------------------------------------------------------------------
    def _notify_task_transitions(self, snapshot: dict[str, list[DownloadTask]]) -> None:
        """
        Notify about tasks that reached ``completed``/``failed`` since the
        previous snapshot.

        The first snapshot only builds the baseline (the Android TaskEvents
        pattern), so tasks that were already finished when the window opened
        never notify, and re-rendering the same snapshot cannot notify twice.
        """
        tasks = flatten_task_snapshot(snapshot)
        events = detect_task_notifications(self._previous_task_states, tasks)
        self._previous_task_states = snapshot_task_states(tasks)
        if not events:
            return
        self._announce_task_events(events)

    def _announce_task_events(self, events: list[TaskNotification]) -> None:
        """Raise one notification: a beep when hidden, then a highlighted line."""
        if self._window_in_background():
            play_notification_sound()
        color = (
            _STATUS_HIGHLIGHT_FAILED
            if any(event.outcome is TaskOutcome.FAILED for event in events)
            else _STATUS_HIGHLIGHT_COMPLETED
        )
        self._highlight_status(task_event_status_message(events), color=color)

    def _window_in_background(self) -> bool:
        """
        Whether the window is not in the foreground right now.

        Only the Tk ``state()`` is consulted: anything other than ``normal``
        (minimized, withdrawn, ...) counts as background.  A destroyed window
        counts as foreground so shutdown never beeps.
        """
        try:
            return bool(self.root.state() != "normal")
        except tk.TclError:
            return False

    def _show_error(self, message: str) -> None:
        logger.error("%s", message)
        self._set_status(message)
        messagebox.showerror(t("Error"), message, parent=self.root)

    def _submit(
        self,
        coro: Coroutine[Any, Any, T],
        describe: Callable[[T], str] | None = None,
    ) -> None:
        """Run a manager coroutine and report its result through the queue."""
        try:
            future = self.engine.submit(coro)
        except Exception as exc:  # noqa: BLE001 - loop startup failure
            coro.close()
            self._show_error(format_error(exc))
            return

        def on_done(done: Future[T]) -> None:
            try:
                value = done.result()
            except Exception as exc:  # noqa: BLE001 - reported by the UI
                self.events.put(("error", format_error(exc)))
            else:
                if describe is not None:
                    self.events.put(("status", describe(value)))

        future.add_done_callback(on_done)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def _start_search(self) -> None:
        keyword = self.search_var.get().strip()
        if not keyword:
            self._set_status(t("Enter a keyword to search."))
            return

        self._last_search = remember_search(keyword, self.sites_var.get())
        self._update_search_memory_control()

        sites = parse_sites(self.sites_var.get())
        if sites is not None:
            sites = [site for site in sites if site in SEARCH_SUPPORT_SITES]
            if not sites:
                self._set_status(t("No known site selected."))
                return

        if self.search_button is not None:
            self.search_button.configure(state="disabled")
        self._set_status(t("Searching for '{keyword}'...").format(keyword=keyword))
        threading.Thread(
            target=self._search_worker,
            args=(keyword, sites),
            name="desktop-search",
            daemon=True,
        ).start()

    def _restore_last_search(self) -> None:
        state = self._last_search
        if state is None or not state.has_previous:
            self._set_status(t("No previous search to restore."))
            return
        self.search_var.set(state.keyword)
        self.sites_var.set(state.sites)
        if self.search_entry is not None:
            self.search_entry.focus_set()
        self._set_status(t("Restored the last search."))

    def _update_search_memory_control(self) -> None:
        button = self.search_last_button
        if button is None:
            return
        state = self._last_search
        enabled = state is not None and state.has_previous
        self._set_button_enabled(button, enabled)

    def _search_worker(self, keyword: str, sites: list[str] | None) -> None:
        try:
            results = asyncio.run(
                search(
                    keyword,
                    sites=sites,
                    per_site_limit=_SEARCH_PER_SITE,
                    timeout=_SEARCH_TIMEOUT,
                )
            )
        except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
            self.events.put(("search_error", format_error(exc)))
            return
        self.events.put(("search_results", (keyword, results)))

    def _search_failed(self, message: str) -> None:
        if self.search_button is not None:
            self.search_button.configure(state="normal")
        self._show_error(t("Search failed: {error}").format(error=message))

    # ------------------------------------------------------------------
    # Window state (geometry) persistence
    # ------------------------------------------------------------------
    def _restore_window_geometry(self) -> None:
        """Apply the stored window geometry, falling back to the default."""
        geometry = load_window_geometry(window_state_file())
        self.root.geometry(
            serialize_geometry(geometry) if geometry is not None else _DEFAULT_GEOMETRY
        )

    def _save_window_state(self) -> None:
        """Persist the current window geometry for the next launch."""
        geometry = parse_geometry(self.root.geometry())
        if geometry is None:
            return
        # A degenerate query result (e.g. a window that was never mapped)
        # must not overwrite the good geometry from a previous session.
        if geometry.width < MIN_WINDOW_WIDTH or geometry.height < MIN_WINDOW_HEIGHT:
            return
        save_window_geometry(window_state_file(), geometry)

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------
    def _register_shortcuts(self) -> None:
        """
        Bind the global keyboard shortcuts (see the shortcuts dialog).

        ``bind_all`` makes the bindings work with focus on any widget; both
        letter cases are bound so Ctrl+Shift+F/Ctrl+Shift+D behave the same.
        """
        for index in range(1, len(_PAGE_ORDER) + 1):
            self.root.bind_all(
                f"<Control-{index}>", partial(self._on_page_shortcut, index)
            )
        for keysym in ("f", "F"):
            self.root.bind_all(f"<Control-{keysym}>", self._on_focus_search_shortcut)
        for keysym in ("d", "D"):
            self.root.bind_all(f"<Control-{keysym}>", self._on_add_to_queue_shortcut)

    def _on_page_shortcut(self, index: int, _event: object) -> None:
        """Switch to the ``index``-th page (1-based; ignore others)."""
        self._show_page(index - 1)

    def _on_focus_search_shortcut(self, _event: object) -> None:
        """Raise the search page and focus its keyword entry."""
        self._show_page(0)
        entry = self.search_entry
        if entry is not None:
            entry.focus_set()

    def _on_add_to_queue_shortcut(self, _event: object) -> None:
        """Queue the search selection, like the toolbar button."""
        self._add_selected_results()

    def _show_keyboard_shortcuts(self) -> None:
        """Show the keyboard shortcut list in a dialog."""
        lines = [
            "Ctrl+1..5  " + t("Switch to tab {number}").format(number="1..5"),
            f"Ctrl+F  {t('Search')}",
            f"Ctrl+D  {t('Add to download queue')}",
        ]
        messagebox.showinfo(t("Keyboard shortcuts"), "\n".join(lines), parent=self.root)

    # ------------------------------------------------------------------
    # Shared widget helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _set_button_enabled(button: ctk.CTkButton | None, enabled: bool) -> None:
        if button is not None:
            button.configure(state="normal" if enabled else "disabled")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._music_stop.set()
        self._set_status(t("Closing..."))
        with contextlib.suppress(tk.TclError):
            self.root.update_idletasks()
        # Saved after ``update_idletasks`` so the geometry query reflects the
        # final laid-out size even for a window that was never mapped.
        self._save_window_state()
        try:
            self.engine.shutdown()
        finally:
            self.root.destroy()


def run_app() -> None:
    """Create the CTk root, show the window, and run the main loop."""
    apply_custom_theme()
    ctk.set_appearance_mode("system")
    root = ctk.CTk()
    try:
        DesktopApp(root)
    except Exception as exc:  # noqa: BLE001 - report instead of a silent crash
        logger.exception("Failed to build the desktop window")
        messagebox.showerror(t("Error"), format_error(exc), parent=root)
        root.destroy()
        return
    root.mainloop()
