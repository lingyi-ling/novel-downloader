#!/usr/bin/env python3
"""
tests.apps.test_embedded_server
-------------------------------

Offline tests for the Android embedded engine: the stdlib HTTP server must
mirror the REST contract the Compose client speaks, without importing
FastAPI or any GUI extras.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.core.models import Status
from novel_downloader.apps.embedded import server as eserver


def _get(path: str) -> tuple[int, dict | bytes]:
    with urllib.request.urlopen(f"http://127.0.0.1:{_port}{path}", timeout=10) as resp:
        raw = resp.read()
    if path.startswith("/downloads/"):
        return 200, raw
    return 200, json.loads(raw.decode("utf-8"))


def _post(path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{_port}{path}",
        data=json.dumps(body or {}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _fake_task(**overrides: Any) -> SimpleNamespace:
    task = SimpleNamespace(
        task_id="t1",
        title="Sample",
        site="qidian",
        book_id="42",
        status=Status.RUNNING,
        chapters_done=3,
        chapters_total=9,
        error=None,
        exported_paths={"txt": "/data/downloads/a.txt"},
        created_at=1_700_000_000.0,
        finished_at=None,
        progress=lambda: 1 / 3,
    )
    for k, v in overrides.items():
        setattr(task, k, v)
    return task


@pytest.fixture()
def running_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Start the embedded server with hermetic fakes and a temp home."""
    home = tmp_path / "embedded_home"

    async def fake_search(keyword, **kwargs):
        return [
            {
                "site": "qidian",
                "book_id": "42",
                "book_url": "https://example.invalid/42",
                "cover_url": "",
                "title": f"{keyword}",
                "author": "author",
                "latest_chapter": "ch1",
                "update_date": "2026-01-01",
                "word_count": "1万",
                "priority": 1,
            }
        ]

    async def fake_add_task(*, title: str, site: str, book_id: str):
        return _fake_task(title=title, site=site, book_id=book_id)

    async def fake_cancel(task_id: str):
        return True

    async def fake_check_all(_store, **_kw):
        from novel_downloader.apps.core.watchlist import CheckResult, WatchEntry

        return [
            CheckResult(
                entry=WatchEntry(site="s", book_id="1", title="B"),
                latest="Chapter 1",
                changed=True,
            ),
        ]

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.add_task", fake_add_task
    )
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.cancel_task", fake_cancel
    )
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot",
        lambda: {
            "running": [_fake_task()],
            "pending": [_fake_task(status=Status.QUEUED, progress=lambda: 0.0)],
            "completed": [_fake_task(status=Status.COMPLETED)],
        },
    )
    monkeypatch.setattr(
        "novel_downloader.apps.core.watchlist.check_all", fake_check_all
    )

    # exported file for the /downloads route
    downloads = home / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "book.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(
        "novel_downloader.apps.common.history_files._output_dir",
        lambda: downloads,
    )
    monkeypatch.setenv("NOVEL_HOME", str(home))

    port = eserver.start_server(str(home), 8765)
    assert port == 8765
    global _port
    _port = 8765
    yield port
    eserver.stop_server()


_port = 0


def test_health_reports_version(running_server) -> None:
    from novel_downloader import __version__

    status, body = _get("/api/v1/health")
    assert status == 200
    assert body == {"status": "ok", "version": __version__}


def test_sites_shape(running_server) -> None:
    _, body = _get("/api/v1/sites")
    assert "search" in body and "download" in body


def test_search_round_trip(running_server) -> None:
    _, body = _post("/api/v1/search", {"keyword": "凡人", "per_site_limit": 2})
    assert body["total"] == 1
    assert body["results"][0]["title"] == "凡人"


def test_tasks_buckets_and_add(running_server) -> None:
    _, snap = _get("/api/v1/tasks")
    assert snap["running"][0]["status"] == "running"
    assert snap["pending"][0]["status"] == "queued"
    assert snap["completed"][0]["finished_at"] is None

    _, added = _post("/api/v1/tasks", {"site": "qidian", "book_id": "42"})
    assert added["task_id"] == "t1"
    assert added["progress"] == pytest.approx(1 / 3)

    _, cancelled = _post("/api/v1/tasks/t1/cancel")
    assert cancelled == {"ok": True}


def test_watchlist_check_with_task_body(running_server) -> None:
    _, body = _post("/api/v1/watchlist/check", {"download": True})
    assert body["checked"] == 1
    assert body["items"][0]["task"]["title"] == "B"


def test_login_requests_shape(running_server, monkeypatch: pytest.MonkeyPatch) -> None:
    from novel_downloader.apps.core import cred_broker

    async def fake_list_requests() -> list[dict[str, Any]]:
        return [
            {
                "req_id": "r1",
                "task_id": "t1",
                "title": "B",
                "fields": [],
                "prefill": {},
                "done": False,
            }
        ]

    monkeypatch.setattr(cred_broker, "list_requests", fake_list_requests)
    _, body = _get("/api/v1/login/requests")
    assert body == {
        "total": 1,
        "requests": [
            {
                "req_id": "r1",
                "task_id": "t1",
                "title": "B",
                "fields": [],
                "prefill": {},
                "done": False,
            }
        ],
    }

    async def fake_complete(_req_id: str, _values: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(cred_broker, "complete_request", fake_complete)
    _, ack = _post("/api/v1/login/requests/r1", {"values": {"cookies": "k=v"}})
    assert ack == {"ok": True}


def test_downloads_serves_exported_file(running_server) -> None:
    status, payload = _get("/downloads/book.txt")
    assert status == 200
    assert payload == b"hello"
    with pytest.raises(urllib.error.HTTPError):
        _get("/downloads/../../etc/passwd")


def test_unknown_route_is_404(running_server) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get("/api/v1/nope")
    assert exc.value.code == 404
