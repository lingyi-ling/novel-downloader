#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.ui
--------------------------------

Tkinter user interface for the native desktop client.

Threading contract
------------------
* Every network/engine call runs outside the Tk thread.
* Async engine calls use ``asyncio.run`` in their own worker thread, except
  download-task manager calls which go through :class:`AsyncEngine` (a
  persistent loop thread; see :mod:`.engine`).
* Worker threads never touch Tk.  They push messages to :attr:`DesktopApp.events`
  and the Tk thread drains that queue in ``_poll`` every 200 ms.
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
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, TypeVar

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
    health_rows,
    health_summary,
    history_row,
    music_row,
    parse_sites,
    retry_params,
    search_menu_state,
    search_row,
    task_detail_text,
    task_export_choices,
    task_filter_code,
    task_filter_label,
    task_menu_state,
    task_output_actions,
    task_row,
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

__all__ = ["DesktopApp", "run_app"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

_POLL_MS = 200
_SNAPSHOT_SECONDS = 0.5
_SEARCH_PER_SITE = 5
_SEARCH_TIMEOUT = 10.0
_MUSIC_SEARCH_LIMIT = 30
_MUSIC_WORKERS = 3

#: How long the status bar keeps a task-notification highlight (ms).
_NOTIFY_STATUS_MS = 5000

#: Status-bar highlight colors; shared with the task tree's row colors.
_STATUS_HIGHLIGHT_COMPLETED = "#1b6e1b"
_STATUS_HIGHLIGHT_FAILED = "#b00020"
_STATUS_DEFAULT_FG = "black"

#: Per-platform budget for the health check; each platform gets one bounded
#: search (see ``apps.music.providers.health_check``).
_MUSIC_HEALTH_TIMEOUT = 15.0

#: Outcome kinds posted by the follow-check worker for each book.
_FOLLOW_ITEM_KINDS = ("first", "updated", "up_to_date", "not_found", "error")

Event = tuple[str, Any]


async def _snapshot_tasks() -> dict[str, list[DownloadTask]]:
    """Read a task snapshot from the background loop thread."""
    return manager.snapshot()


class DesktopApp:
    """Build and drive the desktop window."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[Event] = queue.Queue()
        self.engine = AsyncEngine()
        self._closing = False

        self._status_var = tk.StringVar(value=t("Ready"))
        # Task-completion notifications (the Android TaskEvents pattern): the
        # ``task_id -> status`` baseline of the previous snapshot, plus the
        # status-bar highlight bookkeeping.
        self._previous_task_states: dict[str, str] = {}
        self._status_label: ttk.Label | None = None
        self._status_highlight_after: str | None = None
        self._search_results: list[SearchResult] = []
        self._history_items: list[FileItem] = []
        self._snapshot_future: Future[dict[str, list[DownloadTask]]] | None = None
        self._last_snapshot = 0.0

        # Search widgets / state.
        self.search_var = tk.StringVar()
        self.sites_var = tk.StringVar()
        self.search_entry: ttk.Entry | None = None
        self.search_button: ttk.Button | None = None
        self.search_last_button: ttk.Button | None = None
        self.search_tree: ttk.Treeview | None = None
        # Follow ("追更") button on the search tab: acts on the selected rows.
        self.search_follow_button: ttk.Button | None = None
        # (site, book_id) pairs whose follow-add is currently running on a
        # worker thread; re-triggering those rows is a no-op until the worker
        # reports back, so a double click cannot queue the same book twice.
        self._search_follow_pending: set[tuple[str, str]] = set()
        # Right-click context menu for the search-result rows; built lazily
        # like the task menu and refilled for every popup.
        self._search_menu: tk.Menu | None = None
        self._last_search: SearchState | None = None

        # Download widgets.
        self.task_tree: ttk.Treeview | None = None
        self.retry_button: ttk.Button | None = None
        self.open_file_button: ttk.Menubutton | None = None
        self.open_folder_button: ttk.Button | None = None
        self.task_detail_var = tk.StringVar(
            value=t("Select a task to view its details.")
        )
        self._tasks_by_id: dict[str, DownloadTask] = {}
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
        self.task_filter_combo: ttk.Combobox | None = None
        self.task_keyword_entry: ttk.Entry | None = None

        # Status bar.
        self._env_var = tk.StringVar(value="")

        # History widgets.
        self.history_tree: ttk.Treeview | None = None
        self.history_dir_var = tk.StringVar(value="")

        # Music widgets / state.
        self._music_loaded = False
        self._music_engine: dict[str, Any] | None = None
        self._music_error = ""
        self._music_ready = False
        self._music_display_to_key: dict[str, str] = {}
        self._music_providers: dict[str, Any] = {}
        self._music_provider_lock = threading.Lock()
        self._music_tracks: list[Any] = []
        self._music_stop = threading.Event()
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
        self.music_platform_combo: ttk.Combobox | None = None
        self.music_search_button: ttk.Button | None = None
        self.music_download_button: ttk.Button | None = None
        self.music_stop_button: ttk.Button | None = None
        self.music_tree: ttk.Treeview | None = None
        self.music_progress: ttk.Progressbar | None = None
        self.music_log: ScrolledText | None = None
        # Platform health check ("检查平台") widgets / state.
        self.music_health_button: ttk.Button | None = None
        self.music_health_tree: ttk.Treeview | None = None
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
        # search-tab follow request; the next follow-list refresh shows it
        # instead of the generic entry count.
        self._follow_status_after_reload: str | None = None
        self.follow_tree: ttk.Treeview | None = None
        self.follow_add_button: ttk.Button | None = None
        self.follow_remove_button: ttk.Button | None = None
        self.follow_check_button: ttk.Button | None = None
        self.follow_download_button: ttk.Button | None = None

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(_POLL_MS, self._poll)
        self._refresh_history(initial=True)
        self._reload_follow_async()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title(f"{t('Novel Downloader')} v{__version__}")
        self.root.geometry("1100x720")
        self.root.minsize(760, 480)

        status_bar = ttk.Frame(self.root, relief="sunken", padding=(6, 3))
        status_bar.pack(side="bottom", fill="x")
        status_label = ttk.Label(
            status_bar,
            textvariable=self._status_var,
            anchor="w",
        )
        status_label.pack(side="left", fill="x", expand=True)
        self._status_label = status_label
        self._env_var.set(environment_summary(DEFAULT_API_BASE, app_home()))
        ttk.Label(
            status_bar,
            textvariable=self._env_var,
            anchor="e",
            foreground="#555555",
        ).pack(side="right")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.search_tab = ttk.Frame(notebook, padding=8)
        self.download_tab = ttk.Frame(notebook, padding=8)
        self.history_tab = ttk.Frame(notebook, padding=8)
        self.music_tab = ttk.Frame(notebook, padding=8)
        self.follow_tab = ttk.Frame(notebook, padding=8)
        notebook.add(self.search_tab, text=t("Search"))
        notebook.add(self.download_tab, text=t("Download"))
        notebook.add(self.history_tab, text=t("History"))
        notebook.add(self.music_tab, text=t("Music"))
        notebook.add(self.follow_tab, text=t("Follow"))

        self._build_search_tab()
        self._build_download_tab()
        self._build_history_tab()
        self._build_music_tab()
        self._build_follow_tab()

    def _build_search_tab(self) -> None:
        form = ttk.Frame(self.search_tab)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text=t("Keyword")).grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=2
        )
        self.search_entry = ttk.Entry(form, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew", pady=2)
        self.search_entry.bind("<Return>", lambda _event: self._start_search())
        self.search_button = ttk.Button(
            form, text=t("Search"), command=self._start_search
        )
        self.search_button.grid(row=0, column=2, padx=(6, 0), pady=2)

        ttk.Label(form, text=t("Sites (comma separated, empty = all)")).grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=2
        )
        sites_entry = ttk.Entry(form, textvariable=self.sites_var)
        sites_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=2)

        ttk.Label(
            form,
            text=t("e.g. qidian, b520, syosetu"),
            foreground="#666666",
        ).grid(row=2, column=1, sticky="w", pady=(0, 4))

        tree_frame = ttk.Frame(self.search_tab)
        tree_frame.pack(fill="both", expand=True, pady=(6, 6))

        columns = ("title", "author", "site", "word_count", "updated")
        tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = (
            ("title", t("Title"), 320),
            ("author", t("Author"), 140),
            ("site", t("Site"), 120),
            ("word_count", t("Word count"), 100),
            ("updated", t("Updated"), 120),
        )
        for column, label, width in headings:
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tree.bind("<Double-1>", self._on_search_double_click)
        tree.bind("<Button-3>", self._on_search_right_click)
        tree.bind("<Button-2>", self._on_search_right_click)
        tree.bind("<<TreeviewSelect>>", self._on_search_selection)
        self.search_tree = tree

        actions = ttk.Frame(self.search_tab)
        actions.pack(fill="x")
        self.search_last_button = ttk.Button(
            actions,
            text=t("Restore last search"),
            command=self._restore_last_search,
            state="disabled",
        )
        self.search_last_button.pack(side="left")
        ttk.Button(
            actions,
            text=t("Add to download queue"),
            command=self._add_selected_results,
        ).pack(side="left", padx=(6, 0))
        self.search_follow_button = ttk.Button(
            actions,
            text=t("Follow"),
            command=self._follow_selected_results,
            state="disabled",
        )
        self.search_follow_button.pack(side="left", padx=(6, 0))

    def _build_download_tab(self) -> None:
        filters = ttk.Frame(self.download_tab)
        filters.pack(fill="x", pady=(0, 6))
        ttk.Label(filters, text=t("Status")).pack(side="left")
        self.task_filter_combo = ttk.Combobox(
            filters,
            textvariable=self.task_filter_var,
            values=[task_filter_label(code) for code in TASK_FILTER_CODES],
            state="readonly",
            width=12,
        )
        self.task_filter_combo.pack(side="left", padx=(6, 12))
        self.task_filter_combo.bind(
            "<<ComboboxSelected>>", self._on_task_filter_changed
        )

        ttk.Label(filters, text=t("Keyword")).pack(side="left")
        self.task_keyword_entry = ttk.Entry(
            filters, textvariable=self.task_keyword_var, width=30
        )
        self.task_keyword_entry.pack(side="left", padx=(6, 0))
        self.task_keyword_var.trace_add("write", self._on_task_keyword_changed)
        ttk.Label(
            filters,
            text=t("Filters are applied locally."),
            foreground="#666666",
        ).pack(side="left", padx=8)

        actions = ttk.Frame(self.download_tab)
        actions.pack(fill="x", pady=(0, 6))
        ttk.Button(
            actions,
            text=t("Cancel selected task"),
            command=self._cancel_selected_task,
        ).pack(side="left")
        self.retry_button = ttk.Button(
            actions,
            text=t("Retry"),
            command=self._retry_selected_task,
            state="disabled",
        )
        self.retry_button.pack(side="left", padx=(6, 0))
        self.open_file_button = ttk.Menubutton(
            actions,
            text=t("Open file"),
            state="disabled",
        )
        file_menu = tk.Menu(self.open_file_button, tearoff=0)
        self.open_file_button.configure(menu=file_menu)
        self._task_file_menu = file_menu
        self.open_file_button.pack(side="left", padx=(6, 0))
        self.open_folder_button = ttk.Button(
            actions,
            text=t("Open folder"),
            command=self._open_selected_task_folder,
            state="disabled",
        )
        self.open_folder_button.pack(side="left", padx=(6, 0))
        ttk.Label(
            actions,
            text=t("Progress refreshes automatically."),
            foreground="#666666",
        ).pack(side="left", padx=8)

        tree_frame = ttk.Frame(self.download_tab)
        tree_frame.pack(fill="both", expand=True)

        columns = ("status", "title", "site", "progress", "error")
        tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        tree.heading("#0", text=t("Tasks"))
        tree.column("#0", width=120, anchor="w")
        headings = (
            ("status", t("Status"), 100),
            ("title", t("Title"), 300),
            ("site", t("Site"), 120),
            ("progress", t("Progress"), 160),
            ("error", t("Error"), 260),
        )
        for column, label, width in headings:
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor="w")
        tree.tag_configure("group", background="#e9e9e9")
        tree.tag_configure("empty", foreground="#666666")
        tree.tag_configure("failed", foreground="#b00020")
        tree.tag_configure("completed", foreground="#1b6e1b")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tree.bind("<<TreeviewSelect>>", self._on_task_selection)
        tree.bind("<Double-1>", self._on_task_double_click)
        tree.bind("<Button-3>", self._on_task_right_click)
        tree.bind("<Button-2>", self._on_task_right_click)
        self.task_tree = tree

        detail = ttk.Label(
            self.download_tab,
            textvariable=self.task_detail_var,
            foreground="#555555",
            wraplength=980,
            justify="left",
            anchor="w",
        )
        detail.pack(fill="x", pady=(6, 0))
        detail.bind("<Button-3>", self._on_task_detail_right_click)
        detail.bind(
            "<Configure>",
            lambda event: detail.configure(wraplength=max(200, event.width - 16)),
        )

    def _build_history_tab(self) -> None:
        actions = ttk.Frame(self.history_tab)
        actions.pack(fill="x", pady=(0, 4))
        ttk.Button(
            actions, text=t("Refresh"), command=self._refresh_history_action
        ).pack(side="left")
        ttk.Button(
            actions,
            text=t("Open containing folder"),
            command=self._open_history_folder,
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            actions,
            textvariable=self.history_dir_var,
            foreground="#666666",
        ).pack(side="left", padx=8)

        tree_frame = ttk.Frame(self.history_tab)
        tree_frame.pack(fill="both", expand=True)

        columns = ("name", "size", "modified")
        tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="browse"
        )
        headings = (
            ("name", t("File name"), 500),
            ("size", t("Size"), 110),
            ("modified", t("Modified"), 180),
        )
        for column, label, width in headings:
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tree.bind("<Double-1>", self._open_selected_history_file)
        self.history_tree = tree

    def _build_music_tab(self) -> None:
        engine = self._load_music_engine()
        if engine is None:
            ttk.Label(
                self.music_tab,
                text=(
                    t(
                        "Install the music extras to enable this tab: "
                        'pip install "novel-downloader[music]"'
                    )
                ),
                wraplength=760,
            ).pack(anchor="w", pady=(8, 4))
            if self._music_error:
                ttk.Label(
                    self.music_tab,
                    text=t("Import error: {error}").format(error=self._music_error),
                    wraplength=760,
                    foreground="#b00020",
                ).pack(anchor="w")
            return

        try:
            labels: list[tuple[str, str]] = list(engine["available_labels"]())
        except Exception as exc:  # noqa: BLE001 - optional engine registry
            self._music_error = format_error(exc)
            ttk.Label(
                self.music_tab,
                text=t("Could not load music platforms: {error}").format(
                    error=self._music_error
                ),
                wraplength=760,
                foreground="#b00020",
            ).pack(anchor="w", pady=8)
            return

        if not labels:
            ttk.Label(
                self.music_tab,
                text=t("No music platforms are available."),
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

        form = ttk.Frame(self.music_tab)
        form.pack(fill="x")
        form.columnconfigure(4, weight=1)

        ttk.Label(form, text=t("Platform")).grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=2
        )
        self.music_platform_combo = ttk.Combobox(
            form,
            textvariable=self.music_platform_var,
            values=[display for _, display in labels],
            state="readonly",
            width=24,
        )
        self.music_platform_combo.grid(
            row=0, column=1, sticky="w", padx=(0, 12), pady=2
        )
        self.music_platform_combo.bind("<<ComboboxSelected>>", self._refresh_music_note)

        # The selected platform's note (credentials / network requirements)
        # sits right below the dropdown so it is seen before searching.
        ttk.Label(
            form,
            textvariable=self.music_note_var,
            foreground="#666666",
            wraplength=420,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 2))

        ttk.Label(form, text=t("Keyword")).grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=2
        )
        music_entry = ttk.Entry(form, textvariable=self.music_query_var)
        music_entry.grid(row=0, column=3, columnspan=2, sticky="ew", pady=2)
        music_entry.bind("<Return>", lambda _event: self._start_music_search())
        self.music_search_button = ttk.Button(
            form, text=t("Search"), command=self._start_music_search
        )
        self.music_search_button.grid(row=0, column=5, padx=(6, 0), pady=2)

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

        settings_frame = ttk.LabelFrame(
            self.music_tab, text=t("Download settings"), padding=8
        )
        settings_frame.pack(fill="x", pady=(6, 2))
        settings_frame.columnconfigure(1, weight=1)

        ttk.Label(settings_frame, text=t("Output folder")).grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=2
        )
        out_entry = ttk.Entry(settings_frame, textvariable=self.music_out_dir_var)
        out_entry.grid(row=0, column=1, sticky="ew", pady=2)
        out_entry.bind("<FocusOut>", lambda _event: self._save_music_settings())
        out_entry.bind("<Return>", lambda _event: self._save_music_settings())
        ttk.Button(
            settings_frame,
            text=t("Browse..."),
            command=self._browse_music_dir,
        ).grid(row=0, column=2, padx=(6, 0), pady=2)

        ttk.Label(settings_frame, text=t("Audio quality")).grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=2
        )
        level_combo = ttk.Combobox(
            settings_frame,
            textvariable=self.music_level_var,
            values=[level_display(level) for level in MUSIC_LEVELS],
            state="readonly",
            width=18,
        )
        level_combo.grid(row=1, column=1, sticky="w", pady=2)
        level_combo.bind("<<ComboboxSelected>>", self._on_music_level_selected)

        checks = ttk.Frame(settings_frame)
        checks.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        check_options = (
            (t("Save lyrics"), self.music_save_lyric_var),
            (t("Save cover"), self.music_embed_cover_var),
            (t("Embed tags"), self.music_embed_tags_var),
            (t("Split into folders"), self.music_split_folder_var),
            (t("Skip existing"), self.music_skip_existing_var),
        )
        for label, variable in check_options:
            ttk.Checkbutton(
                checks,
                text=label,
                variable=variable,
                command=self._save_music_settings,
            ).pack(side="left", padx=(0, 12))

        tree_frame = ttk.Frame(self.music_tab)
        tree_frame.pack(fill="both", expand=True, pady=(4, 6))

        columns = ("name", "artist", "album", "duration")
        tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = (
            ("name", t("Title"), 300),
            ("artist", t("Artist"), 180),
            ("album", t("Album"), 220),
            ("duration", t("Duration"), 90),
        )
        for column, label, width in headings:
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.music_tree = tree

        actions = ttk.Frame(self.music_tab)
        actions.pack(fill="x", pady=(0, 4))
        self.music_download_button = ttk.Button(
            actions, text=t("Download selected"), command=self._start_music_download
        )
        self.music_download_button.pack(side="left")
        self.music_stop_button = ttk.Button(
            actions,
            text=t("Stop"),
            command=self._stop_music_download,
            state="disabled",
        )
        self.music_stop_button.pack(side="left", padx=(6, 0))
        ttk.Label(
            actions,
            textvariable=self.music_summary_var,
            foreground="#666666",
        ).pack(side="left", padx=8)

        self.music_progress = ttk.Progressbar(
            self.music_tab, mode="determinate", maximum=100
        )
        self.music_progress.pack(fill="x", pady=(0, 4))

        self.music_log = ScrolledText(
            self.music_tab, height=7, state="disabled", wrap="word"
        )
        self.music_log.pack(fill="both", expand=False)

        # Platform health check ("检查平台"): one bounded search per platform,
        # run serially on a worker thread, results listed as they arrive.
        health_frame = ttk.LabelFrame(
            self.music_tab, text=t("Platform health"), padding=8
        )
        health_frame.pack(fill="x", pady=(6, 0))

        health_actions = ttk.Frame(health_frame)
        health_actions.pack(fill="x")
        self.music_health_button = ttk.Button(
            health_actions,
            text=t("Check platforms"),
            command=self._start_music_health_check,
        )
        self.music_health_button.pack(side="left")
        ttk.Label(
            health_actions,
            textvariable=self.music_health_summary_var,
            foreground="#666666",
        ).pack(side="left", padx=8)

        health_tree_frame = ttk.Frame(health_frame)
        health_tree_frame.pack(fill="x", pady=(4, 0))
        health_columns = ("platform", "ok", "elapsed", "error")
        health_tree = ttk.Treeview(
            health_tree_frame,
            columns=health_columns,
            show="headings",
            selectmode="browse",
            height=5,
        )
        health_headings = (
            ("platform", t("Platform"), 170),
            ("ok", t("OK"), 80),
            ("elapsed", t("Elapsed (ms)"), 110),
            ("error", t("Error"), 430),
        )
        for column, label, width in health_headings:
            health_tree.heading(column, text=label)
            health_tree.column(column, width=width, anchor="w")
        health_scroll = ttk.Scrollbar(
            health_tree_frame, orient="vertical", command=health_tree.yview
        )
        health_tree.configure(yscrollcommand=health_scroll.set)
        health_tree.pack(side="left", fill="both", expand=True)
        health_scroll.pack(side="right", fill="y")
        self.music_health_tree = health_tree

        self._refresh_music_note()

    def _build_follow_tab(self) -> None:
        actions = ttk.Frame(self.follow_tab)
        actions.pack(fill="x", pady=(0, 6))
        self.follow_add_button = ttk.Button(
            actions, text=t("Add"), command=self._open_follow_add_dialog
        )
        self.follow_add_button.pack(side="left")
        self.follow_remove_button = ttk.Button(
            actions, text=t("Remove"), command=self._remove_follow_selected
        )
        self.follow_remove_button.pack(side="left", padx=(6, 0))
        self.follow_check_button = ttk.Button(
            actions, text=t("Check updates"), command=self._start_follow_check
        )
        self.follow_check_button.pack(side="left", padx=(6, 0))
        self.follow_download_button = ttk.Button(
            actions,
            text=t("Download selected"),
            command=self._download_follow_selection,
        )
        self.follow_download_button.pack(side="left", padx=(6, 0))
        ttk.Label(
            actions,
            text=t("Nothing selected downloads every followed book."),
            foreground="#666666",
        ).pack(side="left", padx=8)

        tree_frame = ttk.Frame(self.follow_tab)
        tree_frame.pack(fill="both", expand=True)

        columns = ("site", "book_id", "title", "last_chapter", "updated")
        tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = (
            ("site", t("Site"), 110),
            ("book_id", t("Book ID"), 120),
            ("title", t("Title"), 320),
            ("last_chapter", t("Latest chapter"), 260),
            ("updated", t("Updated"), 160),
        )
        for column, label, width in headings:
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.follow_tree = tree

    # ------------------------------------------------------------------
    # Follow list ("追更")
    # ------------------------------------------------------------------
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

    def _render_follow_entries(self, entries: list[WatchEntry]) -> None:
        self._follow_entries = list(entries)
        tree = self.follow_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for index, entry in enumerate(self._follow_entries):
            tree.insert("", "end", iid=str(index), values=follow_row(entry))
        if self._follow_entries:
            self._set_status(
                t("Found {count} followed book(s).").format(
                    count=len(self._follow_entries)
                )
            )
        else:
            self._set_status(t("No followed books."))
        # A follow request raised on the search tab owns the status line: its
        # outcome is more specific than the generic count rendered above.
        message = self._follow_status_after_reload
        if message is not None:
            self._follow_status_after_reload = None
            self._set_status(message)

    def _follow_selected(self) -> list[WatchEntry]:
        """Return the entries behind the selected rows (empty when none)."""
        tree = self.follow_tree
        if tree is None:
            return []
        chosen: list[WatchEntry] = []
        for iid in tree.selection():
            try:
                chosen.append(self._follow_entries[int(iid)])
            except (ValueError, IndexError):
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
        top = tk.Toplevel(self.root)
        top.title(t("Add a book to the follow list."))
        top.transient(self.root)
        top.resizable(False, False)
        top.bind("<Escape>", lambda _event: top.destroy())

        form = ttk.Frame(top, padding=8)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)

        site_var = tk.StringVar()
        book_var = tk.StringVar()
        title_var = tk.StringVar()

        ttk.Label(form, text=t("Site")).grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=2
        )
        site_entry = ttk.Entry(form, textvariable=site_var)
        site_entry.grid(row=0, column=1, sticky="ew", pady=2)

        ttk.Label(form, text=t("Book ID")).grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=2
        )
        ttk.Entry(form, textvariable=book_var).grid(
            row=1, column=1, sticky="ew", pady=2
        )

        ttk.Label(form, text=t("Title")).grid(
            row=2, column=0, sticky="w", padx=(0, 6), pady=2
        )
        title_entry = ttk.Entry(form, textvariable=title_var)
        title_entry.grid(row=2, column=1, sticky="ew", pady=2)

        ttk.Label(
            form,
            text=t("Display title used when checking for new chapters."),
            foreground="#666666",
            wraplength=280,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 6))

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

        buttons = ttk.Frame(top, padding=(8, 0, 8, 8))
        buttons.pack(fill="x")
        ttk.Button(buttons, text=t("Add"), command=confirm).pack(side="right")
        ttk.Button(buttons, text=t("Cancel"), command=top.destroy).pack(
            side="right", padx=(0, 6)
        )

        top.grab_set()
        site_entry.focus_set()
        top.wait_window()

        if confirmed:
            self._follow_add(*confirmed[0])

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
                label.configure(foreground=_STATUS_DEFAULT_FG)

    def _highlight_status(self, message: str, color: str) -> None:
        """Show ``message`` with a colored status bar for a few seconds."""
        self._set_status(message)
        label = self._status_label
        if label is None:
            return
        with contextlib.suppress(tk.TclError):
            label.configure(foreground=color)
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
            return self.root.state() != "normal"
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
            self.search_button.state(["disabled"])
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
        button.state(["!disabled"] if enabled else ["disabled"])

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
            self.search_button.state(["!disabled"])
        self._show_error(t("Search failed: {error}").format(error=message))

    def _render_search_results(self, payload: tuple[str, list[SearchResult]]) -> None:
        keyword, results = payload
        self._search_results = results
        tree = self.search_tree
        if tree is not None:
            tree.delete(*tree.get_children())
            for index, result in enumerate(results):
                tree.insert("", "end", iid=str(index), values=search_row(result))
        if self.search_button is not None:
            self.search_button.state(["!disabled"])
        self._update_search_follow_button()
        self._set_status(
            t("Found {count} result(s) for '{keyword}'.").format(
                count=len(results), keyword=keyword
            )
        )

    def _on_search_double_click(self, _event: object) -> None:
        self._add_selected_results()

    def _add_selected_results(self) -> None:
        tree = self.search_tree
        if tree is None:
            return
        selection = tree.selection()
        if not selection:
            self._set_status(t("Select one or more search results first."))
            return

        queued = 0
        for iid in selection:
            try:
                result = self._search_results[int(iid)]
            except (ValueError, IndexError):
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

    # ------------------------------------------------------------------
    # Search results context menu (right click)
    # ------------------------------------------------------------------
    def _on_search_right_click(self, event: tk.Event[tk.Misc]) -> None:
        """Pop the row context menu, acting on the selected search results."""
        tree = self.search_tree
        if tree is None:
            return
        iid = tree.identify_row(event.y)
        if iid and iid not in tree.selection():
            # A click on a row outside the selection selects just that row;
            # clicking inside the selection keeps the multi-selection intact.
            tree.selection_set(iid)
        if tree.selection():
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
    # Follow ("追更") from the search tab
    # ------------------------------------------------------------------
    def _on_search_selection(self, _event: object | None = None) -> None:
        self._update_search_follow_button()

    def _selected_search_results(self) -> list[SearchResult]:
        """Return the results behind the selected rows (empty when none)."""
        tree = self.search_tree
        if tree is None:
            return []
        chosen: list[SearchResult] = []
        for iid in tree.selection():
            try:
                chosen.append(self._search_results[int(iid)])
            except (ValueError, IndexError):
                continue
        return chosen

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
        # Point the user at the Follow tab, which the reload below refreshes.
        self._follow_status_after_reload = f"{message} ({t('Follow')})"

    def _on_search_follow_finished(self, _payload: object) -> None:
        """Re-arm the button and refresh the follow tab from the shared store."""
        self._update_search_follow_button()
        self._reload_follow_async()

    # ------------------------------------------------------------------
    # Download progress
    # ------------------------------------------------------------------
    def _refresh_tasks_if_due(self) -> None:
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

    def _render_tasks(self, snapshot: dict[str, list[DownloadTask]]) -> None:
        tree = self.task_tree
        if tree is None:
            return

        self._last_tasks_snapshot = snapshot
        selected = self._selected_task_id()
        status_filter = task_filter_code(self.task_filter_var.get())
        keyword = self.task_keyword_var.get()
        filter_active = status_filter != FILTER_ALL or bool(keyword.strip())

        tree.delete(*tree.get_children())
        self._tasks_by_id = {}
        sections = (
            ("running", t("Running"), snapshot.get("running", [])),
            ("pending", t("Queued"), snapshot.get("pending", [])),
            ("completed", t("Completed"), snapshot.get("completed", [])),
        )
        for key, label, tasks in sections:
            visible = filter_tasks(tasks, status=status_filter, keyword=keyword)
            if filter_active and not visible:
                continue
            parent = tree.insert(
                "",
                "end",
                iid=f"group:{key}",
                text=f"{label} ({len(visible)})",
                open=True,
                tags=("group",),
            )
            for task in visible:
                status_value = str(getattr(task.status, "value", task.status))
                tags: tuple[str, ...] = ()
                if status_value == "failed":
                    tags = ("failed",)
                elif status_value == "completed":
                    tags = ("completed",)
                tree.insert(
                    parent,
                    "end",
                    iid=task.task_id,
                    values=task_row(task),
                    tags=tags,
                )
                self._tasks_by_id[task.task_id] = task

        if not self._tasks_by_id:
            message = (
                t("No tasks match the filter.")
                if filter_active
                else t("No tasks yet. Search for a book and add it to the queue.")
            )
            tree.insert(
                "",
                "end",
                iid="empty",
                text=message,
                tags=("empty",),
            )

        if selected is not None and tree.exists(selected):
            tree.selection_set(selected)
            tree.see(selected)

        self._update_task_controls()
        self._notify_task_transitions(snapshot)

    def _on_task_filter_changed(self, _event: object | None = None) -> None:
        self._render_tasks(self._last_tasks_snapshot)

    def _on_task_keyword_changed(self, *_args: str) -> None:
        self._render_tasks(self._last_tasks_snapshot)

    def _selected_task(self) -> DownloadTask | None:
        task_id = self._selected_task_id()
        if task_id is None:
            return None
        return self._tasks_by_id.get(task_id)

    @staticmethod
    def _set_button_enabled(
        button: ttk.Button | ttk.Menubutton | None, enabled: bool
    ) -> None:
        if button is None:
            return
        button.state(["!disabled"] if enabled else ["disabled"])

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
            return
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

    def _on_task_selection(self, _event: object | None = None) -> None:
        self._update_task_controls()

    def _on_task_double_click(self, event: tk.Event[tk.Misc]) -> None:
        tree = self.task_tree
        if tree is None:
            return
        iid = tree.identify_row(event.y)
        if iid:
            tree.selection_set(iid)
        task = self._selected_task()
        if task is not None:
            self._show_task_details(task)

    def _on_task_right_click(self, event: tk.Event[tk.Misc]) -> None:
        tree = self.task_tree
        if tree is None:
            return
        iid = tree.identify_row(event.y)
        if iid:
            tree.selection_set(iid)
        if self._selected_task() is not None:
            self._show_task_menu(event.x_root, event.y_root)

    def _on_task_detail_right_click(self, event: tk.Event[tk.Misc]) -> None:
        if self._selected_task() is not None:
            self._show_task_menu(event.x_root, event.y_root)

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
        top = tk.Toplevel(self.root)
        top.title(t("Task details"))
        top.transient(self.root)
        top.geometry("700x360")
        top.bind("<Escape>", lambda _event: top.destroy())

        frame = ttk.Frame(top, padding=8)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", height=12)
        text.insert("1.0", task_detail_text(task))
        text.configure(state="disabled")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(top, padding=(8, 0, 8, 8))
        buttons.pack(fill="x")
        detail = task_detail_text(task)
        ttk.Button(
            buttons,
            text=t("Copy"),
            command=lambda: self._copy_text(detail),
        ).pack(side="left")
        ttk.Button(buttons, text=t("Close"), command=top.destroy).pack(side="right")

    def _selected_task_id(self) -> str | None:
        tree = self.task_tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        iid = selection[0]
        if tree.parent(iid) == "":
            return None
        return iid

    def _cancel_selected_task(self) -> None:
        task_id = self._selected_task_id()
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
        tree = self.history_tree
        if tree is not None:
            tree.delete(*tree.get_children())
            for index, item in enumerate(items):
                tree.insert("", "end", iid=str(index), values=history_row(item))

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

    def _selected_history_index(self) -> int | None:
        tree = self.history_tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except ValueError:
            return None

    def _open_history_folder(self) -> None:
        index = self._selected_history_index()
        if index is None:
            self._set_status(t("Select a file first."))
            return
        self._open_path(self._history_items[index].path.parent)

    def _open_selected_history_file(self, _event: object) -> None:
        index = self._selected_history_index()
        if index is None:
            return
        self._open_path(self._history_items[index].path)

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
    def _load_music_engine(self) -> dict[str, Any] | None:
        if self._music_loaded:
            return self._music_engine
        self._music_loaded = True

        try:
            # Probe the optional extras up-front so the tab can explain what is
            # missing instead of failing in the middle of a download.
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

    def _on_music_level_selected(self, _event: object | None = None) -> None:
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

    def _refresh_music_note(self, _event: object | None = None) -> None:
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
        """Check every registered platform once, serially, off the Tk thread."""
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
        tree = self.music_health_tree
        if tree is not None:
            tree.delete(*tree.get_children())
        self.music_health_summary_var.set("")
        if self.music_health_button is not None:
            self.music_health_button.state(["disabled"])
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
        tree = self.music_health_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for index, row in enumerate(health_rows(self._music_health_results)):
            tree.insert("", "end", iid=str(index), values=row)

    def _music_health_finished(self, _payload: object) -> None:
        self._music_health_running = False
        self._render_music_health_rows()
        summary = health_summary(self._music_health_results)
        self.music_health_summary_var.set(summary)
        self._set_status(summary)
        if self.music_health_button is not None:
            self.music_health_button.state(["!disabled"])

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
            self.music_search_button.state(["disabled"])
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
            self.music_search_button.state(["!disabled"])
        self._show_error(t("Music search failed: {error}").format(error=message))

    def _render_music_results(self, payload: tuple[list[Any], int]) -> None:
        tracks, total = payload
        self._music_tracks = tracks
        tree = self.music_tree
        if tree is not None:
            tree.delete(*tree.get_children())
            for index, track in enumerate(tracks):
                tree.insert("", "end", iid=str(index), values=music_row(track))
        if self.music_search_button is not None:
            self.music_search_button.state(["!disabled"])
        self._set_status(t("Found {count} track(s).").format(count=total))

    def _start_music_download(self) -> None:
        engine = self._music_engine
        tree = self.music_tree
        if not self._music_ready or engine is None or tree is None:
            return

        selection = tree.selection()
        if not selection:
            self._set_status(t("Select one or more tracks first."))
            return
        key = self._music_selected_key()
        if not key:
            self._set_status(t("Select a music platform first."))
            return

        tracks: list[Any] = []
        for iid in selection:
            try:
                tracks.append(self._music_tracks[int(iid)])
            except (ValueError, IndexError):
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
            self.music_progress.configure(value=0)
        self.music_summary_var.set(
            t("Downloading {count} track(s) to {dir}...").format(
                count=len(tracks), dir=out_dir
            )
        )
        self._set_status(t("Downloading music..."))
        if self.music_download_button is not None:
            self.music_download_button.state(["disabled"])
        if self.music_search_button is not None:
            self.music_search_button.state(["disabled"])
        if self.music_stop_button is not None:
            self.music_stop_button.state(["!disabled"])
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
            value = max(0.0, min(1.0, fraction)) * 100
            self.music_progress.configure(value=value)
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
            self.music_progress.configure(value=100)
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
            self.music_download_button.state(["!disabled"])
        if self.music_search_button is not None:
            self.music_search_button.state(["!disabled"])
        if self.music_stop_button is not None:
            self.music_stop_button.state(["disabled"])

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
        try:
            self.engine.shutdown()
        finally:
            self.root.destroy()


def run_app() -> None:
    """Create the Tk root, show the window, and run the main loop."""
    root = tk.Tk()
    try:
        DesktopApp(root)
    except Exception as exc:  # noqa: BLE001 - report instead of a silent crash
        logger.exception("Failed to build the desktop window")
        messagebox.showerror(t("Error"), format_error(exc), parent=root)
        root.destroy()
        return
    root.mainloop()
