#!/usr/bin/env python3
"""
tests.apps.test_cli_e2e
-----------------------

End-to-end verification of every ``novel-cli`` subcommand, fully offline.

Harness (deliberate split, see task notes):

* **``subprocess.run([sys.executable, "-m", "novel_downloader.apps.cli.main",
  ...])``** is the default harness. It exercises the real entry point, real
  exit codes, real ``NOVEL_HOME`` isolation via the child environment and a
  real per-test working directory. Used for every test that does not need
  in-process monkeypatching.
* **``cli_main()`` called in-process** is used only for the two cases the
  specification requires to be monkeypatched (the aggregate search function
  and the music platform registry), because a monkeypatch cannot cross a
  process boundary.

Isolation guarantees:

* Every child gets a fresh ``NOVEL_HOME`` (state.json, watchlist.json,
  user rules) and runs in ``tmp_path``, so nothing outside the test
  directories is read or written.
* No test touches the network: searches are replaced with an async stub,
  the music registry with a fake ``_REGISTRY``, and every other command
  under test (help/version/config/sources/follow/doctor/storage/
  completion) is offline by construction.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from novel_downloader import __version__
from novel_downloader.apps.cli import main as cli_main_module
from novel_downloader.infra.paths import DEFAULT_CONFIG_FILE

#: Module invoked by the subprocess harness.
CLI_MODULE = "novel_downloader.apps.cli.main"

#: All subcommands registered by the CLI.
ALL_COMMANDS = (
    "download",
    "search",
    "follow",
    "export",
    "login",
    "config",
    "clean",
    "storage",
    "doctor",
    "sources",
    "music",
    "completion",
)

#: Key option/subcommand names each ``<cmd> --help`` page must mention.
HELP_TOKENS: dict[str, tuple[str, ...]] = {
    "download": ("--site", "--start", "--end", "--no-export", "--format", "--json"),
    "search": (
        "--site",
        "--limit",
        "--site-limit",
        "--timeout",
        "--json",
        "--sort",
        "--group",
    ),
    "follow": ("add", "remove", "list", "check"),
    "export": ("--format", "--site", "--stage", "--json"),
    "login": ("--timeout", "--list"),
    "config": ("init", "set-lang"),
    "clean": ("state", "logs", "cache", "book"),
    "storage": ("--json", "--clean", "--yes"),
    "doctor": ("--port",),
    "sources": ("list", "check", "add", "remove"),
    "music": ("platforms", "search", "download", "export-m3u", "health"),
    "completion": ("--shell",),
}

#: Marker strings identifying each generated completion script.
COMPLETION_MARKERS: dict[str, str] = {
    "bash": "complete -F _novel_cli_completion novel-cli",
    "zsh": "compdef _novel_cli_completion novel-cli",
    "powershell": "Register-ArgumentCompleter",
}


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _child_env(home: Path, *, lang: str | None = "en_US") -> dict[str, str]:
    """Build a fully isolated environment for a CLI child process."""
    env = dict(os.environ)
    env["NOVEL_HOME"] = str(home)
    # Force UTF-8 stdio in the child so Chinese output survives the pipe on
    # Windows (the default pipe codec would be a legacy code page).
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # Stable line wrapping for argparse/rich help output.
    env["COLUMNS"] = "200"
    if lang is None:
        env.pop("NOVEL_LANG", None)
    else:
        env["NOVEL_LANG"] = lang
    return env


def run_cli(
    *args: str,
    cwd: Path,
    home: Path,
    lang: str | None = "en_US",
    timeout: float = 240.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m novel_downloader.apps.cli.main <args>`` in isolation."""
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        capture_output=True,
        cwd=str(cwd),
        env=_child_env(home, lang=lang),
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def run_cli_inproc(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> int:
    """
    Call ``cli_main()`` directly with ``argv`` and return the exit code.

    A normal return means exit code 0; ``SystemExit`` codes are passed
    through (0 for ``--help``/``--version``, 1/2 for failures).
    """
    if cwd is not None:
        monkeypatch.chdir(cwd)
    if home is not None:
        monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["novel-cli", *argv])
    try:
        cli_main_module.cli_main()
    except SystemExit as exc:
        return 0 if exc.code is None else int(exc.code)
    return 0


def _seed_config(workspace: Path) -> None:
    """Drop the bundled sample settings into ``workspace`` as settings.toml."""
    (workspace / "settings.toml").write_bytes(DEFAULT_CONFIG_FILE.read_bytes())


# ---------------------------------------------------------------------------
# Top-level: --help / --version / --lang
# ---------------------------------------------------------------------------


def test_toplevel_help_lists_all_commands(tmp_path: Path) -> None:
    result = run_cli("--help", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    assert "usage: novel-cli" in result.stdout
    for command in ALL_COMMANDS:
        assert command in result.stdout
    for option in ("-h", "--help", "-v", "--version", "--lang"):
        assert option in result.stdout


def test_version_flag_prints_package_version(tmp_path: Path) -> None:
    result = run_cli("--version", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"NovelDownloader {__version__}"


def test_lang_flag_switches_help_to_english(tmp_path: Path) -> None:
    # ``lang=None`` removes NOVEL_LANG and the fresh NOVEL_HOME has no state,
    # so the English output can only come from the ``--lang en`` flag itself.
    result = run_cli(
        "--lang", "en", "--help", cwd=tmp_path, home=tmp_path / "home", lang=None
    )

    assert result.returncode == 0, result.stderr
    assert "Show this help message and exit." in result.stdout
    assert "小说" not in result.stdout


def test_default_language_is_chinese_without_lang_flag(tmp_path: Path) -> None:
    # Baseline for the test above: same isolated environment, no --lang and
    # no persisted preference resolves to the default language (zh_CN).
    result = run_cli("--help", cwd=tmp_path, home=tmp_path / "home", lang=None)

    assert result.returncode == 0, result.stderr
    assert "显示此帮助信息并退出" in result.stdout
    assert "小说下载器" in result.stdout


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_subcommand_help_exits_zero_with_key_options(
    command: str, tmp_path: Path
) -> None:
    result = run_cli(command, "--help", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    assert f"usage: novel-cli {command}" in result.stdout
    for token in HELP_TOKENS[command]:
        assert token in result.stdout, f"{command}: missing {token!r}"


def test_clean_help_lists_subcommands(tmp_path: Path) -> None:
    result = run_cli("clean", "--help", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    for subcommand in ("state", "logs", "cache", "book"):
        assert subcommand in result.stdout


# ---------------------------------------------------------------------------
# config: init --force / set-lang
# ---------------------------------------------------------------------------


def test_config_init_force_creates_parseable_toml(tmp_path: Path) -> None:
    home = tmp_path / "home"

    first = run_cli("config", "init", "--force", cwd=tmp_path, home=home)
    assert first.returncode == 0, first.stderr
    config_path = tmp_path / "settings.toml"
    assert config_path.is_file()
    assert "Copied: settings.toml" in first.stderr

    # A second --force run overwrites instead of prompting.
    second = run_cli("config", "init", "--force", cwd=tmp_path, home=home)
    assert second.returncode == 0, second.stderr
    assert "Overwriting existing file" in second.stderr

    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    assert isinstance(data, dict)
    assert "general" in data
    assert isinstance(data["general"], dict)


def test_config_set_lang_persists_state(tmp_path: Path) -> None:
    home = tmp_path / "home"

    result = run_cli("config", "set-lang", "en", cwd=tmp_path, home=home)

    assert result.returncode == 0, result.stderr
    assert "Language switched to en_US" in result.stderr

    state_path = home / "state.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["lang"] == "en_US"


# ---------------------------------------------------------------------------
# sources: list --json
# ---------------------------------------------------------------------------


def test_sources_list_json_emits_valid_json(tmp_path: Path) -> None:
    result = run_cli("sources", "list", "--json", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)  # stdout must be pure JSON
    assert isinstance(payload, list)
    assert len(payload) >= 1
    for item in payload:
        assert {"site_key", "name", "search", "hosts", "origin"} <= set(item)
        assert isinstance(item["site_key"], str) and item["site_key"]
        assert isinstance(item["hosts"], list)
    site_keys = [item["site_key"] for item in payload]
    assert site_keys == sorted(site_keys)


# ---------------------------------------------------------------------------
# search: --json with a stubbed aggregate search (no network)
# ---------------------------------------------------------------------------


def test_search_json_outputs_valid_json_with_stubbed_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(tmp_path)
    captured: dict[str, Any] = {}

    async def fake_search(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    # ``SearchCmd`` imports the aggregate function lazily at call time, so
    # patching the module attribute is picked up by the real command code.
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    code = run_cli_inproc(
        monkeypatch,
        ["search", "--json", "--limit", "0", "--site", "qidian", "test"],
        cwd=tmp_path,
        home=tmp_path / "home",
    )

    assert code == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == []  # valid JSON, empty result set
    # ``--limit 0`` is clamped to 1 by the command contract.
    assert captured["limit"] == 1
    assert list(captured["sites"]) == ["qidian"]
    assert captured["keyword"] == "test"
    assert captured["per_site_limit"] == 10
    assert captured["sort"] == "quality"
    # Grouping is opt-in: without ``--group`` the search stays flat.
    assert captured["group"] is False
    assert callable(captured["on_event"])
    assert "0 site(s) completed / 0 failed / 0 skipped" in err


def test_search_group_flag_merges_results_in_json_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(tmp_path)
    captured: dict[str, Any] = {}

    async def fake_search(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return [
            {
                "site": "beta",
                "book_id": "beta-1",
                "book_url": "https://example.invalid/beta/1",
                "cover_url": "",
                "title": "三体",
                "author": "刘慈欣",
                "latest_chapter": "",
                "update_date": "",
                "word_count": "300万字",
                "priority": 100,
                "sources": ["beta", "alpha"],
            }
        ]

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    code = run_cli_inproc(
        monkeypatch,
        ["search", "--json", "--group", "三体"],
        cwd=tmp_path,
        home=tmp_path / "home",
    )

    assert code == 0
    out, _err = capsys.readouterr()
    payload = json.loads(out)
    # ``--group`` reaches the aggregate search and the merged entries (with
    # their source capsules) survive the JSON round trip unchanged.
    assert captured["group"] is True
    assert payload[0]["site"] == "beta"
    assert payload[0]["sources"] == ["beta", "alpha"]


def test_search_no_group_flag_keeps_flat_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(tmp_path)
    captured: dict[str, Any] = {}

    async def fake_search(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return [
            {
                "site": "beta",
                "book_id": "beta-1",
                "book_url": "https://example.invalid/beta/1",
                "cover_url": "",
                "title": "三体",
                "author": "刘慈欣",
                "latest_chapter": "",
                "update_date": "",
                "word_count": "300万字",
                "priority": 100,
            }
        ]

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    code = run_cli_inproc(
        monkeypatch,
        ["search", "--json", "三体"],
        cwd=tmp_path,
        home=tmp_path / "home",
    )

    assert code == 0
    out, _err = capsys.readouterr()
    payload = json.loads(out)
    # The default output stays the historical flat shape: one row per result,
    # no grouping parameter, no capsule key.
    assert captured["group"] is False
    assert len(payload) == 1
    assert "sources" not in payload[0]


# ---------------------------------------------------------------------------
# follow: add / list --json / remove lifecycle
# ---------------------------------------------------------------------------


def test_follow_add_list_remove_lifecycle(tmp_path: Path) -> None:
    home = tmp_path / "home"

    added = run_cli(
        "follow",
        "add",
        "qidian",
        "1001",
        "--title",
        "Test Book",
        cwd=tmp_path,
        home=home,
    )
    assert added.returncode == 0, added.stderr
    assert "Added to the follow list: qidian/1001" in added.stderr
    assert (home / "watchlist.json").is_file()

    listed = run_cli("follow", "list", "--json", cwd=tmp_path, home=home)
    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert len(payload) == 1
    assert payload[0]["site"] == "qidian"
    assert payload[0]["book_id"] == "1001"
    assert payload[0]["title"] == "Test Book"

    removed = run_cli("follow", "remove", "qidian", "1001", cwd=tmp_path, home=home)
    assert removed.returncode == 0, removed.stderr
    assert "Removed from the follow list: qidian/1001" in removed.stderr

    empty = run_cli("follow", "list", "--json", cwd=tmp_path, home=home)
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout) == []


def test_follow_remove_unknown_book_is_idempotent(tmp_path: Path) -> None:
    result = run_cli(
        "follow", "remove", "qidian", "missing", cwd=tmp_path, home=tmp_path / "home"
    )

    assert result.returncode == 0, result.stderr
    assert "Not in the follow list: qidian/missing" in result.stderr


# ---------------------------------------------------------------------------
# doctor: full offline self-check
# ---------------------------------------------------------------------------


def _bindable_port() -> int:
    """Return a loopback port that can be bound right now.

    A fixed port is unreliable on a busy machine: an unrelated outbound
    connection can hold it (Windows then refuses the bind with 10013), which
    has nothing to do with the doctor command under test.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_doctor_all_checks_pass(tmp_path: Path) -> None:
    _seed_config(tmp_path)
    port = _bindable_port()

    result = run_cli(
        "doctor", "--port", str(port), cwd=tmp_path, home=tmp_path / "home"
    )

    assert result.returncode == 0, result.stderr
    assert "All checks passed." in result.stderr
    assert f"API port {port}" in result.stderr
    assert "✗" not in result.stderr


# ---------------------------------------------------------------------------
# storage: --json
# ---------------------------------------------------------------------------


def test_storage_json_emits_valid_json(tmp_path: Path) -> None:
    _seed_config(tmp_path)

    result = run_cli("storage", "--json", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)  # stdout must be pure JSON
    assert {"raw", "cache", "downloads", "logs", "total"} <= set(payload)
    for key in ("raw", "cache", "downloads", "logs"):
        entry = payload[key]
        assert {"label", "path", "files", "bytes"} <= set(entry)
        assert isinstance(entry["files"], int) and entry["files"] >= 0
        assert isinstance(entry["bytes"], int) and entry["bytes"] >= 0
    assert isinstance(payload["total"]["files"], int)
    assert isinstance(payload["total"]["bytes"], int)
    # Downloads resolve against the isolated working directory.
    assert str(tmp_path) in payload["downloads"]["path"]


# ---------------------------------------------------------------------------
# music: platforms (real registry + patched registry)
# ---------------------------------------------------------------------------


def test_music_platforms_lists_registered_platforms(tmp_path: Path) -> None:
    # Listing platforms is a static registry read: no network access.
    result = run_cli("music", "platforms", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    assert "Music platforms" in result.stderr  # table renders on stderr
    assert "netease" in result.stderr


def test_music_platforms_with_patched_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from novel_downloader.apps.music import providers as music_providers

    monkeypatch.setattr(
        music_providers,
        "_REGISTRY",
        {
            "alpha": ("Alpha Music", lambda **kw: None),
            "beta": ("Beta Music", lambda **kw: None),
        },
    )

    code = run_cli_inproc(
        monkeypatch, ["music", "platforms"], cwd=tmp_path, home=tmp_path / "home"
    )

    assert code == 0
    out, err = capsys.readouterr()
    assert out == ""  # the table is rendered on stderr
    assert "Music platforms" in err
    assert "alpha" in err and "Alpha Music" in err
    assert "beta" in err and "Beta Music" in err


def test_music_platforms_json_outputs_valid_json(tmp_path: Path) -> None:
    """``music platforms --json`` outputs a JSON object of key→label pairs."""
    result = run_cli(
        "music", "platforms", "--json", cwd=tmp_path, home=tmp_path / "home"
    )

    assert result.returncode == 0
    import json

    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert "netease" in payload


# ---------------------------------------------------------------------------
# completion: shell scripts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("shell", "marker"), sorted(COMPLETION_MARKERS.items()))
def test_completion_script_contains_commands_and_marker(
    shell: str, marker: str, tmp_path: Path
) -> None:
    result = run_cli(
        "completion", "--shell", shell, cwd=tmp_path, home=tmp_path / "home"
    )

    assert result.returncode == 0, result.stderr
    assert marker in result.stdout
    for command in ALL_COMMANDS:
        assert command in result.stdout


# ---------------------------------------------------------------------------
# Bonus behavioural checks (offline, cheap)
# ---------------------------------------------------------------------------


def test_login_list_sites(tmp_path: Path) -> None:
    result = run_cli("login", "--list", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 0, result.stderr
    assert "fanqienovel" in result.stdout


def test_download_without_args_is_a_usage_error(tmp_path: Path) -> None:
    result = run_cli("download", cwd=tmp_path, home=tmp_path / "home")

    assert result.returncode == 2
    assert "No book ID or URL provided" in result.stderr
