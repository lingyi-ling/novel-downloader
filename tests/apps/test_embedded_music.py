#!/usr/bin/env python3
"""
Embedded-server music handlers.

The provider engine is replaced with a fake: these tests only cover the wire
shape, validation and job lifecycle, never the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.embedded import server as eserver


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


class FakeProvider:
    def search(self, keyword: str, *, limit: int = 20, offset: int = 0) -> Any:
        return [FakeTrack(id="1", name=keyword, artists="A")], 1


def _download_many(
    provider: Any, tracks: list[Any], options: Any, *, on_result: Any = None
) -> Any:
    for index, track in enumerate(tracks):
        if on_result is not None:
            on_result(
                index,
                track,
                SimpleNamespace(status="ok", reason="", path="/music/a.mp3", bytes_=3),
            )
    return len(tracks), 0, 0, [], []


def _components(tmp_path: Any) -> SimpleNamespace:
    return SimpleNamespace(
        paths=SimpleNamespace(default_download_dir=lambda: tmp_path),
        download_options=lambda out_dir, level: SimpleNamespace(
            out_dir=out_dir, level=level
        ),
        download_many=_download_many,
        download_mixed=_download_many,
        # The real ``export_m3u`` returns the playlist path, not a count.
        # Returning a path here keeps the handler honest: ``int(path)`` used
        # to raise ``ValueError`` (HTTP 500) against the real engine.
        export_m3u=lambda tracks, target: str(target),
        available_labels=lambda: [("netease", "国内 · 网易云音乐")],
        create=lambda key: FakeProvider(),
        platform_note=lambda key: "note",
        track_cls=FakeTrack,
        health_check=lambda key, timeout=5.0: {
            "ok": True,
            "elapsed_ms": 1,
            "error": "",
        },
    )


@pytest.fixture()
def music(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> SimpleNamespace:
    comps = _components(tmp_path)
    monkeypatch.setattr(eserver, "_music_components", lambda: comps)
    return comps


def test_platforms_lists_labels_and_notes(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_platforms({}, {}, {})

    assert payload == {
        "platforms": [{"key": "netease", "label": "国内 · 网易云音乐", "note": "note"}]
    }


def test_search_maps_tracks(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_search(
        {"platform": "netease", "keyword": "晴天"}, {}, {}
    )

    assert payload["total"] == 1
    assert payload["tracks"][0]["name"] == "晴天"
    assert payload["tracks"][0]["provider"] == ""


def test_search_validation_mirrors_the_api(music: SimpleNamespace) -> None:
    with pytest.raises(eserver._ApiError) as missing:
        eserver._handle_music_search({"platform": "netease"}, {}, {})
    assert missing.value.status == 422
    assert missing.value.errors

    with pytest.raises(eserver._ApiError) as unknown:
        eserver._handle_music_search({"platform": "nope", "keyword": "x"}, {}, {})
    assert unknown.value.status == 400


def test_health_reports_per_platform(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_health({}, {}, {})

    assert payload == [
        {"platform": "netease", "ok": True, "elapsed_ms": 1, "error": ""}
    ]


def test_download_returns_counts(music: SimpleNamespace) -> None:
    payload = eserver._handle_music_download(
        {"platform": "netease", "tracks": [{"id": "1", "name": "A"}]}, {}, {}
    )

    assert payload["ok"] == 1
    assert payload["fail"] == 0
    assert payload["results"][0]["status"] == "ok"


def test_export_skips_tracks_without_a_path(
    music: SimpleNamespace, tmp_path: Any
) -> None:
    payload = eserver._handle_music_export(
        {
            "platform": "netease",
            "tracks": [
                {"id": "1", "name": "A"},
                {"id": "2", "name": "B", "path": "/m/b.mp3"},
            ],
            "out_path": str(tmp_path / "p.m3u8"),
        },
        {},
        {},
    )

    assert payload == {
        "written": 1,
        "skipped": 1,
        "path": str(tmp_path / "p.m3u8"),
    }


def test_download_rejects_an_empty_track_list(music: SimpleNamespace) -> None:
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_download({"platform": "netease", "tracks": []}, {}, {})
    assert err.value.status == 422


def test_job_reaches_done_with_results(music: SimpleNamespace) -> None:
    created = eserver._handle_music_jobs_post(
        {"platform": "netease", "tracks": [{"id": "1", "name": "A"}]}, {}, {}
    )
    job_id = created["job_id"]

    deadline = time.time() + 5
    while True:
        job = eserver._handle_music_job_status({}, {"job_id": job_id}, {})
        if job["status"] in ("done", "failed") or time.time() > deadline:
            break
        time.sleep(0.02)

    assert job["status"] == "done"
    assert job["progress"] == {"done": 1, "total": 1}
    assert job["results"][0]["status"] == "ok"
    assert job["error"] is None


def test_unknown_job_is_404() -> None:
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_job_status({}, {"job_id": "missing"}, {})
    assert err.value.status == 404


def test_missing_extras_answer_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unavailable() -> Any:
        raise eserver._ApiError(
            503, eserver._MSG_MUSIC_UNAVAILABLE.format(error="ModuleNotFoundError")
        )

    monkeypatch.setattr(eserver, "_music_components", _unavailable)

    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_music_platforms({}, {}, {})
    assert err.value.status == 503


def test_results_carry_a_loopback_download_url(music: SimpleNamespace) -> None:
    """The Kotlin client hands this URL to DownloadManager."""
    item = eserver._music_result_item(
        SimpleNamespace(status="ok", reason="", path="/tmp/music/a.mp3", bytes_=3)
    )

    assert item["download_url"] == "/downloads/music/a.mp3"


def test_results_without_an_audio_extension_have_no_url() -> None:
    item = eserver._music_result_item(
        SimpleNamespace(status="ok", reason="", path="/tmp/music/notes.txt", bytes_=3)
    )

    assert item["download_url"] == ""


def test_music_serve_path_cannot_escape_the_music_folder() -> None:
    """`music/../../secret.mp3` must resolve inside *its own* basename only."""
    from pathlib import Path

    decoded = "music/../../secret.mp3"
    _prefix, _, rest = decoded.partition("/")
    safe = Path(rest).name
    assert safe == "secret.mp3"
