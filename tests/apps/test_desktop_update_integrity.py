#!/usr/bin/env python3
"""
tests.apps.test_desktop_update_integrity
----------------------------------------

Integrity-focused tests for the desktop update pipeline.

The rest of the suite covers the update *flow* (check -> download -> install)
against fakes.  This module pins the properties that decide whether a tampered
or damaged package can ever be executed:

* a completed transfer is hashed end to end, and a digest or size mismatch
  deletes the file and never promotes it;
* a resumed transfer verifies the *whole* file, not just the bytes appended;
* a release that publishes no ``sha256`` is refused;
* a path that escapes ``updates/`` -- lexically or through a symlink/junction
  -- is refused before anything is written or launched;
* the installer is re-verified from one descriptor immediately before launch,
  and a missing/unlaunchable installer leaves the download retryable;
* the portable PowerShell helper never deletes ``app_home`` or a download
  directory.

Network access is replaced by an in-memory transport; ``NOVEL_HOME`` points at
``tmp_path`` so no real user directory is touched.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.common import updater
from novel_downloader.apps.common.updater import ReleaseAsset, UpdateInfo
from novel_downloader.apps.desktop import webview_app as wa

_PAYLOAD = b"verified desktop update payload"
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()


# ---------------------------------------------------------------------------
# fake transport
# ---------------------------------------------------------------------------


class _Response(io.BytesIO):
    """Minimal ``urlopen`` result: ``read`` plus the headers updater inspects."""

    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        length: int | None = None,
        content_range: str | None = None,
    ) -> None:
        super().__init__(payload)
        self.status = status
        self.headers: dict[str, str] = {}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        if content_range is not None:
            self.headers["Content-Range"] = content_range

    def geturl(self) -> str:
        return "https://example.test/asset.bin"


def _serve(response: _Response) -> Any:
    """A ``urlopen`` replacement returning ``response`` as a context manager."""

    def _open(*_args: object, **_kwargs: object) -> _Response:
        return response

    return _open


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep ``app_home()`` (and therefore ``updates/``) inside ``tmp_path``."""
    target = tmp_path / "home"
    monkeypatch.setenv("NOVEL_HOME", str(target))
    return target


@pytest.fixture
def prefs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """In-memory replacement for the desktop preference file."""
    stored: dict[str, Any] = {}
    monkeypatch.setattr(wa, "_load_prefs", lambda: dict(stored))
    monkeypatch.setattr(wa, "_save_prefs", lambda values: stored.update(values))
    return stored


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _asset(**overrides: Any) -> ReleaseAsset:
    values: dict[str, Any] = {
        "name": "NovelDownloader-setup-9.0.0-x64.exe",
        "url": "https://example.test/NovelDownloader-setup-9.0.0-x64.exe",
        "size": len(_PAYLOAD),
        "sha256": _SHA,
    }
    values.update(overrides)
    return ReleaseAsset(**values)


def _info(asset: ReleaseAsset) -> UpdateInfo:
    return UpdateInfo(
        current="1.0.0",
        latest="9.0.0",
        available=True,
        notes_url="https://example.test/notes",
        asset=asset,
        assets=(asset,),
    )


def _bridge(info: UpdateInfo | None) -> wa.ApiBridge:
    bridge = wa.ApiBridge()
    bridge._update_cache = wa._UpdateCache(
        info=info,
        error=None if info is not None else "unreachable",
        checked_at=0.0,
        current="1.0.0",
        platform="windows-x64",
    )
    return bridge


def _updates(home: Path) -> Path:
    path = Path(home).resolve() / wa._UPDATES_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_updates_file(home: Path, name: str, content: bytes) -> Path:
    path = _updates(home) / name
    path.write_bytes(content)
    return path


def _recording_popen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace ``subprocess.Popen`` with a recorder that starts nothing."""
    calls: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> Any:
        calls.append(list(cmd))
        return SimpleNamespace(pid=4242, kill=lambda: None)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    return calls


def _use_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")


def _no_leftovers(updates: Path) -> bool:
    """No final file and no ``.part`` staging file remain."""
    return list(updates.iterdir()) == []


def _make_junction(link: Path, target: Path) -> bool:
    """
    Create a directory junction at *link* (no admin rights required).

    Returns ``False`` when the platform cannot do it, so callers can skip.
    """
    if sys.platform != "win32":
        return False
    result = subprocess.run(  # noqa: S603, S607 - fixed argv, local test dirs
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and link.exists()


def _make_file_symlink(link: Path, target: Path) -> bool:
    """Create a file symlink, or ``False`` when the host lacks the privilege."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        return False
    return link.is_symlink()


# ---------------------------------------------------------------------------
# digest / size enforcement in download_asset
# ---------------------------------------------------------------------------


def test_digest_mismatch_deletes_the_file_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that does not match the manifest digest never survives."""
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        _serve(_Response(b"tampered bytes")),
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            expected_sha256=_SHA,
            root=tmp_path,
        )

    assert not (tmp_path / "asset.bin").exists()
    assert not (tmp_path / "asset.bin.part").exists()


def test_empty_expected_digest_is_refused_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``""``/``sha256:`` must not silently disable verification."""
    calls: list[int] = []

    def _open(*_args: object, **_kwargs: object) -> _Response:
        calls.append(1)
        return _Response(b"x")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _open)

    for bad in ("", "   ", "sha256:", "not-a-digest", "0" * 63):
        with pytest.raises(ValueError, match="invalid expected sha256"):
            updater.download_asset(
                "https://example.test/asset.bin",
                Path("asset.bin"),
                expected_sha256=bad,
                root=tmp_path,
            )
    assert calls == [], "an unusable digest must be refused before any request"


def test_size_mismatch_rejects_a_short_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body shorter than the published size is not promoted."""
    short = _PAYLOAD[: len(_PAYLOAD) - 4]
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        _serve(_Response(short)),
    )

    with pytest.raises(OSError, match="ended after"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            expected_sha256=_SHA,
            root=tmp_path,
            resume=True,
            expected_size=len(_PAYLOAD),
        )

    assert not (tmp_path / "asset.bin").exists()
    # The correct prefix is kept so the next attempt can resume.
    assert (tmp_path / "asset.bin.part").read_bytes() == short


def test_size_mismatch_rejects_extra_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body longer than the published size is discarded, not trimmed."""
    padded = _PAYLOAD + b"EXTRA-BYTES"
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        _serve(_Response(padded, length=len(padded))),
    )

    with pytest.raises(ValueError, match="release publishes"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            expected_sha256=_SHA,
            root=tmp_path,
            expected_size=len(_PAYLOAD),
        )

    assert not (tmp_path / "asset.bin").exists()
    assert not (tmp_path / "asset.bin.part").exists()


def test_a_server_length_beyond_the_published_size_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advertised Content-Length is bounded by the manifest size too."""
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        _serve(_Response(b"", length=len(_PAYLOAD) + 1)),
    )

    with pytest.raises(ValueError, match="release publishes"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            expected_sha256=_SHA,
            root=tmp_path,
            expected_size=len(_PAYLOAD),
        )

    assert not (tmp_path / "asset.bin").exists()


def test_truncated_transfer_keeps_a_resumable_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that ends before Content-Length is a failure, not a success."""
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        _serve(_Response(b"half", length=10)),
    )

    with pytest.raises(OSError, match="ended after 4 of 10"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            expected_sha256=_SHA,
            root=tmp_path,
            resume=True,
        )

    assert not (tmp_path / "asset.bin").exists()
    assert (tmp_path / "asset.bin.part").read_bytes() == b"half"


# ---------------------------------------------------------------------------
# resume: the whole file is hashed, not only the new bytes
# ---------------------------------------------------------------------------


def test_resume_verifies_the_whole_file_not_only_the_new_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correct prefix plus a correct suffix yields the manifest digest."""
    prefix = _PAYLOAD[:10]
    suffix = _PAYLOAD[10:]
    (tmp_path / "asset.bin.part").write_bytes(prefix)
    requests: list[dict[str, str]] = []

    def _open(request: object, timeout: float = 0) -> _Response:
        headers = dict(getattr(request, "headers", {}))
        requests.append(headers)
        return _Response(
            suffix,
            status=206,
            length=len(suffix),
            content_range=f"bytes {len(prefix)}-{len(_PAYLOAD) - 1}/{len(_PAYLOAD)}",
        )

    monkeypatch.setattr(updater.urllib.request, "urlopen", _open)

    written = updater.download_asset(
        "https://example.test/asset.bin",
        Path("asset.bin"),
        expected_sha256=_SHA,
        root=tmp_path,
        resume=True,
        expected_size=len(_PAYLOAD),
    )

    assert written.read_bytes() == _PAYLOAD
    assert requests[0].get("Range") == f"bytes={len(prefix)}-"


def test_resume_with_a_corrupt_prefix_fails_the_whole_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suffix can be right while the stored prefix is wrong; it must fail."""
    (tmp_path / "asset.bin.part").write_bytes(b"X" * 10)
    suffix = _PAYLOAD[10:]
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda request, timeout=0: _Response(
            suffix,
            status=206,
            length=len(suffix),
            content_range=f"bytes 10-{len(_PAYLOAD) - 1}/{len(_PAYLOAD)}",
        ),
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            expected_sha256=_SHA,
            root=tmp_path,
            resume=True,
            expected_size=len(_PAYLOAD),
        )

    assert not (tmp_path / "asset.bin").exists()
    assert not (tmp_path / "asset.bin.part").exists()


def test_a_complete_partial_is_promoted_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-complete ``.part`` needs no request, only a final hash."""
    (tmp_path / "asset.bin.part").write_bytes(_PAYLOAD)

    def _boom(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("a complete .part must not be re-requested")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _boom)

    written = updater.download_asset(
        "https://example.test/asset.bin",
        Path("asset.bin"),
        expected_sha256=_SHA,
        root=tmp_path,
        resume=True,
        expected_size=len(_PAYLOAD),
    )

    assert written.read_bytes() == _PAYLOAD
    assert not (tmp_path / "asset.bin.part").exists()


def test_a_corrupt_complete_partial_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same shortcut must still refuse a complete-but-wrong partial."""
    (tmp_path / "asset.bin.part").write_bytes(b"Y" * len(_PAYLOAD))
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", _serve(_Response(b"", length=0))
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            expected_sha256=_SHA,
            root=tmp_path,
            resume=True,
            expected_size=len(_PAYLOAD),
        )

    assert not (tmp_path / "asset.bin.part").exists()


# ---------------------------------------------------------------------------
# a manifest without sha256 is never downloadable
# ---------------------------------------------------------------------------


def test_manifest_entry_without_sha256_is_not_selectable() -> None:
    """A platform entry that advertises no digest is dropped, not trusted."""
    platforms = {
        "windows-x64-installer": {
            "name": "NovelDownloader-setup-9.0.0-x64.exe",
            "url": (
                "https://github.com/lingyi-ling/novel-downloader/releases/"
                "download/v9.0.0/NovelDownloader-setup-9.0.0-x64.exe"
            ),
            "size": 10,
        }
    }
    assert (
        updater.select_manifest_asset(platforms, "windows-x64", shape="installer")
        is None
    )


def test_desktop_download_refuses_an_asset_without_a_digest(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``download_update`` never fetches a file it cannot verify."""
    called: list[int] = []
    monkeypatch.setattr(wa, "download_asset", lambda *a, **k: called.append(1))
    asset = _asset(sha256=None)

    result = _bridge(_info(asset)).download_update()

    assert result["ok"] is False
    assert "checksum" in result["error"].lower()
    assert called == []


# ---------------------------------------------------------------------------
# path confinement: traversal and links
# ---------------------------------------------------------------------------


def test_traversal_asset_name_cannot_escape_the_update_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``../`` destination is refused, never resolved to a sibling path."""
    root = tmp_path / "updates"
    root.mkdir()

    def _boom(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("an escaping path must be refused before the fetch")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _boom)

    with pytest.raises(ValueError, match="outside"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("../../../evil.bin"),
            expected_sha256=_SHA,
            root=root,
        )

    assert not (tmp_path / "evil.bin").exists()
    assert not (tmp_path.parent / "evil.bin").exists()


def test_a_bare_traversal_filename_is_sanitised_under_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop path passes a basename, so the parent can never traverse."""
    monkeypatch.setattr(updater.urllib.request, "urlopen", _serve(_Response(_PAYLOAD)))
    root = tmp_path / "updates"
    root.mkdir()
    # This is what ``_download_update`` passes: the sanitised basename only.
    name = Path("../../../evil.bin".replace("\\", "/")).name

    written = updater.download_asset(
        "https://example.test/asset.bin",
        Path(name),
        expected_sha256=_SHA,
        root=root,
    )

    assert written == (root / "evil.bin").resolve()
    assert not (tmp_path / "evil.bin").exists()


def test_absolute_destination_outside_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute ``dest`` outside ``root`` never reaches the network."""
    root = tmp_path / "updates"
    root.mkdir()

    def _boom(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("must not fetch before the path check")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _boom)

    with pytest.raises(ValueError, match="outside"):
        updater.download_asset(
            "https://example.test/asset.bin",
            tmp_path / "elsewhere" / "asset.bin",
            expected_sha256=_SHA,
            root=root,
        )


def test_destination_that_is_a_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing through a planted link is refused even inside ``root``."""
    root = tmp_path / "updates"
    root.mkdir()
    target = root / "real.bin"
    target.write_bytes(b"original")
    link = root / "asset.bin"
    if not _make_file_symlink(link, target):
        pytest.skip("this host cannot create file symlinks")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _serve(_Response(_PAYLOAD)))

    with pytest.raises(ValueError, match="through a link"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("asset.bin"),
            expected_sha256=_SHA,
            root=root,
        )

    assert target.read_bytes() == b"original", "the link target must be untouched"


def test_destination_directory_that_is_a_junction_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junction for the containing directory is refused the same way."""
    root = tmp_path / "updates"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    nested = root / "nested"
    if not _make_junction(nested, outside):
        pytest.skip("this host cannot create junctions")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _serve(_Response(_PAYLOAD)))

    with pytest.raises(ValueError, match="through a link"):
        updater.download_asset(
            "https://example.test/asset.bin",
            Path("nested") / "asset.bin",
            expected_sha256=_SHA,
            root=root,
        )

    assert list(outside.iterdir()) == [], "nothing may be written outside root"


def test_prune_never_follows_a_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning a link unlinks the link, not the tree behind it."""
    updates = tmp_path / "updates"
    updates.mkdir()
    outside = tmp_path / "user-data"
    (outside / "downloads").mkdir(parents=True)
    keep = outside / "downloads" / "book.txt"
    keep.write_text("novel", encoding="utf-8")
    link = updates / "stale-dir"
    if not _make_junction(link, outside):
        pytest.skip("this host cannot create junctions")

    wa._prune_updates(updates)

    assert keep.read_text(encoding="utf-8") == "novel", "user data must survive"
    assert not link.exists(), "the link itself is removed"


def test_prune_removes_a_link_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app never creates links, so any link in ``updates/`` is planted."""
    updates = tmp_path / "updates"
    updates.mkdir()
    outside = tmp_path / "outside"
    (outside / "keep.txt").parent.mkdir(parents=True)
    (outside / "keep.txt").write_text("data", encoding="utf-8")
    link = updates / "recent-dir"
    if not _make_junction(link, outside):
        pytest.skip("this host cannot create junctions")

    wa._prune_updates(updates)

    assert not link.exists(), "a planted link is removed regardless of age"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "data"


def test_updates_dir_refuses_a_linked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``app_home()/updates`` may not be a junction to somewhere else."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / wa._UPDATES_DIRNAME
    if not _make_junction(link, outside):
        pytest.skip("this host cannot create junctions")
    monkeypatch.setenv("NOVEL_HOME", str(home))

    with pytest.raises(OSError, match="not a real directory"):
        wa._updates_dir()


def test_updates_dir_refuses_a_plain_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file squatting on the ``updates`` name is an error, not a crash later."""
    home = tmp_path / "home"
    home.mkdir()
    (home / wa._UPDATES_DIRNAME).write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))

    with pytest.raises(OSError, match="not a real directory"):
        wa._updates_dir()


def test_install_refuses_an_installer_behind_a_link(
    prefs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link inside ``updates/`` is not a verified download, even if it works."""
    updates = _updates(tmp_path / "home")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real = outside / "setup.exe"
    real.write_bytes(_PAYLOAD)
    linked_dir = updates / "linked"
    if not _make_junction(linked_dir, outside):
        pytest.skip("this host cannot create junctions")
    linked = linked_dir / "setup.exe"
    # Patch ``Popen`` only after the junction exists: the helper shells out.
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(
        path=linked, sha256=_SHA, name=linked.name, size=len(_PAYLOAD)
    )

    result = bridge.install_update()

    assert result["ok"] is False
    assert "outside the update folder" in result["error"]
    assert calls == []
    assert real.exists()


def test_a_file_symlink_inside_updates_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file symlink is rejected even when its target hashes correctly."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    updates = _updates(tmp_path / "home")
    real = updates / "real.exe"
    real.write_bytes(_PAYLOAD)
    link = updates / "setup.exe"
    if not _make_file_symlink(link, real):
        pytest.skip("this host cannot create file symlinks")
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(
        path=link, sha256=_SHA, name=link.name, size=len(_PAYLOAD)
    )

    result = bridge.install_update()

    assert result["ok"] is False
    assert "outside the update folder" in result["error"]
    assert calls == []
    assert real.exists()


# ---------------------------------------------------------------------------
# installer missing / not executable
# ---------------------------------------------------------------------------


def test_missing_installer_is_reported_and_nothing_is_launched(
    prefs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    missing = _updates(tmp_path / "home") / "setup.exe"
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(
        path=missing, sha256=_SHA, name=missing.name, size=len(_PAYLOAD)
    )

    result = bridge.install_update()

    assert result["ok"] is False
    assert "missing" in result["error"].lower()
    assert calls == []


def test_an_unlaunchable_installer_keeps_the_download_retryable(
    prefs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Popen`` failing (bad image / no permission) must not consume the file."""
    _use_windows(monkeypatch)

    def _broken(_cmd: list[str], **_kwargs: Any) -> Any:
        raise OSError("WinError 193: not a valid Win32 application")

    monkeypatch.setattr("subprocess.Popen", _broken)
    path = _write_updates_file(tmp_path / "home", "setup.exe", _PAYLOAD)
    bridge = wa.ApiBridge()
    record = wa._DownloadedUpdate(
        path=path, sha256=_SHA, name=path.name, size=len(_PAYLOAD)
    )
    bridge._downloaded = record

    result = bridge.install_update()

    assert result["ok"] is False
    assert "193" in result["error"]
    assert bridge._downloaded is record, "the claim must be returned"
    assert path.exists(), "a failed launch must keep the package"


def test_install_on_exit_skips_an_installer_that_fails_reverification(
    prefs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shutdown path deletes a file that changed after the download."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    path = _write_updates_file(tmp_path / "home", "setup.exe", b"swapped")
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(
        path=path, sha256=_SHA, name=path.name, size=len(_PAYLOAD)
    )

    bridge._install_on_exit()

    assert calls == []
    assert not path.exists()
    assert bridge._downloaded is None


def test_install_on_exit_skips_a_size_mismatch(
    prefs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose bytes hash right but whose length disagrees is refused."""
    calls = _recording_popen(monkeypatch)
    _use_windows(monkeypatch)
    path = _write_updates_file(tmp_path / "home", "setup.exe", _PAYLOAD)
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(
        path=path,
        sha256=_SHA,
        name=path.name,
        # The digest is right; the published size is not.
        size=len(_PAYLOAD) + 10,
    )

    bridge._install_on_exit()

    assert calls == []
    assert not path.exists()


def test_installer_replaced_during_launch_is_stopped(
    prefs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swap between the hash and ``Popen`` is detected and the process killed."""
    _use_windows(monkeypatch)
    killed: list[int] = []
    path = _write_updates_file(tmp_path / "home", "setup.exe", _PAYLOAD)

    def _popen(cmd: list[str], **_kwargs: Any) -> Any:
        # Simulate the swap landing between verification and the launch.
        Path(cmd[0]).write_bytes(b"swapped payload")
        return SimpleNamespace(pid=777, kill=lambda: killed.append(777))

    monkeypatch.setattr("subprocess.Popen", _popen)
    bridge = wa.ApiBridge()
    bridge._downloaded = wa._DownloadedUpdate(
        path=path, sha256=_SHA, name=path.name, size=len(_PAYLOAD)
    )

    result = bridge.install_update()

    assert result["ok"] is False
    assert "changed while it was being started" in result["error"]
    assert killed == [777]
    assert not result.get("restarting")


# ---------------------------------------------------------------------------
# verify_file: the single-descriptor verifier
# ---------------------------------------------------------------------------


def test_verify_file_accepts_a_matching_digest_and_size(tmp_path: Path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(_PAYLOAD)

    assert updater.verify_file(path, _SHA) is True
    assert updater.verify_file(path, _SHA.upper()) is True
    assert updater.verify_file(path, _SHA, expected_size=len(_PAYLOAD)) is True


def test_verify_file_rejects_a_wrong_digest_or_size(tmp_path: Path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(_PAYLOAD)

    assert updater.verify_file(path, "0" * 64) is False
    assert updater.verify_file(path, _SHA, expected_size=len(_PAYLOAD) + 1) is False
    assert updater.verify_file(path, _SHA, expected_size=len(_PAYLOAD) - 1) is False
    assert updater.verify_file(path, "") is False


def test_verify_file_rejects_a_missing_path_or_a_directory(tmp_path: Path) -> None:
    assert updater.verify_file(tmp_path / "missing.bin", _SHA) is False
    directory = tmp_path / "dir"
    directory.mkdir()
    assert updater.verify_file(directory, _SHA) is False


def test_verify_file_rejects_a_named_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only regular files are executable payloads; a FIFO must be refused."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("this platform has no FIFOs")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert updater.verify_file(fifo, _SHA) is False


# ---------------------------------------------------------------------------
# portable helper: static safety assertions
# ---------------------------------------------------------------------------


def _portable_script() -> str:
    return wa._PORTABLE_UPDATE_SCRIPT


def _portable_script_code() -> str:
    """The helper with comment lines removed, so prose cannot trip a check."""
    lines = []
    for line in _portable_script().splitlines():
        if line.strip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_portable_script_never_targets_app_home_or_downloads() -> None:
    """The helper only ever writes below ``$AppDir`` and its own staging dir."""
    script = _portable_script_code()
    lowered = script.lower()
    # No environment-derived user-data directory is ever touched.
    for forbidden in ("appdata", "localappdata", "novel_home", "downloads"):
        assert forbidden not in lowered, f"the helper must not reference {forbidden}"
    # Every Remove-Item is scoped to a variable the script computed itself.
    allowed = ("$temp", "$payload", "$child", "$backup", "$Zip")
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Remove-Item"):
            continue
        assert any(var in stripped for var in allowed), f"unscoped delete: {stripped}"


def test_portable_script_requires_an_absolute_non_root_app_dir() -> None:
    script = _portable_script()
    assert "GetPathRoot" in script, "a drive root must be refused"
    assert "GetFullPath($AppDir)" in script


def test_portable_script_refuses_links_before_deleting() -> None:
    """Reparse points are unlinked, never recursed into."""
    script = _portable_script()
    assert "Test-ReparsePoint" in script
    assert "ReparsePoint" in script
    # The payload and its children are both guarded.
    assert "Test-ReparsePoint $payload" in script
    assert "Test-ReparsePoint $child.FullName" in script
    # And the program folder itself.
    assert "Test-ReparsePoint $AppDir" in script


def test_portable_script_restores_the_old_payload_on_a_failed_copy() -> None:
    """A failed copy puts ``_internal`` back instead of leaving it broken."""
    script = _portable_script()
    aside = script.index("Move-Item -LiteralPath $payload -Destination $backup")
    copy = script.index("Copy-Item -Path (Join-Path $source '*')")
    restore = script.index("Move-Item -LiteralPath $backup -Destination $payload")
    assert aside < copy < restore
    assert "previous installation was restored" in script


def test_portable_script_waits_for_the_process_and_fails_closed() -> None:
    """If the app does not exit, nothing is replaced."""
    script = _portable_script()
    wait = script.index("Get-Process -Id $ProcessId")
    aside = script.index("Move-Item -LiteralPath $payload -Destination $backup")
    assert wait < aside
    assert "did not exit" in script
    assert "$exited = $false" in script


def test_portable_script_cleans_only_its_own_staging_directory() -> None:
    """Cleanup is bounded to ``%TEMP%`` and the GUID directory it created."""
    script = _portable_script()
    assert "noveldl-update-" in script
    assert "[guid]::NewGuid()" in script
    assert "Test-PathWithin $temp $env:TEMP" in script


def test_portable_script_passes_user_data_inside_the_payload_as_keep() -> None:
    """``_portable_keep_paths`` feeds the ``-Keep`` guard, never a delete list."""
    script = _portable_script()
    assert "Test-Protected" in script
    # The protected subtree is skipped, not removed.
    protect_index = script.index("if (Test-Protected $child.FullName) { continue }")
    remove_index = script.index(
        "Remove-Item -LiteralPath $child.FullName -Recurse -Force"
    )
    assert protect_index < remove_index
