#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.engine
------------------------------------

A persistent background asyncio loop for the download task manager.

``TaskManager`` owns long-lived asyncio primitives (a lock and queues) and
spawns worker tasks.  Running each manager call through ``asyncio.run`` would
bind those primitives to a throwaway loop and cancel the workers when the loop
closes, so the desktop app keeps one loop alive on a daemon thread for the
whole window lifetime instead.

One-shot coroutines (search, for example) are still executed with
``asyncio.run`` in their own worker threads; only the manager needs the
persistent loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from novel_downloader.apps.core.task_manager import manager

__all__ = ["AsyncEngine"]

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncEngine:
    """Own a background event loop and submit manager coroutines to it."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        """Whether the background loop thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start the loop thread on first use and return the loop."""
        with self._lock:
            if self._loop is not None and self.running:
                return self._loop

            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def runner() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(
                target=runner,
                name="novel-desktop-engine",
                daemon=True,
            )
            thread.start()
            if not ready.wait(timeout=5.0):
                loop.close()
                raise RuntimeError("Timed out starting the background event loop")

            self._loop = loop
            self._thread = thread
            return loop

    def submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """
        Schedule ``coro`` on the background loop.

        :param coro: Coroutine to execute.
        :return: A concurrent future resolved by the loop thread.
        """
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        Close the task manager and stop the background loop.

        Falls back to a fresh loop when the engine was never started, matching
        the "always close the manager" contract without keeping an idle thread
        alive for the whole session.

        :param timeout: Maximum seconds to wait for ``manager.close()``.
        """
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None

        if loop is None:
            self._close_standalone(timeout)
            return

        try:
            if not loop.is_closed():
                asyncio.run_coroutine_threadsafe(manager.close(), loop).result(timeout)
        except Exception:  # noqa: BLE001 - best effort during shutdown
            logger.exception("Failed to close download tasks cleanly")
        finally:
            if not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout)
            if not loop.is_closed():
                try:
                    loop.close()
                except Exception:  # noqa: BLE001 - defensive
                    logger.exception("Failed to close the background event loop")

    @staticmethod
    def _close_standalone(timeout: float) -> None:
        """Run ``manager.close()`` on a short-lived loop."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(asyncio.wait_for(manager.close(), timeout))
        except Exception:  # noqa: BLE001 - best effort during shutdown
            logger.exception("Failed to close download tasks cleanly")
        finally:
            loop.close()
