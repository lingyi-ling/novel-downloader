#!/usr/bin/env python3
"""
Installation precedence tests.

``install_rule`` writes into ``install_dir()`` (``$NOVEL_RULES_DIR`` when set),
but :func:`load_rules` scans the user directories in a fixed order afterwards:

    builtin  ->  ./rules  ->  $NOVEL_RULES_DIR  ->  <user-config>/rules

The last rule for a ``site_key`` wins (and inside one directory the last file
name wins).  Therefore a fresh install into ``$NOVEL_RULES_DIR`` used to be
silently overridden by an older rule in the per-user config directory.

These tests pin which file is effective after an install, and prove that a
would-be-shadowed install is refused (even with ``--force``) instead of
reporting success while doing nothing.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import novel_downloader.plugins.rule as rule_pkg
from novel_downloader.plugins.rule import (
    RuleError,
    install_rule,
    iter_rules,
    reload_rules,
    remove_rule,
)

#: Captured before any test patches ``_user_dirs``.
_ORIGINAL_USER_DIRS = rule_pkg._user_dirs


@pytest.fixture(autouse=True)
def _isolated_rule_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start from real locations, end back at them (no leaked env/dirs)."""
    monkeypatch.delenv("NOVEL_RULES_DIR", raising=False)
    reload_rules()
    yield
    os.environ.pop("NOVEL_RULES_DIR", None)
    rule_pkg._user_dirs = _ORIGINAL_USER_DIRS
    reload_rules()


def _write(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _rule(
    site_key: str, base_url: str, *, host: str = "demo.example"
) -> dict[str, Any]:
    return {
        "site_key": site_key,
        "base_url": base_url,
        "hosts": [host],
        "resolve": {"book": r"^/book/(\d+)/$"},
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
        "search": {
            "url": f"{base_url}/s?q={{keyword}}",
            "item": "div.result",
            "fields": {"title": "h3", "url": {"attr": "href"}},
        },
    }


def _use_dirs(monkeypatch: pytest.MonkeyPatch, directories: list[Path]) -> None:
    """Model the scan order deterministically (no real cwd/user dirs)."""
    monkeypatch.setattr(rule_pkg, "_user_dirs", lambda: list(directories))


def _meta() -> dict[str, dict[str, str]]:
    return {key: info for key, _, info in iter_rules()}


# --------------------------------------------------------------------------- #
# a would-be-shadowed install is refused
# --------------------------------------------------------------------------- #
def test_install_refuses_when_a_user_config_rule_would_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$NOVEL_RULES_DIR is scanned before <user-config>, so it cannot win."""
    cwd_rules = tmp_path / "cwd_rules"
    env_dir = tmp_path / "env_rules"
    user_dir = tmp_path / "user_rules"
    legacy = _write(user_dir, "legacy.json", _rule("demo", "https://legacy.example"))
    _use_dirs(monkeypatch, [cwd_rules, env_dir, user_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    with pytest.raises(RuleError, match="would be shadowed") as excinfo:
        install_rule(_rule("demo", "https://fresh.example"))

    # The message names the file that would win, and nothing is written.
    assert str(legacy) in str(excinfo.value)
    assert not (env_dir / "demo.json").exists()
    assert reload_rules()["demo"].base_url == "https://legacy.example"

    # ``--force`` only governs the target file; it cannot reorder the scan.
    with pytest.raises(RuleError, match="would be shadowed"):
        install_rule(_rule("demo", "https://fresh.example"), force=True)
    assert not (env_dir / "demo.json").exists()


def test_install_refuses_the_same_filename_in_a_higher_priority_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same name in different dirs: the scan order, not the name, decides."""
    env_dir = tmp_path / "env_rules"
    user_dir = tmp_path / "user_rules"
    legacy = _write(user_dir, "demo.json", _rule("demo", "https://legacy.example"))
    _use_dirs(monkeypatch, [env_dir, user_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    with pytest.raises(RuleError, match="would be shadowed") as excinfo:
        install_rule(_rule("demo", "https://fresh.example"), name="demo")

    assert str(legacy) in str(excinfo.value)
    assert not (env_dir / "demo.json").exists()
    assert reload_rules()["demo"].base_url == "https://legacy.example"


def test_install_refuses_when_a_later_file_in_the_same_dir_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "env_rules"
    later = _write(env_dir, "zzz.json", _rule("demo", "https://legacy.example"))
    _use_dirs(monkeypatch, [env_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    with pytest.raises(RuleError, match="would be shadowed") as excinfo:
        install_rule(_rule("demo", "https://fresh.example"), name="aaa")

    assert str(later) in str(excinfo.value)
    assert not (env_dir / "aaa.json").exists()
    assert reload_rules()["demo"].base_url == "https://legacy.example"


def test_builtin_rule_does_not_block_an_install_with_the_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builtins are scanned first, so a same-key user install always wins."""
    env_dir = tmp_path / "env_rules"
    user_dir = tmp_path / "user_rules"
    _use_dirs(monkeypatch, [env_dir, user_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    target = install_rule(_rule("biquge5r", "https://fresh.example"))

    rules = reload_rules()
    assert target == env_dir / "biquge5r.json"
    assert rules["biquge5r"].base_url == "https://fresh.example"
    assert _meta()["biquge5r"] == {"origin": "user", "file": str(target)}


# --------------------------------------------------------------------------- #
# which installed file actually takes effect
# --------------------------------------------------------------------------- #
def test_installed_env_rule_takes_effect_over_lower_priority_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_rules = tmp_path / "cwd_rules"
    _write(cwd_rules, "demo.json", _rule("demo", "https://cwd.example"))
    env_dir = tmp_path / "env_rules"
    user_dir = tmp_path / "user_rules"
    _use_dirs(monkeypatch, [cwd_rules, env_dir, user_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    target = install_rule(_rule("demo", "https://fresh.example"))

    rules = reload_rules()
    assert rules["demo"].base_url == "https://fresh.example"
    assert _meta()["demo"] == {"origin": "user", "file": str(target)}


def test_new_file_wins_over_an_earlier_file_in_the_same_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "env_rules"
    earlier = _write(env_dir, "aaa.json", _rule("demo", "https://stale.example"))
    _use_dirs(monkeypatch, [env_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    target = install_rule(_rule("demo", "https://fresh.example"), name="zzz")

    assert earlier.is_file()  # the stale copy is left alone ...
    rules = reload_rules()
    assert rules["demo"].base_url == "https://fresh.example"  # ... but loses
    assert _meta()["demo"]["file"] == str(target)


def test_same_filename_in_a_lower_priority_directory_is_not_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing into <user-config> must ignore a same-named ./rules file."""
    cwd_rules = tmp_path / "cwd_rules"
    cwd_copy = _write(cwd_rules, "demo.json", _rule("demo", "https://cwd.example"))
    user_dir = tmp_path / "user_rules"
    # With the env var unset, install_dir() is the last scanned user directory.
    _use_dirs(monkeypatch, [cwd_rules, user_dir])

    target = install_rule(_rule("demo", "https://fresh.example"), name="demo")

    assert target == user_dir / "demo.json"
    assert cwd_copy.is_file()
    rules = reload_rules()
    assert rules["demo"].base_url == "https://fresh.example"
    assert _meta()["demo"]["file"] == str(target)


# --------------------------------------------------------------------------- #
# overwrite / uninstall
# --------------------------------------------------------------------------- #
def test_overwrite_replaces_the_rule_that_is_effective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "env_rules"
    _use_dirs(monkeypatch, [env_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    first = install_rule(_rule("demo", "https://one.example"))
    with pytest.raises(RuleError, match="already exists"):
        install_rule(_rule("demo", "https://two.example"))
    assert reload_rules()["demo"].base_url == "https://one.example"

    returned = install_rule(_rule("demo", "https://two.example"), overwrite=True)

    assert returned == first
    assert reload_rules()["demo"].base_url == "https://two.example"


def test_remove_rule_deletes_the_effective_file_and_reveals_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``remove_rule`` targets the *effective* file (the override), so removing a
    stale user-config rule unmasks the rule installed into $NOVEL_RULES_DIR.
    """
    env_dir = tmp_path / "env_rules"
    user_dir = tmp_path / "user_rules"
    _use_dirs(monkeypatch, [env_dir, user_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    installed = install_rule(_rule("demo", "https://installed.example"))
    legacy = _write(user_dir, "legacy.json", _rule("demo", "https://legacy.example"))
    assert reload_rules()["demo"].base_url == "https://legacy.example"

    removed = remove_rule("demo")

    assert removed == legacy
    rules = reload_rules()
    assert rules["demo"].base_url == "https://installed.example"
    assert _meta()["demo"]["file"] == str(installed)


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def test_override_log_names_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cwd_rules = tmp_path / "cwd_rules"
    env_dir = tmp_path / "env_rules"
    _write(cwd_rules, "first.json", _rule("demo", "https://one.example"))
    _write(env_dir, "second.json", _rule("demo", "https://two.example"))
    _use_dirs(monkeypatch, [cwd_rules, env_dir])
    monkeypatch.setenv("NOVEL_RULES_DIR", str(env_dir))

    with caplog.at_level(logging.INFO, logger="novel_downloader.plugins.rule"):
        reload_rules()

    overrides = [
        record.getMessage()
        for record in caplog.records
        if " overrides " in record.getMessage()
    ]
    assert overrides, "an override must be logged at INFO"
    assert "first.json" in overrides[-1]
    assert "second.json" in overrides[-1]
    assert "demo" in overrides[-1]
