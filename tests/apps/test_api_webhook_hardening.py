"""Hardening tests for the optional task-finished webhook.

Covers the delivery path that the API exposes through ``NOVEL_API_WEBHOOK``:
SSRF restrictions, redirect handling, bounded retries, concurrency limits,
payload redaction and optional HMAC signing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Any

import pytest
import requests

from novel_downloader.apps.api import webhook as api_webhook
from novel_downloader.apps.api.webhook import (
    WebhookNotifier,
    build_task_finished_payload,
    post_task_finished,
)

#: A public unicast literal, so URL validation never performs a DNS lookup.
PUBLIC_URL = "https://93.184.216.34/hook"


class _Response:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code: int = 204) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"status {self.status_code}",
                response=self,  # type: ignore[arg-type]
            )


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry tests fast; the retry count is what matters."""
    monkeypatch.setattr(
        api_webhook, "_retry_sleep", lambda _seconds: None, raising=False
    )


# ---------------------------------------------------------------------------
# SSRF: only http(s) to public addresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/hook",
        "ftp://example.com/hook",
        "gopher://example.com/hook",
        "http://localhost/hook",
        "http://localhost./hook",
        "http://app.localhost/hook",
        "http://127.0.0.1/hook",
        "http://127.10.20.30/hook",
        "http://10.1.2.3/hook",
        "http://172.16.0.1/hook",
        "http://192.168.0.1/hook",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://100.64.0.1/hook",
        "http://0.0.0.0/hook",
        "http://[::1]/hook",
        "http://[::127.0.0.1]/hook",
        "http://[64:ff9b::127.0.0.1]/hook",
        "http://[fd00:ec2::254]/hook",
        "http://[fe80::1]/hook",
        "http://127.0.0.1\\@evil.com/hook",
        "http://exa mple.com/hook",
    ],
)
def test_post_rejects_non_public_targets(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        api_webhook.requests,
        "post",
        lambda target, **_kwargs: called.append(target) or _Response(),
    )

    assert post_task_finished(url, {"event": "task_finished"}) is False
    assert called == []


def test_post_rejects_hostname_resolving_to_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_webhook, "_resolve_host", lambda _host: ["10.0.0.7"])

    assert post_task_finished("https://internal.example/hook", {}) is False


def test_post_rejects_hostname_resolving_to_metadata_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_webhook, "_resolve_host", lambda _host: ["169.254.169.254"])

    assert post_task_finished("https://metadata.example/hook", {}) is False


def test_post_accepts_public_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_webhook, "_resolve_host", lambda _host: ["93.184.216.34"])
    posted: list[str] = []

    def fake_post(url: str, **_kwargs: Any) -> _Response:
        posted.append(url)
        return _Response(204)

    monkeypatch.setattr(api_webhook.requests, "post", fake_post)

    assert post_task_finished("https://hooks.example/push", {"event": "x"}) is True
    assert posted == ["https://hooks.example/push"]


# ---------------------------------------------------------------------------
# Redirects and retries
# ---------------------------------------------------------------------------


def test_post_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def redirect_post(url: str, **kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(307)

    monkeypatch.setattr(api_webhook.requests, "post", redirect_post)

    assert post_task_finished(PUBLIC_URL, {"event": "task_finished"}) is False
    assert captured["allow_redirects"] is False


def test_post_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def flaky_post(url: str, **_kwargs: Any) -> _Response:
        calls.append(len(calls) + 1)
        if len(calls) < api_webhook.MAX_WEBHOOK_ATTEMPTS:
            raise requests.ConnectionError("temporarily down")
        return _Response(204)

    monkeypatch.setattr(api_webhook.requests, "post", flaky_post)

    assert post_task_finished(PUBLIC_URL, {"event": "task_finished"}) is True
    assert len(calls) == api_webhook.MAX_WEBHOOK_ATTEMPTS


def test_post_stops_after_bounded_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def always_down(url: str, **_kwargs: Any) -> _Response:
        calls.append(1)
        raise requests.Timeout("slow endpoint")

    monkeypatch.setattr(api_webhook.requests, "post", always_down)

    assert post_task_finished(PUBLIC_URL, {"event": "task_finished"}) is False
    assert len(calls) == api_webhook.MAX_WEBHOOK_ATTEMPTS


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_post_retries_transient_http_status(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls: list[int] = []

    def status_post(url: str, **_kwargs: Any) -> _Response:
        calls.append(1)
        if len(calls) < 2:
            return _Response(status)
        return _Response(204)

    monkeypatch.setattr(api_webhook.requests, "post", status_post)

    assert post_task_finished(PUBLIC_URL, {"event": "task_finished"}) is True
    assert len(calls) == 2


def test_post_does_not_retry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def bad_request(url: str, **_kwargs: Any) -> _Response:
        calls.append(1)
        return _Response(400)

    monkeypatch.setattr(api_webhook.requests, "post", bad_request)

    assert post_task_finished(PUBLIC_URL, {"event": "task_finished"}) is False
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------


def test_notifier_bounds_concurrent_deliveries() -> None:
    lock = threading.Lock()
    started = threading.Semaphore(0)
    release = threading.Event()
    active = 0
    peak = 0

    def sender(url: str, payload: dict[str, Any], timeout: float) -> bool:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        started.release()
        release.wait(5.0)
        with lock:
            active -= 1
        return True

    notifier = WebhookNotifier(PUBLIC_URL, sender=sender, max_in_flight=2)
    for index in range(6):
        notifier.notify_task_finished({"task_id": str(index)})

    assert started.acquire(timeout=2.0)
    assert started.acquire(timeout=2.0)
    time.sleep(0.1)
    assert peak <= 2

    release.set()
    notifier.wait(timeout=5.0)
    assert peak == 2


def test_notifier_creates_no_threads_before_first_event() -> None:
    before = threading.active_count()
    notifier = WebhookNotifier(PUBLIC_URL, sender=lambda *_: True)
    assert threading.active_count() == before
    assert notifier.wait(timeout=0.01) is None


# ---------------------------------------------------------------------------
# Payload redaction
# ---------------------------------------------------------------------------


def test_payload_keeps_field_names_and_redacts_paths_and_tokens() -> None:
    task = {
        "task_id": "t1",
        "title": "Demo",
        "status": "failed",
        "error": (
            "POST https://api.example/path?token=SECRET failed while writing "
            r"C:\Users\alice\output\book.epub"
        ),
        "exported_paths": {
            "epub": r"C:\Users\alice\output\book.epub",
            "txt": "/home/alice/output/book.txt",
        },
    }

    payload = build_task_finished_payload(task)

    assert payload["event"] == "task_finished"
    assert set(payload["task"]) == set(task)
    assert payload["task"]["exported_paths"] == {
        "epub": "book.epub",
        "txt": "book.txt",
    }
    assert "SECRET" not in payload["task"]["error"]
    assert "alice" not in payload["task"]["error"]
    assert r"C:\Users" not in payload["task"]["error"]
    # The caller's task mapping is not mutated.
    assert task["exported_paths"]["epub"].startswith("C:")


def test_payload_redacts_authorization_style_secrets() -> None:
    payload = build_task_finished_payload(
        {"task_id": "t1", "error": "HTTP 401 (Authorization: Bearer deadbeef)"}
    )

    assert "deadbeef" not in payload["task"]["error"]
    assert "Authorization" in payload["task"]["error"]


# ---------------------------------------------------------------------------
# Optional HMAC signature
# ---------------------------------------------------------------------------


def test_signature_covers_exact_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def signing_post(url: str, **kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(204)

    monkeypatch.setattr(api_webhook.requests, "post", signing_post)

    payload = {"event": "task_finished", "task": {"task_id": "1"}}
    assert (
        post_task_finished(PUBLIC_URL, payload, secret="s3cret")  # noqa: S106
        is True
    )

    body = captured["data"]
    assert isinstance(body, bytes)
    assert "json" not in captured
    assert json.loads(body) == payload

    header = captured["headers"]["X-Novel-Webhook-Signature"]
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert header == f"sha256={expected}"
    assert api_webhook.verify_webhook_signature(body, header, "s3cret")
    assert not api_webhook.verify_webhook_signature(body + b" ", header, "s3cret")
    assert not api_webhook.verify_webhook_signature(body, header, "wrong")


def test_notifier_signs_with_secret_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVEL_API_WEBHOOK_SECRET", "env-secret")
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(204)

    monkeypatch.setattr(api_webhook.requests, "post", fake_post)

    notifier = WebhookNotifier(PUBLIC_URL)
    notifier.notify_task_finished({"task_id": "1"})
    notifier.wait(timeout=5.0)

    header = captured["headers"]["X-Novel-Webhook-Signature"]
    assert api_webhook.verify_webhook_signature(captured["data"], header, "env-secret")
