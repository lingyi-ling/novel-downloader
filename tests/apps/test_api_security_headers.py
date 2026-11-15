#!/usr/bin/env python3
"""
tests.apps.test_api_security_headers
------------------------------------

Contract tests for the REST API's security response headers and its CORS
surface, plus the documented differences of the Android/desktop embedded
engine.

Header contract pinned here (constants live in
``novel_downloader.apps.api.middleware`` and ``...api.app``):

===========================================  ==================================
Response class                               Required headers
===========================================  ==================================
``/api/v1/*`` success                        ``X-Content-Type-Options: nosniff``,
                                             ``Referrer-Policy: no-referrer``,
                                             ``X-Request-ID``,
                                             ``Cache-Control`` per path
``/api/v1/*`` error (400/401/404/413/415/    same security headers and
422/429/500)                                 ``X-Request-ID``, plus
                                             ``Cache-Control: no-store``
``/downloads/*``                             the security headers,
                                             ``Content-Security-Policy:
                                             sandbox; default-src 'none'``,
                                             ``Cache-Control: public,
                                             max-age=3600``; never a JSON
                                             content type
CORS preflight (``--cors``)                  the security headers,
                                             ``Access-Control-Allow-Origin``;
                                             no ``Access-Control-Allow-
                                             Credentials`` ever
===========================================  ==================================

CORS is opt-in (``create_app(cors=True)`` / ``novel-api --cors``). With it
off, no ``Access-Control-*`` header is emitted and a browser request carrying
a non-loopback ``Origin`` is rejected by
:class:`~novel_downloader.apps.api.middleware.OriginCheckMiddleware` with
``403``. With it on, the wildcard origin is allowed but credentials are
disabled, and the preflight admits only the verbs/headers the API uses.

The embedded engine (``apps/embedded/server.py``) deliberately has **no CORS
layer** (it is loopback-only and speaks to native clients), so these tests
assert the absence of ``Access-Control-*`` there and pin the headers it does
share with REST (``nosniff``/``no-referrer``/``X-Request-ID``, the downloads
CSP). Its lack of the REST ``Cache-Control`` policy is recorded, not
"fixed": the engine answers native clients, not browsers.

Everything is offline: REST requests go through ``httpx.ASGITransport``; the
embedded checks use a loopback listener the test starts itself.
"""

from __future__ import annotations

import http.client
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.app import (
    CORS_ALLOW_HEADERS,
    CORS_ALLOW_METHODS,
    CORS_EXPOSE_HEADERS,
)
from novel_downloader.apps.api.middleware import (
    CACHE_CONTROL_NO_STORE,
    CACHE_CONTROL_PRIVATE,
    CACHE_CONTROL_PUBLIC,
    DOWNLOADS_CSP,
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
    SecurityHeadersMiddleware,
)
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.common import history_files
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.embedded import server as embedded_server

#: The two headers every API/download response must carry.
NOSNIFF = ("X-Content-Type-Options", "nosniff")
NO_REFERRER = ("Referrer-Policy", "no-referrer")

#: A foreign origin: not loopback, so the default-mode gate rejects it.
FOREIGN_ORIGIN = "https://evil.example"

#: A loopback origin: passes the default-mode gate (locally served page).
LOOPBACK_ORIGIN = "http://127.0.0.1:5173"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class FakeManager:
    """In-memory stand-in so task endpoints never queue real downloads."""

    def __init__(self) -> None:
        self.pending: list[DownloadTask] = []
        self.running: list[DownloadTask] = []
        self.completed: list[DownloadTask] = []

    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        task = DownloadTask(title=title, site=site, book_id=book_id)
        self.pending.append(task)
        return task

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {
            "pending": list(self.pending),
            "running": list(self.running),
            "completed": list(self.completed),
        }

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


@pytest.fixture
def fake_manager() -> FakeManager:
    return FakeManager()


@pytest.fixture
def make_app(tmp_path: Path, fake_manager: FakeManager) -> Any:
    """Factory building apps whose ``/downloads`` and task manager are fakes."""

    def _make(**kwargs: Any) -> FastAPI:
        kwargs.setdefault("output_dir", tmp_path / "downloads")
        kwargs.setdefault("rate_limit", None)
        application = create_app(**kwargs)
        application.dependency_overrides[get_manager] = lambda: fake_manager
        return application

    return _make


@asynccontextmanager
async def _client(
    app: FastAPI, *, raise_app_exceptions: bool = True
) -> AsyncIterator[httpx.AsyncClient]:
    """Serve ``app`` in-process without a socket."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


def _assert_security_headers(response: httpx.Response) -> None:
    """Every governed response carries the two baseline headers."""
    assert response.headers[NOSNIFF[0]] == NOSNIFF[1], response.request.url
    assert response.headers[NO_REFERRER[0]] == NO_REFERRER[1], response.request.url


def _assert_correlation_id(response: httpx.Response) -> None:
    """``X-Request-ID`` is present and non-empty on every response."""
    assert response.headers.get(REQUEST_ID_HEADER, "").strip(), response.request.url


def _write_download(tmp_path: Path, name: str, body: str) -> Path:
    """Create a file inside the app's ``/downloads`` directory."""
    output_dir = tmp_path / "downloads"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------


def test_security_header_contract_is_the_two_baseline_headers() -> None:
    """``nosniff`` + ``no-referrer``; no document policy on the API itself."""
    assert SECURITY_HEADERS == (NOSNIFF, NO_REFERRER)
    # The sandbox policy belongs to /downloads only, never to JSON API data.
    assert "sandbox" in DOWNLOADS_CSP


def test_cors_contract_admits_only_the_verbs_and_headers_the_api_uses() -> None:
    """A wildcard method/header list would widen the preflight for no gain."""
    assert CORS_ALLOW_METHODS == ("GET", "POST", "DELETE", "HEAD", "OPTIONS")
    assert "PUT" not in CORS_ALLOW_METHODS
    assert "PATCH" not in CORS_ALLOW_METHODS
    assert "*" not in CORS_ALLOW_HEADERS
    assert {"Authorization", "X-API-Key", REQUEST_ID_HEADER} <= set(CORS_ALLOW_HEADERS)
    # Browser clients must be able to read the retry hint and the request id.
    assert {"Retry-After", REQUEST_ID_HEADER} <= set(CORS_EXPOSE_HEADERS)


# ---------------------------------------------------------------------------
# Default responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/api/v1/health", "/api/v1/sites", "/api/v1/tasks", "/api/v1/login/requests"],
)
async def test_successful_api_responses_carry_security_headers(
    make_app: Any, path: str
) -> None:
    async with _client(make_app()) as client:
        response = await client.get(path)

    assert response.status_code == 200, response.text
    _assert_security_headers(response)
    _assert_correlation_id(response)
    # JSON data is not a document, so no sandbox policy is added.
    assert "content-security-policy" not in response.headers


@pytest.mark.asyncio
async def test_docs_pages_are_outside_the_governed_prefixes(make_app: Any) -> None:
    """
    ``/docs`` is not API JSON and is documented as outside the policy.

    This pins the boundary: only ``/api/v1`` and ``/downloads`` are governed,
    so a change that starts stamping every path is a contract change.
    """
    async with _client(make_app()) as client:
        response = await client.get("/docs")

    assert response.status_code == 200
    assert "x-content-type-options" not in response.headers


# ---------------------------------------------------------------------------
# CORS: default (off)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, LOOPBACK_ORIGIN])
async def test_default_mode_emits_no_cors_headers(
    make_app: Any, origin: str | None
) -> None:
    """Without ``--cors`` the API never advertises a cross-origin grant."""
    headers = {"Origin": origin} if origin else {}
    async with _client(make_app()) as client:
        response = await client.get("/api/v1/health", headers=headers)

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers
    assert "access-control-expose-headers" not in response.headers


@pytest.mark.asyncio
async def test_default_mode_rejects_foreign_origin_before_the_router(
    make_app: Any,
) -> None:
    """The gate's 403 is itself hardened (headers + id + no-store)."""
    async with _client(make_app()) as client:
        response = await client.get(
            "/api/v1/health", headers={"Origin": FOREIGN_ORIGIN}
        )

    assert response.status_code == 403
    assert "detail" in response.json()
    _assert_security_headers(response)
    _assert_correlation_id(response)
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_default_mode_has_no_cors_middleware(make_app: Any) -> None:
    """The absence of the grant is structural, not accidental."""
    app = make_app()
    assert CORSMiddleware not in [mw.cls for mw in app.user_middleware]


# ---------------------------------------------------------------------------
# CORS: enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cors_enabled_allows_any_origin_without_credentials(
    make_app: Any,
) -> None:
    async with _client(make_app(cors=True)) as client:
        response = await client.get(
            "/api/v1/health", headers={"Origin": FOREIGN_ORIGIN}
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    # ``*`` + credentials is forbidden; the API authenticates with headers.
    assert "access-control-allow-credentials" not in response.headers
    _assert_security_headers(response)
    _assert_correlation_id(response)


@pytest.mark.asyncio
async def test_cors_middleware_never_enables_credentials(make_app: Any) -> None:
    """Pin the configuration, not just the observed response."""
    app = make_app(cors=True)
    entries = [mw for mw in app.user_middleware if mw.cls is CORSMiddleware]
    assert len(entries) == 1
    assert entries[0].kwargs["allow_origins"] == ["*"]
    assert entries[0].kwargs["allow_credentials"] is False
    assert entries[0].kwargs["allow_methods"] == list(CORS_ALLOW_METHODS)
    assert entries[0].kwargs["allow_headers"] == list(CORS_ALLOW_HEADERS)
    assert entries[0].kwargs["expose_headers"] == list(CORS_EXPOSE_HEADERS)


@pytest.mark.asyncio
async def test_preflight_admits_configured_methods_and_headers(
    make_app: Any,
) -> None:
    async with _client(make_app(cors=True)) as client:
        response = await client.options(
            "/api/v1/search",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "authorization,content-type,x-api-key,x-request-id"
                ),
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]
    allow_headers = response.headers["access-control-allow-headers"].lower()
    for name in ("authorization", "content-type", "x-api-key", "x-request-id"):
        assert name in allow_headers
    assert "access-control-allow-credentials" not in response.headers
    # The preflight itself is stamped like any other response.
    _assert_security_headers(response)
    _assert_correlation_id(response)


@pytest.mark.asyncio
async def test_preflight_rejects_a_method_the_api_does_not_expose(
    make_app: Any,
) -> None:
    async with _client(make_app(cors=True)) as client:
        response = await client.options(
            "/api/v1/search",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "PUT",
            },
        )

    assert response.status_code == 400
    assert response.headers["access-control-allow-origin"] == "*"
    _assert_security_headers(response)
    _assert_correlation_id(response)


@pytest.mark.asyncio
async def test_preflight_rejects_an_unlisted_request_header(make_app: Any) -> None:
    async with _client(make_app(cors=True)) as client:
        response = await client.options(
            "/api/v1/search",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-not-allowed",
            },
        )

    assert response.status_code == 400
    _assert_security_headers(response)


@pytest.mark.asyncio
async def test_no_origin_means_no_cors_header_even_when_enabled(
    make_app: Any,
) -> None:
    """The wildcard is only emitted for a request that actually has an Origin."""
    async with _client(make_app(cors=True)) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_security_headers_wrap_the_cors_layer(make_app: Any) -> None:
    """
    Ordering contract: security headers are *outside* CORS.

    Otherwise the preflight responses CORSMiddleware generates itself (the
    ``200`` and the ``400`` for a disallowed verb/header) would be the only
    responses without ``nosniff``/``no-referrer``.
    """
    order = [mw.cls for mw in make_app(cors=True).user_middleware]
    assert order.index(SecurityHeadersMiddleware) < order.index(CORSMiddleware)


# ---------------------------------------------------------------------------
# Error responses: every status, same hardening
# ---------------------------------------------------------------------------


def _origin_headers(origin: str | None, **extra: str) -> dict[str, str]:
    """Merge an optional ``Origin`` into a header dict."""
    headers = dict(extra)
    if origin is not None:
        headers["Origin"] = origin
    return headers


async def _error_400(app: FastAPI, origin: str | None = None) -> httpx.Response:
    async with _client(app) as client:
        return await client.get(
            "/api/v1/browse/categories",
            params={"site": "not-a-real-site"},
            headers=_origin_headers(origin),
        )


async def _error_401(app: FastAPI, origin: str | None = None) -> httpx.Response:
    async with _client(app) as client:
        return await client.get("/api/v1/tasks", headers=_origin_headers(origin))


async def _error_404(app: FastAPI, origin: str | None = None) -> httpx.Response:
    async with _client(app) as client:
        return await client.get(
            "/api/v1/does-not-exist", headers=_origin_headers(origin)
        )


async def _error_413(app: FastAPI, origin: str | None = None) -> httpx.Response:
    async with _client(app) as client:
        return await client.post(
            "/api/v1/search",
            content=json.dumps({"keyword": "x" * 100}).encode(),
            headers=_origin_headers(origin, **{"Content-Type": "application/json"}),
        )


async def _error_415(app: FastAPI, origin: str | None = None) -> httpx.Response:
    async with _client(app) as client:
        return await client.post(
            "/api/v1/search",
            json={"keyword": "x"},
            headers=_origin_headers(origin, **{"Content-Encoding": "gzip"}),
        )


async def _error_422(app: FastAPI, origin: str | None = None) -> httpx.Response:
    async with _client(app) as client:
        return await client.post(
            "/api/v1/search", json={}, headers=_origin_headers(origin)
        )


async def _error_429(app: FastAPI, origin: str | None = None) -> httpx.Response:
    async with _client(app) as client:
        first = await client.get("/api/v1/tasks", headers=_origin_headers(origin))
        assert first.status_code == 200, first.text
        return await client.get("/api/v1/tasks", headers=_origin_headers(origin))


async def _error_500(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    origin: str | None = None,
) -> httpx.Response:
    async def exploding_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(api_routes, "search", exploding_search)
    async with _client(app, raise_app_exceptions=False) as client:
        return await client.post(
            "/api/v1/search",
            json={"keyword": "x"},
            headers=_origin_headers(origin),
        )


#: ``(status label, producer)`` pairs. Every producer takes the app, an
#: optional ``Origin`` and — for the ``500`` only — the monkeypatch fixture.
_ERROR_CASES = [
    ("400", _error_400),
    ("401", _error_401),
    ("404", _error_404),
    ("413", _error_413),
    ("415", _error_415),
    ("422", _error_422),
    ("429", _error_429),
    ("500", _error_500),
]


def _build_error_app(make_app: Any, label: str, *, cors: bool = False) -> FastAPI:
    """Build an app configured so the named status is reachable."""
    return make_app(
        cors=cors,
        token="secret-token" if label == "401" else None,
        max_body_bytes=16 if label == "413" else 1024 * 1024,
        rate_limit=1 if label == "429" else None,
    )


async def _produce_error(
    label: str,
    produce: Any,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    origin: str | None = None,
) -> httpx.Response:
    if label == "500":
        return await produce(app, monkeypatch, origin)
    return await produce(app, origin)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "produce"), _ERROR_CASES, ids=[case[0] for case in _ERROR_CASES]
)
async def test_error_responses_carry_security_headers_and_request_id(
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    produce: Any,
) -> None:
    response = await _produce_error(
        label, produce, _build_error_app(make_app, label), monkeypatch
    )

    assert response.status_code == int(label), response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()
    _assert_security_headers(response)
    _assert_correlation_id(response)
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE
    # The API JSON is not a document; the sandbox CSP stays on /downloads.
    assert "content-security-policy" not in response.headers
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "produce"), _ERROR_CASES, ids=[case[0] for case in _ERROR_CASES]
)
async def test_error_responses_keep_cors_headers_when_enabled(
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    produce: Any,
) -> None:
    """
    In ``--cors`` mode a browser must be able to read the error body.

    Every custom layer that can answer directly (auth 401, body cap 413/415,
    limiter 429, origin-independent routing 404, validation 422) sits inside
    the CORS layer, and the catch-all 500 stamps the grant itself, so the
    origin header is present for every status.
    """
    response = await _produce_error(
        label,
        produce,
        _build_error_app(make_app, label, cors=True),
        monkeypatch,
        FOREIGN_ORIGIN,
    )

    assert response.status_code == int(label), response.text
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
    _assert_security_headers(response)
    _assert_correlation_id(response)
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_downloads_error_keeps_the_sandbox_policy(
    make_app: Any,
) -> None:
    """A missing export is a JSON 404 *and* still carries the downloads CSP."""
    async with _client(make_app()) as client:
        response = await client.get("/downloads/missing.txt")

    assert response.status_code == 404
    assert response.headers["content-security-policy"] == DOWNLOADS_CSP
    _assert_security_headers(response)
    _assert_correlation_id(response)
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


# ---------------------------------------------------------------------------
# Cache-Control on sensitive responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/tasks",
        "/api/v1/tasks/unknown-task",
        "/api/v1/login/requests",
        "/api/v1/music/jobs/unknown-job",
    ],
)
async def test_sensitive_responses_are_never_stored(make_app: Any, path: str) -> None:
    """Task state, credential requests and job state must not be cached."""
    async with _client(make_app()) as client:
        response = await client.get(path)

    assert response.status_code in (200, 404), response.text
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_created_task_response_is_not_stored(make_app: Any) -> None:
    async with _client(make_app()) as client:
        response = await client.post(
            "/api/v1/tasks",
            json={"title": "book", "site": "qidian", "book_id": "1"},
        )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_reference_data_is_privately_cacheable(
    make_app: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the explicitly listed reference endpoints get a positive max-age."""
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    monkeypatch.setattr(history_files, "_output_dir", lambda: history_dir)

    async with _client(make_app()) as client:
        sites = await client.get("/api/v1/sites")
        history = await client.get("/api/v1/history")

    for response in (sites, history):
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == CACHE_CONTROL_PRIVATE


# ---------------------------------------------------------------------------
# Downloads: files are not JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_response_is_a_file_with_sandbox_headers(
    make_app: Any, tmp_path: Path
) -> None:
    _write_download(tmp_path, "book.txt", "download-body-canary")

    async with _client(make_app()) as client:
        response = await client.get("/downloads/book.txt")

    assert response.status_code == 200
    assert response.text == "download-body-canary"
    # StaticFiles picks the type from the extension; never the API's JSON.
    assert response.headers["content-type"].startswith("text/plain")
    assert "application/json" not in response.headers["content-type"]
    assert response.headers["content-security-policy"] == DOWNLOADS_CSP
    _assert_security_headers(response)
    assert response.headers["cache-control"] == CACHE_CONTROL_PUBLIC


@pytest.mark.asyncio
async def test_download_html_export_is_sandboxed(make_app: Any, tmp_path: Path) -> None:
    """An exported HTML file is served but cannot script the API origin."""
    _write_download(tmp_path, "book.html", "<script>fetch('/api/v1/tasks')</script>")

    async with _client(make_app()) as client:
        response = await client.get("/downloads/book.html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    assert "sandbox" in policy
    assert "default-src 'none'" in policy
    _assert_security_headers(response)


@pytest.mark.asyncio
async def test_download_head_response_carries_the_same_headers(
    make_app: Any, tmp_path: Path
) -> None:
    _write_download(tmp_path, "book.txt", "canary")

    async with _client(make_app()) as client:
        response = await client.head("/downloads/book.txt")

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == DOWNLOADS_CSP
    _assert_security_headers(response)
    assert response.headers["cache-control"] == CACHE_CONTROL_PUBLIC


# ---------------------------------------------------------------------------
# Embedded engine: documented differences
# ---------------------------------------------------------------------------


@pytest.fixture
def embedded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Start the embedded engine on a loopback port with a temp home."""
    home = tmp_path / "embedded_home"
    downloads = home / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "book.txt").write_text("embedded-body", encoding="utf-8")
    monkeypatch.setattr(history_files, "_output_dir", lambda: downloads)

    original_cwd = os.getcwd()
    port = embedded_server.start_server(str(home), 0)
    try:
        yield port
    finally:
        embedded_server.stop_server()
        os.chdir(original_cwd)


def _embedded_request(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
) -> tuple[int, bytes, dict[str, str]]:
    """One request against the real embedded listener."""
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        return (
            response.status,
            raw,
            {name.lower(): value for name, value in response.getheaders()},
        )
    finally:
        connection.close()


def test_embedded_engine_has_no_cors_surface(embedded: int) -> None:
    """
    Documented difference: the embedded engine has no CORS layer.

    It binds ``127.0.0.1`` only and is called by native clients (Android
    Compose / desktop bridge), so no origin grant is ever advertised — a
    browser therefore cannot read a cross-origin response from it. The
    request itself still executes; that residual risk is covered by the
    loopback bind, not by a header.
    """
    status, _, headers = _embedded_request(
        embedded, "GET", "/api/v1/health", {"Origin": FOREIGN_ORIGIN}
    )
    assert status == 200
    assert not any(name.startswith("access-control-") for name in headers)


def test_embedded_engine_stamps_security_headers_on_success_and_errors(
    embedded: int,
) -> None:
    """Parity with REST for the headers that do not depend on a browser layer."""
    cases = [
        ("GET", "/api/v1/health", None, 200),
        ("GET", "/api/v1/does-not-exist", None, 404),
        ("GET", "/downloads/missing.txt", None, 404),
    ]
    for method, path, headers, expected in cases:
        status, _, response_headers = _embedded_request(embedded, method, path, headers)
        assert status == expected, path
        assert response_headers["x-content-type-options"] == "nosniff", path
        assert response_headers["referrer-policy"] == "no-referrer", path
        assert response_headers.get("x-request-id", "").strip(), path


def test_embedded_download_is_sandboxed_and_not_json(embedded: int) -> None:
    status, raw, headers = _embedded_request(embedded, "GET", "/downloads/book.txt")

    assert status == 200
    assert raw == b"embedded-body"
    assert headers["content-type"] == "application/octet-stream"
    assert headers["content-security-policy"] == DOWNLOADS_CSP
    assert headers["x-content-type-options"] == "nosniff"


def test_embedded_engine_has_no_rest_cache_policy(embedded: int) -> None:
    """
    Recorded difference: the embedded engine sends no ``Cache-Control``.

    The REST policy (``no-store`` for task/credential state) protects a
    browser-reachable HTTP surface. The embedded engine serves native clients
    over loopback and deliberately keeps the stdlib-only stack; adding a cache
    policy there would be a behaviour change, not a parity fix. If a browser
    is ever pointed at the embedded port, this test is the marker to revisit.
    """
    for path in ("/api/v1/health", "/api/v1/tasks", "/api/v1/does-not-exist"):
        _, _, headers = _embedded_request(embedded, "GET", path)
        assert "cache-control" not in headers, path


def test_embedded_engine_never_enables_credentials(embedded: int) -> None:
    """A preflight gets no CORS answer at all, let alone a credential grant."""
    status, _, headers = _embedded_request(
        embedded,
        "OPTIONS",
        "/api/v1/tasks",
        {
            "Origin": FOREIGN_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )

    # ``OPTIONS`` is not a route: the engine answers 405 with ``Allow``.
    assert status == 405
    assert "allow" in headers
    assert not any(name.startswith("access-control-") for name in headers)
