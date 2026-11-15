#!/usr/bin/env python3
"""
tests.apps.test_api_rate_limit_contract
---------------------------------------

Wire-contract tests for the two documented ``429`` paths of the REST API and
the embedded engine:

* the per-client-IP sliding-window limiter
  (:class:`novel_downloader.apps.api.middleware.RateLimitMiddleware`), which
  answers ``429 {"detail": "Rate limit exceeded. Try again in <n>s."}`` with a
  ``Retry-After`` header on every governed ``/api/v1`` route except
  ``/api/v1/health``; and
* the music-job *load* signal (:data:`novel_downloader.apps.api.routes.
  MAX_ACTIVE_MUSIC_JOBS`), which answers ``429`` with the fixed
  ``Retry-After: 5`` once four jobs are queued/running.

The tests pin the documented promises in ``docs/guide/api.md``:

* ``429`` bodies keep the uniform ``{"detail": "..."}`` shape and never embed
  the correlation id;
* ``Retry-After`` is a positive whole number of seconds, equals the ceiling of
  the remaining sliding window, never exceeds the window, and waiting for it
  admits the same client again (``2xx`` or the music route's ``202``);
* every ``429`` also carries ``X-Request-ID`` (the outermost middleware runs
  for direct middleware answers too);
* buckets are per TCP peer: a different IP gets its own budget, the same IP on
  a different source port shares one, and a different client cannot spend
  another client's slot;
* ``rate_limit <= 0`` is rejected by the public factory (disabling is
  ``rate_limit=None``), while the middleware itself treats ``<= 0`` as a
  pass-through if constructed directly;
* ``MAX_RATE_LIMIT`` is accepted; its ``429`` is unreachable in a test without
  a million-plus request burst, so the documentable behaviour is pinned
  instead of the rejection;
* the embedded engine has **no per-IP request limiter** (documented as a
  REST-only feature); it still emits the same music-job ``429`` with
  ``Retry-After: 5`` and ``X-Request-ID``.

No network access: REST requests use ``httpx.ASGITransport``, embedded
requests use a real loopback listener started on an OS-assigned port.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import middleware as api_middleware
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.app import MAX_RATE_LIMIT
from novel_downloader.apps.api.middleware import RateLimitMiddleware
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.embedded import server as eserver
from novel_downloader.apps.music.providers.base import Track

API = "/api/v1"

#: Exact ``detail`` template of the per-IP limiter (``middleware.py``).
RATE_LIMIT_DETAIL_RE = re.compile(r"Rate limit exceeded\. Try again in (\d+)s\.")

#: A valid body for ``POST /api/v1/music/jobs``; the engine is faked below.
MUSIC_JOB_BODY: dict[str, Any] = {
    "platform": "all",
    "tracks": [{"id": "1", "name": "contract"}],
}


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class FakeManager:
    """Minimal in-memory task manager for ``GET /api/v1/tasks``."""

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {"pending": [], "running": [], "completed": []}

    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        return DownloadTask(title=title, site=site, book_id=book_id)

    async def close(self) -> None:
        pass


class FakeClock:
    """Deterministic replacement for the limiter's ``_monotonic`` hook."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the developer profile, token or webhook configuration."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(api_middleware, "_monotonic", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_music_state() -> Iterator[None]:
    api_routes._MUSIC_JOBS.clear()
    try:
        yield
    finally:
        api_routes._MUSIC_JOBS.clear()


def _music_components(
    root: Path,
    *,
    gate: threading.Event | None = None,
    calls: list[int] | None = None,
) -> Any:
    """Fake music engine: no network, optional blocking for concurrency tests."""
    root.mkdir(parents=True, exist_ok=True)
    recorded = calls if calls is not None else []

    def download(
        _create_provider: Any,
        tracks: list[Any],
        _options: Any,
        *,
        on_result: Any = None,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        recorded.append(len(tracks))
        if gate is not None:
            gate.wait(5.0)
        for index, track in enumerate(tracks):
            if on_result is not None:
                on_result(
                    index,
                    track,
                    SimpleNamespace(
                        status="ok",
                        reason="",
                        path="",
                        bytes_=1,
                    ),
                )
        return len(tracks), 0, 0, [], []

    return api_routes._MusicComponents(
        paths=SimpleNamespace(default_download_dir=lambda: root),
        download_options=lambda out_dir, level: SimpleNamespace(
            out_dir=out_dir, level=level
        ),
        download_many=download,
        download_mixed=download,
        available_labels=lambda: [("netease", "NetEase")],
        create=lambda _key: SimpleNamespace(),
        platform_note=lambda _key: "",
        track_cls=Track,
        export_m3u=None,
        health_check=None,
        load_settings=lambda: {"out_dir": str(root)},
    )


@pytest.fixture(autouse=True)
def _fake_music(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Install an offline music engine for every test in this module."""
    components = _music_components(tmp_path / "music")
    monkeypatch.setattr(api_routes, "_load_music_components", lambda: components)
    monkeypatch.setattr(eserver, "_music_components", lambda: components)
    return components


def _make_app(
    tmp_path: Path,
    *,
    manager: Any | None = None,
    **kwargs: Any,
) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads", **kwargs)
    application.dependency_overrides[get_manager] = lambda: manager or FakeManager()
    return application


def _client(
    application: FastAPI,
    *,
    ip: str = "203.0.113.7",
    port: int = 1234,
) -> httpx.AsyncClient:
    """A REST client whose ASGI peer is fixed, i.e. a stable limiter bucket."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=(ip, port)),
        base_url="http://testserver",
    )


def _assert_request_id(
    response: httpx.Response, *, expected: str | None = None
) -> None:
    value = response.headers.get("X-Request-ID")
    assert value and value.strip(), response.headers
    if expected is not None:
        assert value == expected


def _assert_rate_limit_429(
    response: httpx.Response,
    *,
    retry_after: int,
    window: float,
) -> None:
    """Pin the documented per-IP limiter error shape and headers."""
    assert response.status_code == 429, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == str(retry_after)
    assert 1 <= retry_after <= window

    body = response.json()
    assert set(body) == {"detail"}, body
    match = RATE_LIMIT_DETAIL_RE.fullmatch(body["detail"])
    assert match is not None, body["detail"]
    assert int(match.group(1)) == retry_after
    _assert_request_id(response)


def _assert_music_jobs_429(
    response: httpx.Response,
    *,
    limit: int,
    expected_retry_after: int,
) -> None:
    """Pin the documented music-job load-signal 429 (not the IP limiter)."""
    assert response.status_code == 429, response.text
    body = response.json()
    assert set(body) == {"detail"}, body
    assert body["detail"] == api_routes.MSG_MUSIC_JOBS_BUSY.format(limit=limit)
    assert response.headers["retry-after"] == str(expected_retry_after)
    _assert_request_id(response)


# ---------------------------------------------------------------------------
# Per-IP limiter across the documented REST endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "admitted_status"),
    [
        ("GET", f"{API}/tasks", None, 200),
        ("GET", f"{API}/sites", None, 200),
        ("GET", f"{API}/music/platforms", None, 200),
        ("POST", f"{API}/music/jobs", MUSIC_JOB_BODY, 202),
    ],
    ids=["tasks", "sites", "music-platforms", "music-jobs"],
)
async def test_rate_limiter_429_on_documented_endpoints(
    tmp_path: Path,
    clock: FakeClock,
    method: str,
    path: str,
    payload: Any,
    admitted_status: int,
) -> None:
    """
    Every governed ``/api/v1`` path answers the same 429 shape.

    ``/api/v1/health`` is the documented exception (pinned separately in the
    existing middleware suite); ``/music/jobs`` is included because it is the
    route integration clients hammer hardest.
    """
    application = _make_app(
        tmp_path,
        rate_limit=2,
        rate_limit_window=60.0,
    )
    async with _client(application) as http_client:
        for _ in range(2):
            admitted = await http_client.request(method, path, json=payload)
            assert admitted.status_code == admitted_status, admitted.text
            _assert_request_id(admitted)

        rejected = await http_client.request(method, path, json=payload)

    _assert_rate_limit_429(rejected, retry_after=60, window=60.0)


@pytest.mark.asyncio
async def test_music_jobs_recovers_with_202_after_the_window(
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    """
    ``Retry-After`` is the ceiling of the remaining window and is honest.

    A single admitted request at ``t=1000`` with a 60 s window; at ``t=1012.5``
    the slot frees at ``t=1060``, so the advertising value is ``ceil(47.5)``.
    Waiting exactly that long must let the same client create a job again.
    """
    application = _make_app(
        tmp_path,
        rate_limit=1,
        rate_limit_window=60.0,
    )
    async with _client(application) as http_client:
        first = await http_client.post(
            f"{API}/music/jobs",
            json=MUSIC_JOB_BODY,
            headers={"X-Request-ID": "contract-music-job"},
        )
        assert first.status_code == 202, first.text
        assert first.headers["X-Request-ID"] == "contract-music-job"

        clock.advance(12.5)
        rejected = await http_client.post(
            f"{API}/music/jobs",
            json=MUSIC_JOB_BODY,
            headers={"X-Request-ID": "contract-rate-limited"},
        )
        _assert_rate_limit_429(rejected, retry_after=48, window=60.0)
        # The client-supplied id is echoed and the body never repeats it.
        assert rejected.headers["X-Request-ID"] == "contract-rate-limited"
        assert "contract-rate-limited" not in rejected.text

        # The advertised wait is sufficient: t=1060.5 > 1000 + 60.
        clock.advance(48)
        recovered = await http_client.post(f"{API}/music/jobs", json=MUSIC_JOB_BODY)

    assert recovered.status_code == 202, recovered.text
    _assert_request_id(recovered)


@pytest.mark.asyncio
async def test_buckets_are_per_tcp_peer_not_per_request(
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    """
    The limiter keys on the TCP peer address, exactly as documented.

    A different IP has an independent budget; the same IP on another source
    port shares the existing bucket. ``X-Forwarded-For`` never creates one.
    """
    application = _make_app(tmp_path, rate_limit=1, rate_limit_window=60.0)

    async with (
        _client(application, ip="203.0.113.10", port=1000) as client_a,
        _client(application, ip="203.0.113.11", port=2000) as client_b,
        _client(application, ip="203.0.113.10", port=3000) as client_a_other_port,
    ):
        assert (await client_a.get(f"{API}/tasks")).status_code == 200
        assert (await client_a.get(f"{API}/tasks")).status_code == 429

        # Another source port is still the same IP and therefore the same bucket.
        assert (await client_a_other_port.get(f"{API}/tasks")).status_code == 429

        # Another IP was never affected by A's exhausted budget.
        assert (await client_b.get(f"{API}/tasks")).status_code == 200
        assert (await client_b.get(f"{API}/tasks")).status_code == 429

        # A forged forwarded address cannot spend from a fresh bucket either.
        forged = await client_a.get(
            f"{API}/tasks",
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        assert forged.status_code == 429


# ---------------------------------------------------------------------------
# Music-job concurrency 429 (load signal, not the per-IP limiter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_job_capacity_429_is_distinguishable_from_rate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The active-job cap answers ``429`` with ``Retry-After: 5`` and its own
    detail; the per-IP limiter is explicitly disabled so only the cap can fire.
    """
    gate = threading.Event()
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _music_components(tmp_path / "music", gate=gate),
    )
    application = _make_app(tmp_path, rate_limit=None)
    monkeypatch.setattr(api_routes, "MAX_ACTIVE_MUSIC_JOBS", 1)

    async with _client(application) as http_client:
        try:
            first = await http_client.post(f"{API}/music/jobs", json=MUSIC_JOB_BODY)
            assert first.status_code == 202, first.text
            _assert_request_id(first)

            rejected = await http_client.post(f"{API}/music/jobs", json=MUSIC_JOB_BODY)
            _assert_music_jobs_429(
                rejected,
                limit=1,
                expected_retry_after=api_routes.MUSIC_JOBS_RETRY_AFTER_SECONDS,
            )
            assert rejected.headers["retry-after"] == "5"
            # Not the rate limiter's variable value: the detail differs.
            assert not RATE_LIMIT_DETAIL_RE.fullmatch(rejected.json()["detail"])
        finally:
            gate.set()

        # Let the blocked worker finish before the autouse fixture clears the
        # job table; otherwise a still-running thread would update a removed id
        # (harmless, but messy at teardown).
        job_id = first.json()["job_id"]
        for _ in range(100):
            status = await http_client.get(f"{API}/music/jobs/{job_id}")
            if status.json()["status"] in {"done", "failed"}:
                break
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# Configuration boundaries
# ---------------------------------------------------------------------------


class _Downstream:
    """Counting ASGI app used to observe whether the limiter passed a request."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _scope() -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"{API}/tasks",
        "raw_path": f"{API}/tasks".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("10.0.0.1", 5000),
        "server": ("testserver", 80),
    }


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _run_one(middleware: Any) -> int:
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await middleware(_scope(), _receive, send)
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    return int(start["status"])


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, -1000])
async def test_non_positive_middleware_limit_is_a_pass_through(
    clock: FakeClock,
    limit: int,
) -> None:
    """
    The *internal* middleware disables itself for ``limit <= 0``.

    This value is not reachable through the public API: ``create_app`` rejects
    it and asks for ``rate_limit=None`` instead (pinned next).
    """
    downstream = _Downstream()
    middleware = RateLimitMiddleware(downstream, limit=limit, window=60.0)

    for _ in range(130):
        assert await _run_one(middleware) == 200

    assert downstream.calls == 130
    assert middleware._hits == {}


@pytest.mark.parametrize("limit", [0, -1, MAX_RATE_LIMIT + 1])
def test_create_app_rejects_out_of_range_limits(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="rate_limit"):
        create_app(output_dir=tmp_path / "downloads", rate_limit=limit)


@pytest.mark.asyncio
async def test_max_limit_is_accepted_but_its_429_is_unreachable_in_tests(
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    """
    ``MAX_RATE_LIMIT`` is the accepted ceiling.

    A test cannot send ``MAX_RATE_LIMIT + 1`` requests, so the only checkable
    half is that the middleware installs and admits a representative burst
    without overflowing (and that ``MAX + 1`` is rejected above).
    """
    application = create_app(
        output_dir=tmp_path / "downloads", rate_limit=MAX_RATE_LIMIT
    )
    assert RateLimitMiddleware in [
        middleware.cls for middleware in application.user_middleware
    ]

    downstream = _Downstream()
    middleware = RateLimitMiddleware(downstream, limit=MAX_RATE_LIMIT, window=60.0)
    for _ in range(200):
        assert await _run_one(middleware) == 200
    assert downstream.calls == 200


# ---------------------------------------------------------------------------
# Embedded engine: documented rate-limiting difference
# ---------------------------------------------------------------------------


def _embedded_request(
    port: int,
    method: str,
    path: str,
    body: Any = None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    """One request against the real embedded listener."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    try:
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        header_map = {name.lower(): value for name, value in response.getheaders()}
        status = response.status
    finally:
        connection.close()
    decoded: Any = json.loads(raw.decode("utf-8")) if raw else None
    return status, decoded, header_map


@pytest.fixture
def embedded(
    tmp_path: Path,
    _fake_music: Any,
) -> Iterator[SimpleNamespace]:
    """Start the embedded engine on an OS-assigned loopback port."""
    home = tmp_path / "embedded_home"
    original_cwd = os.getcwd()
    port = eserver.start_server(str(home), 0)
    try:
        yield SimpleNamespace(port=port, components=_fake_music)
    finally:
        eserver.stop_server()
        os.chdir(original_cwd)
        eserver._MUSIC_JOBS.clear()


def test_embedded_engine_has_no_per_ip_rate_limiter(
    embedded: SimpleNamespace,
) -> None:
    """
    The per-IP sliding window is a REST-only feature (the docs describe it
    under the FastAPI server and mention only the job cap for the embedded
    engine). Well past the default 120-request window, the embedded engine
    still admits every request and never emits ``Retry-After``.
    """
    for _ in range(130):
        status, payload, headers = _embedded_request(
            embedded.port, "GET", f"{API}/sites"
        )
        assert status == 200, payload
        assert "retry-after" not in headers
        assert headers.get("x-request-id", "").strip()


def test_embedded_music_job_429_matches_rest(
    embedded: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Both transports emit the same music-job ``429``: same detail, same
    ``Retry-After: 5`` and an ``X-Request-ID`` header.
    """
    gate = threading.Event()
    components = _music_components(
        Path(embedded.components.paths.default_download_dir()), gate=gate
    )
    monkeypatch.setattr(eserver, "_music_components", lambda: components)
    monkeypatch.setattr(eserver, "_MAX_ACTIVE_MUSIC_JOBS", 1)

    try:
        status, payload, headers = _embedded_request(
            embedded.port, "POST", f"{API}/music/jobs", MUSIC_JOB_BODY
        )
        assert status == 202, payload
        assert headers.get("x-request-id", "").strip()

        status, payload, headers = _embedded_request(
            embedded.port,
            "POST",
            f"{API}/music/jobs",
            MUSIC_JOB_BODY,
            headers={"X-Request-ID": "embedded-contract-429"},
        )
        assert status == 429, payload
        assert payload == {"detail": api_routes.MSG_MUSIC_JOBS_BUSY.format(limit=1)}
        assert headers["retry-after"] == str(api_routes.MUSIC_JOBS_RETRY_AFTER_SECONDS)
        assert headers["x-request-id"] == "embedded-contract-429"
    finally:
        gate.set()


def test_embedded_and_rest_music_retry_after_constants_agree() -> None:
    """The duplicated embedded constant is pinned to the REST source of truth."""
    assert (
        eserver._MUSIC_JOBS_RETRY_AFTER_SECONDS
        == api_routes.MUSIC_JOBS_RETRY_AFTER_SECONDS
        == 5
    )
    assert eserver._MSG_MUSIC_JOBS_BUSY == api_routes.MSG_MUSIC_JOBS_BUSY


# ---------------------------------------------------------------------------
# OpenAPI advertises the same 429 contract
# ---------------------------------------------------------------------------


def test_openapi_429_response_is_documented_for_music_jobs(tmp_path: Path) -> None:
    """The generated schema documents 429 + Retry-After for ``/music/jobs``."""
    application = create_app(output_dir=tmp_path / "downloads", rate_limit=None)
    spec = application.openapi()
    operation = spec["paths"][f"{API}/music/jobs"]["post"]

    response_429 = operation["responses"]["429"]
    retry_after = response_429["headers"]["Retry-After"]
    assert retry_after["schema"]["type"] == "integer"
    # The generated schema is looser (``minimum: 0``) than the runtime contract
    # enforced by ``_assert_rate_limit_429`` above, which always requires >= 1.
    assert retry_after["schema"]["minimum"] == 0
