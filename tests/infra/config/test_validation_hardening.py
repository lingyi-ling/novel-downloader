"""Boundary tests for configuration validation.

Covers environment-variable expansion (with undefined, recursive and
self-referential values), proxy address checks and the redaction of the
user's home directory from diagnostics.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novel_downloader.infra.config import Problem, validate_config
from novel_downloader.infra.config.validation import expand_env_path


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "app-home"))


def _paths(problems: list[Problem], level: str | None = None) -> set[str]:
    return {p.path for p in problems if level is None or p.level == level}


# ---------------------------------------------------------------------------
# expand_env_path
# ---------------------------------------------------------------------------


def test_expand_env_path_supports_all_placeholder_styles(monkeypatch):
    monkeypatch.setenv("NOVEL_A", "alpha")
    monkeypatch.setenv("NOVEL_B", "beta")

    assert expand_env_path("${NOVEL_A}/x") == "alpha/x"
    assert expand_env_path("$NOVEL_B/x") == "beta/x"
    assert expand_env_path("%NOVEL_A%/y") == "alpha/y"


def test_expand_env_path_leaves_undefined_placeholders(monkeypatch):
    monkeypatch.delenv("NOVEL_MISSING", raising=False)

    assert expand_env_path("${NOVEL_MISSING}/x") == "${NOVEL_MISSING}/x"
    assert expand_env_path("%NOVEL_MISSING%/x") == "%NOVEL_MISSING%/x"


def test_expand_env_path_is_bounded_and_cycle_safe(monkeypatch):
    monkeypatch.setenv("NOVEL_LOOP_A", "${NOVEL_LOOP_B}")
    monkeypatch.setenv("NOVEL_LOOP_B", "${NOVEL_LOOP_A}")
    monkeypatch.setenv("NOVEL_SELF", "${NOVEL_SELF}")

    assert expand_env_path("${NOVEL_SELF}") == "${NOVEL_SELF}"
    assert expand_env_path("${NOVEL_LOOP_A}") in {
        "${NOVEL_LOOP_A}",
        "${NOVEL_LOOP_B}",
    }


def test_expand_env_path_expands_tilde(monkeypatch, tmp_path):
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    assert Path(expand_env_path("~/downloads")) == home / "downloads"


# ---------------------------------------------------------------------------
# Directory validation
# ---------------------------------------------------------------------------


def test_undefined_env_var_in_directory_is_an_error(monkeypatch):
    monkeypatch.delenv("NOVEL_MISSING_DIR", raising=False)

    problems = validate_config({"general": {"output_dir": "${NOVEL_MISSING_DIR}"}})

    assert _paths(problems, "error") == {"general.output_dir"}
    assert "NOVEL_MISSING_DIR" in problems[0].message


def test_directory_env_var_is_expanded_before_checks(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NOVEL_DIR", str(blocker))

    problems = validate_config({"general": {"output_dir": "${NOVEL_DIR}/child"}})

    assert _paths(problems, "error") == {"general.output_dir"}
    assert "cannot create" in problems[0].message


def test_directory_with_expanded_existing_target_is_valid(monkeypatch, tmp_path):
    out = tmp_path / "ready"
    out.mkdir()
    monkeypatch.setenv("NOVEL_DIR", str(out))

    assert validate_config({"general": {"output_dir": "${NOVEL_DIR}"}}) == []


def test_directory_error_redacts_home(monkeypatch, tmp_path):
    home = tmp_path / "fake-home"
    (home / "not-a-dir").mkdir(parents=True)
    (home / "not-a-dir" / "file").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    problems = validate_config(
        {"general": {"output_dir": str(home / "not-a-dir" / "file")}}
    )

    assert _paths(problems, "error") == {"general.output_dir"}
    assert str(home) not in problems[0].message
    assert "~" + os.sep in problems[0].message


# ---------------------------------------------------------------------------
# Proxy validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proxy",
    [
        None,
        "http://127.0.0.1:7890",
        "https://user:pass@proxy.example:443",
        "socks5h://127.0.0.1:1080",
        "socks4a://127.0.0.1:1080",
    ],
)
def test_valid_proxy_addresses_have_no_problems(proxy):
    assert validate_config({"general": {"proxy": proxy}}) == []


@pytest.mark.parametrize(
    "proxy",
    [
        "ftp://127.0.0.1:21",
        "127.0.0.1:7890",
        "http://",
        "http://proxy example:8080",
        "",
        "   ",
    ],
)
def test_invalid_proxy_addresses_are_errors(proxy):
    problems = validate_config({"general": {"proxy": proxy}})

    assert "general.proxy" in _paths(problems, "error")


def test_undefined_env_var_in_proxy_is_an_error(monkeypatch):
    monkeypatch.delenv("NOVEL_PROXY_MISSING", raising=False)

    problems = validate_config({"general": {"proxy": "${NOVEL_PROXY_MISSING}"}})

    assert "general.proxy" in _paths(problems, "error")
    assert "NOVEL_PROXY_MISSING" in problems[0].message


def test_proxy_env_var_is_expanded_and_accepted(monkeypatch):
    monkeypatch.setenv("NOVEL_PROXY_URL", "socks5://127.0.0.1:1080")

    assert validate_config({"general": {"proxy": "${NOVEL_PROXY_URL}"}}) == []


def test_site_proxy_is_validated(monkeypatch):
    monkeypatch.delenv("NOVEL_PROXY_MISSING", raising=False)

    problems = validate_config({"sites": {"s": {"proxy": "%NOVEL_PROXY_MISSING%"}}})

    assert "sites.s.proxy" in _paths(problems, "error")
