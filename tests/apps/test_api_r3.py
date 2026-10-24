#!/usr/bin/env python3
"""
tests.apps.test_api_r3
----------------------

Offline tests for the Round 3 API surface:

* watchlist CRUD and ``/watchlist/check`` (fake search, no downloads)
* music playlist export into ``tmp_path`` and platform health probes
* the optional ``NOVEL_API_WEBHOOK`` task-finished notification
* SDK client methods and the new endpoints' auth / error shapes

Everything runs through ``httpx.ASGITransport`` / ``httpx.MockTransport``; no
server is started and no real network access happens.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api import webhook as api_webhook
from novel_downloader.apps.api.client import NovelApiClient
from novel_downloader.apps.api.routes import get_manager, get_watchlist_store
from novel_downloader.apps.api.webhook import (
    TaskFinishedWatcher,
    WebhookNotifier,
    post_task_finished,
)
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.watchlist import WatchlistStore
from novel_downloader.apps.music.downloader import export_m3u
from novel_downloader.apps.music.providers.base import Track

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep app state/config and webhook configuration out of the real env."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


class RecordingManager:
    """Task manager stand-in that records whether a task was queued."""

    def __init__(self) -> None:
        self.added: list[dict[str, str]] = []

    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        self.added.append({"title": title, "site": site, "book_id": book_id})
        return DownloadTask(title=title, site=site, book_id=book_id)

    async def cancel_task(self, task_id: str) -> bool:
        return False

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {"pending": [], "running": [], "completed": []}

    async def close(self) -> None:
        return None


@pytest.fixture
def store(tmp_path: Path) -> WatchlistStore:
    return WatchlistStore(tmp_path / "watchlist.json")


@pytest.fixture
def manager() -> RecordingManager:
    return RecordingManager()


@pytest.fixture
def app(tmp_path: Path, store: WatchlistStore, manager: RecordingManager) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads")
    application.dependency_overrides[get_watchlist_store] = lambda: store
    application.dependency_overrides[get_manager] = lambda: manager
    return application


@asynccontextmanager
async def _client(application: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Watchlist CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_crud_roundtrip(app: FastAPI) -> None:
    async with _client(app) as client:
        empty = await client.get("/api/v1/watchlist")
        assert empty.status_code == 200
        assert empty.json() == {"total": 0, "books": []}

        created = await client.post(
            "/api/v1/watchlist",
            json={"site": "demo", "book_id": "42", "title": "Book"},
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["created"] is True
        assert payload["entry"] == {
            "site": "demo",
            "book_id": "42",
            "title": "Book",
            "last_chapter": "",
            "updated_at": payload["entry"]["updated_at"],
        }

        duplicated = await client.post(
            "/api/v1/watchlist", json={"site": "demo", "book_id": "42"}
        )
        assert duplicated.status_code == 200
        assert duplicated.json()["created"] is False
        # The stored title survives a re-add without one.
        assert duplicated.json()["entry"]["title"] == "Book"

        listing = await client.get("/api/v1/watchlist")
        assert listing.json()["total"] == 1
        assert listing.json()["books"][0]["book_id"] == "42"

        removed = await client.delete("/api/v1/watchlist/demo/42")
        assert removed.status_code == 200
        assert removed.json() == {"ok": True}

        missing = await client.delete("/api/v1/watchlist/demo/42")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Book not in watchlist: demo/42"}


@pytest.mark.asyncio
async def test_watchlist_add_validates_empty_fields(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/watchlist", json={"site": "", "book_id": "42"}
        )

    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], str)
    assert "site" in body["detail"]


# ---------------------------------------------------------------------------
# Watchlist check
# ---------------------------------------------------------------------------


def _search_result(
    *, site: str, book_id: str, title: str, latest: str
) -> dict[str, Any]:
    return {
        "site": site,
        "book_id": book_id,
        "book_url": f"https://example.invalid/{book_id}",
        "cover_url": "",
        "title": title,
        "author": "Author",
        "latest_chapter": latest,
        "update_date": "",
        "word_count": "",
        "priority": 1,
    }


@pytest.mark.asyncio
async def test_watchlist_check_updates_chapter_without_downloading(
    app: FastAPI,
    store: WatchlistStore,
    manager: RecordingManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.add("demo", "42", title="Book", last_chapter="1")
    calls: list[dict[str, Any]] = []

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"keyword": keyword, **kwargs})
        return [_search_result(site="demo", book_id="42", title="Book", latest="2")]

    monkeypatch.setattr(api_routes.search_module, "search", fake_search)

    async with _client(app) as client:
        response = await client.post("/api/v1/watchlist/check", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["checked"] == 1
    assert payload["updated"] == 1
    assert payload["failed"] == 0
    assert payload["download"] is False
    assert [item["book_id"] for item in payload["needs_download"]] == ["42"]
    item = payload["items"][0]
    assert item["previous_chapter"] == "1"
    assert item["last_chapter"] == "2"
    assert item["needs_download"] is True
    assert "task" not in item

    # The stored entry was updated, and no download was queued.
    assert store.get("demo", "42").last_chapter == "2"  # type: ignore[union-attr]
    assert manager.added == []

    assert calls[0]["keyword"] == "Book"
    assert calls[0]["sites"] == ["demo"]
    assert calls[0]["per_site_limit"] == api_routes.WATCHLIST_CHECK_PER_SITE_LIMIT


@pytest.mark.asyncio
async def test_watchlist_check_download_flag_adds_task_body(
    app: FastAPI,
    store: WatchlistStore,
    manager: RecordingManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.add("demo", "42", title="Book")

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_search_result(site="demo", book_id="42", title="Book", latest="2")]

    monkeypatch.setattr(api_routes.search_module, "search", fake_search)

    async with _client(app) as client:
        response = await client.post("/api/v1/watchlist/check", json={"download": True})

    payload = response.json()
    assert payload["download"] is True
    item = payload["needs_download"][0]
    assert item["task"] == {"site": "demo", "book_id": "42", "title": "Book"}
    # Even with download=true nothing is queued server-side.
    assert manager.added == []


@pytest.mark.asyncio
async def test_watchlist_check_isolates_search_failures(
    app: FastAPI,
    store: WatchlistStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.add("broken", "1", title="Broken")
    store.add("demo", "2", title="Good", last_chapter="1")

    async def fake_search(keyword: str, *, sites: list[str], **kwargs: Any) -> Any:
        if sites == ["broken"]:
            raise RuntimeError("site exploded")
        return [_search_result(site="demo", book_id="2", title="Good", latest="2")]

    monkeypatch.setattr(api_routes.search_module, "search", fake_search)

    async with _client(app) as client:
        response = await client.post("/api/v1/watchlist/check", json={})

    payload = response.json()
    assert payload["checked"] == 2
    assert payload["updated"] == 1
    assert payload["failed"] == 1
    failed = next(item for item in payload["items"] if item["site"] == "broken")
    assert failed["error"] == "RuntimeError: site exploded"
    assert failed["needs_download"] is False


@pytest.mark.asyncio
async def test_watchlist_check_unchanged_entry(
    app: FastAPI,
    store: WatchlistStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.add("demo", "42", title="Book", last_chapter="2")

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_search_result(site="demo", book_id="42", title="Book", latest="2")]

    monkeypatch.setattr(api_routes.search_module, "search", fake_search)

    async with _client(app) as client:
        response = await client.post("/api/v1/watchlist/check", json={})

    payload = response.json()
    assert payload["updated"] == 0
    assert payload["needs_download"] == []
    assert payload["items"][0]["changed"] is False


# ---------------------------------------------------------------------------
# Music export / health
# ---------------------------------------------------------------------------


class _FakePaths:
    def __init__(self, out_dir: Path) -> None:
        self._out_dir = Path(out_dir)

    def default_download_dir(self) -> Path:
        return self._out_dir


def _music_components(
    *,
    out_dir: Path,
    health: Callable[..., dict[str, Any]] | None = None,
    platforms: tuple[tuple[str, str], ...] = (("demo", "Demo"),),
) -> api_routes._MusicComponents:
    def default_health(platform: str, *, timeout: float) -> dict[str, Any]:
        return {"ok": True, "elapsed_ms": 1, "error": ""}

    return api_routes._MusicComponents(
        paths=_FakePaths(out_dir),
        download_options=SimpleNamespace,
        download_many=lambda *args, **kwargs: (0, 0, 0, [], []),
        available_labels=lambda: list(platforms),
        create=lambda platform: SimpleNamespace(),
        platform_note=lambda platform: "",
        track_cls=Track,
        export_m3u=export_m3u,
        health_check=health or default_health,
        load_settings=lambda: {"out_dir": str(out_dir)},
    )


@pytest.mark.asyncio
async def test_music_export_writes_playlist_and_counts_skipped(
    app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "audio" / "song one.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"fake")
    target = tmp_path / "playlists" / "mine.m3u8"
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _music_components(out_dir=tmp_path / "music"),
    )

    async with _client(app) as client:
        response = await client.post(
            "/api/v1/music/export",
            json={
                "platform": "demo",
                "tracks": [
                    {
                        "id": "1",
                        "name": "晴天",
                        "artists": "周杰伦",
                        "duration_s": 269,
                        "path": str(audio),
                    },
                    {"id": "2", "name": "No path"},
                ],
                "out_path": str(target),
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "path": str(target),
        "tracks_written": 1,
        "skipped": 1,
    }
    content = target.read_text(encoding="utf-8")
    assert content.startswith("#EXTM3U\n")
    assert "#EXTINF:269,周杰伦 - 晴天" in content
    assert "audio/song one.mp3" in content


@pytest.mark.asyncio
async def test_music_export_defaults_to_settings_out_dir(
    app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    out_dir = tmp_path / "music-out"
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _music_components(out_dir=out_dir),
    )

    async with _client(app) as client:
        response = await client.post(
            "/api/v1/music/export",
            json={
                "platform": "demo",
                "tracks": [
                    {"id": "1", "name": "Song", "artists": "A", "path": str(audio)}
                ],
            },
        )

    assert response.status_code == 200
    written = Path(response.json()["path"])
    assert written.parent == out_dir
    assert written.suffix == ".m3u8"
    assert written.is_file()


@pytest.mark.asyncio
async def test_music_export_rejects_unknown_platform(
    app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _music_components(out_dir=tmp_path),
    )

    async with _client(app) as client:
        response = await client.post(
            "/api/v1/music/export",
            json={"platform": "nope", "tracks": [{"id": "1", "path": "x"}]},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Unknown music platform: nope"}


@pytest.mark.asyncio
async def test_music_health_single_and_all_platforms(
    app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def fake_health(platform: str, *, timeout: float) -> dict[str, Any]:
        calls.append((platform, timeout))
        if platform == "bad":
            return {"ok": False, "elapsed_ms": 3, "error": "boom"}
        return {"ok": True, "elapsed_ms": 2, "error": ""}

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _music_components(
            out_dir=tmp_path,
            health=fake_health,
            platforms=(("demo", "Demo"), ("bad", "Bad")),
        ),
    )

    async with _client(app) as client:
        one = await client.get("/api/v1/music/health", params={"platform": "demo"})
        all_platforms = await client.get(
            "/api/v1/music/health", params={"timeout": 1.5}
        )
        unknown = await client.get("/api/v1/music/health", params={"platform": "nope"})

    assert one.status_code == 200
    assert one.json() == [
        {"platform": "demo", "ok": True, "elapsed_ms": 2, "error": ""}
    ]
    assert [item["platform"] for item in all_platforms.json()] == ["demo", "bad"]
    assert all_platforms.json()[1]["ok"] is False
    assert unknown.status_code == 400
    assert unknown.json() == {"detail": "Unknown music platform: nope"}
    assert calls == [("demo", 5.0), ("demo", 1.5), ("bad", 1.5)]


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def test_post_task_finished_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[tuple[str, dict[str, Any], float]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

    def ok_post(url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        posted.append((url, json, timeout))
        return _Response()

    monkeypatch.setattr(api_webhook.requests, "post", ok_post)
    assert post_task_finished("http://hook", {"event": "task_finished"}, timeout=2.5)
    assert posted[0][2] == 2.5

    def broken_post(url: str, *, json: dict[str, Any], timeout: float) -> Any:
        raise api_webhook.requests.ConnectionError("unreachable")

    monkeypatch.setattr(api_webhook.requests, "post", broken_post)
    assert post_task_finished("http://hook", {"event": "task_finished"}) is False


def test_notifier_uses_requests_with_five_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, Any], float]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        sent.append((url, json, timeout))
        return _Response()

    monkeypatch.setattr(api_webhook.requests, "post", fake_post)
    notifier = WebhookNotifier("http://hook")
    notifier.notify_task_finished({"task_id": "1"})
    notifier.wait(timeout=5)

    assert sent == [
        (
            "http://hook",
            {"event": "task_finished", "task": {"task_id": "1"}},
            5.0,
        )
    ]

    def failing_post(url: str, *, json: dict[str, Any], timeout: float) -> Any:
        raise api_webhook.requests.Timeout("slow endpoint")

    monkeypatch.setattr(api_webhook.requests, "post", failing_post)
    notifier = WebhookNotifier("http://hook")
    notifier.notify_task_finished({"task_id": "2"})
    notifier.wait(timeout=5)  # a failing endpoint must not raise


def test_notifier_delivers_in_background_and_swallows_sender_errors() -> None:
    delivered: list[tuple[str, dict[str, Any], float]] = []

    def sender(url: str, payload: dict[str, Any], timeout: float) -> bool:
        delivered.append((url, payload, timeout))
        return True

    notifier = WebhookNotifier("http://hook", sender=sender)
    notifier.notify_task_finished({"task_id": "1"})
    notifier.wait(timeout=5)

    assert delivered == [
        (
            "http://hook",
            {"event": "task_finished", "task": {"task_id": "1"}},
            5.0,
        )
    ]

    def exploding(url: str, payload: dict[str, Any], timeout: float) -> bool:
        raise RuntimeError("sender exploded")

    notifier = WebhookNotifier("http://hook", sender=exploding)
    notifier.notify_task_finished({"task_id": "2"})
    notifier.wait(timeout=5)  # must not raise


def test_watcher_notifies_only_new_terminal_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    notifier = WebhookNotifier(
        "http://hook",
        sender=lambda url, payload, timeout: calls.append(payload) or True,
    )
    watcher = TaskFinishedWatcher(notifier)

    old = DownloadTask(title="Old", site="demo", book_id="1")
    old.status = Status.COMPLETED
    watcher.seed({"completed": [old]})

    running = DownloadTask(title="Running", site="demo", book_id="2")
    running.status = Status.RUNNING
    finished = DownloadTask(title="New", site="demo", book_id="3")
    finished.status = Status.FAILED

    fired = watcher.poll(
        {"completed": [old], "running": [running]},
        serialize=api_routes.task_to_dict,
    )
    assert fired == []

    fired = watcher.poll(
        {"completed": [old, finished]},
        serialize=api_routes.task_to_dict,
    )
    notifier.wait(timeout=5)

    assert [item["book_id"] for item in fired] == ["3"]
    assert calls[0]["event"] == "task_finished"
    assert calls[0]["task"]["status"] == "failed"
    assert calls[0]["task"]["task_id"] == finished.task_id

    # A second poll of the same snapshot must not duplicate the event.
    assert (
        watcher.poll(
            {"completed": [old, finished]},
            serialize=api_routes.task_to_dict,
        )
        == []
    )
    notifier.wait(timeout=5)
    assert len(calls) == 1


def test_create_app_webhook_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVEL_API_WEBHOOK", "http://hook/env")
    from_env = create_app(output_dir=tmp_path / "a")
    assert isinstance(from_env.state.webhook_notifier, WebhookNotifier)
    assert from_env.state.webhook_url == "http://hook/env"

    explicit = create_app(output_dir=tmp_path / "b", webhook_url="http://hook/explicit")
    assert isinstance(explicit.state.webhook_notifier, WebhookNotifier)
    assert explicit.state.webhook_url == "http://hook/explicit"

    disabled = create_app(output_dir=tmp_path / "c", webhook_url="")
    assert disabled.state.webhook_notifier is None
    assert disabled.state.webhook_url is None


# ---------------------------------------------------------------------------
# Auth and error shapes for the new endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_endpoints_require_token(
    tmp_path: Path, store: WatchlistStore
) -> None:
    application = create_app(token="secret", output_dir=tmp_path / "downloads")
    application.dependency_overrides[get_watchlist_store] = lambda: store

    async with _client(application) as client:
        missing = await client.get("/api/v1/watchlist")
        wrong = await client.post(
            "/api/v1/watchlist",
            json={"site": "demo", "book_id": "1"},
            headers={"Authorization": "Bearer nope"},
        )
        allowed = await client.get(
            "/api/v1/watchlist", headers={"Authorization": "Bearer secret"}
        )
        music_export = await client.post(
            "/api/v1/music/export",
            json={"platform": "demo", "tracks": [{"id": "1", "path": "x"}]},
        )

    assert missing.status_code == 401
    assert missing.json() == {"detail": "Invalid or missing API token"}
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert music_export.status_code == 401
    assert music_export.json() == {"detail": "Invalid or missing API token"}


@pytest.mark.asyncio
async def test_watchlist_errors_use_detail_shape(app: FastAPI) -> None:
    async with _client(app) as client:
        bad_json = await client.post("/api/v1/watchlist", json={"site": "demo"})
        check_bad = await client.post(
            "/api/v1/watchlist/check", json={"download": "not-a-bool"}
        )

    assert bad_json.status_code == 422
    assert isinstance(bad_json.json()["detail"], str)
    assert "book_id" in bad_json.json()["detail"]
    assert check_bad.status_code == 422
    assert "download" in check_bad.json()["detail"]


# ---------------------------------------------------------------------------
# SDK client
# ---------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


def _make_client(handler: Handler) -> NovelApiClient:
    return NovelApiClient(
        base_url="http://api.test",
        transport=httpx.MockTransport(handler),
    )


def test_client_watchlist_methods() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            return httpx.Response(
                200, json={"created": True, "entry": {"site": "demo"}}
            )
        return httpx.Response(200, json={"total": 0, "books": []})

    with _make_client(handler) as client:
        assert client.watchlist()["total"] == 0
        client.add_watch("demo", "42", title="Book")
        client.remove_watch("de mo", "4/2")
        client.check_watchlist(download=True)

    assert requests[0].url.path == "/api/v1/watchlist"
    assert json.loads(requests[1].content) == {
        "site": "demo",
        "book_id": "42",
        "title": "Book",
    }
    assert requests[2].method == "DELETE"
    assert requests[2].url.raw_path == b"/api/v1/watchlist/de%20mo/4%2F2"
    assert json.loads(requests[3].content) == {"download": True}
    assert requests[3].url.path == "/api/v1/watchlist/check"


def test_client_music_export_and_health(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/music/health"):
            return httpx.Response(
                200,
                json=[
                    {
                        "platform": "demo",
                        "ok": True,
                        "elapsed_ms": 1,
                        "error": "",
                    }
                ],
            )
        return httpx.Response(
            200, json={"path": "x.m3u8", "tracks_written": 1, "skipped": 0}
        )

    tracks = [{"id": "1", "name": "Song", "artists": "A", "path": "a.mp3"}]

    with _make_client(handler) as client:
        exported = client.music_export("demo", tracks, out_path=tmp_path)
        health = client.music_health("demo")

    assert exported["tracks_written"] == 1
    assert json.loads(requests[0].content) == {
        "platform": "demo",
        "tracks": tracks,
        "out_path": str(tmp_path),
    }
    assert health == [{"platform": "demo", "ok": True, "elapsed_ms": 1, "error": ""}]
    assert requests[1].url.params.get("platform") == "demo"
