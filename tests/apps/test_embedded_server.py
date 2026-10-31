#!/usr/bin/env python3
"""
tests.apps.test_embedded_server
-------------------------------

Offline tests for the Android embedded engine: the stdlib HTTP server must
mirror the REST contract the Compose client speaks, without importing
FastAPI or any GUI extras.

Each contract assertion mirrors ``apps/api/routes.py`` (field names, ``null``
vs ``""``, pagination) and the error bodies of ``apps/api/middleware.py``
(``{"detail": ...}``, 422 with an ``errors`` list, opaque 500, 404/405/409).
HTTP-level tests cover keep-alive body draining, HEAD/OPTIONS, the request
concurrency cap, the request-body ceiling and the start/stop lifecycle.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.core.models import Status
from novel_downloader.apps.embedded import server as eserver

_port = 0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _call(
    method: str,
    path: str,
    body: Any = None,
    port: int | None = None,
) -> tuple[int, Any]:
    """One request; HTTP errors come back as ``(status, parsed body)``."""
    target = _port if port is None else port
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{target}{path}", data=data, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as err:
        raw = err.read()
        status = err.code
    if path.startswith("/downloads/"):
        return status, raw
    return status, json.loads(raw.decode("utf-8"))


def _get(path: str, port: int | None = None) -> tuple[int, Any]:
    return _call("GET", path, port=port)


def _post(path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    return _call("POST", path, body=body)


def _delete(path: str) -> tuple[int, Any]:
    return _call("DELETE", path)


def _raw_exchange(port: int, raw: bytes, wait: float = 1.0) -> bytes:
    """Send raw bytes on a fresh socket and collect the server's reply."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw)
        sock.settimeout(wait)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def _downloads_dir(tmp_path: Path) -> Path:
    return tmp_path / "embedded_home" / "downloads"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


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
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


@pytest.fixture()
def running_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Start the embedded server with hermetic fakes and a temp home."""
    home = tmp_path / "embedded_home"
    calls: dict[str, list[dict[str, Any]]] = {"search": [], "add": []}

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls["search"].append(kwargs)
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

    async def fake_add_task(*, title: str, site: str, book_id: str) -> SimpleNamespace:
        calls["add"].append({"title": title, "site": site, "book_id": book_id})
        return _fake_task(title=title, site=site, book_id=book_id)

    async def fake_cancel(task_id: str) -> bool:
        return True

    async def fake_check_all(_store: Any, **_kw: Any) -> list[Any]:
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
    yield SimpleNamespace(port=port, home=home, calls=calls)
    eserver.stop_server()


# ---------------------------------------------------------------------------
# contract: system endpoints
# ---------------------------------------------------------------------------


def test_health_reports_version(running_server: SimpleNamespace) -> None:
    from novel_downloader import __version__

    status, body = _get("/api/v1/health")
    assert status == 200
    assert body == {"status": "ok", "version": __version__}


def test_sites_shape(running_server: SimpleNamespace) -> None:
    _, body = _get("/api/v1/sites")
    assert "search" in body and "download" in body


def test_start_server_is_idempotent(running_server: SimpleNamespace) -> None:
    assert eserver.start_server("/ignored-when-running", 9999) == _port
    status, _ = _get("/api/v1/health")
    assert status == 200


# ---------------------------------------------------------------------------
# contract: search
# ---------------------------------------------------------------------------


def test_search_round_trip(running_server: SimpleNamespace) -> None:
    _, body = _post("/api/v1/search", {"keyword": "凡人", "per_site_limit": 2})
    assert body["total"] == 1
    assert body["results"][0]["title"] == "凡人"


def test_search_defaults_mirror_api(running_server: SimpleNamespace) -> None:
    status, body = _post("/api/v1/search", {"keyword": "凡人"})
    assert status == 200
    # exactly the REST response shape: no extra "elapsed" field
    assert set(body) == {"total", "results"}
    kwargs = running_server.calls["search"][-1]
    assert kwargs["per_site_limit"] == 5
    assert kwargs["timeout"] == 5.0
    assert kwargs["sort"] == "quality"
    assert kwargs["order"] is None
    assert kwargs["limit"] is None
    assert kwargs["sites"] is None


def test_search_empty_sites_list_searches_none(running_server: SimpleNamespace) -> None:
    status, _ = _post("/api/v1/search", {"keyword": "x", "sites": []})
    assert status == 200
    assert running_server.calls["search"][-1]["sites"] == []


def test_search_validation_422(running_server: SimpleNamespace) -> None:
    cases: list[dict[str, Any]] = [
        {},
        {"keyword": ""},
        {"keyword": "x", "sort": "bogus"},
        {"keyword": "x", "order": "bogus"},
        {"keyword": "x", "sites": "qidian"},
        {"keyword": "x", "per_site_limit": 0},
        {"keyword": "x", "limit": 0},
    ]
    for payload in cases:
        status, body = _post("/api/v1/search", payload)
        assert status == 422, (payload, body)
        assert body["detail"].startswith("Request validation failed")
        assert body["errors"]
        assert body["errors"][0]["loc"][0] == "body"


def test_search_rejects_non_object_body(running_server: SimpleNamespace) -> None:
    status, body = _call("POST", "/api/v1/search", [1, 2, 3])
    assert status == 422
    assert "valid dictionary" in body["detail"]


def test_search_rejects_malformed_json(running_server: SimpleNamespace) -> None:
    raw = (
        b"POST /api/v1/search HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 8\r\n"
        b"\r\n{not jsn"
    )
    data = _raw_exchange(_port, raw)
    assert data.startswith(b"HTTP/1.1 422")
    assert b"JSON decode error" in data


def test_search_timeout_returns_504(
    running_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow_search(keyword: str, **_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(1.0)
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", slow_search)
    monkeypatch.setattr(eserver, "_SEARCH_TIMEOUT", 0.2, raising=False)
    status, body = _post("/api/v1/search", {"keyword": "x"})
    assert status == 504
    assert body == {"detail": "Search timed out after 0.2s"}


# ---------------------------------------------------------------------------
# contract: tasks
# ---------------------------------------------------------------------------


def test_tasks_buckets_and_add(running_server: SimpleNamespace) -> None:
    _, snap = _get("/api/v1/tasks")
    assert snap["running"][0]["status"] == "running"
    assert snap["pending"][0]["status"] == "queued"
    assert snap["completed"][0]["finished_at"] is None

    _, added = _post("/api/v1/tasks", {"site": "qidian", "book_id": "42"})
    assert added["task_id"] == "t1"
    assert added["progress"] == pytest.approx(1 / 3)

    _, cancelled = _post("/api/v1/tasks/t1/cancel")
    assert cancelled == {"ok": True}


def test_tasks_response_shape_and_pagination(running_server: SimpleNamespace) -> None:
    _, snap = _get("/api/v1/tasks")
    assert (snap["total"], snap["offset"], snap["limit"]) == (3, 0, 3)
    task = snap["pending"][0]
    # task dicts mirror routes.task_to_dict, including export_stage and a
    # nullable error (never "" standing in for null)
    assert task["error"] is None
    assert task["export_stage"] is None
    assert set(task) == {
        "task_id",
        "title",
        "site",
        "book_id",
        "status",
        "chapters_done",
        "chapters_total",
        "progress",
        "error",
        "exported_paths",
        "created_at",
        "finished_at",
        "export_stage",
    }

    _, page = _get("/api/v1/tasks?limit=1")
    assert (page["total"], page["offset"], page["limit"]) == (3, 0, 1)
    assert [len(page[g]) for g in ("pending", "running", "completed")] == [1, 0, 0]

    _, tail = _get("/api/v1/tasks?offset=2")
    assert (tail["total"], tail["offset"], tail["limit"]) == (3, 2, 50)
    assert [len(tail[g]) for g in ("pending", "running", "completed")] == [0, 0, 1]


def test_tasks_invalid_pagination_422(running_server: SimpleNamespace) -> None:
    for bad in ("limit=0", "limit=999", "limit=abc", "offset=-1"):
        status, body = _get(f"/api/v1/tasks?{bad}")
        assert status == 422, (bad, body)
        assert body["detail"].startswith("Request validation failed")
        assert body["errors"][0]["loc"] == ["query", bad.split("=")[0]]


def test_tasks_serialises_error_and_export_stage(
    running_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot",
        lambda: {
            "pending": [_fake_task(error="boom", export_stage="txt")],
            "running": [],
            "completed": [],
        },
    )
    _, snap = _get("/api/v1/tasks")
    assert snap["pending"][0]["error"] == "boom"
    assert snap["pending"][0]["export_stage"] == "txt"


def test_get_single_task(running_server: SimpleNamespace) -> None:
    status, task = _get("/api/v1/tasks/t1")
    assert status == 200
    assert task["task_id"] == "t1"

    status, body = _get("/api/v1/tasks/zzz")
    assert status == 404
    assert body == {"detail": "Task not found: zzz"}


def test_add_task_validation_and_default_title(
    running_server: SimpleNamespace,
) -> None:
    status, body = _post("/api/v1/tasks", {})
    assert status == 422
    assert body["detail"] == (
        "Request validation failed: site: Field required; book_id: Field required"
    )
    assert body["errors"][0]["loc"] == ["body", "site"]

    status, body = _post("/api/v1/tasks", {"site": ""})
    assert status == 422
    assert "String should have at least 1 character" in body["detail"]

    status, added = _post("/api/v1/tasks", {"site": "qidian", "book_id": "42"})
    assert status == 200
    # routes.create_task default title: f"{site} (id = {book_id})"
    assert added["title"] == "qidian (id = 42)"


def test_cancel_unknown_task_404(
    running_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def not_found(task_id: str) -> bool:
        return False

    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.cancel_task", not_found
    )
    status, body = _post("/api/v1/tasks/zzz/cancel")
    assert status == 404
    assert body == {"detail": "Task not found: zzz"}


# ---------------------------------------------------------------------------
# contract: login requests
# ---------------------------------------------------------------------------


def test_login_requests_shape(
    running_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    async def fake_state(_req_id: str) -> tuple[bool, bool]:
        return True, False

    async def fake_complete(_req_id: str, _values: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(cred_broker, "get_req_state", fake_state)
    monkeypatch.setattr(cred_broker, "complete_request", fake_complete)
    _, ack = _post("/api/v1/login/requests/r1", {"values": {"cookies": "k=v"}})
    assert ack == {"ok": True}


def test_login_submit_unknown_404_conflict_409(
    running_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_downloader.apps.core import cred_broker

    async def fake_state(req_id: str) -> tuple[bool, bool]:
        if req_id == "gone":
            return False, False
        return True, True

    monkeypatch.setattr(cred_broker, "get_req_state", fake_state)

    status, body = _post("/api/v1/login/requests/gone", {"values": {}})
    assert status == 404
    assert body == {"detail": "Login request not found: gone"}

    status, body = _post("/api/v1/login/requests/r1", {"values": {}})
    assert status == 409
    assert body == {"detail": "Login request already completed: r1"}


# ---------------------------------------------------------------------------
# contract: history and downloads
# ---------------------------------------------------------------------------


def test_history_pagination(running_server: SimpleNamespace, tmp_path: Path) -> None:
    downloads = _downloads_dir(tmp_path)
    for index, name in enumerate(("a.txt", "b.txt", "c.txt")):
        target = downloads / name
        target.write_text("x", encoding="utf-8")
        stamp = 1_000_000 + index
        os.utime(target, (stamp, stamp))

    _, body = _get("/api/v1/history")
    assert (body["total"], body["offset"], body["limit"]) == (4, 0, 4)
    assert [f["name"] for f in body["files"]] == ["book.txt", "c.txt", "b.txt", "a.txt"]

    _, page = _get("/api/v1/history?limit=2")
    assert (page["total"], page["offset"], page["limit"]) == (4, 0, 2)
    assert [f["name"] for f in page["files"]] == ["book.txt", "c.txt"]

    _, tail = _get("/api/v1/history?offset=3")
    assert [f["name"] for f in tail["files"]] == ["a.txt"]

    status, bad = _get("/api/v1/history?limit=0")
    assert status == 422
    assert bad["errors"][0]["loc"] == ["query", "limit"]


def test_downloads_serves_exported_file(running_server: SimpleNamespace) -> None:
    status, payload = _get("/downloads/book.txt")
    assert status == 200
    assert payload == b"hello"
    status, _ = _get("/downloads/../../etc/passwd")
    assert status == 404
    status, missing = _get("/downloads/missing.txt")
    assert status == 404
    assert json.loads(missing) == {"detail": "Not Found"}


def test_downloads_serves_percent_encoded_name(
    running_server: SimpleNamespace, tmp_path: Path
) -> None:
    downloads = _downloads_dir(tmp_path)
    (downloads / "凡人 修仙传.txt").write_text("中文内容", encoding="utf-8")

    _, history = _get("/api/v1/history")
    url = next(f["download_url"] for f in history["files"] if "凡人" in f["name"])
    status, payload = _get(url)
    assert status == 200
    assert payload == "中文内容".encode()


# ---------------------------------------------------------------------------
# contract: watchlist
# ---------------------------------------------------------------------------


def test_watchlist_crud_shapes(running_server: SimpleNamespace) -> None:
    entry_keys = {"site", "book_id", "title", "last_chapter", "updated_at"}

    status, added = _post(
        "/api/v1/watchlist", {"site": "s", "book_id": "1", "title": "B"}
    )
    assert status == 200
    assert added["created"] is True
    assert set(added["entry"]) == entry_keys

    status, listing = _get("/api/v1/watchlist")
    assert status == 200
    assert listing["total"] == 1
    assert set(listing["books"][0]) == entry_keys

    status, removed = _delete("/api/v1/watchlist/s/1")
    assert (status, removed) == (200, {"ok": True})

    status, body = _delete("/api/v1/watchlist/s/1")
    assert status == 404
    assert body == {"detail": "Book not in watchlist: s/1"}

    status, body = _post("/api/v1/watchlist", {})
    assert status == 422
    assert body["detail"] == (
        "Request validation failed: site: Field required; book_id: Field required"
    )


def test_watchlist_check_with_task_body(
    running_server: SimpleNamespace,
) -> None:
    _, body = _post("/api/v1/watchlist/check", {"download": True})
    assert body["checked"] == 1
    assert body["items"][0]["task"]["title"] == "B"


def test_watchlist_check_mirrors_api_semantics(
    running_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_downloader.apps.core.watchlist import CheckResult, WatchEntry

    async def fake_check_all(_store: Any, **_kw: Any) -> list[CheckResult]:
        return [
            CheckResult(
                entry=WatchEntry(site="s", book_id="1", title="B"),
                latest="Chapter 2",
                matched_title="B",
                changed=True,
            ),
            CheckResult(
                entry=WatchEntry(site="s", book_id="2", title="C", last_chapter="Ch 1"),
                latest="Ch 1",
                matched_title="C",
            ),
            CheckResult(
                entry=WatchEntry(site="s", book_id="3", title="D"),
                error="TimeoutError: boom",
            ),
            CheckResult(
                entry=WatchEntry(site="s", book_id="4"),
                latest="Chapter 9",
                changed=True,
            ),
        ]

    monkeypatch.setattr(
        "novel_downloader.apps.core.watchlist.check_all", fake_check_all
    )

    status, body = _post("/api/v1/watchlist/check", {})
    assert status == 200
    assert (body["checked"], body["updated"], body["failed"]) == (4, 2, 1)
    assert body["download"] is False
    first, second, third, fourth = body["items"]
    assert first["needs_download"] is True
    assert first["changed"] is True
    assert first["previous_chapter"] == ""
    assert first["last_chapter"] == "Chapter 2"
    assert first["task"] is None
    assert second["needs_download"] is False
    assert second["last_chapter"] == "Ch 1"
    assert third["error"] == "TimeoutError: boom"
    assert third["found"] is False
    assert fourth["found"] is True
    assert [i["book_id"] for i in body["needs_download"]] == ["1", "4"]

    status, body = _post("/api/v1/watchlist/check", {"download": True})
    assert status == 200
    assert body["needs_download"][0]["task"] == {
        "site": "s",
        "book_id": "1",
        "title": "B",
    }
    # an entry with no known title gets a null task title, like the REST API
    assert body["items"][3]["task"] == {
        "site": "s",
        "book_id": "4",
        "title": None,
    }


# ---------------------------------------------------------------------------
# contract: music stubs
# ---------------------------------------------------------------------------


def test_music_stub_contract(running_server: SimpleNamespace) -> None:
    status, body = _get("/api/v1/music/platforms")
    assert (status, body) == (200, {"platforms": []})

    for path in ("/api/v1/music/search", "/api/v1/music/jobs"):
        status, body = _post(path, {"keyword": "x", "platform": "netease"})
        assert status == 503, (path, body)
        assert body["detail"].startswith("Music support is not available")


# ---------------------------------------------------------------------------
# contract: error shapes
# ---------------------------------------------------------------------------


def test_unknown_route_is_404(running_server: SimpleNamespace) -> None:
    status, body = _get("/api/v1/nope")
    assert status == 404
    assert body == {"detail": "Not Found"}


def test_wrong_method_and_options_mirror_api(
    running_server: SimpleNamespace,
) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{_port}/api/v1/health", method="PUT"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected 405")
    except urllib.error.HTTPError as exc:
        assert exc.code == 405
        assert json.loads(exc.read()) == {"detail": "Method Not Allowed"}
        assert "GET" in (exc.headers.get("Allow") or "")

    status, body = _post("/api/v1/health")
    assert (status, body) == (405, {"detail": "Method Not Allowed"})

    status, body = _call("OPTIONS", "/api/v1/tasks")
    assert (status, body) == (405, {"detail": "Method Not Allowed"})

    status, body = _call("PUT", "/api/v1/tasks")
    assert (status, body) == (405, {"detail": "Method Not Allowed"})


def test_handler_exception_is_opaque_500(
    running_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> dict[str, list[Any]]:
        raise RuntimeError("secret /data/user/0 internals")

    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot", boom
    )
    status, body = _get("/api/v1/tasks")
    assert status == 500
    assert body == {"detail": "Internal server error"}
    assert "secret" not in json.dumps(body)


def test_error_messages_match_rest_constants() -> None:
    from novel_downloader.apps.api.constants import (
        MSG_BOOK_NOT_IN_WATCHLIST,
        MSG_LOGIN_REQUEST_ALREADY_COMPLETED,
        MSG_LOGIN_REQUEST_NOT_FOUND,
        MSG_MUSIC_UNAVAILABLE,
        MSG_TASK_NOT_FOUND,
        MSG_TIMED_OUT,
    )

    assert eserver._MSG_TASK_NOT_FOUND == MSG_TASK_NOT_FOUND
    assert eserver._MSG_LOGIN_REQUEST_NOT_FOUND == MSG_LOGIN_REQUEST_NOT_FOUND
    assert (
        eserver._MSG_LOGIN_REQUEST_ALREADY_COMPLETED
        == MSG_LOGIN_REQUEST_ALREADY_COMPLETED
    )
    assert eserver._MSG_BOOK_NOT_IN_WATCHLIST == MSG_BOOK_NOT_IN_WATCHLIST
    assert eserver._MSG_MUSIC_UNAVAILABLE == MSG_MUSIC_UNAVAILABLE
    assert eserver._MSG_TIMED_OUT == MSG_TIMED_OUT


# ---------------------------------------------------------------------------
# HTTP semantics: keep-alive, HEAD, body handling
# ---------------------------------------------------------------------------


def test_keep_alive_reuses_connection(running_server: SimpleNamespace) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=10)
    conn.request("GET", "/api/v1/health")
    first = conn.getresponse()
    assert first.status == 200
    first.read()
    conn.request("GET", "/api/v1/sites")
    second = conn.getresponse()
    assert second.status == 200
    second.read()
    conn.close()


def test_keep_alive_survives_404_with_body(running_server: SimpleNamespace) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=10)
    conn.request(
        "POST",
        "/api/v1/does-not-exist",
        body=b'{"x": 1}',
        headers={"Content-Type": "application/json"},
    )
    first = conn.getresponse()
    assert first.status == 404
    first.read()
    # The unread request body must have been drained: the connection stays
    # usable instead of feeding the body to the next request line.
    conn.request("GET", "/api/v1/health")
    second = conn.getresponse()
    body = second.read()
    assert second.status == 200
    assert json.loads(body)["status"] == "ok"
    conn.close()


def test_head_requests_behave_like_get(running_server: SimpleNamespace) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=10)
    conn.request("HEAD", "/api/v1/health")
    head = conn.getresponse()
    assert head.status == 200
    assert head.read() == b""
    head_length = int(head.headers["Content-Length"])

    conn.request("GET", "/api/v1/health")
    got = conn.getresponse()
    got_body = got.read()
    assert got.status == 200
    assert head_length == len(got_body)
    conn.close()


def test_head_download_has_no_body(running_server: SimpleNamespace) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=10)
    conn.request("HEAD", "/downloads/book.txt")
    head = conn.getresponse()
    assert head.status == 200
    assert head.read() == b""
    assert head.headers["Content-Length"] == "5"
    conn.close()


def test_malformed_content_length_returns_400(
    running_server: SimpleNamespace,
) -> None:
    raw = (
        b"POST /api/v1/search HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: abc\r\n"
        b"\r\n"
    )
    data = _raw_exchange(_port, raw)
    assert data.startswith(b"HTTP/1.1 400")
    assert b"Invalid Content-Length header" in data


def test_chunked_request_body_rejected(running_server: SimpleNamespace) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=10)
    conn.request("POST", "/api/v1/search", body=iter([b'{"keyword": "x"}']))
    resp = conn.getresponse()
    assert resp.status == 501
    resp.read()
    conn.close()


def test_body_size_limit_413(running_server: SimpleNamespace) -> None:
    raw = (
        b"POST /api/v1/search HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2097152\r\n"
        b"\r\n"
    )
    data = _raw_exchange(_port, raw)
    assert data.startswith(b"HTTP/1.1 413")
    assert b"Request body too large" in data
    assert b"1048576" in data


# ---------------------------------------------------------------------------
# HTTP semantics: concurrency and lifecycle
# ---------------------------------------------------------------------------


def test_busy_server_answers_503(
    running_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        eserver, "_request_slots", threading.BoundedSemaphore(1), raising=False
    )

    async def slow_search(keyword: str, **_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(1.0)
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", slow_search)

    outcome: dict[str, int] = {}

    def hold_slot() -> None:
        status, _body = _post("/api/v1/search", {"keyword": "x"})
        outcome["status"] = status

    worker = threading.Thread(target=hold_slot)
    worker.start()
    time.sleep(0.3)
    try:
        status, body = _get("/api/v1/health")
        assert status == 503
        assert body == {"detail": "Embedded engine busy"}
    finally:
        worker.join(timeout=15)
    assert outcome["status"] == 200


def test_concurrent_mixed_requests(running_server: SimpleNamespace) -> None:
    results: list[tuple[int, int, int]] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        one, _ = _get("/api/v1/tasks")
        two, _ = _post("/api/v1/tasks", {"site": "qidian", "book_id": str(index)})
        three, _ = _get("/api/v1/health")
        with lock:
            results.append((one, two, three))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 8
    assert all(result == (200, 200, 200) for result in results)


def test_concurrent_watchlist_adds_are_not_lost(
    running_server: SimpleNamespace,
) -> None:
    def add(index: int) -> None:
        _post(
            "/api/v1/watchlist",
            {"site": "s", "book_id": str(index), "title": f"B{index}"},
        )

    threads = [threading.Thread(target=add, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    status, listing = _get("/api/v1/watchlist")
    assert status == 200
    assert listing["total"] == 8
    assert {book["book_id"] for book in listing["books"]} == {
        str(index) for index in range(8)
    }


def test_start_server_bind_failure_no_silent_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"count": 0}

    class FailOnce(ThreadingHTTPServer):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("address in use")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(eserver, "_QuietThreadingHTTPServer", FailOnce, raising=False)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    try:
        with pytest.raises(OSError):
            eserver.start_server(str(tmp_path), 8791)
        assert eserver._server is None
        # once the port frees up, a retry binds the requested port again
        port = eserver.start_server(str(tmp_path), 8791)
        assert port == 8791
        status, _body = _get("/api/v1/health", port=8791)
        assert status == 200
    finally:
        eserver.stop_server()


def test_stop_server_then_restart_reuses_engine_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_downloader.apps.core import cred_broker

    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    port = eserver.start_server(str(tmp_path), 8781)
    request = eserver._run(
        cred_broker.create_cred_request(task_id="t0", title="B", fields=[])
    )
    try:
        loop_before = eserver._loop
        status, body = _get("/api/v1/login/requests", port=port)
        assert status == 200
        assert body["total"] == 1

        eserver.stop_server()
        eserver.start_server(str(tmp_path), port)

        # The broker's asyncio.Lock is bound to the first engine loop; a
        # restart on a fresh loop raises RuntimeError and surfaces as a 500.
        status, body = _get("/api/v1/login/requests", port=port)
        assert status == 200
        assert body["total"] == 1
        assert eserver._loop is loop_before
    finally:
        cred_broker.cleanup_request(request.req_id)
        eserver.stop_server()
