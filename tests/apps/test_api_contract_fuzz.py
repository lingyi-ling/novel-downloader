#!/usr/bin/env python3
"""
tests.apps.test_api_contract_fuzz
---------------------------------

Table-driven fuzz guard for the REST/embedded contract.

Every case sends the *same* payload to both transports over the wire (the REST
app through ``httpx.ASGITransport``, the embedded engine through a loopback
server) and asserts that:

* the status codes are identical,
* neither transport answers an opaque ``500``,
* a ``detail`` string never leaks an internal path, traceback or module name.

The matrix is deterministic (no hypothesis dependency): empty/blank strings,
explicit JSON ``null`` on both optional and non-optional fields, overlong
text, C0/DEL control characters, unpaired surrogates, non-finite floats,
overflowing integers, negative/zero page bounds, malformed ``sites`` and
``tracks`` structures, path parameters and the task-list filters.

No network access happens: search, the task manager, the credential broker and
the music engine are all replaced with local fakes, and only payloads that
fail before provider I/O are used for browse.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.routes import get_manager, get_watchlist_store
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.task_manager import manager
from novel_downloader.apps.core.watchlist import WatchlistStore
from novel_downloader.apps.embedded import server as eserver

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeTrack:
    provider: str = ""
    id: str = ""
    name: str = ""
    artists: str = ""
    album: str = ""
    duration_s: int = 0
    cover_url: str = ""
    track_no: int = 0
    ext: str = ""
    vip: bool = False
    download_url: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)
    path: str = ""
    lyric: str = ""
    sources: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class FakeProvider:
    supports_offset = True

    def search(self, keyword: str, *, limit: int = 20, offset: int = 0) -> Any:
        return [FakeTrack(provider="netease", id="1", name=keyword)], 1


def _fake_download_mixed(
    _create_provider: Any,
    tracks: list[Any],
    _options: Any,
    *,
    on_result: Any = None,
) -> Any:
    for index, track in enumerate(tracks):
        if on_result is not None:
            on_result(
                index,
                track,
                SimpleNamespace(status="ok", reason="", path="", bytes_=0),
            )
    return len(tracks), 0, 0, [], []


def _fake_export_m3u(_tracks: list[Any], target: Any) -> str:
    return str(target)


def _make_components(root: Any) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)

    def _create(key: str) -> Any:
        if key != "netease":
            raise KeyError(f"unknown platform: {key}")
        return FakeProvider()

    return SimpleNamespace(
        paths=SimpleNamespace(default_download_dir=lambda: root),
        download_options=lambda out_dir, level: SimpleNamespace(
            out_dir=out_dir, level=level
        ),
        download_many=_fake_download_mixed,
        download_mixed=_fake_download_mixed,
        export_m3u=_fake_export_m3u,
        available_labels=lambda: [("netease", "网易云音乐")],
        create=_create,
        platform_note=lambda _key: "note",
        track_cls=FakeTrack,
        health_check=lambda _key, timeout=5.0: {
            "ok": True,
            "elapsed_ms": 1,
            "error": "",
        },
        load_settings=lambda: {"out_dir": str(root)},
    )


def _fake_search_all(
    keyword: str, _sites: Any, _limit: int, **_kwargs: Any
) -> SimpleNamespace:
    return SimpleNamespace(
        total=1,
        tracks=[FakeTrack(provider="netease", id="1", name=keyword)],
        platforms=[
            SimpleNamespace(
                as_dict=lambda: {
                    "platform": "netease",
                    "ok": True,
                    "elapsed_ms": 1,
                    "error": "",
                }
            )
        ],
    )


class _FakeManager:
    def __init__(self, snapshot: dict[str, list[DownloadTask]]) -> None:
        self._snapshot = snapshot
        self.added: list[dict[str, str]] = []
        self._added_task: DownloadTask | None = None

    async def start(self) -> None:
        return None

    async def add_task(self, *, title: str, site: str, book_id: str) -> Any:
        self.added.append({"title": title, "site": site, "book_id": book_id})
        return self._added_task

    async def cancel_task(self, task_id: str) -> bool:
        return task_id == "known-task"

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return self._snapshot


def _task(status: Status, site: str, created_at: float, task_id: str) -> DownloadTask:
    item = DownloadTask(title=f"T-{task_id}", site=site, book_id=task_id)
    item.task_id = task_id
    item.status = status
    item.created_at = created_at
    item.chapters_total = 4
    item.chapters_done = 1
    return item


@pytest.fixture()
def engines(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Serve REST (ASGI) and embedded (loopback) over the same fakes."""
    home = tmp_path / "fuzz_home"
    home.mkdir()
    original_cwd = os.getcwd()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)

    pending = _task(Status.QUEUED, "qidian", 100.0, "pending-task")
    running = _task(Status.RUNNING, "mangg", 300.0, "running-task")
    completed = _task(Status.COMPLETED, "qidian", 200.0, "completed-task")
    fake_manager = _FakeManager(
        {"pending": [pending], "running": [running], "completed": [completed]}
    )
    fake_manager._added_task = _task(Status.QUEUED, "qidian", 50.0, "added-task")

    monkeypatch.setattr(manager, "start", fake_manager.start)
    monkeypatch.setattr(manager, "add_task", fake_manager.add_task)
    monkeypatch.setattr(manager, "cancel_task", fake_manager.cancel_task)
    monkeypatch.setattr(manager, "snapshot", fake_manager.snapshot)

    async def fake_search(_keyword: str, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    async def fake_req_state(_req_id: str) -> tuple[bool, bool]:
        return False, False

    async def fake_complete(_req_id: str, _values: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(api_routes, "get_req_state", fake_req_state)
    monkeypatch.setattr(api_routes, "complete_request", fake_complete)
    monkeypatch.setattr(
        "novel_downloader.apps.core.cred_broker.get_req_state", fake_req_state
    )
    monkeypatch.setattr(
        "novel_downloader.apps.core.cred_broker.complete_request", fake_complete
    )

    components = _make_components(tmp_path / "music-root")
    monkeypatch.setattr(api_routes, "_load_music_components", lambda: components)
    monkeypatch.setattr(eserver, "_music_components", lambda: components)
    monkeypatch.setattr(
        "novel_downloader.apps.music.aggregate.search_all", _fake_search_all
    )

    store = WatchlistStore(path=tmp_path / "watchlist.json")
    app: FastAPI = create_app(
        token="",
        output_dir=str(tmp_path / "downloads"),
        rate_limit=0,
    )
    app.dependency_overrides[get_manager] = lambda: fake_manager
    app.dependency_overrides[get_watchlist_store] = lambda: store

    port = eserver.start_server(str(home), 0)
    try:
        yield SimpleNamespace(
            app=app,
            port=port,
            manager=fake_manager,
            components=components,
        )
    finally:
        eserver.stop_server()
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# wire helpers
# ---------------------------------------------------------------------------

_MISSING = object()

#: Substrings that must never appear in an error ``detail``.
_FORBIDDEN_DETAIL = (
    "Traceback",
    "Internal server error",
    "novel_downloader",
    "raw_data",
    "/data/user",
    "site-packages",
)


def _encode(payload: Any) -> bytes:
    """JSON with escapes, so surrogates/controls/NaN survive the client."""
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


async def _call(client: httpx.AsyncClient, method: str, path: str, payload: Any) -> Any:
    if payload is _MISSING:
        response = await client.request(method, path)
    elif isinstance(payload, bytes):
        response = await client.request(
            method, path, content=payload, headers={"Content-Type": "application/json"}
        )
    else:
        response = await client.request(
            method,
            path,
            content=_encode(payload),
            headers={"Content-Type": "application/json"},
        )
    body = response.json() if response.content else None
    return response.status_code, body


def _detail(body: Any) -> str:
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return ""


def _assert_safe(name: str, status: int, body: Any) -> None:
    detail = _detail(body)
    for marker in _FORBIDDEN_DETAIL:
        assert marker not in detail, f"{name}: leaked {marker!r} in {detail!r}"


async def _run_cases(
    api: httpx.AsyncClient, embedded: httpx.AsyncClient, cases: list[tuple[Any, ...]]
) -> None:
    for name, method, path, payload, expected in cases:
        try:
            api_status, api_body = await _call(api, method, path, payload)
        except Exception as exc:  # noqa: BLE001 - pinpoint the failing case
            raise AssertionError(f"{name}: REST call failed: {exc!r}") from exc
        try:
            emb_status, emb_body = await _call(embedded, method, path, payload)
        except Exception as exc:  # noqa: BLE001 - pinpoint the failing case
            raise AssertionError(f"{name}: embedded call failed: {exc!r}") from exc

        assert api_status != 500, (name, "REST answered 500", api_body)
        assert emb_status != 500, (name, "embedded answered 500", emb_body)
        assert api_status == emb_status, (
            name,
            method,
            path,
            payload,
            "REST",
            api_status,
            api_body,
            "embedded",
            emb_status,
            emb_body,
        )
        assert api_status == expected, (name, expected, api_status, api_body)
        _assert_safe(name, api_status, api_body)
        _assert_safe(name, emb_status, emb_body)


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=30,
    )


# ---------------------------------------------------------------------------
# deterministic value matrices
# ---------------------------------------------------------------------------

#: Type-confused scalars every text field must reject.
_BAD_STRINGS: list[Any] = [None, "", "   ", "\t\n", "a\x00b", "\ud800", 5, True, [], {}]


def _sweep(
    prefix: str,
    path: str,
    base: dict[str, Any],
    field_name: str,
    values: list[Any],
    *,
    expected: int = 422,
) -> list[tuple[Any, ...]]:
    """One case per ``(field, value)``; payloads differ in one field only."""
    cases: list[tuple[Any, ...]] = []
    for index, value in enumerate(values):
        payload = dict(base)
        payload[field_name] = value
        cases.append(
            (f"{prefix}-{field_name}-{index}", "POST", path, payload, expected)
        )
    return cases


def _search_cases() -> list[tuple[Any, ...]]:
    path = "/api/v1/search"
    cases: list[tuple[Any, ...]] = [
        ("search-null-body", "POST", path, None, 422),
        ("search-list-body", "POST", path, [], 422),
        ("search-string-body", "POST", path, "keyword=x", 422),
        ("search-malformed-json", "POST", path, b"{", 422),
        ("search-empty-body", "POST", path, b"", 422),
        ("search-no-body", "POST", path, _MISSING, 422),
        ("search-missing-keyword", "POST", path, {}, 422),
        ("search-valid", "POST", path, {"keyword": "凡人"}, 200),
        ("search-keyword-max", "POST", path, {"keyword": "x" * 200}, 200),
        ("search-empty-sites", "POST", path, {"keyword": "x", "sites": []}, 200),
        ("search-null-sites", "POST", path, {"keyword": "x", "sites": None}, 200),
        ("search-null-limit", "POST", path, {"keyword": "x", "limit": None}, 200),
        ("search-null-order", "POST", path, {"keyword": "x", "order": None}, 200),
        ("search-valid-options", "POST", path, {"keyword": "x", "limit": 500}, 200),
        (
            "search-valid-timeout",
            "POST",
            path,
            {"keyword": "x", "timeout": 120.0},
            200,
        ),
        (
            "search-valid-sites",
            "POST",
            path,
            {"keyword": "x", "sites": ["demo", "DEMO", " qidian "]},
            200,
        ),
    ]
    keyword_values = list(_BAD_STRINGS) + ["x" * 201]
    cases += _sweep("search", path, {}, "keyword", keyword_values)
    cases += _sweep(
        "search",
        path,
        {"keyword": "x"},
        "sites",
        [
            ["bad site"],
            [""],
            ["   "],
            ["a/b"],
            ["..\\escape"],
            ["a\x00b"],
            ["\ud800"],
            [5],
            [None],
            [True],
            [{}],
            "demo",
            5,
            True,
            {},
            ["demo"] * 101,
        ],
    )
    cases += _sweep(
        "search",
        path,
        {"keyword": "x"},
        "sites",
        [["demo"] * 100, ["qidian", "mangg"]],
        expected=200,
    )
    cases += _sweep(
        "search",
        path,
        {"keyword": "x"},
        "per_site_limit",
        [None, 0, -1, 101, 1.5, [], 10**30],
    )
    cases += _sweep(
        "search",
        path,
        {"keyword": "x"},
        "timeout",
        [
            None,
            0,
            -1,
            120.1,
            1e6,
            float("nan"),
            float("inf"),
            float("-inf"),
            10**400,
            -(10**400),
            [],
        ],
    )
    cases += _sweep(
        "search",
        path,
        {"keyword": "x"},
        "sort",
        [None, "bogus", "", 5, True, []],
    )
    cases += _sweep(
        "search",
        path,
        {"keyword": "x"},
        "order",
        ["up", "", 5, True, []],
    )
    cases += _sweep(
        "search",
        path,
        {"keyword": "x"},
        "limit",
        [0, -1, 501, 1.5, []],
    )
    cases += [
        (
            "search-per-site-limit-str",
            "POST",
            path,
            {"keyword": "x", "per_site_limit": "5"},
            200,
        ),
        (
            "search-per-site-limit-bool",
            "POST",
            path,
            {"keyword": "x", "per_site_limit": True},
            200,
        ),
        ("search-timeout-str", "POST", path, {"keyword": "x", "timeout": "5"}, 200),
        ("search-timeout-bool", "POST", path, {"keyword": "x", "timeout": True}, 200),
        ("search-limit-str", "POST", path, {"keyword": "x", "limit": "5"}, 200),
    ]
    cases += _sweep(
        "search",
        path,
        {"keyword": "x"},
        "group",
        [None, 2, " true ", "junk", []],
    )
    return cases


def _browse_cases() -> list[tuple[Any, ...]]:
    path = "/api/v1/browse"
    cases: list[tuple[Any, ...]] = [
        ("browse-null-body", "POST", path, None, 422),
        ("browse-list-body", "POST", path, [], 422),
        ("browse-missing-site", "POST", path, {}, 422),
        (
            "browse-categories-missing",
            "GET",
            "/api/v1/browse/categories",
            _MISSING,
            422,
        ),
        (
            "browse-categories-empty",
            "GET",
            "/api/v1/browse/categories?site=",
            _MISSING,
            422,
        ),
        (
            "browse-categories-bad",
            "GET",
            "/api/v1/browse/categories?site=bad%20site",
            _MISSING,
            422,
        ),
        (
            "browse-categories-last-value",
            "GET",
            "/api/v1/browse/categories?site=demo&site=bad%20site",
            _MISSING,
            422,
        ),
    ]
    cases += _sweep(
        "browse",
        path,
        {},
        "site",
        list(_BAD_STRINGS) + ["bad site", "a/b", "x" * 65],
    )
    cases += _sweep(
        "browse",
        path,
        {"site": "qidian"},
        "category_id",
        [5, True, [], {}, "a\x00b", "\ud800", "x" * 129],
    )
    cases += _sweep(
        "browse",
        path,
        {"site": "qidian"},
        "gender",
        [None, "x", "", 5, True, []],
    )
    cases += _sweep(
        "browse",
        path,
        {"site": "qidian"},
        "limit",
        [None, 0, -1, 101, 1.5, []],
    )
    return cases


def _task_post_cases() -> list[tuple[Any, ...]]:
    path = "/api/v1/tasks"
    base = {"site": "qidian", "book_id": "1"}
    cases: list[tuple[Any, ...]] = [
        ("task-null-body", "POST", path, None, 422),
        ("task-list-body", "POST", path, [], 422),
        ("task-missing-site", "POST", path, {"book_id": "1"}, 422),
        ("task-missing-book-id", "POST", path, {"site": "qidian"}, 422),
        ("task-valid", "POST", path, base, 200),
        ("task-valid-title", "POST", path, {**base, "title": "Title"}, 200),
        ("task-blank-title", "POST", path, {**base, "title": "   "}, 200),
        ("task-null-title", "POST", path, {**base, "title": None}, 200),
    ]
    cases += _sweep(
        "task",
        path,
        base,
        "title",
        ["a\r\nX-Injected: 1", "a\x00b", "\ud800", 5, True, [], {}, "x" * 513],
    )
    cases += _sweep(
        "task",
        path,
        {"book_id": "1"},
        "site",
        [None, "", "   ", "bad site", "a/b", "\ud800", "a\x00b", 5, True, [], {}]
        + ["x" * 65],
    )
    cases += _sweep(
        "task",
        path,
        {"site": "qidian"},
        "book_id",
        [
            None,
            "",
            "   ",
            "a/b",
            "a\\b",
            ".",
            "..",
            "abc.",
            "a:b",
            "a*b",
            "a?b",
            "a\x00b",
            "\ud800",
            5,
            True,
            [],
            {},
        ]
        + ["x" * 129],
    )
    return cases


def _task_query_cases() -> list[tuple[Any, ...]]:
    base = "/api/v1/tasks"
    return [
        ("tasks-offset-empty", "GET", f"{base}?offset=", _MISSING, 422),
        ("tasks-offset-negative", "GET", f"{base}?offset=-1", _MISSING, 422),
        ("tasks-offset-text", "GET", f"{base}?offset=x", _MISSING, 422),
        ("tasks-offset-float", "GET", f"{base}?offset=1.5", _MISSING, 422),
        ("tasks-offset-last", "GET", f"{base}?offset=1&offset=x", _MISSING, 422),
        ("tasks-limit-zero", "GET", f"{base}?limit=0", _MISSING, 422),
        ("tasks-limit-over", "GET", f"{base}?limit=201", _MISSING, 422),
        ("tasks-limit-text", "GET", f"{base}?limit=x", _MISSING, 422),
        ("tasks-limit-bool", "GET", f"{base}?limit=true", _MISSING, 422),
        ("tasks-status-bogus", "GET", f"{base}?status=bogus", _MISSING, 422),
        ("tasks-status-empty", "GET", f"{base}?status=", _MISSING, 422),
        ("tasks-status-case", "GET", f"{base}?status=QUEUED", _MISSING, 422),
        ("tasks-site-bad", "GET", f"{base}?site=bad%20site", _MISSING, 422),
        ("tasks-site-empty", "GET", f"{base}?site=", _MISSING, 422),
        ("tasks-site-control", "GET", f"{base}?site=a%00b", _MISSING, 422),
        ("tasks-valid", "GET", base, _MISSING, 200),
        ("tasks-status-valid", "GET", f"{base}?status=queued", _MISSING, 200),
        (
            "tasks-status-repeat",
            "GET",
            f"{base}?status=queued&status=running",
            _MISSING,
            200,
        ),
        ("tasks-site-valid", "GET", f"{base}?site=qidian", _MISSING, 200),
        ("tasks-site-stripped", "GET", f"{base}?site=%20Qidian%20", _MISSING, 200),
        (
            "tasks-filter-combined",
            "GET",
            f"{base}?status=completed&site=qidian",
            _MISSING,
            200,
        ),
        ("tasks-limit-max", "GET", f"{base}?limit=200", _MISSING, 200),
        ("tasks-unknown-query", "GET", f"{base}?bogus=1", _MISSING, 200),
    ]


def _watchlist_cases() -> list[tuple[Any, ...]]:
    post = "/api/v1/watchlist"
    cases: list[tuple[Any, ...]] = [
        ("watchlist-null-body", "POST", post, None, 422),
        ("watchlist-list-body", "POST", post, [], 422),
        ("watchlist-missing-site", "POST", post, {"book_id": "1"}, 422),
        ("watchlist-missing-book-id", "POST", post, {"site": "qidian"}, 422),
        ("watchlist-valid", "POST", post, {"site": "qidian", "book_id": "1"}, 200),
        (
            "watchlist-valid-title",
            "POST",
            post,
            {"site": "qidian", "book_id": "1", "title": "T"},
            200,
        ),
        (
            "watchlist-null-title",
            "POST",
            post,
            {"site": "qidian", "book_id": "1", "title": None},
            200,
        ),
        (
            "watchlist-delete-bad-site",
            "DELETE",
            "/api/v1/watchlist/bad%20site/1",
            _MISSING,
            422,
        ),
        (
            "watchlist-delete-bad-id",
            "DELETE",
            "/api/v1/watchlist/demo/" + "x" * 129,
            _MISSING,
            422,
        ),
        (
            "watchlist-delete-nul-site",
            "DELETE",
            "/api/v1/watchlist/%00/1",
            _MISSING,
            422,
        ),
        (
            "watchlist-delete-nul-id",
            "DELETE",
            "/api/v1/watchlist/demo/%00",
            _MISSING,
            422,
        ),
        (
            "watchlist-delete-colon",
            "DELETE",
            "/api/v1/watchlist/demo/a%3Ab",
            _MISSING,
            422,
        ),
        (
            "watchlist-delete-dotdot",
            "DELETE",
            "/api/v1/watchlist/demo/%2E%2E",
            _MISSING,
            422,
        ),
        (
            "watchlist-delete-trailing-dot",
            "DELETE",
            "/api/v1/watchlist/demo/abc.",
            _MISSING,
            422,
        ),
        (
            "watchlist-delete-encoded-slash",
            "DELETE",
            "/api/v1/watchlist/demo/a%2Fb",
            _MISSING,
            404,
        ),
        (
            "watchlist-delete-unknown",
            "DELETE",
            "/api/v1/watchlist/demo/zzz",
            _MISSING,
            404,
        ),
    ]
    cases += _sweep(
        "watchlist",
        post,
        {"book_id": "1"},
        "site",
        [None, "", "   ", "bad site", "a/b", "a\x00b", "\ud800", 5, True, [], {}]
        + ["x" * 65],
    )
    cases += _sweep(
        "watchlist",
        post,
        {"site": "qidian"},
        "book_id",
        [None, "", "   ", "a/b", ".", "..", "abc.", "a:b", "a\x00b", 5, True, []]
        + ["x" * 129],
    )
    cases += _sweep(
        "watchlist",
        post,
        {"site": "qidian", "book_id": "1"},
        "title",
        ["a\r\nX-Injected: 1", "a\x00b", "\ud800", 5, True, [], {}, "x" * 513],
    )
    return cases


def _watchlist_check_cases() -> list[tuple[Any, ...]]:
    path = "/api/v1/watchlist/check"
    return [
        ("check-empty", "POST", path, {}, 200),
        ("check-download-true", "POST", path, {"download": True}, 200),
        ("check-download-false", "POST", path, {"download": False}, 200),
        ("check-download-null", "POST", path, {"download": None}, 422),
        ("check-download-int", "POST", path, {"download": 2}, 422),
        ("check-download-padded", "POST", path, {"download": " true "}, 422),
        ("check-download-junk", "POST", path, {"download": "junk"}, 422),
        ("check-list-body", "POST", path, [], 422),
    ]


def _login_cases() -> list[tuple[Any, ...]]:
    path = "/api/v1/login/requests/fuzz-request"
    cases: list[tuple[Any, ...]] = [
        ("login-null-body", "POST", path, None, 422),
        ("login-list-body", "POST", path, [], 422),
        ("login-empty-values", "POST", path, {"values": {}}, 404),
        ("login-valid", "POST", path, {"values": {"cookie": "a=b; c=d"}}, 404),
        ("login-missing-values", "POST", path, {}, 404),
        ("login-null-values", "POST", path, {"values": None}, 422),
        ("login-values-list", "POST", path, {"values": []}, 422),
        ("login-values-string", "POST", path, {"values": "cookie=a=b"}, 422),
        ("login-values-number", "POST", path, {"values": {"cookie": 1}}, 422),
        (
            "login-too-many-fields",
            "POST",
            path,
            {"values": {f"f{i}": "v" for i in range(51)}},
            422,
        ),
        ("login-empty-name", "POST", path, {"values": {"": "v"}}, 422),
        ("login-long-name", "POST", path, {"values": {"x" * 65: "v"}}, 422),
        ("login-control-name", "POST", path, {"values": {"a\x00b": "v"}}, 422),
        ("login-crlf-value", "POST", path, {"values": {"cookie": "a\r\nX: 1"}}, 422),
        ("login-nul-value", "POST", path, {"values": {"cookie": "a\x00b"}}, 422),
        ("login-surrogate-value", "POST", path, {"values": {"cookie": "\ud800"}}, 422),
        (
            "login-long-value",
            "POST",
            path,
            {"values": {"cookie": "x" * 65537}},
            422,
        ),
    ]
    return cases


_TRACK_FIELD_VALUES: dict[str, list[Any]] = {
    "provider": [None, 5, True, [], {}, "x" * 513],
    "id": [None, 5, True, [], {}, "x" * 513],
    "name": [None, 5, True, [], {}, "x" * 513],
    "artists": [None, 5, True, [], {}, "x" * 513],
    "album": [None, 5, True, [], {}, "x" * 513],
    "ext": [None, 5, True, [], {}, "x" * 513],
    "duration_s": [None, 1.5, "3.5", [], {}],
    "track_no": [None, 1.5, "3.5", [], {}],
    "vip": [None, 2, "junk", [], {}],
    "cover_url": [
        None,
        5,
        True,
        [],
        {},
        "file:///etc/passwd",
        "javascript:alert(1)",
        "a\x00b",
        "\ud800",
        "http://[",
        "https://example.test/" + "x" * 2048,
    ],
    "download_url": [
        None,
        5,
        True,
        [],
        {},
        "file:///etc/passwd",
        "ftp://example.test/a.mp3",
        "javascript:alert(1)",
        "a\x00b",
        "\ud800",
        "http://[",
    ],
    "path": [None, 5, True, [], {}, "a\x00b", "\ud800", "x" * 1025],
    "lyric": [None, 5, True, [], {}, "\ud800", "x" * 100001],
    "http_headers": [
        None,
        [],
        "X-Test: 1",
        33,
        {f"H{i}": "v" for i in range(33)},
        {"Bad Name": "v"},
        {"H": 1},
        {"H": "a\r\nX-Injected: 1"},
        {"H": "\ud800"},
        {"H": "x" * 2049},
        {"X" * 65: "v"},
    ],
    "sources": [
        None,
        "netease",
        33,
        ["ok"] * 33,
        [5],
        [None],
        ["ok\x00"],
        ["\ud800"],
        ["x" * 513],
    ],
    "extra": [None, [], "x", 5],
}


def _track_cases(prefix: str, path: str) -> list[tuple[Any, ...]]:
    cases: list[tuple[Any, ...]] = []
    for name, values in _TRACK_FIELD_VALUES.items():
        payloads = [{"tracks": [{"id": "1", name: value}]} for value in values]
        for index, payload in enumerate(payloads):
            cases.append((f"{prefix}-track-{name}-{index}", "POST", path, payload, 422))
    return cases


def _music_search_cases() -> list[tuple[Any, ...]]:
    path = "/api/v1/music/search"
    cases: list[tuple[Any, ...]] = [
        ("music-search-null-body", "POST", path, None, 422),
        ("music-search-list-body", "POST", path, [], 422),
        ("music-search-missing-keyword", "POST", path, {}, 422),
        ("music-search-valid-all", "POST", path, {"keyword": "x"}, 200),
        (
            "music-search-valid-platform",
            "POST",
            path,
            {"platform": "netease", "keyword": "x"},
            200,
        ),
        (
            "music-search-valid-max",
            "POST",
            path,
            {"platform": "netease", "keyword": "x", "limit": 100, "offset": 10000},
            200,
        ),
        (
            "music-search-valid-whitespace-platform",
            "POST",
            path,
            {"platform": " ", "keyword": "x"},
            200,
        ),
        (
            "music-search-unknown-platform",
            "POST",
            path,
            {"platform": "bogus", "keyword": "x"},
            400,
        ),
    ]
    cases += _sweep(
        "music-search",
        path,
        {},
        "keyword",
        list(_BAD_STRINGS) + ["x" * 201],
    )
    cases += _sweep(
        "music-search",
        path,
        {"keyword": "x"},
        "platform",
        [None, "", 5, True, [], {}, "a\x00b", "\ud800", "x" * 65],
    )
    cases += _sweep(
        "music-search",
        path,
        {"keyword": "x"},
        "limit",
        [None, 0, -1, 101, 1.5, []],
    )
    cases += _sweep(
        "music-search",
        path,
        {"keyword": "x"},
        "offset",
        [None, -1, 10001, 1.5, []],
    )
    cases += [
        (
            "music-search-limit-str",
            "POST",
            path,
            {"platform": "netease", "keyword": "x", "limit": "5"},
            200,
        ),
        (
            "music-search-limit-bool",
            "POST",
            path,
            {"platform": "netease", "keyword": "x", "limit": True},
            200,
        ),
        (
            "music-search-offset-str",
            "POST",
            path,
            {"platform": "netease", "keyword": "x", "offset": "5"},
            200,
        ),
    ]
    return cases


def _music_download_cases() -> list[tuple[Any, ...]]:
    path = "/api/v1/music/download"
    cases: list[tuple[Any, ...]] = [
        ("music-download-null-body", "POST", path, None, 422),
        ("music-download-list-body", "POST", path, [], 422),
        ("music-download-missing-tracks", "POST", path, {}, 422),
        ("music-download-empty-tracks", "POST", path, {"tracks": []}, 422),
        ("music-download-tracks-string", "POST", path, {"tracks": "x"}, 422),
        ("music-download-tracks-null", "POST", path, {"tracks": None}, 422),
        ("music-download-track-null", "POST", path, {"tracks": [None]}, 422),
        ("music-download-track-number", "POST", path, {"tracks": [5]}, 422),
        ("music-download-track-string", "POST", path, {"tracks": ["x"]}, 422),
        (
            "music-download-too-many",
            "POST",
            path,
            {"tracks": [{"id": str(i)} for i in range(201)]},
            422,
        ),
        (
            "music-download-max-tracks",
            "POST",
            path,
            {"tracks": [{"id": str(i)} for i in range(200)]},
            200,
        ),
        (
            "music-download-valid",
            "POST",
            path,
            {"tracks": [{"id": "1"}]},
            200,
        ),
        (
            "music-download-valid-platform",
            "POST",
            path,
            {"platform": "netease", "tracks": [{"id": "1"}]},
            200,
        ),
        (
            "music-download-valid-lax-int",
            "POST",
            path,
            {"tracks": [{"id": "1", "duration_s": "3", "track_no": 3.0}]},
            200,
        ),
        (
            "music-download-valid-lax-int-decimal",
            "POST",
            path,
            {"tracks": [{"id": "1", "duration_s": "3.0"}]},
            200,
        ),
        (
            "music-download-valid-lax-int-bool",
            "POST",
            path,
            {"tracks": [{"id": "1", "duration_s": True, "track_no": False}]},
            200,
        ),
        (
            "music-download-valid-lax-bool",
            "POST",
            path,
            {"tracks": [{"id": "1", "vip": "yes"}]},
            200,
        ),
        (
            "music-download-valid-headers",
            "POST",
            path,
            {
                "tracks": [
                    {
                        "id": "1",
                        "http_headers": {f"H{i}": "v" for i in range(32)},
                        "sources": ["netease"] * 32,
                    }
                ]
            },
            200,
        ),
        (
            "music-download-blank-search-id",
            "POST",
            path,
            {"tracks": [{"id": "1"}], "search_id": "   "},
            200,
        ),
        (
            "music-download-unknown-search-id",
            "POST",
            path,
            {"tracks": [{"id": "1"}], "search_id": "abc"},
            200,
        ),
        (
            "music-download-blank-level",
            "POST",
            path,
            {"tracks": [{"id": "1"}], "level": "   "},
            200,
        ),
        (
            "music-download-blank-out-dir",
            "POST",
            path,
            {"tracks": [{"id": "1"}], "out_dir": "   "},
            200,
        ),
        (
            "music-download-unknown-platform",
            "POST",
            path,
            {"platform": "bogus", "tracks": [{"id": "1"}]},
            400,
        ),
    ]
    cases += _track_cases("music-download", path)
    cases += _sweep(
        "music-download",
        path,
        {"tracks": [{"id": "1"}]},
        "platform",
        [None, "", 5, True, [], {}, "a\x00b", "\ud800", "x" * 65],
    )
    cases += _sweep(
        "music-download",
        path,
        {"tracks": [{"id": "1"}]},
        "search_id",
        [5, True, [], {}, "a/b", "a\x00b", "x" * 65],
    )
    cases += _sweep(
        "music-download",
        path,
        {"tracks": [{"id": "1"}]},
        "level",
        [5, True, [], {}, "a\x00b", "\ud800", "x" * 65],
    )
    cases += _sweep(
        "music-download",
        path,
        {"tracks": [{"id": "1"}]},
        "out_dir",
        [5, True, [], {}, "a\x00b", "\ud800", "x" * 1025],
    )
    cases += _sweep(
        "music-download",
        path,
        {"tracks": [{"id": "1"}]},
        "out_dir",
        ["/etc", "C:\\Windows", "../escape"],
        expected=400,
    )
    return cases


def _music_export_cases() -> list[tuple[Any, ...]]:
    path = "/api/v1/music/export"
    cases: list[tuple[Any, ...]] = [
        ("music-export-null-body", "POST", path, None, 422),
        ("music-export-list-body", "POST", path, [], 422),
        ("music-export-missing-tracks", "POST", path, {}, 422),
        ("music-export-empty-tracks", "POST", path, {"tracks": []}, 422),
        ("music-export-tracks-string", "POST", path, {"tracks": "x"}, 422),
        ("music-export-track-null", "POST", path, {"tracks": [None]}, 422),
        ("music-export-too-many", "POST", path, {"tracks": [{"id": "1"}] * 201}, 422),
        ("music-export-valid", "POST", path, {"tracks": [{"id": "1"}]}, 200),
        (
            "music-export-valid-path",
            "POST",
            path,
            {"tracks": [{"id": "1", "path": "a.mp3"}], "out_path": "list.m3u8"},
            200,
        ),
        (
            "music-export-absolute-path",
            "POST",
            path,
            {"tracks": [{"id": "1"}], "out_path": "/etc/passwd"},
            400,
        ),
        (
            "music-export-windows-path",
            "POST",
            path,
            {"tracks": [{"id": "1"}], "out_path": "C:\\Windows\\evil.m3u8"},
            400,
        ),
        (
            "music-export-traversal",
            "POST",
            path,
            {"tracks": [{"id": "1"}], "out_path": "../escape.m3u8"},
            400,
        ),
        (
            "music-export-unknown-platform",
            "POST",
            path,
            {"platform": "bogus", "tracks": [{"id": "1"}]},
            400,
        ),
    ]
    cases += _track_cases("music-export", path)
    cases += _sweep(
        "music-export",
        path,
        {"tracks": [{"id": "1"}]},
        "platform",
        [None, "", 5, True, [], {}, "a\x00b", "\ud800", "x" * 65],
    )
    cases += _sweep(
        "music-export",
        path,
        {"tracks": [{"id": "1"}]},
        "out_path",
        [5, True, [], {}, "a\x00b", "\ud800", "x" * 1025],
    )
    return cases


def _music_health_cases() -> list[tuple[Any, ...]]:
    base = "/api/v1/music/health"
    return [
        ("health-valid", "GET", base, _MISSING, 200),
        ("health-timeout-empty", "GET", f"{base}?timeout=", _MISSING, 422),
        ("health-timeout-zero", "GET", f"{base}?timeout=0", _MISSING, 422),
        ("health-timeout-negative", "GET", f"{base}?timeout=-1", _MISSING, 422),
        ("health-timeout-over", "GET", f"{base}?timeout=61", _MISSING, 422),
        ("health-timeout-nan", "GET", f"{base}?timeout=nan", _MISSING, 422),
        ("health-timeout-inf", "GET", f"{base}?timeout=inf", _MISSING, 422),
        ("health-timeout-text", "GET", f"{base}?timeout=x", _MISSING, 422),
        ("health-timeout-last", "GET", f"{base}?timeout=1&timeout=999", _MISSING, 422),
        ("health-valid-timeout", "GET", f"{base}?timeout=60", _MISSING, 200),
        ("health-platform-bogus", "GET", f"{base}?platform=bogus", _MISSING, 400),
        ("health-platform-empty", "GET", f"{base}?platform=", _MISSING, 400),
        (
            "health-platform-last",
            "GET",
            f"{base}?platform=netease&platform=bogus",
            _MISSING,
            400,
        ),
        ("health-valid-platform", "GET", f"{base}?platform=netease", _MISSING, 200),
    ]


def _music_job_cases() -> list[tuple[Any, ...]]:
    post = "/api/v1/music/jobs"
    get = "/api/v1/music/jobs/nope"
    cases: list[tuple[Any, ...]] = [
        ("job-null-body", "POST", post, None, 422),
        ("job-list-body", "POST", post, [], 422),
        ("job-missing-tracks", "POST", post, {}, 422),
        ("job-empty-tracks", "POST", post, {"tracks": []}, 422),
        (
            "job-unknown-platform",
            "POST",
            post,
            {"platform": "bogus", "tracks": [{"id": "1"}]},
            400,
        ),
        ("job-get-unknown", "GET", get, _MISSING, 404),
        ("job-get-encoded-slash", "GET", "/api/v1/music/jobs/a%2Fb", _MISSING, 404),
        ("job-get-control", "GET", "/api/v1/music/jobs/a%0Ab", _MISSING, 422),
        ("job-get-space", "GET", "/api/v1/music/jobs/bad%20id", _MISSING, 422),
        ("job-get-long", "GET", "/api/v1/music/jobs/" + "x" * 65, _MISSING, 422),
    ]
    cases += _sweep(
        "job",
        post,
        {"tracks": [{"id": "1"}]},
        "platform",
        [None, "", 5, True, [], {}, "a\x00b", "x" * 65],
    )
    cases += _sweep(
        "job",
        post,
        {"tracks": [{"id": "1"}]},
        "search_id",
        [5, True, [], {}, "a/b", "x" * 65],
    )
    return cases


def _history_cases() -> list[tuple[Any, ...]]:
    base = "/api/v1/history"
    return [
        ("history-valid", "GET", base, _MISSING, 200),
        ("history-offset-empty", "GET", f"{base}?offset=", _MISSING, 422),
        ("history-offset-negative", "GET", f"{base}?offset=-1", _MISSING, 422),
        ("history-offset-text", "GET", f"{base}?offset=x", _MISSING, 422),
        ("history-limit-zero", "GET", f"{base}?limit=0", _MISSING, 422),
        ("history-limit-over", "GET", f"{base}?limit=201", _MISSING, 422),
        ("history-limit-text", "GET", f"{base}?limit=x", _MISSING, 422),
        ("history-valid-page", "GET", f"{base}?offset=0&limit=1", _MISSING, 200),
        ("history-limit-max", "GET", f"{base}?limit=200", _MISSING, 200),
    ]


# ---------------------------------------------------------------------------
# tests (one per endpoint family, so the loopback server starts once each)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_contract(engines: Any) -> None:
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        await _run_cases(api, embedded, _search_cases())


@pytest.mark.asyncio
async def test_browse_contract(engines: Any) -> None:
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        await _run_cases(api, embedded, _browse_cases())


@pytest.mark.asyncio
async def test_tasks_contract(engines: Any) -> None:
    cases = _task_post_cases() + _task_query_cases()
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        await _run_cases(api, embedded, cases)


@pytest.mark.asyncio
async def test_task_filters_and_order_match(engines: Any) -> None:
    """Filtered/paginated task bodies are identical (filter + total order)."""
    paths = [
        "/api/v1/tasks?status=running",
        "/api/v1/tasks?status=queued",
        "/api/v1/tasks?site=qidian",
        "/api/v1/tasks?site=%20Qidian%20",
        "/api/v1/tasks?limit=2",
        "/api/v1/tasks?offset=1&limit=1",
        "/api/v1/tasks?status=completed&site=qidian",
    ]
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        for path in paths:
            api_status, api_body = await _call(api, "GET", path, _MISSING)
            emb_status, emb_body = await _call(embedded, "GET", path, _MISSING)
            assert api_status == emb_status == 200, (path, api_status, emb_status)
            assert emb_body == api_body, (path, api_body, emb_body)


@pytest.mark.asyncio
async def test_watchlist_contract(engines: Any) -> None:
    cases = _watchlist_cases() + _watchlist_check_cases()
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        await _run_cases(api, embedded, cases)


@pytest.mark.asyncio
async def test_login_contract(engines: Any) -> None:
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        await _run_cases(api, embedded, _login_cases())


@pytest.mark.asyncio
async def test_music_contract(engines: Any) -> None:
    cases = (
        _music_search_cases()
        + _music_download_cases()
        + _music_export_cases()
        + _music_health_cases()
        + _music_job_cases()
    )
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        await _run_cases(api, embedded, cases)


@pytest.mark.asyncio
async def test_history_contract(engines: Any) -> None:
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        await _run_cases(api, embedded, _history_cases())


# ---------------------------------------------------------------------------
# field-location parity for the fault classes this module pins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_field_locations_match(engines: Any) -> None:
    """Single-fault payloads report the same first error loc and type."""
    cases: list[tuple[str, str, str, Any]] = [
        (
            "search-timeout-null",
            "POST",
            "/api/v1/search",
            {"keyword": "x", "timeout": None},
        ),
        (
            "search-per-site-limit-null",
            "POST",
            "/api/v1/search",
            {"keyword": "x", "per_site_limit": None},
        ),
        ("search-sort-null", "POST", "/api/v1/search", {"keyword": "x", "sort": None}),
        (
            "search-timeout-overflow",
            "POST",
            "/api/v1/search",
            {"keyword": "x", "timeout": 10**400},
        ),
        (
            "browse-gender-null",
            "POST",
            "/api/v1/browse",
            {"site": "qidian", "gender": None},
        ),
        (
            "music-limit-null",
            "POST",
            "/api/v1/music/search",
            {"keyword": "x", "limit": None},
        ),
        (
            "track-url-invalid-ipv6",
            "POST",
            "/api/v1/music/download",
            {"tracks": [{"id": "1", "download_url": "http://["}]},
        ),
        ("tasks-status-enum", "GET", "/api/v1/tasks?status=bogus", _MISSING),
        ("tasks-site-value", "GET", "/api/v1/tasks?site=bad%20site", _MISSING),
        (
            "tasks-site-empty",
            "GET",
            "/api/v1/tasks?site=",
            _MISSING,
        ),
    ]
    async with (
        _client(engines.app) as api,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{engines.port}", timeout=30
        ) as embedded,
    ):
        for name, method, path, payload in cases:
            api_status, api_body = await _call(api, method, path, payload)
            emb_status, emb_body = await _call(embedded, method, path, payload)
            assert api_status == emb_status == 422, (name, api_status, emb_status)
            api_loc = [
                part
                for part in api_body["errors"][0]["loc"]
                if part not in ("body", "query", "path", "header")
            ]
            emb_loc = [
                part
                for part in emb_body["errors"][0]["loc"]
                if part not in ("body", "query", "path", "header")
            ]
            assert api_loc == emb_loc, (name, api_loc, emb_loc)
            assert api_body["errors"][0]["type"] == emb_body["errors"][0]["type"], (
                name,
                api_body["errors"][0],
                emb_body["errors"][0],
            )
