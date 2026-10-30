#!/usr/bin/env python3
"""
tests.apps.test_api_security
----------------------------

Security and robustness tests for the local REST API: token handling, static
file traversal, request limits, blocking-endpoint timeouts, error shape
consistency and music-job table pruning.

Everything is offline: requests go through ``httpx.ASGITransport`` and every
network-touching collaborator (search plugin, music engine) is replaced by a
fake via ``monkeypatch``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import middleware as api_middleware
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.common import history_files

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


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


@pytest.fixture
def make_app(tmp_path: Path) -> Any:
    """Factory building apps whose ``/downloads`` points at ``tmp_path``."""

    def _make(**kwargs: Any) -> FastAPI:
        kwargs.setdefault("output_dir", tmp_path / "downloads")
        return create_app(**kwargs)

    return _make


@pytest.fixture
def clean_music_jobs() -> Any:
    """Start and end each affected test with an empty music job table."""
    api_routes._MUSIC_JOBS.clear()
    yield
    api_routes._MUSIC_JOBS.clear()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_token_configured_means_no_auth(make_app: Any) -> None:
    """The localhost default (no token) must keep working unauthenticated."""
    async with _client(make_app()) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_auth_accepts_non_ascii_token(make_app: Any) -> None:
    """
    Tokens outside the ASCII range must not break the constant-time comparison.

    ``secrets.compare_digest`` raises ``TypeError`` for non-ASCII ``str`` values,
    so the API compares UTF-8 encodings instead. The header is sent as raw bytes
    because HTTP header values are byte strings (Starlette decodes them as
    latin-1), which is exactly how such a token arrives in practice.
    """
    token = "café-token"
    app = make_app(token=token)
    async with _client(app) as client:
        bearer = await client.get(
            "/api/v1/health",
            headers={"Authorization": "Bearer café-token".encode("latin-1")},
        )
        api_key = await client.get(
            "/api/v1/health",
            headers={"X-API-Key": "café-token".encode("latin-1")},
        )

    assert bearer.status_code == 200
    assert api_key.status_code == 200


@pytest.mark.asyncio
async def test_auth_rejects_non_ascii_token_mismatch(make_app: Any) -> None:
    app = make_app(token="café-token")
    async with _client(app) as client:
        response = await client.get(
            "/api/v1/health",
            headers={"Authorization": "Bearer café-token-x".encode("latin-1")},
        )

    assert response.status_code == 401
    assert response.json()["detail"]


@pytest.mark.asyncio
async def test_auth_rejects_unknown_scheme(make_app: Any) -> None:
    app = make_app(token="secret-token")
    async with _client(app) as client:
        response = await client.get(
            "/api/v1/health",
            headers={"Authorization": "Basic secret-token"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


# ---------------------------------------------------------------------------
# Auth: /downloads static files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_downloads_require_token_when_configured(
    tmp_path: Path, make_app: Any
) -> None:
    """With a token set, the static mount is protected like ``/api/v1``."""
    _prepare_downloads(tmp_path)
    app = make_app(token="secret-token")

    async with _client(app) as client:
        missing = await client.get("/downloads/visible.txt")
        missing_head = await client.head("/downloads/visible.txt")
        wrong = await client.get(
            "/downloads/visible.txt",
            headers={"Authorization": "Bearer wrong-token"},
        )
        wrong_key = await client.get(
            "/downloads/visible.txt", headers={"X-API-Key": "wrong-token"}
        )
        bearer = await client.get(
            "/downloads/visible.txt",
            headers={"Authorization": "Bearer secret-token"},
        )
        api_key = await client.get(
            "/downloads/visible.txt", headers={"X-API-Key": "secret-token"}
        )

    for denied in (missing, missing_head, wrong, wrong_key):
        assert denied.status_code == 401, denied.text
        assert denied.headers["www-authenticate"] == "Bearer"
        assert "public" not in denied.text

    assert missing.json() == {"detail": "Invalid or missing API token"}
    assert bearer.status_code == 200
    assert bearer.text == "public"
    assert api_key.status_code == 200
    assert api_key.text == "public"


@pytest.mark.asyncio
async def test_downloads_are_open_when_no_token_configured(
    tmp_path: Path, make_app: Any
) -> None:
    """The localhost-only default (no token) keeps serving files unauthenticated."""
    _prepare_downloads(tmp_path)
    app = make_app()

    async with _client(app) as client:
        response = await client.get("/downloads/visible.txt")
        head = await client.head("/downloads/visible.txt")

    assert response.status_code == 200
    assert response.text == "public"
    assert head.status_code == 200


@pytest.mark.asyncio
async def test_downloads_token_guard_does_not_match_lookalike_prefix(
    tmp_path: Path, make_app: Any
) -> None:
    """A path such as ``/downloadsfoo`` must not be treated as the mount."""
    app = make_app(token="secret-token")

    async with _client(app) as client:
        response = await client.get("/downloadsfoo")
        health = await client.get("/api/v1/health")

    assert response.status_code == 404
    assert health.status_code == 401


@pytest.mark.asyncio
async def test_downloads_traversal_still_rejected_with_valid_token(
    tmp_path: Path, make_app: Any
) -> None:
    """Authentication must not weaken Starlette's traversal protection."""
    _prepare_downloads(tmp_path)
    app = make_app(token="secret-token")
    headers = {"Authorization": "Bearer secret-token"}

    async with _client(app) as client:
        for path in (
            "/downloads/../secret.toml",
            "/downloads/%2e%2e/secret.toml",
            "/downloads/..%2fsecret.toml",
            "/downloads/%2e%2e%2fsecret.toml",
            "/downloads/..%2f..%2fsecret.toml",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code in (400, 403, 404), (path, response.text)
            assert "SECRET_MARKER" not in response.text, path

        served = await client.get("/downloads/visible.txt", headers=headers)

    assert served.status_code == 200
    assert served.text == "public"


@pytest.mark.asyncio
async def test_downloads_401_keeps_cors_headers(tmp_path: Path, make_app: Any) -> None:
    """The 401 from the static guard must pass back through CORS."""
    _prepare_downloads(tmp_path)
    app = make_app(token="secret-token", cors=True)

    async with _client(app) as client:
        response = await client.get(
            "/downloads/visible.txt", headers={"Origin": "http://example.test"}
        )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "*"


# ---------------------------------------------------------------------------
# Static files: no directory traversal
# ---------------------------------------------------------------------------


def _prepare_downloads(tmp_path: Path) -> Path:
    """Create the served dir plus a sibling file that must stay unreachable."""
    downloads = tmp_path / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    (downloads / "visible.txt").write_text("public", encoding="utf-8")
    (tmp_path / "secret.toml").write_text("SECRET_MARKER=1", encoding="utf-8")
    return downloads


@pytest.mark.asyncio
async def test_downloads_rejects_directory_traversal(
    tmp_path: Path, make_app: Any
) -> None:
    """
    Percent-encoded ``..`` segments reach Starlette's StaticFiles as such and
    must be refused; the neighbouring repository file must never be served.

    The variants are percent-encoded because httpx itself collapses a literal
    ``../`` (that request is checked separately below).
    """
    _prepare_downloads(tmp_path)
    app = make_app()

    async with _client(app) as client:
        for path in (
            "/downloads/../secret.toml",
            "/downloads/%2e%2e/secret.toml",
            "/downloads/..%2fsecret.toml",
            "/downloads/%2e%2e%2fsecret.toml",
            "/downloads/..%2f..%2fsecret.toml",
        ):
            response = await client.get(path)
            assert response.status_code in (400, 403, 404), (path, response.text)
            assert "SECRET_MARKER" not in response.text, path

        served = await client.get("/downloads/visible.txt")
        health = await client.get("/api/v1/health")

    assert served.status_code == 200
    assert served.text == "public"
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_downloads_serves_percent_encoded_unicode_name(
    tmp_path: Path, make_app: Any
) -> None:
    downloads = _prepare_downloads(tmp_path)
    name = "空 格 #1.txt"
    (downloads / name).write_text("book", encoding="utf-8")
    app = make_app()

    async with _client(app) as client:
        response = await client.get("/downloads/%E7%A9%BA%20%E6%A0%BC%20%231.txt")

    assert response.status_code == 200
    assert response.text == "book"


@pytest.mark.asyncio
async def test_history_download_url_round_trips(
    tmp_path: Path,
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``download_url`` must percent-encode the name (including ``#``) so the value
    returned by ``/history`` can be fetched back verbatim.
    """
    downloads = _prepare_downloads(tmp_path)
    name = "空 格 #1.txt"
    (downloads / name).write_text("book-bytes", encoding="utf-8")
    monkeypatch.setattr(history_files, "_output_dir", lambda: downloads)

    app = make_app()
    async with _client(app) as client:
        listing = await client.get("/api/v1/history")
        assert listing.status_code == 200
        files = listing.json()["files"]
        entry = next(item for item in files if item["name"] == name)

        assert entry["download_url"] == "/downloads/%E7%A9%BA%20%E6%A0%BC%20%231.txt"
        fetched = await client.get(entry["download_url"])

    assert fetched.status_code == 200
    assert fetched.text == "book-bytes"


# ---------------------------------------------------------------------------
# Request body limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/search",
        "/api/v1/music/search",
        "/api/v1/music/download",
        "/api/v1/music/jobs",
    ],
)
async def test_oversized_body_is_rejected_with_413(path: str, make_app: Any) -> None:
    app = make_app(max_body_bytes=256)
    body = b"x" * 4000

    async with _client(app) as client:
        response = await client.post(
            path, content=body, headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "too large" in detail
    assert "256" in detail


@pytest.mark.asyncio
async def test_body_within_limit_is_accepted(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)

    app = make_app(max_body_bytes=512)
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/search", json={"keyword": "斗破", "sites": ["demo"]}
        )

    assert response.status_code == 200
    assert response.json() == {"total": 0, "results": []}


def test_only_json_endpoints_are_body_limited(make_app: Any) -> None:
    """Only the guarded endpoints are limited; others pass through untouched."""
    app = make_app(max_body_bytes=64)
    guarded = api_middleware.BodySizeLimitMiddleware(app, max_bytes=64)

    assert guarded._applies("POST", "/api/v1/search")
    assert guarded._applies("POST", "/api/v1/music/jobs/")
    assert not guarded._applies("POST", "/api/v1/tasks")
    assert not guarded._applies("GET", "/api/v1/search")


@pytest.mark.asyncio
async def test_chunked_body_without_content_length_is_still_limited(
    make_app: Any,
) -> None:
    """
    A body sent without ``Content-Length`` must be counted as it arrives, so the
    limit cannot be bypassed by streaming the payload.
    """

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b"x" * 100

    app = make_app(max_body_bytes=256)
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/search",
            content=chunks(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert "too large" in response.json()["detail"]


@pytest.mark.asyncio
async def test_body_limit_response_keeps_cors_headers(make_app: Any) -> None:
    """A 413 must still carry CORS headers so browser clients can read it."""
    app = make_app(max_body_bytes=64, cors=True)
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/search",
            content=b"x" * 500,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://example.test",
            },
        )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "*"


# ---------------------------------------------------------------------------
# Track-count limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/music/download", "/api/v1/music/jobs"])
async def test_too_many_tracks_is_rejected_with_422(path: str, make_app: Any) -> None:
    from novel_downloader.apps.api.schemas import MAX_MUSIC_TRACKS

    assert MAX_MUSIC_TRACKS > 0
    tracks = [{"id": str(index)} for index in range(MAX_MUSIC_TRACKS + 1)]
    app = make_app()

    async with _client(app) as client:
        response = await client.post(path, json={"platform": "demo", "tracks": tracks})

    assert response.status_code == 422
    payload = response.json()
    assert isinstance(payload["detail"], str)
    assert str(MAX_MUSIC_TRACKS) in payload["detail"]
    assert payload["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/music/download", "/api/v1/music/jobs"])
async def test_track_count_at_limit_passes_validation(
    path: str, make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Exactly ``MAX_MUSIC_TRACKS`` tracks are schema-valid: with the music extras
    unavailable the request proceeds far enough to hit the ``503`` guard instead
    of ``422``.
    """
    from novel_downloader.apps.api.schemas import MAX_MUSIC_TRACKS

    monkeypatch.setitem(sys.modules, "mutagen", None)
    tracks = [{"id": str(index)} for index in range(MAX_MUSIC_TRACKS)]
    app = make_app()

    async with _client(app) as client:
        response = await client.post(path, json={"platform": "demo", "tracks": tracks})

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Timeouts on blocking endpoints
# ---------------------------------------------------------------------------


def test_blocking_timeout_defaults_and_override() -> None:
    defaults = api_routes.BLOCKING_TIMEOUTS
    assert set(defaults) == {"search", "music_search", "music_download"}
    assert all(value > 0 for value in defaults.values())

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(api_timeouts={"search": 0.5}))
    )
    assert api_routes._blocking_timeout(request, "search") == 0.5
    assert (
        api_routes._blocking_timeout(request, "music_download")
        == defaults["music_download"]
    )

    # A malformed override must not break the endpoint.
    broken = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(api_timeouts={"search": "abc"}))
    )
    assert api_routes._blocking_timeout(broken, "search") == defaults["search"]


@pytest.mark.asyncio
async def test_search_timeout_returns_504(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hanging_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(api_routes, "search", hanging_search)

    app = make_app(timeouts={"search": 0.02})
    async with _client(app) as client:
        response = await client.post("/api/v1/search", json={"keyword": "x"})

    assert response.status_code == 504
    detail = response.json()["detail"]
    assert "Search timed out" in detail
    assert "0.02" in detail


@pytest.mark.asyncio
async def test_music_search_timeout_returns_504(
    make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def slow_search(keyword: str, limit: int, offset: int = 0) -> tuple[list[Any], int]:
        time.sleep(1.0)
        return [], 0

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(search=slow_search, out_dir=tmp_path),
    )

    app = make_app(timeouts={"music_search": 0.02})
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/music/search", json={"platform": "demo", "keyword": "x"}
        )

    assert response.status_code == 504
    assert "Music search timed out" in response.json()["detail"]


@pytest.mark.asyncio
async def test_music_download_timeout_returns_504(
    make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def slow_download(
        provider: Any, tracks: list[Any], options: Any, *, on_result: Any
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        time.sleep(1.0)
        return len(tracks), 0, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(download_many=slow_download, out_dir=tmp_path),
    )

    app = make_app(timeouts={"music_download": 0.02})
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/music/download",
            json={"platform": "demo", "tracks": [{"id": "1"}]},
        )

    assert response.status_code == 504
    assert "Music download timed out" in response.json()["detail"]


@pytest.mark.asyncio
async def test_music_job_timeout_marks_job_failed(
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clean_music_jobs: Any,
) -> None:
    def slow_download(
        provider: Any, tracks: list[Any], options: Any, *, on_result: Any
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        time.sleep(1.0)
        return len(tracks), 0, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(download_many=slow_download, out_dir=tmp_path),
    )

    app = make_app(timeouts={"music_download": 0.02})
    async with _client(app) as client:
        created = await client.post(
            "/api/v1/music/jobs",
            json={"platform": "demo", "tracks": [{"id": "1"}]},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        payload = await _wait_for_job(client, job_id)

    assert payload["status"] == "failed"
    assert "timed out" in payload["error"]
    assert payload["progress"] == {"done": 0, "total": 1}


# ---------------------------------------------------------------------------
# Error shape consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unhandled_exception_returns_generic_500(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exploding_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("leak /home/user/secret-token-abc")

    monkeypatch.setattr(api_routes, "search", exploding_search)

    app = make_app()
    async with _client(app, raise_app_exceptions=False) as client:
        response = await client.post("/api/v1/search", json={"keyword": "x"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "leak" not in response.text
    assert "secret-token-abc" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_routing_errors_carry_detail(tmp_path: Path, make_app: Any) -> None:
    """Unknown routes, wrong methods and missing static files share the shape."""
    _prepare_downloads(tmp_path)
    app = make_app()

    async with _client(app) as client:
        unknown = await client.get("/api/v1/nope")
        wrong_method = await client.get("/api/v1/search")
        missing_file = await client.get("/downloads/nope.txt")

    assert unknown.status_code == 404
    assert wrong_method.status_code == 405
    assert missing_file.status_code == 404
    for response in (unknown, wrong_method, missing_file):
        assert isinstance(response.json()["detail"], str), response.status_code


@pytest.mark.asyncio
async def test_validation_error_detail_is_readable(make_app: Any) -> None:
    app = make_app()
    async with _client(app) as client:
        response = await client.post("/api/v1/search", json={})

    assert response.status_code == 422
    payload = response.json()
    assert isinstance(payload["detail"], str)
    assert "keyword" in payload["detail"]
    assert isinstance(payload["errors"], list)
    assert payload["errors"][0]["loc"][-1] == "keyword"


@pytest.mark.asyncio
async def test_error_responses_always_carry_detail(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """401/404/413/422/504 all share the ``{"detail": ...}`` shape."""
    from novel_downloader.apps.core.task_manager import manager as real_manager

    app = make_app(token="secret-token", max_body_bytes=128)
    assert real_manager.snapshot() is not None  # offline: no network touched

    async with _client(app) as client:
        unauthorized = await client.get("/api/v1/health")
        not_found = await client.get(
            "/api/v1/tasks/missing",
            headers={"Authorization": "Bearer secret-token"},
        )
        too_large = await client.post(
            "/api/v1/search",
            content=b"x" * 1000,
            headers={
                "Authorization": "Bearer secret-token",
                "Content-Type": "application/json",
            },
        )
        invalid = await client.post(
            "/api/v1/search",
            json={},
            headers={"Authorization": "Bearer secret-token"},
        )

    for response in (unauthorized, not_found, too_large, invalid):
        assert "detail" in response.json(), response.status_code
        assert response.headers["content-type"].startswith("application/json")

    assert unauthorized.status_code == 401
    assert not_found.status_code == 404
    assert too_large.status_code == 413
    assert invalid.status_code == 422


# ---------------------------------------------------------------------------
# Music job table pruning
# ---------------------------------------------------------------------------


def _job(status: str) -> dict[str, Any]:
    return {
        "job_id": status,
        "status": status,
        "progress": {"done": 0, "total": 0},
        "results": [],
        "error": None,
        "created_at": time.time(),
        "finished_at": None,
    }


def test_prune_music_jobs_evicts_finished_first(
    monkeypatch: pytest.MonkeyPatch, clean_music_jobs: Any
) -> None:
    monkeypatch.setattr(api_routes, "MAX_MUSIC_JOBS", 2)
    with api_routes._MUSIC_JOBS_LOCK:
        api_routes._MUSIC_JOBS["done-1"] = _job("done")
        api_routes._MUSIC_JOBS["running-1"] = _job("running")
        api_routes._MUSIC_JOBS["failed-1"] = _job("failed")
        api_routes._prune_music_jobs_locked()

        assert len(api_routes._MUSIC_JOBS) < 2
        # Terminal jobs go first; the still-running one survives.
        assert "running-1" in api_routes._MUSIC_JOBS
        assert "done-1" not in api_routes._MUSIC_JOBS
        assert "failed-1" not in api_routes._MUSIC_JOBS


def test_prune_music_jobs_drops_oldest_when_all_active(
    monkeypatch: pytest.MonkeyPatch, clean_music_jobs: Any
) -> None:
    monkeypatch.setattr(api_routes, "MAX_MUSIC_JOBS", 2)
    with api_routes._MUSIC_JOBS_LOCK:
        api_routes._MUSIC_JOBS["first"] = _job("running")
        api_routes._MUSIC_JOBS["second"] = _job("queued")
        api_routes._prune_music_jobs_locked()

        assert len(api_routes._MUSIC_JOBS) == 1
        assert "first" not in api_routes._MUSIC_JOBS
        assert "second" in api_routes._MUSIC_JOBS


@pytest.mark.asyncio
async def test_create_music_job_keeps_table_bounded(
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clean_music_jobs: Any,
) -> None:
    monkeypatch.setattr(api_routes, "MAX_MUSIC_JOBS", 2)

    def quick_download(
        provider: Any, tracks: list[Any], options: Any, *, on_result: Any
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        return len(tracks), 0, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(download_many=quick_download, out_dir=tmp_path),
    )

    app = make_app()
    async with _client(app) as client:
        for _ in range(4):
            response = await client.post(
                "/api/v1/music/jobs",
                json={"platform": "demo", "tracks": [{"id": "1"}]},
            )
            assert response.status_code == 202
            await _wait_for_job(client, response.json()["job_id"])

    with api_routes._MUSIC_JOBS_LOCK:
        assert len(api_routes._MUSIC_JOBS) <= 2


# ---------------------------------------------------------------------------
# Startup warnings
# ---------------------------------------------------------------------------


def test_warn_on_public_bind_without_token(capsys: pytest.CaptureFixture[str]) -> None:
    from novel_downloader.apps.api.main import warn_if_insecure_bind

    warnings = warn_if_insecure_bind("0.0.0.0", None, False)

    assert warnings
    assert "token" in warnings[0].lower()
    assert "WARNING" in capsys.readouterr().err


def test_warn_on_public_bind_with_cors(capsys: pytest.CaptureFixture[str]) -> None:
    from novel_downloader.apps.api.main import warn_if_insecure_bind

    warnings = warn_if_insecure_bind("0.0.0.0", "s3cret", True)

    assert any("cors" in message.lower() for message in warnings)
    assert "WARNING" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_no_warning_for_local_bind(
    host: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from novel_downloader.apps.api.main import warn_if_insecure_bind

    assert warn_if_insecure_bind(host, None, True) == []
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Music fakes
# ---------------------------------------------------------------------------


class _FakeTrack:
    def __init__(self, **kwargs: Any) -> None:
        self.data = kwargs


class _FakeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.data = kwargs


class _FakePaths:
    def __init__(self, out_dir: Path) -> None:
        self._out_dir = Path(out_dir)

    def default_download_dir(self) -> Path:
        return self._out_dir


def _components(
    *,
    search: Any = None,
    download_many: Any = None,
    out_dir: Path,
) -> Any:
    """Build a ``_MusicComponents`` bundle backed by fakes."""
    provider = SimpleNamespace(search=search or (lambda keyword, limit: ([], 0)))
    return api_routes._MusicComponents(
        paths=_FakePaths(out_dir),
        download_options=_FakeOptions,
        download_many=download_many or (lambda *a, **k: (0, 0, 0, [], [])),
        available_labels=lambda: [],
        create=lambda platform: provider,
        platform_note=lambda platform: "",
        track_cls=_FakeTrack,
    )


async def _wait_for_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Poll a music job until it leaves the queued/running states."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"/api/v1/music/jobs/{job_id}")
        assert response.status_code == 200
        payload: dict[str, Any] = response.json()
        if payload["status"] in {"done", "failed"}:
            return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job {job_id} did not finish: {payload}")
        await asyncio.sleep(0.01)
