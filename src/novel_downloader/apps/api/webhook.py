#!/usr/bin/env python3
"""
novel_downloader.apps.api.webhook
---------------------------------

Optional "task finished" webhook.

Set ``NOVEL_API_WEBHOOK`` (or pass ``webhook_url`` to :func:`create_app`) to
have the API POST a JSON document to that URL whenever a download task reaches
a terminal state::

    {"event": "task_finished", "task": {...same fields as GET /tasks...}}

Delivery runs in a daemon thread with a hard 5-second timeout. A failing
endpoint (connection error, timeout, non-2xx) is logged and otherwise ignored:
it must never slow down or break the task manager. With no URL configured the
API installs no notifier and performs no polling.

The API has no hook into :class:`~novel_downloader.apps.core.task_manager.
TaskManager` (it only observes snapshots), so a small watcher polls the
manager's ``completed`` list and notifies for each task id it has not seen
before. Tasks that were already finished when the API started are seeded as
"already notified" and never produce a webhook.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

import requests

__all__ = [
    "DEFAULT_WEBHOOK_TIMEOUT",
    "FINISHED_STATUSES",
    "WEBHOOK_ENV_VAR",
    "TaskFinishedWatcher",
    "WebhookNotifier",
    "build_task_finished_payload",
    "post_task_finished",
    "webhook_url_from_env",
]

logger = logging.getLogger(__name__)

#: Environment variable holding the webhook URL.
WEBHOOK_ENV_VAR = "NOVEL_API_WEBHOOK"

#: Delivery timeout in seconds.
DEFAULT_WEBHOOK_TIMEOUT = 5.0

#: Task statuses that count as terminal.
FINISHED_STATUSES = frozenset({"completed", "cancelled", "failed"})

#: Sender signature: ``(url, payload, timeout) -> delivered``.
Sender = Callable[[str, dict[str, Any], float], bool]


def webhook_url_from_env() -> str | None:
    """Return the configured webhook URL, or ``None`` when disabled."""
    raw = os.environ.get(WEBHOOK_ENV_VAR)
    if raw is None:
        return None
    url = raw.strip()
    return url or None


def build_task_finished_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a serialised task in the webhook envelope."""
    return {"event": "task_finished", "task": dict(task)}


def post_task_finished(
    url: str,
    payload: Mapping[str, Any],
    *,
    timeout: float = DEFAULT_WEBHOOK_TIMEOUT,
) -> bool:
    """
    POST ``payload`` to ``url`` once.

    :return: ``True`` when the endpoint answered with a 2xx status, ``False``
        for any failure. Exceptions are logged instead of propagated.
    """
    try:
        response = requests.post(url, json=dict(payload), timeout=timeout)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        logger.warning("Webhook delivery to %s failed: %s", url, exc)
        return False
    return True


class WebhookNotifier:
    """
    Fire-and-forget webhook sender.

    Every notification starts its own daemon thread, so a slow endpoint cannot
    block the caller or the API event loop. ``wait`` is provided for tests and
    graceful shutdown.

    :param url: Destination URL.
    :param timeout: Per-delivery timeout in seconds.
    :param sender: Optional replacement for :func:`post_task_finished`
        (``(url, payload, timeout) -> bool``); mainly for tests.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_WEBHOOK_TIMEOUT,
        sender: Sender | None = None,
    ) -> None:
        self.url = url
        self.timeout = max(0.1, float(timeout))
        self._sender = sender
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()

    def notify_task_finished(self, task: Mapping[str, Any]) -> threading.Thread:
        """Start a background delivery for ``task`` and return its thread."""
        payload = build_task_finished_payload(task)
        thread = threading.Thread(
            target=self._deliver,
            args=(payload,),
            name="novel-api-webhook",
            daemon=True,
        )
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return thread

    def _deliver(self, payload: dict[str, Any]) -> None:
        """Run one delivery, swallowing every exception."""
        try:
            if self._sender is not None:
                delivered = self._sender(self.url, payload, self.timeout)
            else:
                delivered = post_task_finished(self.url, payload, timeout=self.timeout)
            if not delivered:
                logger.warning(
                    "Webhook %s did not accept event %r",
                    self.url,
                    payload.get("event"),
                )
        except Exception as exc:  # noqa: BLE001 - a sender must not crash us
            logger.warning("Webhook delivery to %s raised: %s", self.url, exc)
        finally:
            current = threading.current_thread()
            with self._lock:
                self._threads.discard(current)

    def wait(self, timeout: float | None = None) -> None:
        """Join all in-flight deliveries (bounded by ``timeout`` each)."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)


def _task_status(task: Any) -> str:
    """Return a task's status value as a plain string."""
    status = getattr(task, "status", None)
    return str(getattr(status, "value", status) or "")


class TaskFinishedWatcher:
    """
    Detect newly finished tasks in successive task-manager snapshots.

    The first call (or an explicit :meth:`seed` at startup) records the tasks
    that are already terminal without notifying them. Every later task id that
    appears in a terminal state is reported exactly once. Ids that leave the
    bounded ``completed`` deque are forgotten, keeping memory flat.
    """

    def __init__(self, notifier: WebhookNotifier) -> None:
        self._notifier = notifier
        self._notified: set[str] = set()
        self._seeded = False

    def seed(self, snapshot: Mapping[str, list[Any]]) -> None:
        """Record the currently finished tasks without notifying them."""
        self._notified = {
            str(task.task_id)
            for task in snapshot.get("completed") or []
            if _task_status(task) in FINISHED_STATUSES
        }
        self._seeded = True

    def poll(
        self,
        snapshot: Mapping[str, list[Any]],
        *,
        serialize: Callable[[Any], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Notify newly finished tasks and return their serialised payloads.
        """
        completed = list(snapshot.get("completed") or [])
        if not self._seeded:
            self.seed(snapshot)
            return []

        finished = [
            task for task in completed if _task_status(task) in FINISHED_STATUSES
        ]
        fired: list[dict[str, Any]] = []
        for task in finished:
            task_id = str(task.task_id)
            if task_id in self._notified:
                continue
            self._notified.add(task_id)
            payload = serialize(task)
            self._notifier.notify_task_finished(payload)
            fired.append(payload)

        # Completed tasks are kept in a bounded deque; forget ids that were
        # evicted so long-running processes do not grow an unbounded set.
        self._notified.intersection_update(str(task.task_id) for task in finished)
        return fired
