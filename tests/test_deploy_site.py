#!/usr/bin/env python3
"""Tests for the website deployment helper (no network access)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy_site.py"
_spec = importlib.util.spec_from_file_location("deploy_site", _SCRIPT)
assert _spec and _spec.loader
deploy_site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_site)


def _make_site(tmp_path: Path, names: tuple[str, ...]) -> Path:
    site = tmp_path / "website"
    site.mkdir()
    for name in names:
        (site / name).write_text("<html></html>", encoding="utf-8")
    return site


def test_site_files_only_lists_known_and_present_files(tmp_path: Path) -> None:
    site = _make_site(tmp_path, ("index.html", "main.js", "style.css", "stray.bak"))

    names = [path.name for path in deploy_site.site_files(site)]

    assert names == ["index.html", "main.js", "style.css"]


def test_site_files_preserves_the_declared_order(tmp_path: Path) -> None:
    site = _make_site(tmp_path, ("style.css", "index.html"))

    assert [p.name for p in deploy_site.site_files(site)] == [
        "index.html",
        "style.css",
    ]


def test_missing_required_reports_absent_files(tmp_path: Path) -> None:
    site = _make_site(tmp_path, ("index.html",))

    assert deploy_site.missing_required(site) == ["main.js", "style.css"]


def test_missing_required_is_empty_for_a_complete_site(tmp_path: Path) -> None:
    site = _make_site(tmp_path, deploy_site.REQUIRED_FILES)

    assert deploy_site.missing_required(site) == []


def test_remote_script_is_strict_and_quotes_every_name() -> None:
    script = deploy_site._remote_script(
        "/tmp/stage", "/var/www/noveldl", ["index.html", "a b.js"]
    )

    assert script.startswith("set -eu && ")
    assert "'a b.js'" in script
    assert "sudo -n cp -a" in script
    # A failure anywhere aborts the chain instead of publishing half a site.
    assert script.count(" && ") >= 3


def test_dry_run_returns_names_without_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _make_site(tmp_path, deploy_site.REQUIRED_FILES)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry run must not open a connection")

    monkeypatch.setattr(deploy_site, "_connect", _boom)

    names = deploy_site.deploy(site_dir=site, dry_run=True)

    assert names == list(deploy_site.REQUIRED_FILES)


def test_deploy_refuses_an_incomplete_site(tmp_path: Path) -> None:
    site = _make_site(tmp_path, ("index.html",))

    with pytest.raises(deploy_site.DeployError, match="missing"):
        deploy_site.deploy(site_dir=site, dry_run=True)


def test_deploy_refuses_an_empty_site(tmp_path: Path) -> None:
    site = tmp_path / "website"
    site.mkdir()

    with pytest.raises(deploy_site.DeployError):
        deploy_site.deploy(site_dir=site, dry_run=True)


def test_every_website_file_is_on_the_deploy_list() -> None:
    """A new asset must not be silently left out of a deployment."""
    website = _SCRIPT.parent.parent / "website"
    present = {path.name for path in website.iterdir() if path.is_file()}

    assert present <= set(deploy_site.SITE_FILES), (
        "add the new file to SITE_FILES in scripts/deploy_site.py"
    )


def test_a_relative_docroot_is_rejected(tmp_path: Path) -> None:
    site = _make_site(tmp_path, deploy_site.REQUIRED_FILES)

    with pytest.raises(deploy_site.DeployError, match="absolute"):
        deploy_site.deploy(site_dir=site, docroot="var/www/site", dry_run=True)


# ---------------------------------------------------------------------------
# SSH doubles: no network is touched by these tests
# ---------------------------------------------------------------------------


class _Channel:
    def __init__(self, code: int) -> None:
        self._code = code

    def recv_exit_status(self) -> int:
        return self._code


class _Stream:
    def __init__(
        self, body: bytes = b"", code: int = 0, *, fail_read: bool = False
    ) -> None:
        self._body = body
        self.channel = _Channel(code)
        self._fail_read = fail_read

    def read(self) -> bytes:
        if self._fail_read:
            raise TimeoutError("channel read timed out")
        return self._body


class _SFTP:
    def __init__(self, fail_on: str | None = None) -> None:
        self.made: list[str] = []
        self.files: dict[str, bytes] = {}
        self.closed = False
        self._fail_on = fail_on

    def mkdir(self, path: str) -> None:
        self.made.append(path)

    def put(self, local: str, remote: str) -> None:
        if self._fail_on is not None and remote.endswith(self._fail_on):
            raise OSError("connection reset by peer")
        self.files[remote] = Path(local).read_bytes()

    def close(self) -> None:
        self.closed = True


class _SSH:
    """Records exec_command calls; exit codes are consumed in order."""

    def __init__(
        self,
        *,
        exit_codes: tuple[int, ...] = (),
        put_fail: str | None = None,
        fail_exec_once: bool = False,
        fail_cleanup_read: bool = False,
    ) -> None:
        self.sftp = _SFTP(put_fail)
        self.commands: list[tuple[str, float | None]] = []
        self.closed = False
        self._codes = list(exit_codes)
        self._fail_exec_once = fail_exec_once
        self._fail_cleanup_read = fail_cleanup_read

    def open_sftp(self) -> _SFTP:
        return self.sftp

    def exec_command(self, command: str, timeout: float | None = None):
        if self._fail_exec_once:
            self._fail_exec_once = False
            self.commands.append((command, timeout))
            raise OSError("socket closed")
        self.commands.append((command, timeout))
        code = self._codes.pop(0) if self._codes else 0
        stdout = _Stream(
            b"",
            code,
            fail_read=self._fail_cleanup_read and command.startswith("sudo -n rm -rf"),
        )
        stderr = _Stream(b"remote said no" if code else b"")
        return None, stdout, stderr

    def close(self) -> None:
        self.closed = True


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch, client: _SSH
) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    def fake_connect(host: str, user: str) -> _SSH:
        seen.append((host, user))
        return client

    monkeypatch.setattr(deploy_site, "_connect", fake_connect)
    return seen


def test_deploy_uploads_then_swaps_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _make_site(tmp_path, deploy_site.SITE_FILES)
    client = _SSH()
    _patch_connect(monkeypatch, client)

    names = deploy_site.deploy(
        site_dir=site, host="deploy.test", user="deployer", docroot="/srv/site"
    )

    assert names == list(deploy_site.SITE_FILES)
    assert sorted(path.split("/")[-1] for path in client.sftp.files) == sorted(
        deploy_site.SITE_FILES
    )
    install_command, timeout = client.commands[0]
    assert timeout == deploy_site.REMOTE_TIMEOUT
    assert "sudo -n cp -a" in install_command
    assert "/srv/site.new-" in install_command
    assert "/srv/site.old-" in install_command
    # The cleanup command runs after the swap and removes the staging tree.
    cleanup_command, cleanup_timeout = client.commands[1]
    assert cleanup_timeout == deploy_site.CLEANUP_TIMEOUT
    assert "rm -rf" in cleanup_command
    assert "/tmp/noveldl-deploy-" in cleanup_command
    assert client.sftp.closed and client.closed


def test_a_remote_install_failure_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _make_site(tmp_path, deploy_site.REQUIRED_FILES)
    client = _SSH(exit_codes=(1, 0))
    _patch_connect(monkeypatch, client)

    with pytest.raises(deploy_site.DeployError, match="remote install failed"):
        deploy_site.deploy(site_dir=site, docroot="/srv/site")

    # The install failed, but the staging tree is still removed.
    assert len(client.commands) == 2
    assert "rm -rf" in client.commands[1][0]
    assert client.sftp.closed and client.closed


def test_an_upload_failure_cleans_up_and_does_not_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _make_site(tmp_path, deploy_site.REQUIRED_FILES)
    client = _SSH(put_fail="style.css")
    _patch_connect(monkeypatch, client)

    with pytest.raises(deploy_site.DeployError, match="upload .* failed"):
        deploy_site.deploy(site_dir=site, docroot="/srv/site")

    # No install command ran; only cleanup did.
    assert len(client.commands) == 1
    assert "rm -rf" in client.commands[0][0]
    assert client.sftp.closed and client.closed


def test_an_interrupted_exec_is_wrapped_and_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _make_site(tmp_path, deploy_site.REQUIRED_FILES)
    client = _SSH(fail_exec_once=True)
    _patch_connect(monkeypatch, client)

    with pytest.raises(deploy_site.DeployError, match="interrupted"):
        deploy_site.deploy(site_dir=site, docroot="/srv/site")

    assert len(client.commands) == 2
    assert "rm -rf" in client.commands[1][0]
    assert client.closed


def test_a_hung_cleanup_read_is_bounded_and_the_client_still_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = _make_site(tmp_path, deploy_site.REQUIRED_FILES)
    client = _SSH(fail_cleanup_read=True)
    _patch_connect(monkeypatch, client)

    # The install succeeded; only the best-effort cleanup read times out.  It
    # must not turn a completed deployment into a failure, and must not block
    # long enough to keep the client open.
    names = deploy_site.deploy(site_dir=site, docroot="/srv/site")

    assert names == list(deploy_site.REQUIRED_FILES)
    assert "could not clean up" in capsys.readouterr().err
    assert client.sftp.closed and client.closed


def test_staging_paths_are_unique_per_run() -> None:
    first = deploy_site._new_work_id()
    second = deploy_site._new_work_id()
    assert first != second
    assert str(os.getpid()) in first.split("-", 1)[0]
    new_a, old_a = deploy_site._install_paths("/srv/site", first)
    new_b, _ = deploy_site._install_paths("/srv/site", second)
    assert new_a != new_b and old_a.endswith(first)
    assert new_a.startswith("/srv/site.new-")


def test_the_remote_script_swaps_with_rollback() -> None:
    script = deploy_site._remote_script(
        "/tmp/stage", "/srv/site", ["index.html"], work_id="w1"
    )

    assert "/srv/site.new-w1" in script
    assert "/srv/site.old-w1" in script
    # Copy the old tree first, so unrelated files survive the swap.
    assert "cp -a '/srv/site'/." in script or "cp -a /srv/site/." in script
    # A failed swap restores the previous tree before exiting non-zero.
    assert "mv /srv/site.old-w1 /srv/site" in script
    assert script.endswith("exit 1; fi")


def test_connect_failures_become_deploy_errors_without_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    class _BrokenClient:
        def set_missing_host_key_policy(self, policy: object) -> None:
            pass

        def connect(self, *args: object, **kwargs: object) -> None:
            raise OSError("timed out")

        def close(self) -> None:
            pass

    fake_paramiko = types.SimpleNamespace(
        SSHClient=_BrokenClient,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setenv("NOVELDL_DEPLOY_PASSWORD", "hunter2")

    with pytest.raises(deploy_site.DeployError, match="SSH connection") as info:
        deploy_site._connect("deploy.test", "deployer")

    assert "hunter2" not in str(info.value)


def test_main_reports_deploy_errors_as_exit_code_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: object, **kwargs: object) -> list[str]:
        raise deploy_site.DeployError("server unreachable")

    monkeypatch.setattr(deploy_site, "deploy", boom)

    assert deploy_site.main([]) == 1
    assert "deploy failed: server unreachable" in capsys.readouterr().err
