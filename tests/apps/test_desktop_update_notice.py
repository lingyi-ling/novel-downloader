#!/usr/bin/env python3
"""
tests.apps.test_desktop_update_notice
-------------------------------------

Behaviour tests for the desktop client's update *notice*: when a release is
considered newer, how the 24-hour throttle is persisted, what the UI state is
after a failed or unsigned check, the persisted "skip this version" and the
release-page actions.

The release feed and the transfer are replaced with fakes, and the preference
file lives in memory, so nothing here touches the network or a real user
profile.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.common.updater import (
    DEFAULT_REPO,
    ReleaseAsset,
    UpdateCheck,
    UpdateInfo,
    parse_manifest,
)
from novel_downloader.apps.desktop import webview_app as wa
from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.infra.i18n import get_translation

_SHA = "a" * 64


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def prefs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """In-memory replacement for the preference file."""
    stored: dict[str, Any] = {}
    monkeypatch.setattr(wa, "_load_prefs", lambda: dict(stored))
    monkeypatch.setattr(wa, "_save_prefs", lambda values: stored.update(values) or True)
    return stored


def _release_payload(
    tag: str,
    *,
    notes: str | None = "https://example.invalid/notes",
    asset: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"tag_name": tag}
    if notes is not None:
        payload["html_url"] = notes
    if asset:
        payload["assets"] = [
            {
                "name": "NovelDownloader-setup-9.0.0-x64.exe",
                "browser_download_url": (
                    "https://example.invalid/NovelDownloader-setup-9.0.0-x64.exe"
                ),
                "size": 1234,
                "digest": f"sha256:{_SHA}",
            }
        ]
    return payload


def _asset() -> ReleaseAsset:
    return ReleaseAsset(
        name="NovelDownloader-setup-9.0.0-x64.exe",
        url="https://example.invalid/NovelDownloader-setup-9.0.0-x64.exe",
        size=1234,
        sha256=_SHA,
    )


def _info(
    *,
    current: str = "1.2.3",
    latest: str = "1.3.0",
    available: bool = True,
    notes_url: str | None = "https://example.invalid/notes",
    asset: ReleaseAsset | None = None,
) -> UpdateInfo:
    chosen = _asset() if asset is None else asset
    return UpdateInfo(
        current=current,
        latest=latest,
        available=available,
        notes_url=notes_url,
        asset=chosen,
        assets=(chosen,),
    )


def _cache(info: UpdateInfo | None, *, error: str | None = None) -> Any:
    return wa._UpdateCache(
        info=info,
        error=error,
        checked_at=1234.0,
        current="1.2.3",
        platform="windows-x64",
    )


def _bridge(
    info: UpdateInfo | None = None, *, error: str | None = None
) -> wa.ApiBridge:
    bridge = wa.ApiBridge()
    bridge._update_cache = _cache(info, error=error)
    return bridge


def _wait_for_check(bridge: wa.ApiBridge, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with bridge._update_guard:
            thread = bridge._update_thread
        if thread is None:
            return
        thread.join(0.05)
    raise AssertionError("the update check did not finish in time")


class _Response:
    """Minimal ``urllib.request.urlopen`` context manager."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


# ---------------------------------------------------------------------------
# version comparison: equal / newer / older / pre-release
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "tag", "latest", "available"),
    [
        # Same release: no notice.
        ("1.2.3", "v1.2.3", "1.2.3", False),
        # Missing trailing components compare as zero.
        ("1.2.3", "v1.2.3.0", "1.2.3.0", False),
        ("1.2.3.0", "v1.2.3", "1.2.3", False),
        # Build metadata never affects precedence.
        ("1.2.3", "v1.2.3+build.7", "1.2.3+build.7", False),
        # A newer release is announced.
        ("1.2.3", "v1.2.4", "1.2.4", True),
        ("1.9.9", "v1.10.0", "1.10.0", True),
        # A downgrade is never "available".
        ("1.2.3", "v1.2.2", "1.2.2", False),
        # Pre-release ordering: rc2 > rc1, final > rc.
        ("1.5.0-rc1", "v1.5.0-rc2", "1.5.0-rc2", True),
        ("1.5.0-rc2", "v1.5.0", "1.5.0", True),
        ("1.5.0", "v1.5.0-rc1", "1.5.0-rc1", False),
        # Numeric pre-release identifiers compare numerically (10 > 9).
        ("1.5.0-9", "v1.5.0-10", "1.5.0-10", True),
        ("1.5.0-10", "v1.5.0-9", "1.5.0-9", False),
    ],
)
def test_update_notice_version_matrix(
    prefs: dict[str, Any],
    current: str,
    tag: str,
    latest: str,
    available: bool,
) -> None:
    """The notice follows SemVer-ish ordering through the real parser."""
    info = parse_manifest(
        _release_payload(tag), current=current, platform_tag="windows-x64"
    )
    bridge = _bridge(info)
    prefs["last_update_check"] = str(time.time())

    state = bridge.update_state()

    assert info.latest == latest
    assert state["latest"] == latest
    assert state["available"] is available
    assert state["current"] == "1.2.3"


# ---------------------------------------------------------------------------
# check frequency and last_update_check persistence
# ---------------------------------------------------------------------------


def test_update_due_honours_the_interval_and_tolerates_bad_values() -> None:
    now = 200_000.0
    interval = wa._UPDATE_CHECK_INTERVAL
    assert wa._update_due({}, now) is True
    assert wa._update_due({"last_update_check": str(now)}, now) is False
    assert wa._update_due({"last_update_check": str(now - interval + 1)}, now) is False
    assert wa._update_due({"last_update_check": str(now - interval)}, now) is True
    # A hand-edited/corrupt value reads as "never checked", not as a crash.
    assert wa._update_due({"last_update_check": "not-a-number"}, now) is True
    assert wa._update_due({"last_update_check": None}, now) is True
    # A clock that jumped backwards must not enable a tight retry loop.
    assert wa._update_due({"last_update_check": str(now + 3600)}, now) is False


def test_automatic_check_is_throttled_and_the_stamp_is_persisted(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_check(current: str, **kwargs: Any) -> UpdateCheck:
        calls.append(current)
        return UpdateCheck(info=_info(), error=None, checked_at=42.0)

    monkeypatch.setattr(wa, "check_for_update", fake_check)
    bridge = wa.ApiBridge()

    first = bridge.check_update_async()
    assert first["started"] is True
    _wait_for_check(bridge)

    stamp = float(prefs["last_update_check"])
    assert stamp > 0
    assert calls == [wa.ApiBridge._version()]

    second = bridge.check_update_async()
    assert second["started"] is False
    assert second["reason"] == "throttled"
    assert calls == [wa.ApiBridge._version()], (
        "within 24 h the feed must not be contacted again"
    )
    assert float(prefs["last_update_check"]) == stamp


def test_forced_check_bypasses_the_interval(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        wa,
        "check_for_update",
        lambda current, **kwargs: (
            calls.append(current)
            or UpdateCheck(info=_info(), error=None, checked_at=1.0)
        ),
    )
    prefs["last_update_check"] = str(time.time())
    bridge = wa.ApiBridge()

    result = bridge.check_update_async(force=True)

    assert result["started"] is True
    _wait_for_check(bridge)
    assert len(calls) == 1


def test_update_state_starts_at_most_one_background_check(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    entered = threading.Event()
    calls: list[str] = []

    def slow_check(current: str, **kwargs: Any) -> UpdateCheck:
        calls.append(current)
        entered.set()
        release.wait(3.0)
        return UpdateCheck(info=_info(), error=None, checked_at=9.0)

    monkeypatch.setattr(wa, "check_for_update", slow_check)
    bridge = wa.ApiBridge()

    first = bridge.update_state()
    assert first["pending"] is True
    assert entered.wait(1.0)
    bridge.update_state()
    assert calls == [wa.ApiBridge._version()], "a live worker must not be duplicated"

    release.set()
    _wait_for_check(bridge)
    assert bridge.update_state()["available"] is True


# ---------------------------------------------------------------------------
# failures: offline, timeout, untrusted manifest
# ---------------------------------------------------------------------------


def test_a_failed_check_is_reported_without_blocking_the_state(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        wa,
        "check_for_update",
        lambda *a, **k: UpdateCheck(
            info=None, error="untrusted-manifest: bad signature", checked_at=7.0
        ),
    )
    bridge = wa.ApiBridge()

    result = bridge.check_update_async()
    assert result["started"] is True
    assert result["pending"] is True
    _wait_for_check(bridge)

    state = bridge.update_state()
    assert state["available"] is False
    assert state["latest"] == ""
    assert state["error"] == "untrusted-manifest: bad signature"
    assert state["checked_at"] == 7.0
    assert state["pending"] is False
    # The failed attempt still counts towards the throttle, so an offline
    # machine does not retry the fetch on every panel switch.
    assert float(prefs["last_update_check"]) > 0


def test_a_timed_out_check_survives_and_does_not_kill_the_worker(
    prefs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def timeout(*_args: Any, **_kwargs: Any) -> UpdateCheck:
        raise TimeoutError("the release feed timed out")

    caplog.set_level("DEBUG", logger="novel_downloader.apps.desktop.webview_app")
    monkeypatch.setattr(wa, "check_for_update", timeout)
    bridge = wa.ApiBridge()

    bridge.check_update_async()
    _wait_for_check(bridge)

    state = bridge.update_state()
    assert state["available"] is False
    assert state["pending"] is False
    assert "TimeoutError" in state["error"]
    assert "timed out" in state["error"]
    assert "Update check failed" in caplog.text
    with bridge._update_guard:
        assert bridge._update_thread is None


def test_the_async_check_returns_before_the_feed_answers(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening the page must not wait for the (up to 8 s) network call."""
    release = threading.Event()

    def slow_check(*_args: Any, **_kwargs: Any) -> UpdateCheck:
        release.wait(3.0)
        return UpdateCheck(info=_info(), error=None, checked_at=1.0)

    monkeypatch.setattr(wa, "check_for_update", slow_check)
    bridge = wa.ApiBridge()

    started = time.monotonic()
    result = bridge.check_update_async()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert result["started"] is True and result["pending"] is True
    release.set()
    _wait_for_check(bridge)


def test_the_legacy_blocking_check_reports_an_offline_failure(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("offline")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    bridge = wa.ApiBridge()

    result = bridge.check_update()

    assert result["available"] is False
    assert result["latest"] == ""
    assert result["url"] == wa._RELEASES_PAGE
    assert "TimeoutError" in result["error"]
    # The stamp is written before the request, so an offline machine does not
    # retry the fetch every time the panel is opened.
    assert float(prefs["last_update_check"]) > 0


def test_the_legacy_check_is_throttled_before_reaching_the_network(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    prefs["last_update_check"] = str(time.time())
    fetches: list[int] = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: fetches.append(1) or _Response(_release_payload("v99.0.0")),
    )
    bridge = wa.ApiBridge()

    result = bridge.check_update()

    assert result["available"] is False
    assert result["throttled"] is True
    assert fetches == []


def test_a_platform_probe_failure_does_not_break_the_legacy_check(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    def broken_probe() -> str:
        raise OSError("no platform information")

    monkeypatch.setattr(wa, "detect_platform", broken_probe)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: _Response(_release_payload("v99.0.0")),
    )
    bridge = wa.ApiBridge()

    result = bridge.check_update()

    assert result["available"] is True
    assert result["platform"] == ""


# ---------------------------------------------------------------------------
# skip this version
# ---------------------------------------------------------------------------


def test_skipping_a_version_persists_and_hides_the_notice(
    prefs: dict[str, Any],
) -> None:
    prefs["last_update_check"] = str(time.time())
    bridge = _bridge(_info(latest="1.3.0"))

    result = bridge.skip_update_version("v1.3.0")

    assert result["ok"] is True
    assert result["version"] == "1.3.0"
    assert prefs["skipped_update_version"] == "1.3.0"
    state = bridge.update_state()
    assert state["latest"] == "1.3.0"
    assert state["available"] is False
    assert state["skipped"] is True
    # The page can still reach the release notes if it wants to.
    assert state["notes_url"] == "https://example.invalid/notes"


def test_skip_defaults_to_the_announced_release(prefs: dict[str, Any]) -> None:
    prefs["last_update_check"] = str(time.time())
    bridge = _bridge(_info(latest="v2.0.0"))

    result = bridge.skip_update_version()

    assert result["ok"] is True
    assert result["version"] == "2.0.0"
    assert prefs["skipped_update_version"] == "2.0.0"


def test_skip_without_an_announced_release_is_refused(prefs: dict[str, Any]) -> None:
    prefs["last_update_check"] = str(time.time())
    bridge = wa.ApiBridge()

    result = bridge.skip_update_version()

    assert result["ok"] is False
    assert result["error"]
    assert "skipped_update_version" not in prefs


def test_a_new_release_restores_the_notice_while_the_skip_survives(
    prefs: dict[str, Any],
) -> None:
    prefs["last_update_check"] = str(time.time())
    prefs["skipped_update_version"] = "1.3.0"
    bridge = _bridge(_info(latest="1.4.0"))

    state = bridge.update_state()

    assert state["available"] is True
    assert state["skipped"] is False
    # The old preference is inert rather than requiring a manual reset.
    assert prefs["skipped_update_version"] == "1.3.0"


def test_a_skipped_release_is_not_downloaded_in_the_background(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prefs["skipped_update_version"] = "1.3.0"
    prefs["auto_download_updates"] = True
    bridge = _bridge(_info(latest="1.3.0"))
    downloaded: list[int] = []

    monkeypatch.setattr(
        bridge, "_download_update", lambda **kwargs: downloaded.append(1)
    )
    bridge._maybe_auto_download()
    time.sleep(0.05)

    assert not downloaded


def test_the_skip_is_read_by_a_fresh_bridge_instance(prefs: dict[str, Any]) -> None:
    """Persisted state, not per-process memory: a restart still suppresses it."""
    prefs["skipped_update_version"] = "v1.3.0"
    prefs["last_update_check"] = str(time.time())

    state = _bridge(_info(latest="1.3.0")).update_state()

    assert state["available"] is False
    assert state["skipped"] is True


def test_the_skip_really_lands_in_the_preference_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory fixture aside, ``_save_prefs`` writes valid JSON on disk."""
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path))
    bridge = _bridge(_info(latest="1.3.0"))

    result = bridge.skip_update_version("1.3.0")

    assert result["ok"] is True
    stored = json.loads(wa._prefs_path().read_text(encoding="utf-8"))
    assert stored["skipped_update_version"] == "1.3.0"

    # Keep the fresh bridge from starting an automatic network check.
    assert wa._save_prefs({"last_update_check": str(time.time())})
    fresh = _bridge(_info(latest="1.3.0"))

    assert fresh.update_state()["available"] is False


# ---------------------------------------------------------------------------
# release page / asset: opening, never downloading implicitly
# ---------------------------------------------------------------------------


def test_the_notice_payload_carries_the_release_page_and_asset(
    prefs: dict[str, Any],
) -> None:
    prefs["last_update_check"] = str(time.time())
    bridge = _bridge(_info())

    state = bridge.update_state()

    assert state["notes_url"] == "https://example.invalid/notes"
    assert state["url"] == state["notes_url"]
    assert state["asset_name"] == "NovelDownloader-setup-9.0.0-x64.exe"
    assert state["asset_size"] == 1234
    assert state["platform"] == "windows-x64"


def test_a_release_without_notes_falls_back_to_the_releases_page(
    prefs: dict[str, Any],
) -> None:
    prefs["last_update_check"] = str(time.time())
    bridge = _bridge(_info(notes_url=None))

    state = bridge.update_state()

    assert state["notes_url"] == wa._RELEASES_PAGE
    assert state["url"] == wa._RELEASES_PAGE


def test_checking_for_an_update_never_downloads_or_installs(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: _Response(_release_payload("v99.0.0")),
    )
    bridge = wa.ApiBridge()
    monkeypatch.setattr(
        bridge,
        "download_update",
        lambda: (_ for _ in ()).throw(AssertionError("check must not download")),
    )
    monkeypatch.setattr(
        bridge,
        "install_update",
        lambda: (_ for _ in ()).throw(AssertionError("check must not install")),
    )

    result = bridge.check_update()

    assert result["available"] is True
    assert result["latest"] == "99.0.0"
    assert result["url"] == "https://example.invalid/notes"
    assert result["asset_name"] == "NovelDownloader-setup-9.0.0-x64.exe"


def test_the_release_notes_button_only_opens_the_page() -> None:
    handler = APP_HTML.split('$("#update-notes").onclick', 1)[1].split(
        '$("#update-install").onclick', 1
    )[0]
    assert 'api("open_url", url)' in handler
    assert 'api("download_update"' not in handler
    assert 'api("install_update")' not in handler
    assert "update_download_state" not in handler


def test_the_install_button_is_the_only_install_entry_point() -> None:
    assert '$("#update-install").onclick' in APP_HTML
    assert APP_HTML.count('api("install_update")') == 1


# ---------------------------------------------------------------------------
# settings: turning the check off means zero network access
# ---------------------------------------------------------------------------


def test_auto_check_off_makes_automatic_paths_do_no_network_io(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    prefs["auto_check_updates"] = False
    checks: list[int] = []
    fetches: list[int] = []

    def forbidden_check(*_args: Any, **_kwargs: Any) -> Any:
        checks.append(1)
        raise AssertionError("check_for_update must not run")

    def forbidden_open(*_args: Any, **_kwargs: Any) -> Any:
        fetches.append(1)
        raise AssertionError("urlopen must not run")

    monkeypatch.setattr(wa, "check_for_update", forbidden_check)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_open)
    bridge = wa.ApiBridge()

    for _ in range(5):
        state = bridge.update_state()
        assert state["auto_check"] is False
        result = bridge.check_update_async()
        assert result["started"] is False
        assert result["reason"] == "disabled"

    assert bridge._update_thread is None
    assert checks == []
    assert fetches == []
    assert "last_update_check" not in prefs


def test_the_manual_check_still_works_with_auto_check_off(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prefs["auto_check_updates"] = False
    calls: list[int] = []
    monkeypatch.setattr(
        wa,
        "check_for_update",
        lambda *a, **k: (
            calls.append(1) or UpdateCheck(info=_info(), error=None, checked_at=1.0)
        ),
    )
    bridge = wa.ApiBridge()

    result = bridge.check_update_async(force=True)

    assert result["started"] is True
    _wait_for_check(bridge)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# front end / i18n
# ---------------------------------------------------------------------------


def test_the_update_notice_offers_skip_and_later() -> None:
    for element_id in ("update-install", "update-notes", "update-skip", "update-later"):
        assert f'id="{element_id}"' in APP_HTML
    assert '$("#update-skip").onclick' in APP_HTML
    assert 'api("skip_update_version"' in APP_HTML
    assert '$("#update-later").onclick' in APP_HTML


def test_a_dismissal_does_not_hide_a_different_version() -> None:
    assert "st.latest !== updates.dismissedVersion" in APP_HTML
    assert "updates.dismissed = false;" in APP_HTML
    assert "updates.dismissedVersion =" in APP_HTML


def test_update_notice_strings_are_translated_in_zh_cn() -> None:
    front_end_keys = (
        "Update available: v{ver}",
        "No package for this platform in v{ver}; open the release page",
        "Download and install",
        "Install and restart",
        "Release notes",
        "Skip this version",
        "Later",
        "Downloading…",
        "Installing…",
        "Update download failed",
        "Could not start the installer",
    )
    catalog = wa._i18n_catalog()
    for key in front_end_keys:
        assert key in catalog, f"missing front-end key: {key}"

    # Bridge-side strings never reach ``_i18n_catalog``; they still go through
    # ``t()`` and must have a zh_CN entry in the compiled catalogue.
    bridge_keys = (
        "No update is available to skip",
        "Could not save the settings",
    )
    zh = get_translation("zh_CN")
    for key in front_end_keys + bridge_keys:
        assert zh.gettext(key) != key, f"untranslated zh_CN string: {key}"
    assert zh.gettext("Skip this version") == "跳过此版本"
    assert zh.gettext("No update is available to skip") == "当前没有可跳过的更新"
    assert 'data-i18n="Skip this version"' in APP_HTML


def test_the_desktop_check_uses_the_project_slug_and_timeout(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_check(current: str, **kwargs: Any) -> UpdateCheck:
        seen["current"] = current
        seen.update(kwargs)
        return UpdateCheck(info=_info(), error=None, checked_at=1.0)

    monkeypatch.setattr(wa, "check_for_update", fake_check)
    bridge = wa.ApiBridge()

    bridge.check_update_async()
    _wait_for_check(bridge)

    assert seen["current"] == wa.ApiBridge._version()
    assert seen["slug"] == DEFAULT_REPO
    assert seen["timeout"] == wa._UPDATE_TIMEOUT == 8.0
    assert seen["platform_tag"] == wa.detect_platform()
