#!/usr/bin/env python3
"""
tests.apps.test_contract_parity
-------------------------------

Route-level anti-drift guard between the two ``/api/v1`` servers.

The Android embedded engine
(:mod:`novel_downloader.apps.embedded.server`) hand-mirrors the FastAPI
contract in :mod:`novel_downloader.apps.api.routes`.  Two independently
maintained implementations drift; ``test_error_messages_match_rest_constants``
pins the shared error wording, and this module pins the rest:

* **Route sets** — every ``(method, path)`` in the embedded ``ROUTES`` table
  must exist among the ``@router`` registrations of ``apps/api/routes.py``,
  and the REST-only routes must be exactly the documented gap (the three SSE
  ``/stream`` endpoints: the Android client polls, the stdlib server has no
  event-stream support).  Music routes are not allowed to be *missing*:
  the embedded server must register each one (as a stub) so a client that
  follows the REST contract never sees a route-miss ``404`` where the REST
  API answers ``503`` / ``404`` with a real payload.
* **Response shapes** — ``health`` / ``sites`` / ``tasks`` (plus search and
  task creation) are sampled on both servers backed by the *same* fake
  manager and search engine, so status codes, JSON bodies, field sets and
  the arguments forwarded to the engine must match.
* **Status semantics** — ``404`` (unknown route / task / watchlist book /
  music job) and ``405`` (+ ``Allow``) are sampled on both, and the music
  stubs must answer ``503`` with the same detail template the REST API
  emits without the ``music`` extras.

The REST side is exercised through ``httpx.ASGITransport`` (no network, no
auth token); the embedded side through a real loopback server on a dedicated
port.  ``GET /api/v1/music/platforms`` is a documented intentional stub
(``200 {"platforms": []}`` gates the Compose music UI; the REST API answers
``503`` without the extras) and is pinned on the embedded side only.
"""

from __future__ import annotations

import ast
import http.client
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from novel_downloader import __version__
from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.constants import (
    MSG_MUSIC_JOB_NOT_FOUND,
    MSG_MUSIC_UNAVAILABLE,
)
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.task_manager import manager
from novel_downloader.apps.embedded import server as eserver

#: Dedicated loopback port for this module (8765 is test_embedded_server's).
EMBEDDED_PORT = 8767

#: REST routes deliberately absent from the embedded ``ROUTES`` table.  The
#: Android client polls; the stdlib server has no Server-Sent Events support.
#: Anything else missing from the embedded table is drift and fails the tests
#: below (music routes must exist there as stubs, not be absent).
ALLOWED_API_ONLY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/search/stream"),
        ("GET", "/api/v1/tasks/stream"),
        ("GET", "/api/v1/login/requests/stream"),
    }
)

#: Verbs that can register a route on ``router``.
_ROUTE_VERBS = {"get", "post", "put", "delete", "patch", "options"}

_embedded_port = EMBEDDED_PORT


# ---------------------------------------------------------------------------
# route-table extraction
# ---------------------------------------------------------------------------


def _api_routes_from_source() -> set[tuple[str, str]]:
    """``(method, path)`` pairs of the ``@router`` registrations, from source."""
    tree = ast.parse(
        Path(api_routes.__file__).read_text(encoding="utf-8"),
        filename="routes.py",
    )
    prefix = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "router"
            for target in node.targets
        ):
            continue
        call = node.value
        if isinstance(call, ast.Call):
            for keyword in call.keywords:
                if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                    prefix = str(keyword.value.value)

    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute) or func.attr not in _ROUTE_VERBS:
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "router":
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                continue
            path = decorator.args[0].value
            if isinstance(path, str):
                routes.add((func.attr.upper(), prefix + path))
    return routes


def _api_routes_live() -> set[tuple[str, str]]:
    """``(method, path)`` pairs the built router actually serves."""
    routes: set[tuple[str, str]] = set()
    for route in api_routes.router.routes:
        for method in getattr(route, "methods", ()) or ():
            if method.upper() == "HEAD":
                continue
            routes.add((method.upper(), route.path))
    return routes


def _embedded_routes() -> set[tuple[str, str]]:
    return {(method.upper(), path) for (method, path) in eserver.ROUTES}


def test_api_route_extraction_matches_live_router() -> None:
    """Source decorators and the built router register the same routes."""
    extracted = _api_routes_from_source()
    live = _api_routes_live()
    assert extracted == live, (
        f"@router decorators minus live router: {sorted(extracted - live)}; "
        f"live router minus decorators: {sorted(live - extracted)}"
    )


def test_embedded_routes_are_an_api_subset() -> None:
    """No embedded route may exist outside the REST API's route table."""
    extra = _embedded_routes() - _api_routes_live()
    assert extra == set(), f"embedded-only routes: {sorted(extra)}"


def test_api_only_routes_are_exactly_the_documented_gap() -> None:
    """REST-only routes must be exactly the three SSE stream endpoints."""
    gap = _api_routes_live() - _embedded_routes()
    assert gap == set(ALLOWED_API_ONLY_ROUTES), (
        "REST routes missing from the embedded ROUTES table (register them "
        f"there, or extend ALLOWED_API_ONLY_ROUTES deliberately): "
        f"{sorted(gap - ALLOWED_API_ONLY_ROUTES)}; documented gaps removed: "
        f"{sorted(ALLOWED_API_ONLY_ROUTES - gap)}"
    )


def test_embedded_error_constants_pin_the_rest_wording() -> None:
    """The mirrored music-job wording cannot drift from the API constant."""
    assert eserver._MSG_MUSIC_JOB_NOT_FOUND == MSG_MUSIC_JOB_NOT_FOUND
    assert eserver._MSG_MUSIC_UNAVAILABLE == MSG_MUSIC_UNAVAILABLE


# ---------------------------------------------------------------------------
# shared fakes and the dual-engine fixture
# ---------------------------------------------------------------------------


def _fake_task(**overrides: Any) -> DownloadTask:
    task = DownloadTask(title="Sample", site="qidian", book_id="42")
    task.status = Status.RUNNING
    task.chapters_done = 3
    task.chapters_total = 9
    task.created_at = 1_700_000_000.0
    task.exported_paths = {"txt": Path("downloads/a.txt")}
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


_SAMPLE_RESULT: dict[str, Any] = {
    "site": "qidian",
    "book_id": "42",
    "book_url": "https://example.invalid/42",
    "cover_url": "",
    "title": "凡人修仙传",
    "author": "author",
    "latest_chapter": "ch1",
    "update_date": "2026-01-01",
    "word_count": "1万",
    "priority": 1,
}


@pytest.fixture()
def parity_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """
    Start both servers over the *same* fakes.

    The task-manager singleton and the search engine are replaced with
    recorders, then the REST app (ASGI, no network) and the embedded server
    (loopback HTTP) are both served from that shared state, so any body or
    forwarded-argument difference is drift, not test data.
    """
    home = tmp_path / "parity_home"
    home.mkdir(parents=True)
    original_cwd = os.getcwd()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)

    pending = _fake_task(status=Status.QUEUED, title="P1", book_id="101")
    running = _fake_task(title="R1", book_id="202", export_stage="txt")
    completed = _fake_task(
        status=Status.COMPLETED,
        title="C1",
        book_id="303",
        finished_at=1_700_000_100.0,
        error="boom",
    )
    snapshot: dict[str, list[DownloadTask]] = {
        "pending": [pending],
        "running": [running],
        "completed": [completed],
    }

    added = _fake_task(title="qidian (id = 42)", site="qidian", book_id="42")
    added.task_id = "added-fixed"
    add_calls: list[dict[str, Any]] = []
    search_calls: list[dict[str, Any]] = []

    async def fake_add_task(**kwargs: Any) -> DownloadTask:
        add_calls.append(dict(kwargs))
        return added

    async def fake_cancel(task_id: str) -> bool:
        return task_id == added.task_id

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        search_calls.append({"keyword": keyword, **kwargs})
        return [dict(_SAMPLE_RESULT)]

    # Both engines read the same singleton manager / search function; the
    # REST routes bound ``search`` at import time, the embedded handlers
    # import it per request, so both bindings are patched.
    monkeypatch.setattr(manager, "snapshot", lambda: snapshot)
    monkeypatch.setattr(manager, "add_task", fake_add_task)
    monkeypatch.setattr(manager, "cancel_task", fake_cancel)
    monkeypatch.setattr(api_routes, "search", fake_search)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    port = eserver.start_server(str(home), EMBEDDED_PORT)
    global _embedded_port
    _embedded_port = port
    application = create_app(token="", output_dir=str(tmp_path / "downloads"))
    try:
        yield SimpleNamespace(
            app=application,
            port=port,
            add_calls=add_calls,
            search_calls=search_calls,
            running_id=running.task_id,
        )
    finally:
        eserver.stop_server()
        # ``start_server`` -> ``_prepare_home`` chdirs into the app home; this
        # file sorts before cwd-sensitive tests (e.g. test_desktop_app's
        # argv0 absolutising), so the original cwd is restored here.
        os.chdir(original_cwd)


def _embedded_call(method: str, path: str, body: Any = None) -> tuple[int, Any, str]:
    """One request against the embedded server: ``(status, json, allow)``."""
    connection = http.client.HTTPConnection("127.0.0.1", _embedded_port, timeout=10)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    allow = response.getheader("Allow") or ""
    connection.close()
    payload: Any = json.loads(raw.decode("utf-8")) if raw else None
    return status, payload, allow


async def _api_call(
    client: httpx.AsyncClient, method: str, path: str, body: Any = None
) -> tuple[int, Any, str]:
    """One request against the REST app: ``(status, json, allow)``."""
    response = await client.request(method, path, json=body)
    payload: Any = response.json() if response.content else None
    return response.status_code, payload, response.headers.get("allow") or ""


def _shape(value: Any) -> Any:
    """Structure of a JSON value: field names and types, values erased."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return type(value).__name__


def _api_client(parity: SimpleNamespace) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=parity.app),
        base_url="http://testserver",
    )


# ---------------------------------------------------------------------------
# response-shape sampling (shared manager mock, both engines)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_and_sites_shape_parity(
    parity_engines: SimpleNamespace,
) -> None:
    async with _api_client(parity_engines) as client:
        api_status, api_health, _ = await _api_call(client, "GET", "/api/v1/health")
        emb_status, emb_health, _ = _embedded_call("GET", "/api/v1/health")
        assert (emb_status, emb_health) == (api_status, api_health)
        assert emb_health == {"status": "ok", "version": __version__}

        api_status, api_sites, _ = await _api_call(client, "GET", "/api/v1/sites")
        emb_status, emb_sites, _ = _embedded_call("GET", "/api/v1/sites")
        assert api_status == emb_status == 200
        assert _shape(emb_sites) == _shape(api_sites)
        # both import the same support-site constants, so bodies are equal
        assert emb_sites == api_sites
        assert set(api_sites) == {"search", "download"}


@pytest.mark.asyncio
async def test_tasks_shape_and_pagination_parity(
    parity_engines: SimpleNamespace,
) -> None:
    async with _api_client(parity_engines) as client:
        api_status, full, _ = await _api_call(client, "GET", "/api/v1/tasks")
        emb_status, emb_full, _ = _embedded_call("GET", "/api/v1/tasks")
        assert (emb_status, emb_full) == (api_status, full)
        assert set(full) == {
            "pending",
            "running",
            "completed",
            "total",
            "offset",
            "limit",
        }
        assert (full["total"], full["offset"], full["limit"]) == (3, 0, 3)

        for path in ("/api/v1/tasks?limit=2", "/api/v1/tasks?offset=1&limit=1"):
            api_status, api_body, _ = await _api_call(client, "GET", path)
            emb_status, emb_body, _ = _embedded_call("GET", path)
            assert api_status == emb_status == 200
            assert _shape(emb_body) == _shape(api_body)
            # both sides serialise the same task objects, so bodies match
            # field for field (null vs "", pagination echoes included)
            assert emb_body == api_body


@pytest.mark.asyncio
async def test_single_task_and_create_task_parity(
    parity_engines: SimpleNamespace,
) -> None:
    async with _api_client(parity_engines) as client:
        task_path = f"/api/v1/tasks/{parity_engines.running_id}"
        api_status, api_body, _ = await _api_call(client, "GET", task_path)
        emb_status, emb_body, _ = _embedded_call("GET", task_path)
        assert (api_status, api_body) == (emb_status, emb_body)
        assert api_status == 200
        assert set(api_body) == {
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

        create_body = {"site": "qidian", "book_id": "42"}
        api_status, api_body, _ = await _api_call(
            client, "POST", "/api/v1/tasks", create_body
        )
        emb_status, emb_body, _ = _embedded_call("POST", "/api/v1/tasks", create_body)
        assert (api_status, api_body) == (emb_status, emb_body)
        assert api_status == 200
        # both engines forwarded the same arguments (incl. the default title)
        assert parity_engines.add_calls == [
            {"title": "qidian (id = 42)", "site": "qidian", "book_id": "42"},
            {"title": "qidian (id = 42)", "site": "qidian", "book_id": "42"},
        ]


@pytest.mark.asyncio
async def test_search_request_forwarding_parity(
    parity_engines: SimpleNamespace,
) -> None:
    async with _api_client(parity_engines) as client:
        body = {"keyword": "凡人", "per_site_limit": 2, "group": True}
        api_status, api_body, _ = await _api_call(
            client, "POST", "/api/v1/search", body
        )
        emb_status, emb_body, _ = _embedded_call("POST", "/api/v1/search", body)
        assert (api_status, api_body) == (emb_status, emb_body)
        assert api_status == 200
        assert set(api_body) == {"total", "results"}
        # both engines saw the same request, ``group`` included
        assert parity_engines.search_calls[0] == parity_engines.search_calls[1]
        assert parity_engines.search_calls[0] == {
            "keyword": "凡人",
            "sites": None,
            "limit": None,
            "per_site_limit": 2,
            "timeout": 5.0,
            "sort": "quality",
            "order": None,
            "group": True,
        }

        # Lax-bool boundary parity: pydantic accepts 1/1.0/"t"/"y" (and
        # their falsy twins), rejects whitespace-padded strings and
        # explicit null; the embedded parser must agree on every edge.
        boundary_cases: list[tuple[Any, int]] = [
            (1, 200),
            (0, 200),
            (1.0, 200),
            (0.0, 200),
            ("t", 200),
            ("y", 200),
            ("f", 200),
            ("n", 200),
            ("T", 200),
            ("Y", 200),
            (" true ", 422),
            ("true ", 422),
            (None, 422),
            (2, 422),
            ("junk", 422),
        ]
        for value, expected_status in boundary_cases:
            body = {"keyword": "凡人", "group": value}
            api_status, _api_body, _ = await _api_call(
                client, "POST", "/api/v1/search", body
            )
            emb_status, _emb_body, _ = _embedded_call("POST", "/api/v1/search", body)
            assert api_status == emb_status == expected_status, (
                value,
                api_status,
                emb_status,
            )


# ---------------------------------------------------------------------------
# status-code semantics sampling (404 / 405, both engines)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_semantics_parity(parity_engines: SimpleNamespace) -> None:
    cases: list[tuple[str, str, Any]] = [
        ("GET", "/api/v1/does-not-exist", None),
        ("GET", "/api/v1/tasks/zzz", None),
        ("POST", "/api/v1/tasks/zzz/cancel", {}),
        ("DELETE", "/api/v1/watchlist/s/zzz", None),
    ]
    async with _api_client(parity_engines) as client:
        for method, path, body in cases:
            api_status, api_body, _ = await _api_call(client, method, path, body)
            emb_status, emb_body, _ = _embedded_call(method, path, body)
            assert (emb_status, emb_body) == (api_status, api_body), (
                f"{method} {path}: embedded {(emb_status, emb_body)} vs "
                f"REST {(api_status, api_body)}"
            )
            assert api_status == 404
            assert set(api_body) == {"detail"}


@pytest.mark.asyncio
async def test_405_semantics_parity(parity_engines: SimpleNamespace) -> None:
    async with _api_client(parity_engines) as client:
        # single-method path: status, body and the Allow header all match
        api_status, api_body, api_allow = await _api_call(
            client, "POST", "/api/v1/health", {}
        )
        emb_status, emb_body, emb_allow = _embedded_call("POST", "/api/v1/health", {})
        assert (api_status, api_body, api_allow) == (
            emb_status,
            emb_body,
            emb_allow,
        )
        assert api_status == 405
        assert api_body == {"detail": "Method Not Allowed"}
        assert "GET" in api_allow

        # multi-method paths: status and body match (Starlette lists only the
        # first matching route's verbs in Allow while the embedded server
        # lists every verb registered for the path, so only coverage of the
        # REST list is required)
        for method, path in (
            ("PUT", "/api/v1/tasks"),
            ("OPTIONS", "/api/v1/tasks"),
            ("DELETE", "/api/v1/search"),
        ):
            api_status, api_body, api_allow = await _api_call(client, method, path)
            emb_status, emb_body, emb_allow = _embedded_call(method, path)
            assert (emb_status, emb_body) == (api_status, api_body)
            assert api_status == 405
            api_verbs = {verb for verb in api_allow.split(",") if verb}
            emb_verbs = {verb for verb in emb_allow.split(",") if verb}
            assert api_verbs, (method, path, api_allow)
            assert api_verbs <= emb_verbs, (method, path, api_allow, emb_allow)
            assert method not in emb_verbs


@pytest.mark.asyncio
async def test_music_stub_status_semantics_parity(
    parity_engines: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``None`` in sys.modules makes the real _load_music_components raise its
    # 503, mirroring an install without the music extras (same trick as
    # test_api.py).  The embedded engine never bundles the extras.
    monkeypatch.setitem(sys.modules, "mutagen", None)
    unavailable_prefix = MSG_MUSIC_UNAVAILABLE.split("{error}")[0]
    bodies: list[tuple[str, str, Any]] = [
        ("POST", "/api/v1/music/search", {"platform": "netease", "keyword": "x"}),
        (
            "POST",
            "/api/v1/music/export",
            {"platform": "netease", "tracks": [{"id": "1"}]},
        ),
        (
            "POST",
            "/api/v1/music/download",
            {"platform": "netease", "tracks": [{"id": "1"}]},
        ),
        (
            "POST",
            "/api/v1/music/jobs",
            {"platform": "netease", "tracks": [{"id": "1"}]},
        ),
        ("GET", "/api/v1/music/health", None),
    ]
    async with _api_client(parity_engines) as client:
        for method, path, body in bodies:
            api_status, api_body, _ = await _api_call(client, method, path, body)
            emb_status, emb_body, _ = _embedded_call(method, path, body)
            assert api_status == emb_status == 503, (method, path)
            assert api_body["detail"].startswith(unavailable_prefix)
            assert emb_body["detail"].startswith(unavailable_prefix)

        # unknown async music job: 404 with the shared wording on both sides
        api_status, api_body, _ = await _api_call(
            client, "GET", "/api/v1/music/jobs/nope"
        )
        emb_status, emb_body, _ = _embedded_call("GET", "/api/v1/music/jobs/nope")
        assert (emb_status, emb_body) == (
            api_status,
            {"detail": MSG_MUSIC_JOB_NOT_FOUND.format(job_id="nope")},
        )


def test_music_platforms_stub_is_the_documented_empty_list() -> None:
    """The Compose music UI gates on this stub (see server.py's docstring)."""
    handler = eserver.ROUTES[("GET", "/api/v1/music/platforms")]
    assert handler({}, {}, {}) == {"platforms": []}
