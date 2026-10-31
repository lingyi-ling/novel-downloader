#!/usr/bin/env python3
"""
tests.apps.test_music_providers
-------------------------------

Offline tests for the provider registry:

* ``health_check`` result shape, timing boundary and error normalization
* lazy imports: a missing optional dependency (``pathvalidate``) must not
  prevent ``import providers``, ``available()`` or other platforms from working
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.music import providers
from novel_downloader.apps.music.providers import health_check
from novel_downloader.apps.music.providers.base import Track
from novel_downloader.apps.music.providers.musicdl import MusicdlProvider


class _Provider:
    def __init__(self, *, error: BaseException | None = None, delay: float = 0.0):
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, int]] = []

    def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
        self.calls.append((keyword, limit))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return [object()], 1


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


def test_health_check_success_reports_ok_and_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    created: list[tuple[str, dict[str, Any]]] = []

    def fake_create(platform: str, **kwargs: Any) -> _Provider:
        created.append((platform, kwargs))
        return provider

    monkeypatch.setattr(providers, "create", fake_create)

    result = health_check("fake", "周杰伦", timeout=5)

    assert set(result) == {"ok", "elapsed_ms", "error"}
    assert result["ok"] is True
    assert result["error"] == ""
    assert isinstance(result["elapsed_ms"], int)
    assert result["elapsed_ms"] >= 0

    assert created and created[0][0] == "fake"
    # musicdl providers get a bounded (connect, read) timeout
    assert created[0][1]["timeout"] == (5.0, 5.0)
    assert provider.calls == [("周杰伦", 1)]


def test_health_check_accepts_zero_results_as_connectivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyProvider:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            return [], 0

    monkeypatch.setattr(providers, "create", lambda *a, **k: EmptyProvider())

    result = health_check("fake")

    assert result["ok"] is True
    assert result["error"] == ""


def test_health_check_normalizes_platform_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider(error=RuntimeError("interface changed"))
    monkeypatch.setattr(providers, "create", lambda *a, **k: provider)

    result = health_check("fake")

    assert result["ok"] is False
    assert "RuntimeError" in result["error"]
    assert "interface changed" in result["error"]
    assert len(result["error"]) < 300


def test_health_check_reports_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ModuleNotFoundError("No module named 'pathvalidate'", name="pathvalidate")
    provider = _Provider(error=error)
    monkeypatch.setattr(providers, "create", lambda *a, **k: provider)

    result = health_check("fake")

    assert result["ok"] is False
    assert "缺少依赖" in result["error"]
    assert "pathvalidate" in result["error"]


def test_health_check_reports_unknown_platform() -> None:
    result = health_check("definitely-not-a-platform")

    assert result["ok"] is False
    assert "未知平台" in result["error"]


def test_health_check_falls_back_when_create_rejects_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    calls: list[dict[str, Any]] = []

    def fake_create(platform: str, **kwargs: Any) -> _Provider:
        calls.append(kwargs)
        if kwargs:
            raise TypeError("unexpected keyword argument 'timeout'")
        return provider

    monkeypatch.setattr(providers, "create", fake_create)

    result = health_check("netease-like")

    assert result["ok"] is True
    assert calls == [{"timeout": (5.0, 15.0)}, {}]


def test_health_check_does_not_misclassify_search_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KeyError raised by the platform's search is a platform bug.

    The platform exists and was created successfully, so reporting it as
    ``未知平台`` (reserved for registry misses) misleads the UI and the user.
    """
    provider = _Provider(error=KeyError("trackId"))
    monkeypatch.setattr(providers, "create", lambda *a, **k: provider)

    result = health_check("migu")

    assert result["ok"] is False
    assert "未知平台" not in result["error"]
    assert "KeyError" in result["error"]


def test_health_check_enforces_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _Provider(delay=0.8)
    monkeypatch.setattr(providers, "create", lambda *a, **k: provider)

    started = time.perf_counter()
    result = health_check("slow", timeout=0.05)
    elapsed = time.perf_counter() - started

    assert result["ok"] is False
    assert "超时" in result["error"]
    # The call returned without waiting for the slow search.
    assert elapsed < 0.5
    assert result["elapsed_ms"] < 500


# ---------------------------------------------------------------------------
# Dependency guard / lazy imports
# ---------------------------------------------------------------------------


def test_registry_survives_missing_pathvalidate_and_stays_lazy() -> None:
    """Import the provider package in a fresh interpreter without pathvalidate.

    Also asserts that no vendored platform module is imported just by listing
    platforms (the per-platform failure must stay isolated until search).
    """
    src = Path(__file__).resolve().parents[2] / "src"
    code = "\n".join(
        [
            "import sys",
            "sys.modules['pathvalidate'] = None",
            "import novel_downloader.apps.music.providers as p",
            "assert p.available(), 'registry is empty'",
            "assert callable(p.health_check)",
            "assert callable(p.create)",
            "loaded = [m for m in sys.modules",
            "          if '.providers._vendor.musicdl.modules.sources.' in m]",
            "assert not loaded, loaded",
            "print('ok')",
        ]
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(src.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_one_platform_import_failure_does_not_break_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.music.providers.musicdl as musicdl_mod

    real_import = importlib.import_module

    def broken_import(name: str, package: str | None = None):
        if name.endswith(".migu"):
            raise ModuleNotFoundError(
                "No module named 'pathvalidate'", name="pathvalidate"
            )
        return real_import(name, package)

    monkeypatch.setattr(musicdl_mod.importlib, "import_module", broken_import)

    before = providers.available()
    assert ("migu", "咪咕音乐") in before

    # Creating the provider must not import the platform module...
    provider = providers.create("migu")
    assert provider.key == "migu"
    # ...and listing every platform still works.
    assert providers.available() == before

    # Only the failing platform's search reports an error, wrapped readably.
    with pytest.raises(RuntimeError, match="咪咕音乐") as excinfo:
        provider.search("test")
    assert "缺少可选依赖" in str(excinfo.value)
    assert "pathvalidate" in str(excinfo.value)


# ---------------------------------------------------------------------------
# MusicdlProvider: Track conversion / resolve robustness
# ---------------------------------------------------------------------------


def _song_info(**overrides: Any) -> SimpleNamespace:
    """A vendor ``SongInfo`` double with the attributes the adapter reads."""
    base: dict[str, Any] = {
        "identifier": "id-1",
        "song_name": "Song",
        "singers": "Artist",
        "album": "Album",
        "duration_s": 185,
        "cover_url": "http://fake.test/cover.jpg",
        "ext": "mp3",
        "lyric": "[00:01.00]lrc",
        "protocol": "HTTP",
        "with_valid_download_url": True,
        "download_url": "http://fake.test/a.mp3",
        "default_download_headers": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_musicdl_to_track_tolerates_unparsable_duration() -> None:
    """One bad row from a platform must not kill the whole search result."""
    provider = MusicdlProvider(key="migu")

    track = provider._to_track(_song_info(duration_s="3:45"))

    assert track.duration_s == 0
    assert track.name == "Song"

    assert provider._to_track(_song_info(duration_s="185.5")).duration_s == 185
    assert provider._to_track(_song_info(duration_s=None)).duration_s == 0


def test_musicdl_to_track_handles_missing_identifier() -> None:
    provider = MusicdlProvider(key="kuwo")

    track = provider._to_track(_song_info(identifier=None, song_name=None))

    assert track.id == ""
    assert track.name == "未知歌曲"
    assert track.artists == "Artist"


def test_musicdl_resolve_rejects_non_string_download_url() -> None:
    """``with_valid_download_url`` can be True for non-HTTP url shapes.

    The adapter must not hand a bogus url to the downloader (requests would
    crash on a non-string).
    """
    provider = MusicdlProvider(key="migu")
    track = Track(provider="migu", id="1", extra={"info": _song_info(download_url=42)})

    result = provider.resolve(track)

    assert result.ok is False
    assert result.reason


def test_musicdl_resolve_and_lyric_tolerate_missing_extra() -> None:
    provider = MusicdlProvider(key="migu")
    bare = Track(provider="migu", id="1", extra=None)

    result = provider.resolve(bare)

    assert result.ok is False
    assert "缺少平台解析数据" in result.reason
    assert provider.lyric(bare) == ("", "")


def test_musicdl_resolve_hls_and_invalid_url_reasons() -> None:
    provider = MusicdlProvider(key="migu")

    hls = Track(provider="migu", id="1", extra={"info": _song_info(protocol="HLS")})
    assert "HLS" in provider.resolve(hls).reason

    no_url = Track(
        provider="migu",
        id="2",
        extra={"info": _song_info(with_valid_download_url=False)},
    )
    result = provider.resolve(no_url)
    assert result.ok is False
    assert "未返回可下载直链" in result.reason


def test_musicdl_resolve_success_passes_headers_and_ext() -> None:
    provider = MusicdlProvider(key="migu")
    info = _song_info(
        ext=".flac", default_download_headers={"Referer": "http://fake.test/"}
    )
    track = Track(provider="migu", id="1", extra={"info": info})

    result = provider.resolve(track)

    assert result.ok is True
    assert result.url == "http://fake.test/a.mp3"
    assert result.ext == "flac"
    assert result.http_headers == {"Referer": "http://fake.test/"}
    assert provider.lyric(track) == ("[00:01.00]lrc", "")
