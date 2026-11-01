#!/usr/bin/env python3
"""
tests.apps.test_cli_update
--------------------------

Offline tests for ``novel-cli update`` and the background update hint.

Nothing here touches the network: every test replaces the release lookup (and,
where a download happens, ``download_asset``) with an in-memory fake, and the
application home is redirected to ``tmp_path`` so no cache file is written to
the developer's real ``app_home()``.  An autouse fixture additionally turns an
unpatched call into an assertion failure, so a forgotten patch cannot silently
reach GitHub.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader import __version__
from novel_downloader.apps.cli import main as cli_main_module
from novel_downloader.apps.cli import ui as cli_ui
from novel_downloader.apps.cli import update_notice
from novel_downloader.apps.cli.commands import update as update_module
from novel_downloader.apps.cli.commands.base import Command
from novel_downloader.apps.cli.commands.update import UpdateCmd
from novel_downloader.apps.common import updater
from novel_downloader.infra.config import validate_config

SHA = "a" * 64

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


def _asset(
    name: str = "NovelDownloader-setup-1.5.0-x64.exe",
    *,
    size: int | None = 2048,
    sha256: str | None = SHA,
) -> updater.ReleaseAsset:
    return updater.ReleaseAsset(
        name=name,
        url=f"https://example.invalid/{name}",
        size=size,
        sha256=sha256,
    )


def _info(
    *,
    available: bool = True,
    latest: str = "1.5.0",
    asset: updater.ReleaseAsset | None = None,
) -> updater.UpdateInfo:
    chosen = _asset() if asset is None else asset
    return updater.UpdateInfo(
        current=__version__,
        latest=latest,
        available=available,
        notes_url="https://example.invalid/releases/latest",
        asset=chosen,
        assets=(chosen,),
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


# ---------------------------------------------------------------------------
# novel-cli update --check
# ---------------------------------------------------------------------------


def test_check_with_update_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    messages = _capture_ui(monkeypatch)

    _run(["update", "--check"])

    text = messages.text()
    assert __version__ in text
    assert "1.5.0" in text
    assert "NovelDownloader-setup-1.5.0-x64.exe" in text
    assert "https://example.invalid/releases/latest" in text
    assert "2.0 KiB" in text


def test_check_without_update_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info(available=False, latest=__version__))
    messages = _capture_ui(monkeypatch)

    _run(["update", "--check"])

    assert "Already up to date" in messages.text()


def test_check_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, None, error="unreachable")
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--check"])

    assert excinfo.value.code == 1
    assert messages.error
    assert "unreachable" in messages.text()


def test_check_reports_missing_platform_asset_but_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = updater.UpdateInfo(
        current=__version__,
        latest="1.5.0",
        available=True,
        notes_url=None,
        asset=None,
        assets=(),
    )
    _patch_check(monkeypatch, info)
    messages = _capture_ui(monkeypatch)

    _run(["update", "--check"])

    assert messages.warn
    assert "No release asset matches" in messages.text()


# ---------------------------------------------------------------------------
# novel-cli update --json
# ---------------------------------------------------------------------------


def _json_doc(messages: _Messages) -> dict[str, Any]:
    assert len(messages.emitted) == 1
    parsed: dict[str, Any] = json.loads(messages.emitted[0])
    return parsed


def test_json_keys_are_complete_with_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    messages = _capture_ui(monkeypatch)

    _run(["update", "--json"])

    doc = _json_doc(messages)
    assert set(doc) == {
        "current",
        "latest",
        "available",
        "asset_name",
        "asset_size",
        "sha256",
        "download_path",
        "error",
    }
    assert doc["current"] == __version__
    assert doc["latest"] == "1.5.0"
    assert doc["available"] is True
    assert doc["asset_name"] == "NovelDownloader-setup-1.5.0-x64.exe"
    assert doc["asset_size"] == 2048
    assert doc["sha256"] == SHA
    assert doc["download_path"] is None
    assert doc["error"] is None
    # ``--json`` resumes must not prompt.
    assert messages.info == []


def test_json_reports_check_failure_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, None, error="unreachable")
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--json"])

    assert excinfo.value.code == 1
    doc = _json_doc(messages)
    assert doc["available"] is False
    assert doc["error"] == "unreachable"
    assert doc["download_path"] is None


def test_json_without_update_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info(available=False, latest=__version__))
    messages = _capture_ui(monkeypatch)

    _run(["update", "--json"])

    doc = _json_doc(messages)
    assert doc["available"] is False
    assert doc["latest"] == __version__
    assert doc["download_path"] is None


# ---------------------------------------------------------------------------
# novel-cli update --yes
# ---------------------------------------------------------------------------


def test_yes_downloads_and_prints_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    _patch_check(monkeypatch, _info(asset=asset))
    messages = _capture_ui(monkeypatch)
    calls: dict[str, Any] = {}

    def _fake_download(
        url: str,
        dest: Path,
        expected_sha256: str | None = None,
        mirror: Any = None,
        *,
        root: Path | None = None,
        timeout: float = 30.0,
    ) -> Path:
        calls.update(
            url=url,
            dest=dest,
            expected_sha256=expected_sha256,
            mirror=mirror,
            root=root,
            timeout=timeout,
        )
        return Path(root or Path.cwd()) / asset.name

    monkeypatch.setattr(update_module, "download_asset", _fake_download)

    _run(["update", "--yes"])

    assert messages.success
    downloaded = messages.success[0]
    assert "Downloaded and verified" in downloaded
    assert asset.name in downloaded
    # The file lands in app_home()/updates, never in the working directory.
    assert calls["root"] == update_module.updates_dir()
    assert calls["root"].name == "updates"
    assert calls["url"] == asset.url
    assert calls["expected_sha256"] == SHA
    # The digest is the trust anchor: nothing is executed afterwards.
    assert "Nothing was run or installed" in messages.text()


def test_yes_with_mirror_env_passes_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    _capture_ui(monkeypatch)
    seen: dict[str, Any] = {}

    def _fake_download(*args: object, **kwargs: Any) -> Path:
        seen["mirror"] = kwargs.get("mirror", args[3] if len(args) > 3 else None)
        return Path("x")

    monkeypatch.setattr(update_module, "download_asset", _fake_download)
    monkeypatch.setenv(update_module.MIRROR_ENV_VAR, "https://m.invalid/, ")

    _run(["update", "--yes"])
    assert seen["mirror"] == ["https://m.invalid/"]

    _run(["update", "--yes", "--no-mirror"])
    assert seen["mirror"] is None


def test_yes_missing_digest_refuses_to_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info(asset=_asset(sha256=None)))
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--yes"])

    assert excinfo.value.code == 1
    assert messages.error
    assert "no SHA-256 digest" in messages.text()
    assert "https://example.invalid/releases/latest" in messages.text()


def test_yes_missing_asset_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = updater.UpdateInfo(
        current=__version__,
        latest="1.5.0",
        available=True,
        notes_url=None,
        asset=None,
        assets=(),
    )
    _patch_check(monkeypatch, info)
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--yes"])

    assert excinfo.value.code == 1
    assert messages.error


def test_download_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    messages = _capture_ui(monkeypatch)

    def _failing_download(*args: object, **kwargs: object) -> Path:
        raise OSError("connection reset")

    monkeypatch.setattr(update_module, "download_asset", _failing_download)

    with pytest.raises(SystemExit) as excinfo:
        _run(["update", "--yes"])

    assert excinfo.value.code == 1
    assert "connection reset" in messages.text()


def test_json_yes_reports_download_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    _patch_check(monkeypatch, _info(asset=asset))
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(
        update_module,
        "download_asset",
        lambda url, dest, sha=None, mirror=None, *, root=None, timeout=0: (
            Path(root or Path.cwd()) / asset.name
        ),
    )

    _run(["update", "--json", "--yes"])

    doc = _json_doc(messages)
    assert doc["download_path"] == str(update_module.updates_dir() / asset.name)
    assert doc["error"] is None


def test_interactive_decline_exits_zero_and_does_not_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(cli_ui, "confirm", lambda *a, **k: False)

    _run(["update"])

    assert "Cancelled" in messages.text()


def test_interactive_confirm_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_check(monkeypatch, _info())
    messages = _capture_ui(monkeypatch)
    monkeypatch.setattr(cli_ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(
        update_module, "download_asset", lambda *a, **k: Path("/tmp/asset")
    )

    _run(["update"])

    assert "Downloaded and verified" in messages.text()


# ---------------------------------------------------------------------------
# background notice
# ---------------------------------------------------------------------------


class _FakeUi:
    """Minimal ``ui`` stand-in that records ``info`` lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message: str) -> None:
        self.lines.append(message)


def _tty() -> SimpleNamespace:
    return SimpleNamespace(isatty=lambda: True)


def _not_tty() -> SimpleNamespace:
    return SimpleNamespace(isatty=lambda: False)


def _notice_check(
    monkeypatch: pytest.MonkeyPatch,
    info: updater.UpdateInfo | None,
    *,
    counter: list[int] | None = None,
) -> None:
    def _fake(*args: object, **kwargs: object) -> updater.UpdateCheck:
        if counter is not None:
            counter.append(1)
        return updater.UpdateCheck(info=info, error=None, checked_at=0.0)

    monkeypatch.setattr(update_notice, "check_for_update", _fake)


def _auto_config() -> dict[str, Any]:
    return {"general": {"update_check": "auto"}}


def test_notice_prints_on_tty_and_writes_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)
    fake_ui = _FakeUi()

    update_notice.maybe_notify(
        __version__,
        config=_auto_config(),
        ui=fake_ui,
        now=1_000_000.0,
        stream=_tty(),
    )

    assert calls == [1]
    assert len(fake_ui.lines) == 1
    assert "1.5.0" in fake_ui.lines[0]
    assert __version__ in fake_ui.lines[0]
    assert "novel-cli update" in fake_ui.lines[0]
    assert update_notice.state_path().is_file()


def test_notice_is_throttled(
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


def test_notice_runs_again_after_interval(
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
        now=1_000_000.0 + updater.DEFAULT_CHECK_INTERVAL + 1,
        stream=_tty(),
    )

    assert calls == [1, 1]


def test_notice_skipped_without_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(
        monkeypatch, _info(available=False, latest=__version__), counter=calls
    )
    fake_ui = _FakeUi()

    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=fake_ui, now=1_000_000.0, stream=_tty()
    )

    assert calls == [1]
    assert fake_ui.lines == []
    # The attempt is still recorded, so an up-to-date user is not re-checked.
    assert update_notice.state_path().is_file()


def test_notice_skipped_when_stdout_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)

    update_notice.maybe_notify(
        __version__,
        config=_auto_config(),
        ui=_FakeUi(),
        now=1_000_000.0,
        stream=_not_tty(),
    )

    assert calls == []
    assert not update_notice.state_path().exists()


def test_notice_skipped_for_json_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)

    update_notice.maybe_notify(
        __version__,
        config=_auto_config(),
        ui=_FakeUi(),
        now=1_000_000.0,
        as_json=True,
        stream=_tty(),
    )

    assert calls == []


@pytest.mark.parametrize("mode", ["off", "manual"])
def test_notice_skipped_when_disabled(
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


def test_notice_defaults_to_auto_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)

    assert update_notice.update_check_mode({}) == "auto"
    assert update_notice.update_check_mode(None) == "auto"
    assert update_notice.update_check_mode({"general": {"update_check": "bogus"}}) == (
        "auto"
    )

    update_notice.maybe_notify(
        __version__, config={}, ui=_FakeUi(), now=1_000_000.0, stream=_tty()
    )

    assert calls == [1]


def test_notice_swallows_check_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*args: object, **kwargs: object) -> updater.UpdateCheck:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(update_notice, "check_for_update", _explode)
    fake_ui = _FakeUi()

    # Must not raise. The landing record is written *before* the request, so a
    # worker that dies still throttles the next command instead of making it
    # pay the wait again.
    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=fake_ui, now=1_000_000.0, stream=_tty()
    )

    assert fake_ui.lines == []
    state = json.loads(update_notice.state_path().read_text(encoding="utf-8"))
    assert state["checked_at"] == 1_000_000.0
    # The claim file must not leak when the worker dies or returns.
    assert not update_notice.claim_path().exists()

    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)
    update_notice.maybe_notify(
        __version__,
        config=_auto_config(),
        ui=fake_ui,
        now=1_000_000.0 + 60,
        stream=_tty(),
    )
    assert calls == [], "a failed check must still throttle the next command"


def test_notice_uses_sys_stdout_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)
    monkeypatch.setattr(sys, "stdout", _tty())

    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=_FakeUi(), now=1_000_000.0
    )

    assert calls == [1]


# ---------------------------------------------------------------------------
# background notice: latency budget and concurrency
# ---------------------------------------------------------------------------


def test_notice_wait_budget_stays_under_one_second() -> None:
    """The hint is a courtesy: it must never be felt as command latency."""
    assert 0 < update_notice.NOTICE_TIMEOUT <= 1.0
    # The socket timeout may be larger: the worker outlives the caller.
    assert update_notice.REQUEST_TIMEOUT >= update_notice.NOTICE_TIMEOUT


def test_notice_returns_when_the_worker_is_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung check (no DNS answer) must not hold the command open."""
    release = threading.Event()

    def _stuck(*args: object, **kwargs: object) -> updater.UpdateCheck:
        release.wait(30.0)
        return updater.UpdateCheck(info=None, error="unreachable", checked_at=0.0)

    monkeypatch.setattr(update_notice, "check_for_update", _stuck)

    started = time.perf_counter()
    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=_FakeUi(), now=1_000_000.0, stream=_tty()
    )
    elapsed = time.perf_counter() - started
    release.set()

    assert elapsed < update_notice.NOTICE_TIMEOUT + 0.5
    # The landing record exists even though the worker never finished.
    assert update_notice.state_path().is_file()


def test_notice_records_the_attempt_before_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abandoned worker must still throttle the next command."""
    observed: list[bool] = []

    def _peek(*args: object, **kwargs: object) -> updater.UpdateCheck:
        # By the time the request is made, the stamp is already on disk.
        observed.append(update_notice.state_path().is_file())
        return updater.UpdateCheck(info=None, error="unreachable", checked_at=0.0)

    monkeypatch.setattr(update_notice, "check_for_update", _peek)

    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=_FakeUi(), now=1_000_000.0, stream=_tty()
    )

    assert observed == [True]
    assert updater.check_due(update_notice.state_path(), now=1_000_000.0 + 60) is False


def test_notice_concurrent_callers_make_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five commands started together must not fire five requests."""
    calls: list[int] = []
    entered = threading.Event()
    release = threading.Event()

    def _slow(*args: object, **kwargs: object) -> updater.UpdateCheck:
        calls.append(1)
        entered.set()
        release.wait(10.0)
        return updater.UpdateCheck(info=None, error="unreachable", checked_at=0.0)

    monkeypatch.setattr(update_notice, "check_for_update", _slow)

    barrier = threading.Barrier(5)
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            barrier.wait(5.0)
            update_notice.maybe_notify(
                __version__,
                config=_auto_config(),
                ui=_FakeUi(),
                now=1_000_000.0,
                stream=_tty(),
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    assert entered.wait(5.0), "the winning caller never reached the network step"
    # Let the four losers run into the claim file before the winner finishes.
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(5.0)

    assert errors == []
    assert calls == [1]
    assert not update_notice.claim_path().exists()


def test_notice_claim_is_removed_after_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _notice_check(monkeypatch, _info())

    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=_FakeUi(), now=1_000_000.0, stream=_tty()
    )

    assert not update_notice.claim_path().exists()


def test_notice_skips_while_another_process_holds_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)
    claim = update_notice.claim_path()
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text("1000000.0", encoding="utf-8")

    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=_FakeUi(), now=1_000_000.0, stream=_tty()
    )

    assert calls == []
    # The fresh claim belongs to a live process: it is left alone.
    assert claim.exists()


def test_notice_takes_over_a_stale_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    _notice_check(monkeypatch, _info(), counter=calls)
    claim = update_notice.claim_path()
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text("0", encoding="utf-8")
    stale = 1_000_000.0 - update_notice.CLAIM_STALE_AFTER - 1
    os.utime(claim, (stale, stale))

    update_notice.maybe_notify(
        __version__, config=_auto_config(), ui=_FakeUi(), now=1_000_000.0, stream=_tty()
    )

    assert calls == [1]


# ---------------------------------------------------------------------------
# cli_main wiring
# ---------------------------------------------------------------------------


class _FailingCmd(Command):
    name = "boom"
    help = "always fails"

    @classmethod
    def run(cls, args: argparse.Namespace) -> None:
        raise SystemExit(1)


class _OkCmd(Command):
    name = "fine"
    help = "always succeeds"

    @classmethod
    def run(cls, args: argparse.Namespace) -> None:
        return


def test_cli_main_notifies_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.cli import commands as commands_pkg

    monkeypatch.setattr(commands_pkg, "commands", [_OkCmd])
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        update_notice,
        "maybe_notify",
        lambda *a, **k: seen.append({"args": a, "kwargs": k}),
    )
    monkeypatch.setattr(sys, "argv", ["novel-cli", "fine"])

    cli_main_module.cli_main()

    assert len(seen) == 1
    assert seen[0]["args"][0] == __version__


def test_cli_main_does_not_notify_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.cli import commands as commands_pkg

    monkeypatch.setattr(commands_pkg, "commands", [_FailingCmd])
    seen: list[object] = []
    monkeypatch.setattr(
        update_notice, "maybe_notify", lambda *a, **k: seen.append(object())
    )
    monkeypatch.setattr(sys, "argv", ["novel-cli", "boom"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 1
    assert seen == []


def test_cli_main_skips_notice_for_update_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []
    _patch_check(monkeypatch, _info())
    monkeypatch.setattr(
        update_notice, "maybe_notify", lambda *a, **k: seen.append(object())
    )
    monkeypatch.setattr(sys, "argv", ["novel-cli", "update", "--check"])

    cli_main_module.cli_main()

    assert seen == []


def test_cli_main_turns_ctrl_c_during_the_notice_into_abort(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C inside the hint wait must not leak a traceback."""
    from novel_downloader.apps.cli import commands as commands_pkg

    monkeypatch.setattr(commands_pkg, "commands", [_OkCmd])

    def _interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(update_notice, "maybe_notify", _interrupted)
    monkeypatch.setattr(sys, "argv", ["novel-cli", "fine"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 1
    assert "Aborted." in capsys.readouterr().err


def test_cli_main_passes_music_json_flag_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``music platforms --json`` keeps the default ``json`` dest, not ``as_json``."""

    class _MusicPlatforms(Command):
        name = "platforms"
        help = "list platforms"

        @classmethod
        def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--json", action="store_true")

        @classmethod
        def run(cls, args: argparse.Namespace) -> None:
            print("[]")

    from novel_downloader.apps.cli import commands as commands_pkg

    monkeypatch.setattr(commands_pkg, "commands", [_MusicPlatforms])
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        update_notice,
        "maybe_notify",
        lambda *a, **k: seen.append({"args": a, "kwargs": k}),
    )
    monkeypatch.setattr(sys, "argv", ["novel-cli", "platforms", "--json"])

    cli_main_module.cli_main()

    assert len(seen) == 1
    assert seen[0]["kwargs"]["as_json"] is True


# ---------------------------------------------------------------------------
# configuration validation
# ---------------------------------------------------------------------------


def _update_check_problems(value: object) -> list[Any]:
    return [
        problem
        for problem in validate_config({"general": {"update_check": value}})
        if problem.path == "general.update_check"
    ]


@pytest.mark.parametrize("value", ["auto", "manual", "off"])
def test_validation_accepts_update_check_modes(value: str) -> None:
    assert _update_check_problems(value) == []


def test_validation_rejects_unknown_update_check_mode() -> None:
    problems = _update_check_problems("sometimes")

    assert problems
    assert problems[0].level == "error"
    assert "sometimes" in problems[0].message
    assert "auto, manual, off" in problems[0].fix_hint


def test_validation_reports_non_string_update_check() -> None:
    problems = _update_check_problems(True)

    assert problems
    assert problems[0].level == "error"
