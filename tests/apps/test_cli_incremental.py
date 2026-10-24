#!/usr/bin/env python3
"""
tests.apps.test_cli_incremental
--------------------------------

Offline tests for the incremental CLI features:

* ``music export-m3u`` -- playlist export with skipped pathless tracks
* ``music health`` -- table output and exit codes
* ``sources add --url`` -- bounded remote rule download/installation
* ``completion`` -- generated Bash/Zsh/PowerShell scripts

Network and the optional music engine are replaced with fakes; installed rules
go to a temporary ``NOVEL_RULES_DIR``.
"""

from __future__ import annotations

import argparse
import json
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import music, sources
from novel_downloader.apps.cli.commands.completion import (
    CompletionCmd,
    build_completion_tree,
    detect_shell,
    render_completion,
)
from novel_downloader.apps.cli.commands.music import MusicCmd
from novel_downloader.apps.cli.commands.sources import SourcesCmd
from novel_downloader.apps.music.downloader import export_m3u


def _parser(*commands: type) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in commands:
        command.register(subparsers)
    return parser


# ---------------------------------------------------------------------------
# music export-m3u
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, tracks: list[Any]) -> None:
        self.tracks = tracks
        self.searches: list[tuple[str, int]] = []

    def search(self, keyword: str, *, limit: int) -> tuple[list[Any], int]:
        self.searches.append((keyword, limit))
        return list(self.tracks), len(self.tracks)


class _FakeSettings:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir

    def load_settings(self) -> dict[str, Any]:
        return {"out_dir": str(self.out_dir)}


class _FakeEngine:
    login_expired = RuntimeError

    def __init__(
        self,
        *,
        tracks: list[Any] | None = None,
        out_dir: Path | None = None,
        health: dict[str, Any] | None = None,
        labels: list[tuple[str, str]] | None = None,
    ) -> None:
        self.provider = _FakeProvider(tracks or [])
        self._out_dir = out_dir or Path(".")
        self.settings = _FakeSettings(self._out_dir)
        self._health = health or {}
        self._labels = labels or [("netease", "NetEase")]
        self.health_calls: list[tuple[str, str]] = []
        self.export_m3u = export_m3u

    def labels(self) -> list[tuple[str, str]]:
        return list(self._labels)

    def platform_keys(self) -> list[str]:
        return sorted(key for key, _label in self._labels)

    def platform_note(self, platform: str) -> str:
        return ""

    def create(self, platform: str) -> _FakeProvider:
        return self.provider

    def default_download_dir(self) -> Path:
        return self._out_dir

    def health_check(self, platform: str, keyword: str) -> dict[str, Any]:
        self.health_calls.append((platform, keyword))
        result = self._health[platform]
        if isinstance(result, BaseException):
            raise result
        return result


def _track(index: int, path: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=str(index),
        name=f"Song {index}",
        artists="Artist",
        album="Album",
        duration_s=100,
        vip=False,
        provider="netease",
        path=path,
    )


def test_export_m3u_writes_only_tracks_with_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    engine = _FakeEngine(
        tracks=[
            _track(1, str(first)),
            _track(2),
            _track(3, str(second)),
        ],
        out_dir=tmp_path,
    )
    monkeypatch.setattr(music, "_require_engine", lambda: engine)

    playlist = tmp_path / "playlist.m3u8"
    args = _parser(MusicCmd).parse_args(
        [
            "music",
            "export-m3u",
            "--keyword",
            "hello",
            "--out",
            str(playlist),
        ]
    )
    args.func(args)

    text = playlist.read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U\n")
    assert "Song 1" in text
    assert "Song 3" in text
    assert "Song 2" not in text
    assert engine.provider.searches == [("hello", 20)]


def test_export_m3u_reports_skipped_tracks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _FakeEngine(
        tracks=[_track(1), _track(2)],
        out_dir=tmp_path,
    )
    monkeypatch.setattr(music, "_require_engine", lambda: engine)

    args = _parser(MusicCmd).parse_args(
        ["music", "export-m3u", "--keyword", "hello", "--out", str(tmp_path / "p.m3u8")]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    assert "Skipped 2 track" in stderr
    assert "no playlist was written" in stderr
    assert not (tmp_path / "p.m3u8").exists()


def test_export_m3u_default_path_uses_output_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    track_path = tmp_path / "song.mp3"
    track_path.write_bytes(b"x")
    engine = _FakeEngine(tracks=[_track(1, str(track_path))], out_dir=tmp_path)
    monkeypatch.setattr(music, "_require_engine", lambda: engine)

    args = _parser(MusicCmd).parse_args(
        ["music", "export-m3u", "--keyword", "Jazz/Night"]
    )
    args.func(args)

    assert (tmp_path / "Jazz_Night.m3u8").is_file()


# ---------------------------------------------------------------------------
# music health
# ---------------------------------------------------------------------------


def _health_args(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    args = _parser(MusicCmd).parse_args(["music", "health", *argv])
    args.func(args)


def test_health_all_ok_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _FakeEngine(
        labels=[("netease", "NetEase"), ("qq", "QQ")],
        health={
            "netease": {"ok": True, "elapsed_ms": 5, "error": ""},
            "qq": {"ok": True, "elapsed_ms": 7, "error": ""},
        },
    )
    monkeypatch.setattr(music, "_require_engine", lambda: engine)

    _health_args(monkeypatch, [])

    stderr = capsys.readouterr().err
    assert "netease" in stderr
    assert "qq" in stderr
    assert "5 ms" in stderr
    assert engine.health_calls == [("netease", "test"), ("qq", "test")]


def test_health_one_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _FakeEngine(
        labels=[("netease", "NetEase"), ("qq", "QQ")],
        health={
            "netease": {"ok": True, "elapsed_ms": 1, "error": ""},
            "qq": {"ok": False, "elapsed_ms": 9, "error": "boom"},
        },
    )
    monkeypatch.setattr(music, "_require_engine", lambda: engine)

    with pytest.raises(SystemExit) as excinfo:
        _health_args(monkeypatch, [])

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    assert "boom" in stderr


def test_health_single_platform_and_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine(
        labels=[("netease", "NetEase"), ("qq", "QQ")],
        health={"netease": {"ok": True, "elapsed_ms": 2, "error": ""}},
    )
    monkeypatch.setattr(music, "_require_engine", lambda: engine)

    _health_args(monkeypatch, ["--platform", "netease", "--keyword", "周杰伦"])

    assert engine.health_calls == [("netease", "周杰伦")]


def test_health_unknown_platform_exits_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine(labels=[("netease", "NetEase")])
    monkeypatch.setattr(music, "_require_engine", lambda: engine)

    with pytest.raises(SystemExit) as excinfo:
        _health_args(monkeypatch, ["--platform", "missing"])

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# sources add --url
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, size: int = -1) -> bytes:
        return self._data[:size]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _builtin_rule_bytes() -> bytes:
    resource = files("novel_downloader.plugins.rule.builtin").joinpath("biquge5r.json")
    return resource.read_bytes()


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | Exception,
) -> list[tuple[str, float | None]]:
    calls: list[tuple[str, float | None]] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        calls.append((str(request.full_url), timeout))
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload)

    monkeypatch.setattr(sources.urllib_request, "urlopen", fake_urlopen)
    return calls


def test_sources_add_url_installs_rule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rule_dir = tmp_path / "rules"
    monkeypatch.setenv("NOVEL_RULES_DIR", str(rule_dir))
    payload = _builtin_rule_bytes()
    calls = _patch_urlopen(monkeypatch, payload)

    args = _parser(SourcesCmd).parse_args(
        [
            "sources",
            "add",
            "--url",
            "https://example.com/rules/demo.json",
            "--name",
            "demo",
        ]
    )
    args.func(args)

    installed = rule_dir / "demo.json"
    assert installed.is_file()
    assert json.loads(installed.read_text(encoding="utf-8"))["site_key"] == "biquge5r"
    assert calls == [("https://example.com/rules/demo.json", 15.0)]


def test_sources_add_url_rejects_oversized_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path / "rules"))
    _patch_urlopen(monkeypatch, b"{" + b" " * (1024 * 1024 + 1))

    args = _parser(SourcesCmd).parse_args(
        ["sources", "add", "--url", "https://example.com/big.json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert not (tmp_path / "rules").exists()


def test_sources_add_url_invalid_json_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path / "rules"))
    _patch_urlopen(monkeypatch, b"{not json")

    args = _parser(SourcesCmd).parse_args(
        ["sources", "add", "--url", "https://example.com/bad.json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1


def test_sources_add_url_rejects_non_http_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_urlopen(monkeypatch, b"{}")

    args = _parser(SourcesCmd).parse_args(
        ["sources", "add", "--url", "file:///tmp/rule.json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert calls == []


def test_sources_add_url_network_error_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path / "rules"))
    _patch_urlopen(monkeypatch, OSError("connection refused"))

    args = _parser(SourcesCmd).parse_args(
        ["sources", "add", "--url", "https://example.com/rule.json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1


def test_sources_add_requires_exactly_one_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_urlopen(monkeypatch, b"{}")
    rule_file = tmp_path / "rule.json"
    rule_file.write_text("{}", encoding="utf-8")

    args = _parser(SourcesCmd).parse_args(
        ["sources", "add", str(rule_file), "--url", "https://example.com/r.json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2

    args = _parser(SourcesCmd).parse_args(["sources", "add"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2


def test_sources_add_name_requires_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rule_file = tmp_path / "rule.json"
    rule_file.write_text("{}", encoding="utf-8")

    args = _parser(SourcesCmd).parse_args(
        ["sources", "add", str(rule_file), "--name", "renamed"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------


def test_completion_tree_contains_nested_commands_and_options() -> None:
    node = build_completion_tree([MusicCmd])

    assert "export-m3u" in node.subcommands["music"].subcommands
    options = node.subcommands["music"].subcommands["export-m3u"].options
    assert {"--keyword", "--platform", "--limit", "--out"} <= set(options)


@pytest.mark.parametrize(
    ("shell", "expected"),
    [
        ("bash", "complete -F _novel_cli_completion novel-cli"),
        ("zsh", "compdef _novel_cli_completion novel-cli"),
        ("powershell", "Register-ArgumentCompleter -Native"),
    ],
)
def test_completion_scripts_mention_novel_cli(
    shell: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _parser(CompletionCmd, MusicCmd).parse_args(["completion", "--shell", shell])
    args.func(args)

    stdout = capsys.readouterr().out
    assert expected in stdout
    assert "novel-cli" in stdout


def test_bash_completion_contains_nested_words() -> None:
    node = build_completion_tree([CompletionCmd, MusicCmd, SourcesCmd])
    script = render_completion(node, "bash")

    assert '" music export-m3u")' in script
    assert "--keyword" in script
    assert '" sources add")' in script


@pytest.mark.parametrize(
    ("env", "platform", "expected"),
    [
        ({"SHELL": "/usr/bin/zsh"}, "linux", "zsh"),
        ({"SHELL": "/bin/bash"}, "win32", "bash"),
        ({}, "win32", "powershell"),
        ({}, "linux", "bash"),
    ],
)
def test_detect_shell(
    env: dict[str, str],
    platform: str,
    expected: str,
) -> None:
    assert detect_shell(env, platform=platform) == expected
