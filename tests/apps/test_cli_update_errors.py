#!/usr/bin/env python3
"""
tests.apps.test_cli_update_errors
---------------------------------

Failure paths and exit codes for ``novel-cli update`` and for the silent
"newer version" hint printed after a successful command.

The contracts locked here come from ``docs/guide/cli-examples*.md`` (section
13 plus the exit-code table) and from the module docstrings:

* a failed **check** exits ``1`` and names the reason (``unreachable``,
  ``untrusted-manifest: ...``, ``manifest-unavailable``) both on stderr and in
  the ``--json`` ``error`` field;
* an up-to-date install exits ``0`` with ``available: false``;
* a newer release whose CLI asset cannot be used (no ``novel-cli-*`` archive
  for this platform, no digest, a plaintext URL) is **never downloaded**;
* a failed **download** exits ``1``, leaves no promoted file behind and prints
  a message a user can act on;
* ``--check`` / ``--json`` / ``--yes`` / ``--no-mirror`` keep their documented
  precedence (``--check`` wins over ``--yes``; ``--json`` without ``--yes``
  never downloads);
* the hint stays silent for JSON output, a non-TTY stdout, a disabled config,
  a fresh cache, a concurrent check and a failed check, and it is bounded in
  time.

Nothing here touches the network: the release document is served by a fake
``urlopen`` (or the check/download is replaced by an in-memory fake) and the
application home is redirected to ``tmp_path``.  An autouse guard turns a
forgotten patch into a test failure instead of a real request.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader import __version__
from novel_downloader.apps.cli import main as cli_main_module
from novel_downloader.apps.cli import ui as cli_ui
from novel_downloader.apps.cli import update_notice
from novel_downloader.apps.cli.commands import update as update_module
from novel_downloader.apps.cli.commands.update import UpdateCmd
from novel_downloader.apps.common import updater

SHA = "a" * 64

#: Versions used by the manifest fixtures.
NEW_VERSION = "9.9.9"

# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fail loudly instead of reaching the network; isolate the app home."""

    def _no_check(*args: object, **kwargs: object) -> updater.UpdateCheck:
        raise AssertionError("a real update check was attempted")

    def _no_download(*args: object, **kwargs: object) -> Path:
        raise AssertionError("a real download was attempted")

    monkeypatch.setattr(update_module, "check_for_update", _no_check)
    monkeypatch.setattr(update_module, "download_asset", _no_download)
    monkeypatch.setattr(update_notice, "check_for_update", _no_check)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    UpdateCmd.register(subparsers)
    return parser


def _run(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    assert args.func is not None
    args.func(args)


class _Messages:
    """Records the messages passed to the ``ui`` helpers."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []
        self.emitted: list[str] = []

    def text(self) -> str:
        return "\n".join(
            self.info + self.success + self.warn + self.error + self.emitted
        )


def _capture_ui(monkeypatch: pytest.MonkeyPatch) -> _Messages:
    messages = _Messages()
    sinks = {
        "info": messages.info,
        "success": messages.success,
        "warn": messages.warn,
        "error": messages.error,
        "emit": messages.emitted,
    }
    for name, sink in sinks.items():
        monkeypatch.setattr(cli_ui, name, sink.append)
    return messages


def _json_doc(messages: _Messages) -> dict[str, Any]:
    assert len(messages.emitted) == 1
    parsed: dict[str, Any] = json.loads(messages.emitted[0])
    return parsed


def _asset(
    name: str = "novel-cli-9.9.9-windows-x64.zip",
    *,
    url: str | None = None,
    size: int | None = 2048,
    sha256: str | None = SHA,
) -> updater.ReleaseAsset:
    return updater.ReleaseAsset(
        name=name,
        url=url if url is not None else f"https://example.invalid/{name}",
        size=size,
        sha256=sha256,
    )


_DEFAULT_ASSET = object()


def _info(
    *,
    available: bool = True,
    latest: str = NEW_VERSION,
    asset: updater.ReleaseAsset | None | object = _DEFAULT_ASSET,
    assets: tuple[updater.ReleaseAsset, ...] | None = None,
) -> updater.UpdateInfo:
    chosen = _asset() if asset is _DEFAULT_ASSET else asset
    assert chosen is None or isinstance(chosen, updater.ReleaseAsset)
    if assets is None:
        assets = () if chosen is None else (chosen,)
    return updater.UpdateInfo(
        current=__version__,
        latest=latest,
        available=available,
        notes_url="https://example.invalid/releases/latest",
        asset=chosen,
        assets=assets,
    )


def _patch_check(
    monkeypatch: pytest.MonkeyPatch,
    info: updater.UpdateInfo | None,
    *,
    error: str | None = None,
) -> None:
    def _fake(*args: object, **kwargs: object) -> updater.UpdateCheck:
        return updater.UpdateCheck(info=info, error=error, checked_at=0.0)

    monkeypatch.setattr(update_module, "check_for_update", _fake)


def _real_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the command call the real ``updater.check_for_update``."""
    monkeypatch.setattr(update_module, "check_for_update", updater.check_for_update)


def _real_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the command call the real ``updater.download_asset``."""
    monkeypatch.setattr(update_module, "download_asset", updater.download_asset)
    monkeypatch.delenv(update_module.MIRROR_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# fake transports (offline)
# ---------------------------------------------------------------------------


class _Response(io.BytesIO):
    """Minimal ``urlopen`` response: ``read`` plus the headers inspected."""

    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(payload)
        self.status = status
        self.headers = dict(headers or {})


class _BrokenResponse(_Response):
    """Serves one chunk, then fails the way a dropped connection does."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload, headers={"Content-Length": str(len(payload))})
        self._reads = 0

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads > 1:
            raise ConnectionResetError("connection reset by peer")
        return super().read(size)


def _serve(payload: bytes) -> Any:
    def _open(*_args: object, **_kwargs: object) -> _Response:
        return _Response(payload)

    return _open


#: One factory per transport failure the release lookup must survive.
_TRANSPORT_FAILURES: dict[str, Any] = {
    "dns": lambda url: urllib.error.URLError(OSError("getaddrinfo failed")),
    "timeout": lambda url: TimeoutError("timed out"),
    "http-404": lambda url: urllib.error.HTTPError(url, 404, "Not Found", None, None),
    "connection-reset": lambda url: ConnectionResetError("connection reset"),
}


def _failing_transport(kind: str, seen: list[float] | None = None) -> Any:
    """A ``urlopen`` that records timeouts and raises *kind*."""

    def _open(request: object, timeout: float = 0) -> Any:
        if seen is not None:
            seen.append(timeout)
        url = str(getattr(request, "full_url", ""))
        raise _TRANSPORT_FAILURES[kind](url)

    return _open


def _manifest(
    version: str = NEW_VERSION,
    *,
    platform_tag: str | None = None,
    name: str | None = None,
    url: str | None = None,
    sha256: str = SHA,
) -> dict[str, Any]:
    """A manifest the real parser accepts (one CLI entry for this platform)."""
    tag_platform = platform_tag or updater.detect_platform()
    asset_name = name or f"novel-cli-{version}-{tag_platform}.zip"
    tag = f"v{version}"
    return {
        "schema": updater.MANIFEST_SCHEMA,
        "version": version,
        "tag": tag,
        "repo": updater.DEFAULT_REPO,
        "platforms": {
            f"{tag_platform}-cli": {
                "name": asset_name,
                "url": url
                or (
                    f"https://github.com/{updater.DEFAULT_REPO}/releases/download/"
                    f"{tag}/{asset_name}"
                ),
                "sha256": sha256,
                "size": 2048,
            }
        },
    }


def _serve_json(payload: dict[str, Any]) -> Any:
    return _serve(json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# check failures (real updater, fake transport)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(_TRANSPORT_FAILURES))
def test_transport_failure_exits_one_with_a_readable_reason(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """DNS/TCP/timeout/404 all collapse to ``unreachable`` and exit 1."""
    _real_check(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", _failing_transport(kind))
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--check"])

    assert excinfo.value.code == 1
    text = messages.text()
    assert "Update check failed" in text
    assert "unreachable" in text
    assert messages.emitted == []


@pytest.mark.parametrize("kind", list(_TRANSPORT_FAILURES))
def test_transport_failure_json_reports_the_error_field(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _real_check(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", _failing_transport(kind))
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--json"])

    assert excinfo.value.code == 1
    doc = _json_doc(messages)
    assert doc["error"] == "unreachable"
    assert doc["available"] is False
    assert doc["latest"] == ""
    assert doc["asset_shape"] is None
    assert doc["download_path"] is None


def test_check_failure_prints_no_traceback_through_cli_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _real_check(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", _failing_transport("dns"))
    monkeypatch.setattr(sys, "argv", ["novel-cli", "update", "--check"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Update check failed" in captured.err
    assert captured.out == ""


def test_check_timeout_is_bounded_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is one short request per endpoint, never an open-ended wait."""
    seen: list[float] = []
    _real_check(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", _failing_transport("timeout", seen))
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--check"])

    assert excinfo.value.code == 1
    assert messages.error
    assert update_module.CHECK_TIMEOUT <= 10.0
    assert seen == [update_module.CHECK_TIMEOUT, update_module.CHECK_TIMEOUT]


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"key_id": "unknown-key", "signature": "AAAA"},
    ],
    ids=["unsigned", "unknown-key"],
)
def test_untrusted_manifest_is_reported_without_api_fallback(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, str]
) -> None:
    """Once a key is trusted, a bad manifest fails instead of falling back."""
    payload = {**_manifest(), **extra}
    calls: list[int] = []

    def _open(*_args: object, **_kwargs: object) -> _Response:
        calls.append(1)
        return _Response(json.dumps(payload).encode("utf-8"))

    _real_check(monkeypatch)
    monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {"trusted": "00" * 32})
    monkeypatch.setattr("urllib.request.urlopen", _open)
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--json"])

    assert excinfo.value.code == 1
    assert len(calls) == 1, "the API must not be consulted when a key is trusted"
    doc = _json_doc(messages)
    assert isinstance(doc["error"], str)
    assert doc["error"].startswith("untrusted-manifest: ")
    assert doc["available"] is False


def test_untrusted_manifest_human_message_names_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _real_check(monkeypatch)
    monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {"trusted": "00" * 32})
    monkeypatch.setattr("urllib.request.urlopen", _serve_json(_manifest()))
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--check"])

    assert excinfo.value.code == 1
    assert "untrusted-manifest" in messages.text()


def test_missing_manifest_with_a_trusted_key_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def _open(*_args: object, **_kwargs: object) -> Any:
        calls.append(1)
        raise urllib.error.HTTPError(
            "https://x.invalid/latest.json", 404, "", None, None
        )

    _real_check(monkeypatch)
    monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {"trusted": "00" * 32})
    monkeypatch.setattr("urllib.request.urlopen", _open)
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--json"])

    assert excinfo.value.code == 1
    assert len(calls) == 1
    assert _json_doc(messages)["error"] == "manifest-unavailable"


# ---------------------------------------------------------------------------
# up to date
# ---------------------------------------------------------------------------


def test_up_to_date_exits_zero_and_reports_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real manifest path: same version -> ``available: false``, exit 0."""
    _real_check(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen", _serve_json(_manifest(version=__version__))
    )
    messages = _capture_ui(monkeypatch)

    _run(["update", "--json"])

    doc = _json_doc(messages)
    assert doc["available"] is False
    assert doc["latest"] == __version__
    assert doc["download_path"] is None
    assert doc["error"] is None
    assert messages.error == []


def test_up_to_date_with_yes_never_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info(available=False, latest=__version__))
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        update_module,
        "download_asset",
        lambda *a, **k: pytest.fail("nothing to download"),
    )

    _run(["update", "--yes"])

    assert "Already up to date" in messages.text()
    assert messages.emitted == []
    assert not update_module.updates_dir().exists()


# ---------------------------------------------------------------------------
# a newer release whose CLI asset cannot be used
# ---------------------------------------------------------------------------


def test_check_without_a_cli_asset_succeeds_without_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--check`` reports the missing archive but the check itself worked."""
    info = _info(asset=None, assets=())
    _patch_check(monkeypatch, info)
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        update_module, "download_asset", lambda *a, **k: pytest.fail("no download")
    )

    _run(["update", "--check"])

    assert "No CLI release asset matches" in messages.text()
    assert not update_module.updates_dir().exists()


def test_linux_cli_archive_is_not_used_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI archive for another platform is not "a CLI asset" here."""
    foreign = _asset(name=f"novel-cli-{NEW_VERSION}-linux-x64.zip")
    _patch_check(monkeypatch, _info(asset=None, assets=(foreign,)))
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        update_module, "download_asset", lambda *a, **k: pytest.fail("no download")
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--json", "--yes"])

    assert excinfo.value.code == 1
    doc = _json_doc(messages)
    assert doc["error"] == update_module.NO_CLI_ASSET_ERROR
    assert doc["asset_name"] is None
    assert doc["download_path"] is None


def test_missing_digest_is_not_an_error_until_a_download_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` alone behaves like ``--check``: nothing is verified yet."""
    _patch_check(monkeypatch, _info(asset=_asset(sha256=None)))
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        update_module, "download_asset", lambda *a, **k: pytest.fail("no download")
    )

    _run(["update", "--json"])

    doc = _json_doc(messages)
    assert doc["available"] is True
    assert doc["sha256"] is None
    assert doc["error"] is None
    assert doc["download_path"] is None


def test_insecure_url_is_not_validated_by_a_check_only_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL policy applies to downloads; a check is still a successful check."""
    asset = _asset(url=f"http://example.invalid/novel-cli-{NEW_VERSION}.zip")
    _patch_check(monkeypatch, _info(asset=asset))
    messages = _capture_ui(monkeypatch)

    _run(["update", "--check", "--json"])

    doc = _json_doc(messages)
    assert doc["available"] is True
    assert doc["error"] is None
    assert messages.error == []


@pytest.mark.parametrize("flags", [["--json", "--yes"], ["--yes"]])
def test_unusable_asset_is_refused_before_any_transfer(
    monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    """No digest / insecure URL / missing archive: exit 1, nothing fetched."""
    asset = _asset(url="http://example.invalid/novel-cli-9.9.9.zip")
    _patch_check(monkeypatch, _info(asset=asset))
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        update_module, "download_asset", lambda *a, **k: pytest.fail("no download")
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", *flags])

    assert excinfo.value.code == 1
    if "--json" in flags:
        assert _json_doc(messages)["error"] == update_module.INSECURE_URL_ERROR
        assert messages.error == []
    else:
        assert messages.error
        assert "insecure" in messages.text().lower()
    assert not update_module.updates_dir().exists()


# ---------------------------------------------------------------------------
# download failures (real helper, fake transport)
# ---------------------------------------------------------------------------


def _updates_contents() -> list[str]:
    updates = update_module.updates_dir()
    return sorted(path.name for path in updates.iterdir()) if updates.exists() else []


def test_interrupted_transfer_leaves_no_half_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped connection must not promote or keep a partial download."""
    asset = _asset()
    _patch_check(monkeypatch, _info(asset=asset))
    _real_download(monkeypatch)
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: _BrokenResponse(b"partial bytes")
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--yes"])

    assert excinfo.value.code == 1
    assert "Download failed" in messages.text()
    assert "connection reset" in messages.text()
    assert _updates_contents() == []


def test_download_http_404_is_reported_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    _patch_check(monkeypatch, _info(asset=asset))
    _real_download(monkeypatch)
    messages = _capture_ui(monkeypatch)

    def _open(request: object, timeout: float = 0) -> Any:
        url = str(getattr(request, "full_url", ""))
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr("urllib.request.urlopen", _open)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--yes"])

    assert excinfo.value.code == 1
    assert "404" in messages.text()
    assert _updates_contents() == []


def test_disk_full_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    _patch_check(monkeypatch, _info(asset=asset))
    _real_download(monkeypatch)
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: _Response(b"0123456789", headers={"Content-Length": "10"}),
    )
    monkeypatch.setattr(updater, "_free_disk_bytes", lambda _path: 0)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--yes"])

    assert excinfo.value.code == 1
    assert "disk space" in messages.text()
    assert _updates_contents() == []


def test_unwritable_target_reports_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only ``updates/`` yields a readable error, not a traceback."""
    asset = _asset()
    _patch_check(monkeypatch, _info(asset=asset))
    _real_download(monkeypatch)
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: _Response(b"payload", headers={"Content-Length": "7"}),
    )

    real_open = Path.open

    def _deny(self: Path, *args: Any, **kwargs: Any) -> Any:
        if str(self).endswith(".part"):
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _deny)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--yes"])

    assert excinfo.value.code == 1
    assert "Download failed" in messages.text()
    assert "Permission denied" in messages.text()
    assert _updates_contents() == []


def test_json_download_failure_reports_error_and_null_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    _patch_check(monkeypatch, _info(asset=asset))
    _real_download(monkeypatch)
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: _BrokenResponse(b"partial")
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--json", "--yes"])

    assert excinfo.value.code == 1
    doc = _json_doc(messages)
    assert doc["download_path"] is None
    assert isinstance(doc["error"], str) and doc["error"]
    assert "download failed" in doc["error"].lower()
    assert doc["asset_name"] == asset.name
    assert _updates_contents() == []


def test_download_failure_prints_no_traceback_through_cli_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_check(monkeypatch, _info())
    _real_download(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: _BrokenResponse(b"partial")
    )
    monkeypatch.setattr(sys, "argv", ["novel-cli", "update", "--yes"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Download failed" in captured.err


def test_ctrl_c_during_the_transfer_aborts_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl+C mid-download is an abort (exit 1, ``Aborted.``), not a traceback."""
    _patch_check(monkeypatch, _info())
    messages = _capture_ui(monkeypatch)

    def _interrupted(*_args: object, **_kwargs: object) -> Path:
        raise KeyboardInterrupt

    monkeypatch.setattr(update_module, "download_asset", _interrupted)
    monkeypatch.setattr(sys, "argv", ["novel-cli", "update", "--yes"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Aborted." in captured.err
    assert "Traceback" not in captured.err
    assert "Downloaded and verified" not in messages.text()


# ---------------------------------------------------------------------------
# flag semantics
# ---------------------------------------------------------------------------


def test_check_wins_over_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        update_module, "download_asset", lambda *a, **k: pytest.fail("no download")
    )

    _run(["update", "--check", "--yes"])

    assert messages.error == []
    assert not update_module.updates_dir().exists()


def test_json_without_yes_never_prompts_or_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        cli_ui, "confirm", lambda *a, **k: pytest.fail("JSON output must not prompt")
    )
    monkeypatch.setattr(
        update_module, "download_asset", lambda *a, **k: pytest.fail("no download")
    )

    _run(["update", "--json"])

    assert _json_doc(messages)["download_path"] is None


def test_yes_passes_the_download_timeout_and_skips_the_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    _capture_ui(monkeypatch)
    seen: dict[str, Any] = {}

    def _fake(
        url: str, dest: Path, sha: str | None, mirror: Any, **kwargs: Any
    ) -> Path:
        seen.update(url=url, sha=sha, mirror=mirror, **kwargs)
        return Path("downloaded")

    monkeypatch.setattr(update_module, "download_asset", _fake)
    monkeypatch.setenv(update_module.MIRROR_ENV_VAR, "https://mirror.invalid/")

    _run(["update", "--yes", "--no-mirror"])

    assert seen["mirror"] is None
    assert seen["timeout"] == update_module.DOWNLOAD_TIMEOUT
    assert seen["sha"] == SHA
    assert seen["root"] == update_module.updates_dir()


@pytest.mark.parametrize("flag", ["--download", "--install"])
def test_flags_the_command_does_not_implement_are_usage_errors(flag: str) -> None:
    """There is no ``--download``/``--install``: the download is ``--yes``."""
    with pytest.raises(SystemExit) as excinfo:
        _parser().parse_args(["update", flag])

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# the silent hint after a successful command
# ---------------------------------------------------------------------------


class _FakeUi:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message: str) -> None:
        self.lines.append(message)


def _tty() -> SimpleNamespace:
    return SimpleNamespace(isatty=lambda: True)


def _not_tty() -> SimpleNamespace:
    return SimpleNamespace(isatty=lambda: False)


class _BrokenStream:
    def isatty(self) -> bool:
        raise OSError("stream is closed")


def _notice_check(
    monkeypatch: pytest.MonkeyPatch,
    info: updater.UpdateInfo | None,
    *,
    counter: list[int] | None = None,
    error: str | None = None,
) -> None:
    def _fake(*args: object, **kwargs: object) -> updater.UpdateCheck:
        if counter is not None:
            counter.append(1)
        return updater.UpdateCheck(info=info, error=error, checked_at=0.0)

    monkeypatch.setattr(update_notice, "check_for_update", _fake)


def _auto_config() -> dict[str, Any]:
    return {"general": {"update_check": "auto"}}


def test_notice_stays_silent_when_the_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, None, counter=calls, error="unreachable")
    fake_ui = _FakeUi()

    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=fake_ui, now=1_000_000.0, stream=_tty()
    )

    assert calls == [1]
    assert fake_ui.lines == []
    # A failed check is still throttled, so an offline machine does not pay the
    # wait on every command.
    assert update_notice.state_path().is_file()
    assert not update_notice.claim_path().exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"as_json": True},
        {"stream": _not_tty()},
        {"stream": _BrokenStream()},
    ],
    ids=["json", "not-a-tty", "broken-stream"],
)
def test_notice_is_silent_for_machine_output_and_non_terminals(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)

    update_notice.maybe_notify(
        __version__,
        config=_auto_config(),
        ui=_FakeUi(),
        now=1_000_000.0,
        **kwargs,
    )

    assert calls == []
    assert not update_notice.state_path().exists()


@pytest.mark.parametrize("mode", ["off", "manual"])
def test_notice_is_silent_when_the_config_disables_it(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)

    update_notice.maybe_notify(
        __version__,
        config={"general": {"update_check": mode}},
        ui=_FakeUi(),
        now=1_000_000.0,
        stream=_tty(),
    )

    assert calls == []


def test_notice_is_throttled_by_a_fresh_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)
    fake_ui = _FakeUi()

    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=fake_ui, now=1_000_000.0, stream=_tty()
    )
    update_notice.maybe_notify(
        __version__,
        config=_auto_config(),
        ui=fake_ui,
        now=1_000_000.0 + 60,
        stream=_tty(),
    )

    assert calls == [1]
    assert len(fake_ui.lines) == 1


def test_notice_wait_is_bounded_even_when_the_worker_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    release = threading.Event()

    def _stuck(*args: object, **kwargs: object) -> updater.UpdateCheck:
        release.wait(30.0)
        return updater.UpdateCheck(info=_info(), error=None, checked_at=0.0)

    monkeypatch.setattr(update_notice, "check_for_update", _stuck)
    fake_ui = _FakeUi()

    started = time.perf_counter()
    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=fake_ui, now=1_000_000.0, stream=_tty()
    )
    elapsed = time.perf_counter() - started
    release.set()

    assert elapsed < update_notice.NOTICE_TIMEOUT + 0.5
    assert fake_ui.lines == []
    assert update_notice.state_path().is_file()


def test_notice_is_skipped_for_the_update_command_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint would only repeat what ``update`` already printed."""
    _patch_check(monkeypatch, _info())
    _capture_ui(monkeypatch)
    seen: list[object] = []
    monkeypatch.setattr(
        update_notice, "maybe_notify", lambda *a, **k: seen.append(object())
    )
    monkeypatch.setattr(sys, "argv", ["novel-cli", "update", "--check"])

    cli_main_module.cli_main()

    assert seen == []
