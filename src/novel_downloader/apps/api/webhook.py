#!/usr/bin/env python3
"""
novel_downloader.apps.api.webhook
---------------------------------

Optional "task finished" webhook.

Set ``NOVEL_API_WEBHOOK`` (or pass ``webhook_url`` to :func:`create_app`) to
have the API POST a JSON document to that URL whenever a download task reaches
a terminal state::

    {"event": "task_finished", "task": {...same fields as GET /tasks...}}

Delivery runs on a small pool of daemon threads with a bounded per-attempt
timeout and a finite number of retries. A failing endpoint (connection error,
timeout, non-2xx) is logged and otherwise ignored: it must never slow down or
break the task manager. With no URL configured the API installs no notifier
and performs no polling.

Delivery safety
~~~~~~~~~~~~~~~

* Only ``http`` and ``https`` URLs are accepted.
* ``localhost`` and any host that is (or resolves to) a non-public address are
  rejected before a connection is made. This blocks SSRF against loopback
  services, private networks and cloud metadata endpoints such as
  ``169.254.169.254``. There is no allow-list: private targets must be exposed
  through a public endpoint (or a reverse proxy) instead.
* Redirects are never followed (``allow_redirects=False``); a 3xx answer is
  treated as a failed delivery. This prevents a public endpoint from bouncing
  the POST to ``http://`` or to an internal host.
* Retries are limited to transient failures (connection errors, timeouts,
  ``408``/``425``/``429`` and 5xx). Client errors are not retried.
* Concurrent deliveries are capped by a fixed worker pool; the task watcher
  only enqueues payloads and never blocks on the network.

Payload hygiene
~~~~~~~~~~~~~~~

Before sending, ``task.error`` has embedded URL query strings, userinfo,
``Bearer``/``Authorization``/``token=`` style secrets and local filesystem
paths redacted. ``task.exported_paths`` keeps the field names but reduces each
value to its basename, so the API host's directory layout is not published.
Field names are unchanged.

Optional signing
~~~~~~~~~~~~~~~~

Set ``NOVEL_API_WEBHOOK_SECRET`` to sign the exact JSON body with
HMAC-SHA256. The signature is sent as
``X-Novel-Webhook-Signature: sha256=<hex>``. Receivers can verify it with
:func:`verify_webhook_signature`, which compares digests in constant time.
When the variable is unset, requests are unsigned (the historical behaviour).

Log hygiene
~~~~~~~~~~~

Webhook URLs routinely carry a shared secret in the query string
(``https://hooks.example/push?token=...``). Everything this module logs about
a delivery — the URL and the exception text, which urllib3 renders with the
full path and query — goes through :func:`_redact_url` /
:func:`_redact_text`, mirroring the access-log policy of never recording
query strings or credentials.

The API has no hook into :class:`~novel_downloader.apps.core.task_manager.
TaskManager` (it only observes snapshots), so a small watcher polls the
manager's ``completed`` list and notifies for each task id it has not seen
before. Tasks that were already finished when the API started are seeded as
"already notified" and never produce a webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

__all__ = [
    "DEFAULT_MAX_IN_FLIGHT",
    "DEFAULT_WEBHOOK_TIMEOUT",
    "FINISHED_STATUSES",
    "MAX_WEBHOOK_ATTEMPTS",
    "WEBHOOK_ENV_VAR",
    "WEBHOOK_SECRET_ENV_VAR",
    "TaskFinishedWatcher",
    "WebhookNotifier",
    "build_task_finished_payload",
    "post_task_finished",
    "validate_webhook_url",
    "verify_webhook_signature",
    "webhook_secret_from_env",
    "webhook_url_from_env",
]

logger = logging.getLogger(__name__)

#: Environment variable holding the webhook URL.
WEBHOOK_ENV_VAR = "NOVEL_API_WEBHOOK"

#: Environment variable holding the optional HMAC signing secret.
WEBHOOK_SECRET_ENV_VAR = "NOVEL_API_WEBHOOK_SECRET"

#: Delivery timeout in seconds (per attempt, connect and read).
DEFAULT_WEBHOOK_TIMEOUT = 5.0

#: Total attempts per delivery (the first try plus two retries).
MAX_WEBHOOK_ATTEMPTS = 3

#: Base backoff between retries, multiplied by the attempt number.
_RETRY_BACKOFF_SECONDS = 0.5

#: Maximum number of deliveries performed simultaneously.
DEFAULT_MAX_IN_FLIGHT = 4

#: Task statuses that count as terminal.
FINISHED_STATUSES = frozenset({"completed", "cancelled", "failed"})

#: Sender signature: ``(url, payload, timeout) -> delivered``.
Sender = Callable[[str, dict[str, Any], float], bool]

#: HTTP statuses worth retrying: temporary unavailability / throttling.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})

#: Lower-case schemes accepted for delivery.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Well-known NAT64 prefix; its last 32 bits carry an IPv4 destination.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

#: URL embedded in free-form text (error messages, exception strings).
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")

#: Named secret parameters in a query string / key=value pair.
_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|api[_-]?key|access[_-]?token|secret|password|"
    r"signature|key)=)[^&\s\"'#]+"
)

#: ``Authorization: <value>`` and similar headers.
_AUTH_RE = re.compile(r"(?i)(\bauthorization\s*[:=]\s*)([^\s,;\"']+)")

#: ``Bearer <token>`` credentials.
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]+)")

#: Absolute Windows paths (``C:\Users\...``).
_WINDOWS_PATH_RE = re.compile(r"(?<![\w:/])[A-Za-z]:[\\/][^\s\"'<>|]+")

#: Absolute POSIX paths below commonly used home/scratch roots.
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:/])/(?:home|Users|tmp|var|opt|root|private|data|mnt|media)/"
    r"[^\s\"'<>]+"
)


def webhook_url_from_env() -> str | None:
    """Return the configured webhook URL, or ``None`` when disabled."""
    raw = os.environ.get(WEBHOOK_ENV_VAR)
    if raw is None:
        return None
    url = raw.strip()
    return url or None


def webhook_secret_from_env() -> str | None:
    """Return the optional signing secret, or ``None`` when unsigned."""
    raw = os.environ.get(WEBHOOK_SECRET_ENV_VAR)
    if raw is None:
        return None
    secret = raw.strip()
    return secret or None


def _redact_url(url: str) -> str:
    """
    Reduce ``url`` to ``scheme://host[:port]/path`` for logging.

    Userinfo (basic-auth credentials) and the query string (shared tokens)
    are dropped; a URL without credentials is returned unchanged apart from
    that. Unparseable values collapse to a fixed placeholder.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            return "<unparseable webhook url>"
        port: int | None
        try:
            port = parts.port
        except ValueError:  # malformed port in the URL
            port = None
        port_part = f":{port}" if port is not None else ""
        return f"{parts.scheme}://{host}{port_part}{parts.path}"
    except ValueError:
        return "<unparseable webhook url>"


def _redact_embedded_url(match: re.Match[str]) -> str:
    """Strip credentials/query from a URL found inside free-form text."""
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if not parts.query and "@" not in parts.netloc:
        return raw
    return _redact_url(raw)


def _redact_secrets(text: str) -> str:
    """Replace named secret values while keeping the surrounding key."""
    cleaned = _BEARER_RE.sub(r"\1<redacted>", text)
    cleaned = _AUTH_RE.sub(r"\1<redacted>", cleaned)
    return _SECRET_RE.sub(r"\1<redacted>", cleaned)


def _redact_local_paths(text: str) -> str:
    """Replace absolute local paths embedded in ``text``."""
    cleaned = _WINDOWS_PATH_RE.sub("<redacted-path>", text)
    return _POSIX_PATH_RE.sub("<redacted-path>", cleaned)


def _redact_text(text: str, url: str) -> str:
    """
    Scrub ``url``'s credentials (userinfo, query string) from ``text``.

    urllib3 renders connection errors with the request's full path and query
    (``... with url: /push?token=... ``), so the exception message is redacted
    with the URL it came from before it reaches the log. Other URLs embedded in
    the text, named secrets and local absolute paths are scrubbed as well.
    """
    cleaned = text
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is not None:
        if parts.query:
            cleaned = cleaned.replace(f"?{parts.query}", "?<redacted>")
            cleaned = cleaned.replace(parts.query, "<redacted>")
        if "@" in parts.netloc:
            userinfo, _, _ = parts.netloc.rpartition("@")
            if userinfo:
                cleaned = cleaned.replace(f"{userinfo}@", "<redacted>@")
                cleaned = cleaned.replace(userinfo, "<redacted>")
    cleaned = _URL_RE.sub(_redact_embedded_url, cleaned)
    cleaned = _redact_secrets(cleaned)
    return _redact_local_paths(cleaned)


def _ipv4_embedding(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address embedded in an IPv6 form, if any."""
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64_PREFIX:
        return ipaddress.IPv4Address(address.packed[-4:])
    # Deprecated IPv4-compatible form (``::127.0.0.1``): the first 96 bits are
    # zero and the last 32 bits are not, which some stacks still route to IPv4.
    if address.packed[:12] == b"\x00" * 12 and address.packed[12:] != b"\x00" * 4:
        return ipaddress.IPv4Address(address.packed[-4:])
    return None


def _is_public_address(value: str) -> bool:
    """Whether ``value`` is a globally routable unicast IP address."""
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    embedded = _ipv4_embedding(address)
    if embedded is not None:
        address = embedded
    return bool(address.is_global) and not address.is_multicast


def _resolve_host(host: str) -> list[str]:
    """
    Resolve ``host`` to its address strings (empty list on failure).

    Kept separate so tests can replace DNS with a deterministic result.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return []
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def validate_webhook_url(url: str, *, resolve: bool = True) -> str | None:
    """
    Validate a webhook destination for SSRF safety.

    :return: ``None`` when ``url`` is an ``http(s)`` URL pointing at a public
        unicast address; otherwise a short human-readable reason. The reason
        never contains the URL's credentials or query string.
    """
    candidate = url.strip()
    if not candidate:
        return "URL is empty"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        return "URL contains control characters"
    if any(char.isspace() for char in candidate):
        return "URL contains whitespace"
    if "\\" in candidate:
        # Raw backslashes are invalid in a URL and parsers disagree about
        # whether they separate the host; reject the ambiguity outright.
        return "URL contains backslashes"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "URL is malformed"
    if parts.scheme not in _ALLOWED_SCHEMES:
        return "scheme must be http or https"
    host = parts.hostname
    if not host:
        return "URL has no host"
    try:
        _ = parts.port
    except ValueError:
        return "URL has an invalid port"

    normalised = host.rstrip(".").lower()
    if normalised == "localhost" or normalised.endswith(".localhost"):
        return "localhost targets are not allowed"
    try:
        address = ipaddress.ip_address(normalised.split("%", 1)[0])
    except ValueError:
        if not resolve:
            return None
        addresses = _resolve_host(normalised)
        if not addresses:
            return "host could not be resolved"
        if any(not _is_public_address(item) for item in addresses):
            return "host resolves to a non-public address"
        return None
    if not _is_public_address(str(address)):
        return "non-public addresses are not allowed"
    return None


def _basename(value: str) -> str:
    """Return the final path component, for both ``/`` and ``\\`` separators."""
    trimmed = value.rstrip("\\/")
    if not trimmed:
        return ""
    return re.split(r"[\\/]", trimmed)[-1]


def _sanitize_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Copy ``task`` with local paths and secrets redacted."""
    sanitized = dict(task)
    error = sanitized.get("error")
    if isinstance(error, str):
        sanitized["error"] = _redact_text(error, "")
    exported = sanitized.get("exported_paths")
    if isinstance(exported, Mapping):
        sanitized["exported_paths"] = {
            str(key): _redact_text(_basename(value), "")
            if isinstance(value, str)
            else value
            for key, value in exported.items()
        }
    return sanitized


def build_task_finished_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a serialised task in the webhook envelope."""
    return {"event": "task_finished", "task": _sanitize_task(task)}


def _encode_payload(payload: Mapping[str, Any]) -> bytes:
    """Serialise a payload to the exact bytes covered by the signature."""
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _signature_header(body: bytes, secret: str) -> str:
    """Return the ``sha256=<hex>`` header value for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(body: bytes, header: str | None, secret: str) -> bool:
    """
    Verify an ``X-Novel-Webhook-Signature`` header against ``body``.

    The comparison uses :func:`hmac.compare_digest` so it is constant-time.
    Receivers should call this with the raw request body, before parsing JSON.
    """
    if not header or not secret:
        return False
    algorithm, separator, value = header.partition("=")
    if not separator or algorithm.strip().lower() != "sha256" or not value:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(value.strip().lower(), expected)
    except TypeError:  # non-ASCII header value cannot be a valid digest
        return False


def _is_retryable_status(status: int) -> bool:
    """Whether an HTTP status is a transient failure worth retrying."""
    return status in _RETRYABLE_STATUSES or 500 <= status <= 599


def _retry_sleep(seconds: float) -> None:
    """Sleep between retries; separated out so tests can run instantly."""
    if seconds > 0:
        time.sleep(seconds)


def post_task_finished(
    url: str,
    payload: Mapping[str, Any],
    *,
    timeout: float = DEFAULT_WEBHOOK_TIMEOUT,
    secret: str | None = None,
    attempts: int = MAX_WEBHOOK_ATTEMPTS,
) -> bool:
    """
    POST ``payload`` to ``url`` with validation, redirect protection and retries.

    :param secret: Optional HMAC-SHA256 key. When set, the exact serialised
        body is signed and sent with ``X-Novel-Webhook-Signature``.
    :param attempts: Maximum number of tries for transient failures.
    :return: ``True`` when the endpoint answered with a 2xx status, ``False``
        for any failure. Exceptions are logged instead of propagated.
    """
    problem = validate_webhook_url(url)
    if problem is not None:
        logger.warning("Webhook URL rejected (%s): %s", problem, _redact_url(url))
        return False

    attempts = max(1, int(attempts))
    body: bytes | None = None
    headers: dict[str, str] = {}
    try:
        if secret:
            body = _encode_payload(payload)
            headers = {
                "Content-Type": "application/json",
                "X-Novel-Webhook-Signature": _signature_header(body, secret),
            }
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        logger.warning(
            "Webhook payload for %s could not be serialised: %s",
            _redact_url(url),
            _redact_text(str(exc), url),
        )
        return False

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            request_kwargs: dict[str, Any] = {
                "timeout": timeout,
                # A redirect could cross a host/scheme boundary (for example a
                # public endpoint bouncing to 127.0.0.1); never follow one.
                "allow_redirects": False,
            }
            if body is not None:
                request_kwargs["data"] = body
                request_kwargs["headers"] = headers
            else:
                request_kwargs["json"] = dict(payload)
            response = requests.post(url, **request_kwargs)
            status = int(getattr(response, "status_code", 200) or 200)
            if 300 <= status < 400:
                logger.warning(
                    "Webhook delivery to %s was redirected (status %s); "
                    "redirects are not followed",
                    _redact_url(url),
                    status,
                )
                return False
            response.raise_for_status()
            return True
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = str(exc)
        except requests.HTTPError as exc:
            last_error = str(exc)
            http_response = getattr(exc, "response", None)
            status_code = getattr(http_response, "status_code", None)
            if status_code is None or not _is_retryable_status(int(status_code)):
                logger.warning(
                    "Webhook delivery to %s failed: %s",
                    _redact_url(url),
                    _redact_text(last_error, url),
                )
                return False
        except Exception as exc:  # noqa: BLE001 - delivery must never raise
            logger.warning(
                "Webhook delivery to %s failed: %s",
                _redact_url(url),
                _redact_text(str(exc), url),
            )
            return False

        if attempt >= attempts:
            break
        _retry_sleep(_RETRY_BACKOFF_SECONDS * attempt)

    logger.warning(
        "Webhook delivery to %s failed after %d attempt(s): %s",
        _redact_url(url),
        attempts,
        _redact_text(last_error, url),
    )
    return False


class WebhookNotifier:
    """
    Fire-and-forget webhook sender backed by a bounded daemon worker pool.

    Notifications are queued and delivered by at most ``max_in_flight``
    workers, so a slow endpoint cannot block the caller or the API event loop
    and cannot spawn an unbounded number of threads. Workers start lazily on
    the first notification: an unused notifier owns no threads.

    :param url: Destination URL.
    :param timeout: Per-attempt delivery timeout in seconds.
    :param sender: Optional replacement for :func:`post_task_finished`
        (``(url, payload, timeout) -> bool``); mainly for tests.
    :param max_in_flight: Maximum number of concurrent deliveries (at least 1).
    :param secret: Optional HMAC signing key; defaults to
        :data:`NOVEL_API_WEBHOOK_SECRET`, then unsigned.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_WEBHOOK_TIMEOUT,
        sender: Sender | None = None,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
        secret: str | None = None,
    ) -> None:
        self.url = url
        self.timeout = max(0.1, float(timeout))
        self._sender = sender
        self._secret = secret if secret is not None else webhook_secret_from_env()
        self._max_in_flight = max(1, int(max_in_flight))
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._condition = threading.Condition()
        self._pending = 0
        self._workers: list[threading.Thread] = []
        self._start_lock = threading.Lock()
        self._closed = False
        # Fail fast for statically invalid targets (bad scheme, literal
        # loopback/private IP, localhost) without doing DNS at startup; the
        # full check still runs before every delivery.
        problem = validate_webhook_url(url, resolve=False)
        if problem is not None:
            logger.warning(
                "Webhook URL is not a valid public http(s) target (%s): %s",
                problem,
                _redact_url(url),
            )

    def _start_workers_locked(self) -> None:
        """Start the fixed worker pool; caller holds ``_start_lock``."""
        for index in range(self._max_in_flight):
            worker = threading.Thread(
                target=self._worker,
                name=f"novel-api-webhook-{index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def notify_task_finished(self, task: Mapping[str, Any]) -> None:
        """Enqueue a background delivery for ``task``."""
        payload = build_task_finished_payload(task)
        with self._condition:
            if self._closed:
                logger.warning(
                    "Webhook notifier for %s is closed; dropping event %r",
                    _redact_url(self.url),
                    payload.get("event"),
                )
                return
            self._pending += 1
        with self._start_lock:
            # Re-check under the same lock ``close`` uses before enqueueing
            # sentinels, so a concurrent close cannot strand this payload.
            if self._closed:
                with self._condition:
                    self._pending -= 1
                    self._condition.notify_all()
                logger.warning(
                    "Webhook notifier for %s is closed; dropping event %r",
                    _redact_url(self.url),
                    payload.get("event"),
                )
                return
            if not self._workers:
                self._start_workers_locked()
            try:
                self._queue.put(payload)
            except Exception:  # noqa: BLE001 - keep the pending count honest
                with self._condition:
                    self._pending -= 1
                    self._condition.notify_all()
                raise

    def _worker(self) -> None:
        """Deliver queued payloads until closed."""
        while True:
            payload = self._queue.get()
            try:
                if payload is None:
                    return
                self._deliver(payload)
            finally:
                self._queue.task_done()
                if payload is not None:
                    with self._condition:
                        self._pending -= 1
                        self._condition.notify_all()

    def _deliver(self, payload: dict[str, Any]) -> None:
        """Run one delivery, swallowing every exception."""
        try:
            if self._sender is not None:
                delivered = self._sender(self.url, payload, self.timeout)
            else:
                delivered = post_task_finished(
                    self.url,
                    payload,
                    timeout=self.timeout,
                    secret=self._secret,
                )
            if not delivered:
                logger.warning(
                    "Webhook %s did not accept event %r",
                    _redact_url(self.url),
                    payload.get("event"),
                )
        except Exception as exc:  # noqa: BLE001 - a sender must not crash us
            logger.warning(
                "Webhook delivery to %s raised: %s",
                _redact_url(self.url),
                _redact_text(str(exc), self.url),
            )

    def wait(self, timeout: float | None = None) -> None:
        """Wait until all queued deliveries finish, bounded by ``timeout``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return
                self._condition.wait(remaining)

    def close(self, timeout: float | None = None) -> None:
        """Stop the workers after the current queue drains (or ``timeout``)."""
        with self._start_lock:
            if self._closed:
                workers: list[threading.Thread] = []
            else:
                self._closed = True
                workers = list(self._workers)
                # Enqueue under the same lock ``notify_task_finished`` uses so
                # every payload accepted before the close is ahead of a
                # sentinel and gets drained.
                for _ in workers:
                    self._queue.put(None)
        if not workers:
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        for worker in workers:
            remaining = None if deadline is None else deadline - time.monotonic()
            worker.join(remaining)


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
