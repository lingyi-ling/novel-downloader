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
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, TypeVar

from novel_downloader.apps.common.history_files import (
    FileItem,
    _apply_sort,
    _output_dir,
    _scan_files,
)
from novel_downloader.apps.constants import SEARCH_SUPPORT_SITES
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.core.task_manager import manager
from novel_downloader.infra.i18n import t
from novel_downloader.plugins.search import search
from novel_downloader.schemas import SearchResult

from .engine import AsyncEngine
from .formatting import (
    format_error,
    history_row,
    music_row,
    parse_sites,
    search_row,
    task_row,
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
        self._search_results: list[SearchResult] = []
        self._history_items: list[FileItem] = []
        self._snapshot_future: Future[dict[str, list[DownloadTask]]] | None = None
        self._last_snapshot = 0.0

        # Search widgets / state.
        self.search_var = tk.StringVar()
        self.sites_var = tk.StringVar()
        self.search_entry: ttk.Entry | None = None
        self.search_button: ttk.Button | None = None
        self.search_tree: ttk.Treeview | None = None

        # Download widgets.
        self.task_tree: ttk.Treeview | None = None

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
        self.music_platform_combo: ttk.Combobox | None = None
        self.music_search_button: ttk.Button | None = None
        self.music_download_button: ttk.Button | None = None
        self.music_stop_button: ttk.Button | None = None
        self.music_tree: ttk.Treeview | None = None
        self.music_progress: ttk.Progressbar | None = None
        self.music_log: ScrolledText | None = None

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(_POLL_MS, self._poll)
        self._refresh_history(initial=True)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title(t("Novel Downloader"))
        self.root.geometry("1100x720")
        self.root.minsize(760, 480)

        status_bar = ttk.Label(
            self.root,
            textvariable=self._status_var,
            anchor="w",
            relief="sunken",
            padding=(6, 3),
        )
        status_bar.pack(side="bottom", fill="x")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.search_tab = ttk.Frame(notebook, padding=8)
        self.download_tab = ttk.Frame(notebook, padding=8)
        self.history_tab = ttk.Frame(notebook, padding=8)
        self.music_tab = ttk.Frame(notebook, padding=8)
        notebook.add(self.search_tab, text=t("Search"))
        notebook.add(self.download_tab, text=t("Download"))
        notebook.add(self.history_tab, text=t("History"))
        notebook.add(self.music_tab, text=t("Music"))

        self._build_search_tab()
        self._build_download_tab()
        self._build_history_tab()
        self._build_music_tab()

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
        self.search_tree = tree

        actions = ttk.Frame(self.search_tab)
        actions.pack(fill="x")
        ttk.Button(
            actions,
            text=t("Add to download queue"),
            command=self._add_selected_results,
        ).pack(side="left")

    def _build_download_tab(self) -> None:
        actions = ttk.Frame(self.download_tab)
        actions.pack(fill="x", pady=(0, 6))
        ttk.Button(
            actions,
            text=t("Cancel selected task"),
            command=self._cancel_selected_task,
        ).pack(side="left")
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
        tree.tag_configure("failed", foreground="#b00020")
        tree.tag_configure("completed", foreground="#1b6e1b")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.task_tree = tree

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

        ttk.Label(
            self.music_tab,
            textvariable=self.music_note_var,
            foreground="#666666",
        ).pack(anchor="w", pady=(2, 4))

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

        self._refresh_music_note()

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
        else:
            logger.debug("Ignoring unknown desktop event: %s", kind)

    def _set_status(self, message: str) -> None:
        self._status_var.set(message)

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

        selected = self._selected_task_id()
        tree.delete(*tree.get_children())
        sections = (
            ("running", t("Running"), snapshot.get("running", [])),
            ("pending", t("Queued"), snapshot.get("pending", [])),
            ("completed", t("Completed"), snapshot.get("completed", [])),
        )
        for key, label, tasks in sections:
            parent = tree.insert(
                "",
                "end",
                iid=f"group:{key}",
                text=f"{label} ({len(tasks)})",
                open=True,
                tags=("group",),
            )
            for task in tasks:
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

        if selected is not None and tree.exists(selected):
            tree.selection_set(selected)
            tree.see(selected)

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
                platform_note,
            )

            self._music_engine = {
                "available_labels": available_labels,
                "create": create,
                "platform_note": platform_note,
                "DownloadOptions": DownloadOptions,
                "download_many": download_many,
                "human": human,
                "load_settings": music_settings.load_settings,
                "default_download_dir": music_paths.default_download_dir,
            }
        except Exception as exc:  # noqa: BLE001 - optional dependency guard
            self._music_error = format_error(exc)
            self._music_engine = None

        return self._music_engine

    def _music_selected_key(self) -> str | None:
        display = self.music_platform_var.get()
        return self._music_display_to_key.get(display)

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

        try:
            config: dict[str, Any] = dict(engine["load_settings"]())
            out_dir = str(config.get("out_dir") or engine["default_download_dir"]())
            options = engine["DownloadOptions"](
                out_dir=out_dir,
                level=str(config.get("level") or "exhigh"),
                embed_tags=bool(config.get("embed_tags", True)),
                embed_cover=bool(config.get("embed_cover", True)),
                save_lyric=bool(config.get("save_lyric", True)),
                split_folder=bool(config.get("split_folder", True)),
                skip_existing=bool(config.get("skip_existing", True)),
            )
        except Exception as exc:  # noqa: BLE001 - settings/engine failure
            self._show_error(
                t("Could not prepare the music download: {error}").format(
                    error=format_error(exc)
                )
            )
            return

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
