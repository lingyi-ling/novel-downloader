#!/usr/bin/env python3
"""
tests.apps.test_desktop_update
------------------------------

Offline tests for the desktop client's in-app updater: the cached
``update_state`` / ``check_update_async`` view, the verified ``download_update``
step and the explicitly user-triggered ``install_update``.

Everything that touches the outside world is replaced:

* the release feed by a fake ``check_for_update`` (no network at all);
* the transfer by a fake ``download_asset`` writing into a ``tmp_path`` tree;
* the installer by a fake ``subprocess.Popen`` that never starts a process.

``NOVEL_HOME`` is pointed at ``tmp_path`` and the preference file is kept in
memory, so no real user directory is read or written.
"""

from __future__ import annotations

import ctypes
import hashlib
import inspect
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.common.updater import (
    DEFAULT_REPO,
    ReleaseAsset,
    UpdateCheck,
    UpdateInfo,
    verify_sha256,
)
from novel_downloader.apps.desktop import webview_app as wa
from novel_downloader.apps.desktop.web_assets import APP_HTML

_PAYLOAD = b"verified update payload"
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()

#: Distinguishes "use the default asset" from an explicitly missing asset.
_DEFAULT_ASSET: Any = object()


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def prefs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """In-memory replacement for the preference file."""
    stored: dict[str, Any] = {}
    monkeypatch.setattr(wa, "_load_prefs", lambda: dict(stored))
    monkeypatch.setattr(wa, "_save_prefs", lambda values: stored.update(values))
    return stored


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Private ``app_home()`` so the update folder stays inside ``tmp_path``."""
    target = tmp_path / "home"
    monkeypatch.setenv("NOVEL_HOME", str(target))
    return target


def _asset(**overrides: Any) -> ReleaseAsset:
    values: dict[str, Any] = {
        "name": "NovelDownloader-setup-9.0.0-x64.exe",
        "url": ("https://example.invalid/download/NovelDownloader-setup-9.0.0-x64.exe"),
        "size": len(_PAYLOAD),
        "sha256": _SHA,
    }
    values.update(overrides)
    return ReleaseAsset(**values)


def _info(
    asset: Any = _DEFAULT_ASSET,
    *,
    available: bool = True,
    latest: str = "9.0.0",
) -> UpdateInfo:
    chosen = _asset() if asset is _DEFAULT_ASSET else asset
    return UpdateInfo(
        current="1.0.0",
        latest=latest,
        available=available,
        notes_url="https://example.invalid/notes",
        asset=chosen,
        assets=(chosen,),
    )


def _cache(info: UpdateInfo | None, *, checked_at: float | None = None) -> Any:
    return wa._UpdateCache(
        info=info,
        error=None if info is not None else "unreachable",
        checked_at=time.time() if checked_at is None else checked_at,
        current="1.0.0",
        platform="windows-x64",
    )


def _bridge_with(info: UpdateInfo | None) -> wa.ApiBridge:
    bridge = wa.ApiBridge()
    bridge._update_cache = _cache(info)
    return bridge


def _wait_for_check(bridge: wa.ApiBridge, timeout: float = 5.0) -> None:
    """Block until the bridge's background check thread has finished."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with bridge._update_guard:
            thread = bridge._update_thread
        if thread is None:
            return
        thread.join(0.05)
    raise AssertionError("the update check did not finish in time")


def _write_updates_file(home: Path, name: str, content: bytes) -> Path:
    """Put ``content`` in ``home/updates`` and return its path."""
    target = Path(home).resolve() / wa._UPDATES_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(content)
    return path


def _recording_popen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace ``subprocess.Popen`` with a recorder that starts nothing."""
    calls: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> Any:
        calls.append(list(cmd))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    return calls


def _use_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")


# ---------------------------------------------------------------------------
# update_state / check_update_async
# ---------------------------------------------------------------------------


def test_update_state_exposes_the_documented_keys(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payload carries every key the front end reads, offline."""
    prefs["last_update_check"] = str(time.time())
    calls: list[int] = []
    monkeypatch.setattr(wa, "check_for_update", lambda *a, **k: calls.append(1))

    state = wa.ApiBridge().update_state()

    expected = {
        "current",
        "latest",
        "available",
        "notes_url",
        "asset_name",
        "asset_size",
        "platform",
        "checked_at",
        "error",
    }
    assert expected <= set(state)
    assert state["available"] is False
    assert state["latest"] == ""
    assert state["error"] is None
    assert not calls, "a throttled cache miss must not hit the network"


def test_update_state_serves_the_cache_without_a_second_request(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A populated cache is reused; the feed is contacted at most never."""
    prefs["last_update_check"] = str(time.time())
    calls: list[int] = []

    def boom(*_args: Any, **_kwargs: Any) -> UpdateCheck:
        calls.append(1)
        raise AssertionError("update_state must not check synchronously")

    monkeypatch.setattr(wa, "check_for_update", boom)
    bridge = _bridge_with(_info())

    first = bridge.update_state()
    second = bridge.update_state()

    assert first["available"] is True
    assert first["latest"] == "9.0.0"
    assert first["notes_url"] == "https://example.invalid/notes"
    assert first["asset_name"] == "NovelDownloader-setup-9.0.0-x64.exe"
    assert first["asset_size"] == len(_PAYLOAD)
    assert first["platform"] == "windows-x64"
    assert first["checked_at"] > 0
    assert second["checked_at"] == first["checked_at"]
    assert not calls


def test_check_update_async_returns_the_cache_when_throttled(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within 24 h an existing cache is returned, with no background thread."""
    prefs["last_update_check"] = str(time.time())
    calls: list[int] = []
    monkeypatch.setattr(wa, "check_for_update", lambda *a, **k: calls.append(1))
    bridge = _bridge_with(_info())

    result = bridge.check_update_async()

    assert result["started"] is False
    assert result["reason"] == "throttled"
    assert result["available"] is True
    assert bridge._update_thread is None
    assert not calls


def test_check_update_async_runs_in_the_background(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check runs on a thread and publishes a fully-formed cache."""
    seen: dict[str, Any] = {}

    def fake_check(current: str, **kwargs: Any) -> UpdateCheck:
        seen.update(kwargs)
        seen["current"] = current
        return UpdateCheck(info=_info(), error=None, checked_at=1234.0)

    monkeypatch.setattr(wa, "check_for_update", fake_check)
    bridge = wa.ApiBridge()

    result = bridge.check_update_async()

    assert result["started"] is True
    assert result["pending"] is True, "the page must know it has to poll"
    _wait_for_check(bridge)
    state = bridge.update_state()
    assert state["available"] is True
    assert state["checked_at"] == 1234.0
    assert state["notes_url"] == "https://example.invalid/notes"
    assert state["pending"] is False
    # The shared core is called with the desktop contract (8 s, project slug).
    assert seen["timeout"] == wa._UPDATE_TIMEOUT == 8.0
    assert seen["slug"] == DEFAULT_REPO
    assert seen["platform_tag"] == state["platform"]
    assert prefs.get("last_update_check")


def test_auto_check_disabled_never_touches_the_network(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``auto_check_updates=false`` makes both entry points cache-only."""
    prefs["auto_check_updates"] = False
    calls: list[int] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> UpdateCheck:
        calls.append(1)
        return UpdateCheck(None, None, 0.0)

    monkeypatch.setattr(wa, "check_for_update", forbidden)
    bridge = _bridge_with(_info())

    state = bridge.update_state()
    auto = bridge.check_update_async()

    assert state["auto_check"] is False
    assert state["available"] is True  # cache still served
    assert auto["started"] is False
    assert auto["reason"] == "disabled"
    assert bridge._update_thread is None
    assert not calls


def test_forced_check_bypasses_the_off_switch(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit user action may still check with auto-checking disabled."""
    prefs["auto_check_updates"] = False
    prefs["last_update_check"] = str(time.time())
    monkeypatch.setattr(
        wa,
        "check_for_update",
        lambda *a, **k: UpdateCheck(info=_info(), error=None, checked_at=1.0),
    )
    bridge = wa.ApiBridge()

    result = bridge.check_update_async(force=True)

    assert result["started"] is True
    _wait_for_check(bridge)
    assert bridge.update_state()["available"] is True


def test_initial_state_exposes_the_auto_check_preference(
    prefs: dict[str, Any],
) -> None:
    prefs["auto_check_updates"] = False
    assert wa.ApiBridge().get_initial_state()["auto_check_updates"] is False


# ---------------------------------------------------------------------------
# download_update: scheme and asset validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/System32/evil.exe",
        "ftp://example.invalid/NovelDownloader.exe",
        "data:application/octet-stream;base64,AAAA",
    ],
)
def test_download_update_refuses_non_http_asset_urls(
    url: str,
    prefs: dict[str, Any],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``file:``/``ftp:``/``data:`` assets are rejected before any transfer."""
    called: list[int] = []
    monkeypatch.setattr(wa, "download_asset", lambda *a, **k: called.append(1))
    bridge = _bridge_with(_info(_asset(url=url)))

    result = bridge.download_update()

    assert result["ok"] is False
    assert result["path"] is None
    assert result["error"]
    assert not called


def test_download_update_refuses_a_missing_asset(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``info.asset is None`` (no matching platform build) is a clear error."""
    monkeypatch.setattr(wa, "download_asset", lambda *a, **k: pytest.fail("called"))
    bridge = _bridge_with(_info(asset=None))

    result = bridge.download_update()

    assert result["ok"] is False
    assert "platform" in result["error"].lower()


def test_download_update_refuses_an_unverifiable_release(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a published digest nothing is downloaded (nothing to verify)."""
    monkeypatch.setattr(wa, "download_asset", lambda *a, **k: pytest.fail("called"))
    bridge = _bridge_with(_info(_asset(sha256=None)))

    result = bridge.download_update()

    assert result["ok"] is False
    assert "checksum" in result["error"].lower()


def test_download_update_refuses_when_no_update_is_available(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wa, "download_asset", lambda *a, **k: pytest.fail("called"))
    bridge = _bridge_with(_info(available=False))

    result = bridge.download_update()

    assert result["ok"] is False
    assert result["error"]


# ---------------------------------------------------------------------------
# download_update: verified writes
# ---------------------------------------------------------------------------


def test_download_update_verifies_and_records_the_file(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching digest is downloaded into ``app_home()/updates`` and kept."""
    seen: dict[str, Any] = {}

    def fake_download(
        url: str,
        dest: Path,
        expected_sha256: str | None = None,
        mirror: Any = None,
        *,
        root: Path,
        timeout: float,
    ) -> Path:
        seen.update(
            url=url,
            sha=expected_sha256,
            root=Path(root),
            timeout=timeout,
        )
        target = Path(root) / Path(dest).name
        target.write_bytes(_PAYLOAD)
        return target

    monkeypatch.setattr(wa, "download_asset", fake_download)
    bridge = _bridge_with(_info())

    result = bridge.download_update()

    assert result["ok"] is True
    assert result["error"] is None
    path = Path(result["path"])
    assert path.is_file()
    assert path.read_bytes() == _PAYLOAD
    assert path.parent == Path(home.resolve()) / wa._UPDATES_DIRNAME
    assert seen["sha"] == _SHA
    assert seen["timeout"] == wa._UPDATE_DOWNLOAD_TIMEOUT
    assert bridge._downloaded is not None
    assert bridge._downloaded.sha256 == _SHA


def test_download_update_deletes_a_checksum_mismatch(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that does not match the digest is removed, never left behind."""

    def fake_download(
        _url: str,
        dest: Path,
        expected_sha256: str | None = None,
        mirror: Any = None,
        *,
        root: Path,
        timeout: float,
    ) -> Path:
        target = Path(root) / Path(dest).name
        target.write_bytes(b"tampered bytes")
        return target

    monkeypatch.setattr(wa, "download_asset", fake_download)
    bridge = _bridge_with(_info())

    result = bridge.download_update()

    assert result["ok"] is False
    assert "checksum" in result["error"].lower()
    assert bridge._downloaded is None
    updates = Path(home.resolve()) / wa._UPDATES_DIRNAME
    assert not list(updates.iterdir()), "a failed download must leave no file"


def test_download_update_surfaces_transfer_errors(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_download(*_args: Any, **_kwargs: Any) -> Path:
        raise OSError("network down")

    monkeypatch.setattr(wa, "download_asset", fake_download)

    result = _bridge_with(_info()).download_update()

    assert result["ok"] is False
    assert "network down" in result["error"]


def test_concurrent_downloads_are_rejected(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only one transfer may run; the second call gets a friendly error."""
    started = threading.Event()
    release = threading.Event()

    def slow_download(
        _url: str,
        dest: Path,
        expected_sha256: str | None = None,
        mirror: Any = None,
        *,
        root: Path,
        timeout: float,
    ) -> Path:
        started.set()
        assert release.wait(5.0)
        target = Path(root) / Path(dest).name
        target.write_bytes(_PAYLOAD)
        return target

    monkeypatch.setattr(wa, "download_asset", slow_download)
    bridge = _bridge_with(_info())
    results: dict[str, Any] = {}

    worker = threading.Thread(
        target=lambda: results.update(first=bridge.download_update())
    )
    worker.start()
    try:
        assert started.wait(5.0)
        second = bridge.download_update()
        assert second["ok"] is False
        assert "already" in second["error"].lower()
    finally:
        release.set()
        worker.join(5.0)

    assert results["first"]["ok"] is True


# ---------------------------------------------------------------------------
# install_update: refusal paths
# ---------------------------------------------------------------------------


def test_install_update_refuses_before_a_download(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)

    result = wa.ApiBridge().install_update()

    assert result["ok"] is False
    assert "downloaded" in result["error"].lower()
    assert not calls


def test_install_update_refuses_on_non_windows(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-Windows host is told to install manually, not silently "ok"."""
    calls = _recording_popen(monkeypatch)
    monkeypatch.setattr("sys.platform", "linux")
    path = _write_updates_file(home, "NovelDownloader-setup-9.0.0-x64.exe", _PAYLOAD)
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    result = bridge.install_update()

    assert result["ok"] is False
    assert "windows" in result["error"].lower()
    assert not calls
    assert path.exists(), "a refused install must not touch the file"


def test_install_update_refuses_a_zip_bundle(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zip asset is never unpacked over the running program."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    path = _write_updates_file(home, "NovelDownloader-9.0.0-windows-x64.zip", _PAYLOAD)
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    result = bridge.install_update()

    assert result["ok"] is False
    assert "extract" in result["error"].lower()
    assert not calls
    assert path.exists()


def test_install_update_reports_a_missing_installer(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted installer is reported explicitly, never silently ignored."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    missing = Path(home).resolve() / wa._UPDATES_DIRNAME / "setup.exe"
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(
        path=missing, sha256=_SHA, name=missing.name
    )

    result = bridge.install_update()

    assert result["ok"] is False
    assert "missing" in result["error"].lower()
    assert not calls


def test_install_update_rejects_a_tampered_file(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digest is re-checked before launch; a changed file is deleted."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    path = _write_updates_file(home, "setup.exe", b"swapped by malware")
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    result = bridge.install_update()

    assert result["ok"] is False
    assert "checksum" in result["error"].lower()
    assert not calls
    assert not path.exists()
    assert bridge._downloaded is None


def test_install_update_refuses_a_file_outside_the_update_folder(
    prefs: dict[str, Any], home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    outside = tmp_path / "setup.exe"
    outside.write_bytes(_PAYLOAD)
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(
        path=outside, sha256=_SHA, name=outside.name
    )

    result = bridge.install_update()

    assert result["ok"] is False
    assert "outside" in result["error"].lower()
    assert not calls
    assert outside.exists()


# ---------------------------------------------------------------------------
# install_update: the Windows happy path (never starts a real installer)
# ---------------------------------------------------------------------------


def test_install_update_launches_the_installer_then_exits(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed flags, no shell, verified file, then the window closes."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_DELAY", 0.0)
    path = _write_updates_file(home, "NovelDownloader-setup-9.0.0-x64.exe", _PAYLOAD)
    exited = threading.Event()
    bridge = wa.ApiBridge()
    bridge._attach_window(SimpleNamespace(destroy=exited.set))
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    result = bridge.install_update()

    assert result["ok"] is True
    assert result["pid"] == 4242
    assert calls == [
        [
            str(path),
            "/SILENT",
            "/SUPPRESSMSGBOXES",
            "/CLOSEAPPLICATIONS",
            "/RESTARTAPPLICATIONS",
        ]
    ]
    assert exited.wait(5.0), "the window must be closed after the launch"
    # The record is consumed: a second click cannot start the installer twice.
    assert bridge._downloaded is None
    assert bridge.install_update()["ok"] is False


def test_install_update_does_not_exit_when_the_launch_fails(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Popen failure returns a structured error and keeps the app open."""
    _use_windows(monkeypatch)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_DELAY", 0.0)

    def broken_popen(_cmd: list[str], **_kwargs: Any) -> Any:
        raise OSError("installer is locked")

    monkeypatch.setattr("subprocess.Popen", broken_popen)
    path = _write_updates_file(home, "setup.exe", _PAYLOAD)
    exited = threading.Event()
    bridge = wa.ApiBridge()
    bridge._attach_window(SimpleNamespace(destroy=exited.set))
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    result = bridge.install_update()

    assert result["ok"] is False
    assert "locked" in result["error"]
    assert not exited.wait(0.3), "a failed launch must not close the app"
    assert bridge._downloaded is not None, "the download stays retryable"


# ---------------------------------------------------------------------------
# bridge methods never raise into JS
# ---------------------------------------------------------------------------


def test_check_worker_survives_a_raising_core(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> UpdateCheck:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(wa, "check_for_update", boom)
    bridge = wa.ApiBridge()

    result = bridge.check_update_async()
    assert result["started"] is True
    _wait_for_check(bridge)
    state = bridge.update_state()

    assert state["available"] is False
    assert "kaboom" in str(state["error"])
    assert state["pending"] is False


def test_update_state_survives_a_broken_preference_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("prefs unreadable")

    monkeypatch.setattr(wa, "_load_prefs", boom)

    state = wa.ApiBridge().update_state()  # must not raise

    assert state["available"] is False
    assert "prefs unreadable" in str(state["error"])
    assert state["auto_check"] is True


def test_download_and_install_survive_unexpected_errors(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a non-``OSError`` failure is reported, never raised at JS."""
    real_updates_dir = wa._updates_dir

    def broken_updates_dir() -> Path:
        raise PermissionError("nope")

    monkeypatch.setattr(wa, "_updates_dir", broken_updates_dir)
    downloaded = _bridge_with(_info()).download_update()
    assert downloaded["ok"] is False
    assert "nope" in str(downloaded["error"])

    # Restore only this patch (undoing everything would drop the NOVEL_HOME
    # isolation set up by the fixtures).
    monkeypatch.setattr(wa, "_updates_dir", real_updates_dir)
    _use_windows(monkeypatch)
    path = _write_updates_file(home, "setup.exe", _PAYLOAD)
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    def broken_verify(*_args: Any) -> bool:
        raise IndexError("boom")

    monkeypatch.setattr(wa, "verify_sha256", broken_verify)
    installed = bridge.install_update()
    assert installed["ok"] is False
    assert "boom" in str(installed["error"])


# ---------------------------------------------------------------------------
# update mutex (Windows only, best effort)
# ---------------------------------------------------------------------------


def test_update_mutex_is_a_noop_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Windows API is touched on other platforms."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(wa, "_MUTEX_HANDLE", None)

    wa._hold_update_mutex()

    assert wa._MUTEX_HANDLE is None


# ---------------------------------------------------------------------------
# front end: the update bar
# ---------------------------------------------------------------------------


def test_app_html_update_bar_only_appears_when_a_release_exists() -> None:
    assert 'id="update-bar" hidden' in APP_HTML
    assert "const show = !!st.available && !updates.dismissed;" in APP_HTML
    # A release without a matching asset gets no (dead) install button.
    assert '$("#update-install").hidden = !st.asset_name;' in APP_HTML


def test_app_html_update_bar_has_install_notes_and_later() -> None:
    for element_id in (
        'id="update-install"',
        'id="update-notes"',
        'id="update-later"',
        'id="update-text"',
    ):
        assert element_id in APP_HTML


def test_app_html_update_bar_wires_i18n_keys() -> None:
    catalog = wa._i18n_catalog()
    for key in (
        "Update available: v{ver}",
        "Download and install",
        "Release notes",
        "Later",
        "Downloading…",
        "Installing…",
    ):
        assert key in catalog
        assert f'data-i18n="{key}"' in APP_HTML or f't("{key}")' in APP_HTML


def test_app_html_update_buttons_are_disabled_while_downloading() -> None:
    """The anti-double-click guard must run before the first await."""
    guard = APP_HTML.index("setUpdateBusy(true)")
    first_await = APP_HTML.index('await api("download_update")')
    assert guard < first_await
    selector = 'for (const id of ["#update-install", "#update-notes", "#update-later"])'
    assert selector in APP_HTML


def test_app_html_release_notes_open_in_the_system_browser() -> None:
    assert '$("#update-notes").onclick' in APP_HTML
    assert 'api("open_url", url)' in APP_HTML


def test_app_html_later_dismisses_for_the_session_only() -> None:
    assert '$("#update-later").onclick' in APP_HTML
    assert "updates.dismissed = true;" in APP_HTML
    # The dismissal is deliberately not persisted to localStorage.
    handler = APP_HTML.split('$("#update-later").onclick', 1)[1].split("};", 1)[0]
    assert "store.set" not in handler


def test_app_html_prefers_the_async_check_and_keeps_the_fallback() -> None:
    assert 'typeof bridge.check_update_async === "function"' in APP_HTML
    assert 'api("check_update_async").then(' in APP_HTML
    assert 'api("check_update").then(' in APP_HTML
    assert "if (updatePollTimer) clearInterval(updatePollTimer);" in APP_HTML


def test_app_html_update_errors_are_toasted_not_thrown() -> None:
    assert 'toast((dl && dl.error) || t("Update download failed"), "err"' in APP_HTML
    assert 't("Could not start the installer")' in APP_HTML


# ---------------------------------------------------------------------------
# download_update: HTTPS only (the file is executed later)
# ---------------------------------------------------------------------------


def test_download_update_refuses_a_plaintext_http_asset(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GUI download is executed later, so plain HTTP is never accepted."""
    called: list[int] = []
    monkeypatch.setattr(wa, "download_asset", lambda *a, **k: called.append(1))
    asset = _asset(url="http://example.invalid/NovelDownloader-setup.exe")
    bridge = _bridge_with(_info(asset))

    result = bridge.download_update()

    assert result["ok"] is False
    assert "insecure" in result["error"].lower()
    assert not called, "a plaintext asset must not even reach the transfer helper"


def test_download_update_accepts_an_https_asset(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression guard for the rule above: HTTPS still works."""

    def fake_download(
        _url: str,
        dest: Path,
        expected_sha256: str | None = None,
        mirror: Any = None,
        *,
        root: Path,
        timeout: float,
    ) -> Path:
        target = Path(root) / Path(dest).name
        target.write_bytes(_PAYLOAD)
        return target

    monkeypatch.setattr(wa, "download_asset", fake_download)

    result = _bridge_with(_info()).download_update()

    assert result["ok"] is True


def test_download_update_sanitises_a_traversal_asset_name(
    prefs: dict[str, Any], home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``../../../evil.exe`` is reduced to a basename inside ``app_home/updates``."""
    seen: dict[str, Any] = {}

    def fake_download(
        _url: str,
        dest: Path,
        expected_sha256: str | None = None,
        mirror: Any = None,
        *,
        root: Path,
        timeout: float,
    ) -> Path:
        seen["dest"] = Path(dest)
        seen["root"] = Path(root)
        target = Path(root) / Path(dest).name
        target.write_bytes(_PAYLOAD)
        return target

    monkeypatch.setattr(wa, "download_asset", fake_download)
    updates = Path(home).resolve() / wa._UPDATES_DIRNAME
    asset = _asset(name="../../../evil.exe")

    result = _bridge_with(_info(asset)).download_update()

    assert result["ok"] is True
    assert seen["dest"] == Path("evil.exe"), "the name is a bare basename"
    assert not seen["dest"].is_absolute(), "the helper resolves it under root"
    assert seen["root"] == updates, "the only allowed root is app_home()/updates"
    written = Path(result["path"])
    assert written.parent == updates
    assert written.name == "evil.exe"
    assert not (Path(tmp_path) / "evil.exe").exists()


# ---------------------------------------------------------------------------
# install_update: only a verified download from this session, exactly once
# ---------------------------------------------------------------------------


def test_install_update_ignores_an_exe_that_was_never_downloaded(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file merely present in ``updates`` is not installed without a record."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    _write_updates_file(home, "setup.exe", _PAYLOAD)

    result = wa.ApiBridge().install_update()

    assert result["ok"] is False
    assert "downloaded" in result["error"].lower()
    assert not calls


def test_install_update_takes_no_path_argument(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path cannot be supplied from JS; it always comes from the record."""
    assert list(inspect.signature(wa.ApiBridge.install_update).parameters) == ["self"]
    assert list(inspect.signature(wa.ApiBridge.download_update).parameters) == ["self"]
    # pywebview would forward an extra JS argument here; Python rejects the
    # call before the body runs (no path is ever taken from the page).
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    with pytest.raises(TypeError):
        wa.ApiBridge().install_update("C:/Windows/System32/evil.exe")  # type: ignore[call-arg]
    assert not calls


def test_install_update_keeps_a_path_with_spaces_intact(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list argv (no shell) survives spaces in the installer path."""
    calls: list[list[str]] = []
    kwargs_seen: list[dict[str, Any]] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(list(cmd))
        kwargs_seen.append(kwargs)
        return SimpleNamespace(pid=99)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    _use_windows(monkeypatch)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_DELAY", 0.0)
    name = "Novel Downloader Setup 9.0.0.exe"
    path = _write_updates_file(home, name, _PAYLOAD)
    bridge = wa.ApiBridge()
    bridge._attach_window(SimpleNamespace(destroy=lambda: None))
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=name)

    result = bridge.install_update()

    assert result["ok"] is True
    [argv] = calls
    assert isinstance(argv, list)
    assert argv[0] == str(path), "the exact path is one argv element"
    assert argv[1:] == list(wa._INSTALLER_ARGS)
    assert kwargs_seen[0].get("shell") is False


def test_install_update_launches_one_installer_under_concurrency(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two simultaneous bridge calls must not start the installer twice."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_DELAY", 0.0)
    path = _write_updates_file(home, "setup.exe", _PAYLOAD)
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    entered = threading.Event()
    release = threading.Event()
    real_verify = verify_sha256

    def slow_verify(candidate: Path, expected: str) -> bool:
        entered.set()
        assert release.wait(5.0)
        return real_verify(candidate, expected)

    monkeypatch.setattr(wa, "verify_sha256", slow_verify)
    results: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: results.update(first=bridge.install_update())
    )
    worker.start()
    try:
        assert entered.wait(5.0)
        second = bridge.install_update()
        assert second["ok"] is False
        assert "downloaded" in second["error"].lower()
    finally:
        release.set()
        worker.join(5.0)

    assert results["first"]["ok"] is True
    assert len(calls) == 1, "the claim must be consumed exactly once"


# ---------------------------------------------------------------------------
# window close failures after the installer has started
# ---------------------------------------------------------------------------


def test_install_update_retries_a_transient_close_failure(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close that raises is retried once before giving up."""
    _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_DELAY", 0.0)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_RETRY_DELAY", 0.0)
    path = _write_updates_file(home, "setup.exe", _PAYLOAD)
    attempts: list[int] = []
    closed = threading.Event()

    def destroy() -> None:
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("GUI thread busy")
        closed.set()

    bridge = wa.ApiBridge()
    bridge._attach_window(SimpleNamespace(destroy=destroy))
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    assert bridge.install_update()["ok"] is True
    assert closed.wait(5.0), "the retry must eventually close the window"
    assert len(attempts) == 2


def test_install_update_closes_the_window_from_a_worker_thread(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``window.destroy()`` runs on the dedicated exit thread, never the GUI one.

    pywebview dispatches every ``js_api`` call on a worker thread and the
    WinForms backend marshals ``Window.destroy()`` back through
    ``Control.Invoke``, which is the supported cross-thread close path.  This
    test pins both facts: the call is off the main thread and deliberately
    goes through ``novel-desktop-update-exit``.
    """
    _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_DELAY", 0.0)
    path = _write_updates_file(home, "setup.exe", _PAYLOAD)
    seen: dict[str, Any] = {}
    done = threading.Event()

    def destroy() -> None:
        seen["thread"] = threading.current_thread()
        done.set()

    bridge = wa.ApiBridge()
    bridge._attach_window(SimpleNamespace(destroy=destroy))
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    assert bridge.install_update()["ok"] is True
    assert done.wait(5.0)
    assert seen["thread"] is not threading.main_thread()
    assert seen["thread"].name == "novel-desktop-update-exit"


def test_install_update_warns_and_asks_for_a_manual_close(
    prefs: dict[str, Any], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window that cannot be closed is logged loudly, not swallowed."""
    _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_DELAY", 0.0)
    monkeypatch.setattr(wa, "_INSTALL_EXIT_RETRY_DELAY", 0.0)
    path = _write_updates_file(home, "setup.exe", _PAYLOAD)
    attempts: list[int] = []
    second_attempt = threading.Event()
    warnings: list[str] = []

    def destroy() -> None:
        attempts.append(1)
        if len(attempts) >= 2:
            second_attempt.set()
        raise RuntimeError("no window")

    monkeypatch.setattr(
        wa.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg))
    )
    bridge = wa.ApiBridge()
    bridge._attach_window(SimpleNamespace(destroy=destroy))
    bridge._downloaded = wa._DownloadedUpdate(path=path, sha256=_SHA, name=path.name)

    assert bridge.install_update()["ok"] is True
    assert second_attempt.wait(5.0)
    deadline = time.monotonic() + 5.0
    while not warnings and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(attempts) == 2
    assert warnings, "a close failure with a running installer must be logged"
    assert "manually" in warnings[0].lower()
    assert "installer" in warnings[0].lower()


# ---------------------------------------------------------------------------
# update mutex: prototype, handle retention, non-Windows safety
# ---------------------------------------------------------------------------


class _FakeCreateMutexW:
    """Stand-in for ``kernel32.CreateMutexW`` recording its prototype."""

    def __init__(self, handle: int) -> None:
        self.handle = handle
        self.calls: list[tuple[Any, ...]] = []
        self.restype: Any = None
        self.argtypes: Any = None

    def __call__(self, *args: Any) -> int:
        self.calls.append(args)
        return self.handle


class _WindllTripwire:
    """``ctypes.windll`` stand-in that records any attribute access."""

    def __init__(self) -> None:
        self.accessed = False

    def __getattr__(self, name: str) -> Any:
        self.accessed = True
        raise AttributeError(name)


def test_update_mutex_declares_the_prototype_and_keeps_the_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 64-bit HANDLE and a wide-string name need an explicit prototype."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(wa, "_MUTEX_HANDLE", None)
    fake = _FakeCreateMutexW(0x1234)
    windll = SimpleNamespace(kernel32=SimpleNamespace(CreateMutexW=fake))
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)

    wa._hold_update_mutex()

    assert wa._MUTEX_HANDLE == 0x1234, "the handle must outlive the call"
    assert fake.restype is ctypes.c_void_p
    assert fake.argtypes == (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    assert fake.calls == [(None, False, wa._UPDATE_MUTEX_NAME)]

    # A repeated call must not create (and leak) a second handle.
    wa._hold_update_mutex()
    assert len(fake.calls) == 1


def test_update_mutex_never_touches_windll_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Linux/macOS the module never reaches for ``ctypes.windll``."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(wa, "_MUTEX_HANDLE", None)
    tripwire = _WindllTripwire()
    monkeypatch.setattr(ctypes, "windll", tripwire, raising=False)

    wa._hold_update_mutex()

    assert tripwire.accessed is False
    assert wa._MUTEX_HANDLE is None


def test_update_mutex_survives_a_win32_host_without_windll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated win32 on Linux CI: a missing ``windll`` must not propagate."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(wa, "_MUTEX_HANDLE", None)
    tripwire = _WindllTripwire()
    monkeypatch.setattr(ctypes, "windll", tripwire, raising=False)

    wa._hold_update_mutex()  # must not raise

    assert tripwire.accessed is True
    assert wa._MUTEX_HANDLE is None


# ---------------------------------------------------------------------------
# front end: visibility, button reset, silent check failures
# ---------------------------------------------------------------------------


def test_app_html_hidden_wins_over_the_bar_display_rule() -> None:
    """``.update-bar { display: flex }`` would defeat the ``hidden`` attribute."""
    assert ".update-bar {\n  display: flex;" in APP_HTML
    assert "[hidden] { display: none !important; }" in APP_HTML


def test_app_html_resets_the_buttons_after_a_failed_install() -> None:
    handler = APP_HTML.split('$("#update-install").onclick', 1)[1]
    final_block = handler.split("} finally {", 1)[1].split("};", 1)[0]
    assert "if (!updates.finished)" in final_block
    assert "setUpdateBusy(false);" in final_block
    assert 'btn.textContent = t("Download and install");' in final_block


def test_app_html_update_check_failures_are_silent() -> None:
    """No network must mean no error toast; only an available release shows."""
    assert (
        'api("check_update_async").then(r => { applyUpdateState(r); '
        "pollUpdateState(); }).catch(() => {});" in APP_HTML
    )
    assert "const show = !!st.available && !updates.dismissed;" in APP_HTML
    assert "updates.state.error" not in APP_HTML


def test_app_html_renders_the_version_as_text() -> None:
    """The release tag is never interpolated as HTML."""
    assert '$("#update-text").textContent' in APP_HTML
    assert '$("#update-text").innerHTML' not in APP_HTML
