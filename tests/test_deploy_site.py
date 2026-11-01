#!/usr/bin/env python3
"""Tests for the website deployment helper (no network access)."""

from __future__ import annotations

import importlib.util
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
