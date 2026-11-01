#!/usr/bin/env python3
"""
tests.apps.test_cli_e2e_cross
-----------------------------

Cross-command, cross-process end-to-end flows for ``novel-cli``.

``test_cli_e2e.py`` verifies each subcommand on its own; this module chains
commands the way a real user does, still fully offline:

* state written by ``config set-lang`` in one process is read back by a
  different command in a later process (the CLI has no ``config get``; the
  persisted ``state.json`` plus localized output of later commands is the
  read side of that round trip);
* a rule validated by ``sources check`` is installed by ``sources add``,
  becomes visible to ``sources list`` (and loadable per ``--check``), and is
  deleted by ``sources remove``;
* a settings file created by ``config init`` flips the ``doctor --json``
  exit code from 1 to 0;
* the completion scripts emitted by ``completion`` are actually valid shell
  input: the Bash script passes ``bash -n`` and sources cleanly (when a Bash
  exists), the Zsh script passes ``zsh -n`` (when a Zsh exists) and is
  structurally checked otherwise;
* usage-error contracts (exit 2) for missing keywords / subcommands.

Harness reuse: the subprocess runner, the in-process runner and the port
helper come from ``tests.apps.test_cli_e2e`` so both modules stay byte
identical in how they isolate children (``NOVEL_HOME``, UTF-8 stdio,
explicit timeouts).

Windows notes:

* every child gets ``NOVEL_HOME`` under ``tmp_path``;
* rule installation additionally needs ``NOVEL_RULES_DIR`` -- unlike
  ``NOVEL_HOME``, the app-home override does NOT redirect the user rule
  directory (``platformdirs`` user config), so the ``rules_dir`` fixture
  pins it to ``tmp_path`` to keep the real profile untouched;
* ``bash -n`` / ``zsh -n`` run only when the interpreter is on PATH; the
  tests skip with a reason otherwise (no bash exists on a bare Windows
  box without Git Bash).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.apps.test_cli_e2e import (
    ALL_COMMANDS,
    _bindable_port,
    _seed_config,
    run_cli,
    run_cli_inproc,
)

#: Timeout for the shell syntax probes (they parse one small file).
SHELL_CHECK_TIMEOUT = 30.0

#: Key option/positional names each ``music <sub> --help`` page must mention.
MUSIC_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "platforms": ("--json",),
    "search": ("--platform", "--limit", "--json", "keyword"),
    "download": ("--platform", "--limit", "--level", "--out", "--pick", "--yes"),
    "export-m3u": ("--keyword", "--platform", "--limit", "--out"),
    "health": ("--platform", "--keyword"),
}

#: Container commands whose subparser is required (bare invocation = exit 2).
REQUIRED_SUBCOMMAND_CONTAINERS = ("config", "clean", "follow")

#: A structurally valid, fully offline rule document (chapter-only source).
MINIMAL_RULE: dict[str, Any] = {
    "site_key": "e2etestsite",
    "site_name": "E2E Test Site",
    "base_url": "https://example.invalid",
    "hosts": ["example.invalid"],
    "chapter": {
        "url": "{base_url}/c/{chapter_id}.html",
        "content": {"selector": "div#content"},
    },
}


@pytest.fixture
def rules_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin rule install/discovery to ``tmp_path`` via ``NOVEL_RULES_DIR``.

    ``sources add`` installs into ``install_dir()`` which honours
    ``NOVEL_RULES_DIR``; without it the command would write into the real
    per-user config directory. The variable is inherited by every
    ``run_cli`` child spawned while the fixture is active.
    """
    target = tmp_path / "rules"
    monkeypatch.setenv("NOVEL_RULES_DIR", str(target))
    return target


def _write_rule_file(tmp_path: Path) -> Path:
    """Drop :data:`MINIMAL_RULE` as a JSON file inside ``tmp_path``."""
    rule_file = tmp_path / f"{MINIMAL_RULE['site_key']}.json"
    rule_file.write_text(
        json.dumps(MINIMAL_RULE, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rule_file


# ---------------------------------------------------------------------------
# config: language preference persisted by one command, read by another
# ---------------------------------------------------------------------------


def test_config_set_lang_roundtrip_across_processes(tmp_path: Path) -> None:
    home = tmp_path / "home"

    # 1. Fresh home, no NOVEL_LANG: the interface defaults to Chinese.
    before = run_cli("--help", cwd=tmp_path, home=home, lang=None)
    assert before.returncode == 0, before.stderr
    assert "显示此帮助信息并退出" in before.stdout

    # 2. ``config set-lang`` persists the preference in the child's home.
    switched = run_cli("config", "set-lang", "en", cwd=tmp_path, home=home, lang=None)
    assert switched.returncode == 0, switched.stderr
    assert "Language switched to en_US" in switched.stderr
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert state["lang"] == "en_US"

    # 3. A different command in a later process resolves English from the
    #    persisted state alone (no --lang flag, NOVEL_LANG removed).
    after = run_cli("clean", "--help", cwd=tmp_path, home=home, lang=None)
    assert after.returncode == 0, after.stderr
    assert "Show this help message and exit." in after.stdout
    assert "显示此帮助信息并退出" not in after.stdout


def test_config_set_lang_unsupported_is_usage_error(tmp_path: Path) -> None:
    home = tmp_path / "home"

    result = run_cli("config", "set-lang", "xx", cwd=tmp_path, home=home)

    assert result.returncode == 2, result.stderr
    assert "Unsupported language: xx" in result.stderr
    assert "en_US, zh_CN" in result.stderr  # available choices are listed
    # Nothing is persisted on failure: the round trip above must not be
    # affected by a rejected value.
    assert not (home / "state.json").exists()


@pytest.mark.parametrize("command", REQUIRED_SUBCOMMAND_CONTAINERS)
def test_container_command_without_subcommand_is_usage_error(
    command: str, tmp_path: Path
) -> None:
    result = run_cli(command, cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 2, result.stderr
    assert f"usage: novel-cli {command}" in result.stderr
    assert "the following arguments are required: subcommand" in result.stderr


# ---------------------------------------------------------------------------
# sources: check -> add -> list (--check) -> remove lifecycle
# ---------------------------------------------------------------------------


def test_sources_bare_command_defaults_to_list(tmp_path: Path) -> None:
    # Unlike config/clean/follow, a bare ``sources`` runs the list action
    # instead of erroring out.
    result = run_cli("sources", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # the table renders on stderr
    assert "Rule-based sources" in result.stderr


def test_sources_list_human_table_renders_on_stderr(tmp_path: Path) -> None:
    result = run_cli("sources", "list", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "Rule-based sources" in result.stderr
    # At least the shipped builtin rule is loaded and rendered.
    assert "biquge5r" in result.stderr
    assert "builtin" in result.stderr


def test_sources_check_add_list_remove_roundtrip(
    tmp_path: Path, rules_dir: Path
) -> None:
    home = tmp_path / "home"
    rule_file = _write_rule_file(tmp_path)

    checked = run_cli("sources", "check", str(rule_file), cwd=tmp_path, home=home)
    assert checked.returncode == 0, checked.stderr
    assert "Rule OK: E2E Test Site (site_key=e2etestsite, search=no)" in checked.stderr

    added = run_cli("sources", "add", str(rule_file), cwd=tmp_path, home=home)
    assert added.returncode == 0, added.stderr
    assert "Installed rule to" in added.stderr
    assert (rules_dir / "e2etestsite.json").is_file()

    listed = run_cli("sources", "list", "--json", cwd=tmp_path, home=home)
    assert listed.returncode == 0, listed.stderr
    by_key = {item["site_key"]: item for item in json.loads(listed.stdout)}
    assert "e2etestsite" in by_key
    assert by_key["e2etestsite"]["origin"] == "user"
    assert by_key["e2etestsite"]["hosts"] == ["example.invalid"]

    removed = run_cli("sources", "remove", "e2etestsite", cwd=tmp_path, home=home)
    assert removed.returncode == 0, removed.stderr
    assert "Removed rule 'e2etestsite'" in removed.stderr
    assert not (rules_dir / "e2etestsite.json").exists()

    after = run_cli("sources", "list", "--json", cwd=tmp_path, home=home)
    assert after.returncode == 0, after.stderr
    site_keys = {item["site_key"] for item in json.loads(after.stdout)}
    assert "e2etestsite" not in site_keys


def test_sources_list_check_reports_rules_as_loadable(
    tmp_path: Path, rules_dir: Path
) -> None:
    home = tmp_path / "home"
    rule_file = _write_rule_file(tmp_path)

    added = run_cli("sources", "add", str(rule_file), cwd=tmp_path, home=home)
    assert added.returncode == 0, added.stderr

    probed = run_cli("sources", "list", "--check", "--json", cwd=tmp_path, home=home)
    assert probed.returncode == 0, probed.stderr
    by_key = {item["site_key"]: item for item in json.loads(probed.stdout)}
    # The installed user rule and the builtin one both register cleanly
    # (this is the "rule loading" contract, offline by construction).
    assert by_key["e2etestsite"]["status"] == "ok"
    assert by_key["biquge5r"]["status"] == "ok"


def test_sources_check_missing_file_fails(tmp_path: Path) -> None:
    result = run_cli(
        "sources", "check", "does-not-exist.json", cwd=tmp_path, home=tmp_path / "home"
    )

    assert result.returncode == 1, result.stderr
    assert "Rule file not found" in result.stderr


def test_sources_remove_builtin_is_rejected(tmp_path: Path) -> None:
    result = run_cli(
        "sources", "remove", "biquge5r", cwd=tmp_path, home=tmp_path / "home"
    )

    assert result.returncode == 1, result.stderr
    assert "built-in source" in result.stderr


# ---------------------------------------------------------------------------
# completion: generated scripts are real, sourceable shell input
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash is not on PATH (no Git Bash); cannot syntax-check the script",
)
def test_completion_bash_script_is_valid_and_sourceable(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None  # narrow for the type checker; guarded by skipif

    result = run_cli(
        "completion", "--shell", "bash", cwd=tmp_path, home=tmp_path / "home"
    )
    assert result.returncode == 0, result.stderr

    script = result.stdout
    for command in ALL_COMMANDS:
        assert command in script
    assert "complete -F _novel_cli_completion novel-cli" in script

    # ``text=True`` decoding normalizes the child's CRLF to LF, which is
    # what a POSIX shell expects in a sourced file.
    script_path = tmp_path / "novel-cli-completion.bash"
    script_path.write_bytes(script.encode("utf-8"))

    syntax = subprocess.run(
        [bash, "-n", script_path.name],
        capture_output=True,
        cwd=str(tmp_path),
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SHELL_CHECK_TIMEOUT,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    # Sourcing must define the function and register the completion.
    sourced = subprocess.run(
        [
            bash,
            "-c",
            "source novel-cli-completion.bash"
            " && declare -F _novel_cli_completion"
            " && complete -p novel-cli",
        ],
        capture_output=True,
        cwd=str(tmp_path),
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SHELL_CHECK_TIMEOUT,
        check=False,
    )
    assert sourced.returncode == 0, sourced.stderr
    assert "_novel_cli_completion" in sourced.stdout
    assert "complete -F _novel_cli_completion novel-cli" in sourced.stdout


def test_completion_zsh_script_static_structure(tmp_path: Path) -> None:
    result = run_cli(
        "completion", "--shell", "zsh", cwd=tmp_path, home=tmp_path / "home"
    )

    assert result.returncode == 0, result.stderr
    script = result.stdout
    # Zsh autoload convention + the actual registration call.
    assert script.startswith("#compdef novel-cli")
    assert "compdef _novel_cli_completion novel-cli" in script
    for command in ALL_COMMANDS:
        assert command in script
    # Structural sanity for machines without a zsh binary. Parenthesis
    # counting is deliberately NOT used: shell ``case`` patterns close with
    # a bare ``)`` that has no matching ``(``, so the counts never balance.
    # Instead every generated word is single-quoted, so the quotes must
    # pair up, and every choice block must be a closed ``choices=(...)``.
    assert script.count("'") % 2 == 0
    for line in script.splitlines():
        if "choices=(" in line:
            assert line.rstrip().endswith(")"), line


@pytest.mark.skipif(
    shutil.which("zsh") is None,
    reason="zsh is not installed; only static validation is possible",
)
def test_completion_zsh_script_passes_zsh_syntax_check(tmp_path: Path) -> None:
    zsh = shutil.which("zsh")
    assert zsh is not None  # narrow for the type checker; guarded by skipif

    result = run_cli(
        "completion", "--shell", "zsh", cwd=tmp_path, home=tmp_path / "home"
    )
    assert result.returncode == 0, result.stderr

    script_path = tmp_path / "novel-cli-completion.zsh"
    script_path.write_bytes(result.stdout.encode("utf-8"))

    syntax = subprocess.run(
        [zsh, "-n", script_path.name],
        capture_output=True,
        cwd=str(tmp_path),
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SHELL_CHECK_TIMEOUT,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr


# ---------------------------------------------------------------------------
# doctor --json: exit-code contract driven by config presence
# ---------------------------------------------------------------------------


def test_doctor_json_without_config_exits_one(tmp_path: Path) -> None:
    # Fresh cwd and fresh NOVEL_HOME: no settings.toml anywhere, so the
    # fatal settings/config checks fail and the contract says exit 1.
    result = run_cli("doctor", "--json", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)  # stdout must be pure JSON
    assert {"summary", "checks"} <= set(payload)

    summary = payload["summary"]
    assert {"total", "failures", "optional_failures", "ok"} <= set(summary)
    assert summary["total"] == len(payload["checks"])
    assert summary["ok"] is False
    assert summary["failures"] >= 1

    checks = {check["key"]: check for check in payload["checks"]}
    assert checks["settings"]["ok"] is False
    assert checks["settings"]["fatal"] is True
    assert "config init" in checks["settings"]["suggestion"]
    assert checks["config"]["ok"] is False
    assert checks["config"]["fatal"] is True


def test_doctor_json_after_config_init_exits_zero(tmp_path: Path) -> None:
    home = tmp_path / "home"
    port = _bindable_port()

    # ``config init`` writes the settings file the doctor will read.
    initialized = run_cli("config", "init", cwd=tmp_path, home=home)
    assert initialized.returncode == 0, initialized.stderr
    assert (tmp_path / "settings.toml").is_file()

    result = run_cli("doctor", "--json", "--port", str(port), cwd=tmp_path, home=home)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    summary = payload["summary"]
    assert summary["ok"] is True
    assert summary["failures"] == 0
    assert summary["total"] == len(payload["checks"])

    checks = {check["key"]: check for check in payload["checks"]}
    assert checks["settings"]["ok"] is True
    assert str(tmp_path / "settings.toml") in checks["settings"]["detail"]
    assert checks["api_port"]["ok"] is True


# ---------------------------------------------------------------------------
# search / music: keyword and subcommand usage errors
# ---------------------------------------------------------------------------


def test_search_without_keyword_is_usage_error(tmp_path: Path) -> None:
    result = run_cli("search", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 2, result.stderr
    assert "usage: novel-cli search" in result.stderr
    assert "the following arguments are required: keyword" in result.stderr


def test_search_empty_keyword_is_forwarded_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Documents the *current* contract, which differs from ``music search``.

    An empty-string keyword is not rejected with a usage error: the command
    loads the config and hands ``keyword=""`` to the aggregate search (which
    would go to the network in a real run). ``music search ""`` exits 2
    instead -- see :func:`test_music_search_empty_keyword_is_usage_error`.
    If the CLI ever grows empty-keyword validation, this test will fail and
    should be updated to the exit-2 contract.
    """
    _seed_config(tmp_path)
    captured: dict[str, Any] = {}

    async def fake_search(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    code = run_cli_inproc(
        monkeypatch, ["search", "--json", ""], cwd=tmp_path, home=tmp_path / "home"
    )

    assert code == 0
    out, _err = capsys.readouterr()
    assert json.loads(out) == []
    assert captured["keyword"] == ""


def test_music_search_empty_keyword_is_usage_error(tmp_path: Path) -> None:
    # The keyword check happens before any provider is created, so this is
    # offline even though the music engine is loaded first.
    result = run_cli("music", "search", "", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 2, result.stderr
    assert "No keyword entered." in result.stderr


# ---------------------------------------------------------------------------
# music: full subcommand tree
# ---------------------------------------------------------------------------


def test_music_without_action_is_usage_error(tmp_path: Path) -> None:
    result = run_cli("music", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 2, result.stderr
    assert "the following arguments are required: action" in result.stderr
    # The usage line lists the whole subcommand tree.
    for subcommand in MUSIC_SUBCOMMANDS:
        assert subcommand in result.stderr


@pytest.mark.parametrize(("subcommand", "tokens"), sorted(MUSIC_SUBCOMMANDS.items()))
def test_music_subcommand_help_lists_key_options(
    subcommand: str, tokens: tuple[str, ...], tmp_path: Path
) -> None:
    result = run_cli(
        "music", subcommand, "--help", cwd=tmp_path, home=tmp_path / "home"
    )

    assert result.returncode == 0, result.stderr
    assert f"usage: novel-cli music {subcommand}" in result.stdout
    for token in tokens:
        assert token in result.stdout, f"music {subcommand}: missing {token!r}"
