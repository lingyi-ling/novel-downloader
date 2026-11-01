#!/usr/bin/env python3
"""
tests.apps.test_cli_batch
-------------------------

Offline tests for ``novel-cli batch``.

Search is replaced by a fake coroutine, config loading and logging setup are
stubbed, and every path comes from ``tmp_path``: no network call is made and
nothing is written outside the test directory.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import batch
from novel_downloader.apps.cli.commands.batch import (
    BatchCmd,
    BatchEntry,
    parse_batch_text,
    select_match,
)
from novel_downloader.schemas import SearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Messages:
    """Records the messages passed to the ``ui`` helpers."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> _Messages:
    """Capture user-facing messages instead of printing them."""
    captured = _Messages()
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(batch.ui, name, getattr(captured, name).append)
    return captured


@pytest.fixture
def offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    messages: _Messages,
) -> Path:
    """Stub config/logging and redirect the default not-found directory."""
    config: dict[str, Any] = {
        "general": {
            "debug": {"log_dir": str(tmp_path / "logs")},
            "output_dir": str(tmp_path / "downloads"),
        }
    }
    monkeypatch.setattr(batch, "load_or_init_config", lambda _path=None: config)
    monkeypatch.setattr(batch.ui, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(batch, "_NOT_FOUND_DIR", tmp_path / "logs")
    return tmp_path


def _result(
    site: str = "b520",
    book_id: str = "1",
    title: str = "斗破苍穹",
    author: str = "天蚕土豆",
) -> SearchResult:
    return SearchResult(
        site=site,
        book_id=book_id,
        book_url="",
        cover_url="",
        title=title,
        author=author,
        latest_chapter="",
        update_date="",
        word_count="",
        priority=0,
    )


def _entry(title: str, author: str | None) -> BatchEntry:
    return BatchEntry(line_no=1, title=title, author=author)


def _install_search(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, list[SearchResult]],
) -> list[tuple[str, str | None]]:
    """Replace the single-book search with a lookup table; record its calls."""
    calls: list[tuple[str, str | None]] = []

    async def _fake(entry: BatchEntry, site: str | None) -> list[SearchResult]:
        calls.append((entry.title, site))
        return mapping.get(entry.title, [])

    monkeypatch.setattr(batch, "_search_one", _fake)
    return calls


def _parse_args(*argv: str) -> argparse.Namespace:
    """Parse ``batch`` arguments through the real command registration."""
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    BatchCmd.register(subparsers)
    return parser.parse_args(["batch", *argv])


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def test_parse_fullwidth_space_tabs_and_comments() -> None:
    parsed = parse_batch_text(
        "\ufeff# leading comment\n"
        "\n"
        "斗破苍穹\u3000天蚕土豆\n"
        "  诡秘之主\t爱潜水的乌贼  \n"
        "凡人修仙传   忘语\n"
        "# site=b520 searched_at=x matches=0/0\n"
    )

    assert [(e.title, e.author) for e in parsed.entries] == [
        ("斗破苍穹", "天蚕土豆"),
        ("诡秘之主", "爱潜水的乌贼"),
        ("凡人修仙传", "忘语"),
    ]
    assert parsed.malformed == []
    assert parsed.site_hint == "b520"


def test_parse_title_only_and_malformed_lines() -> None:
    parsed = parse_batch_text(
        "独行月球\n///\n坏\x00标题 作者\n" + "长" * 201 + " 作者\n正常书 正常作者\n"
    )

    assert [(e.title, e.author) for e in parsed.entries] == [
        ("独行月球", None),
        ("正常书", "正常作者"),
    ]
    assert [item.line_no for item in parsed.malformed] == [2, 3, 4]
    assert [item.reason for item in parsed.malformed] == [
        "title has no searchable characters",
        "contains control characters",
        "title is too long",
    ]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_select_match_exact_title_and_author() -> None:
    outcome = select_match(_entry("斗破苍穹", "天蚕土豆"), [_result()], strict=True)

    assert outcome.result is not None
    assert outcome.result["book_id"] == "1"
    assert outcome.candidates == 1
    assert outcome.author_mismatch is False


def test_select_match_ignores_title_punctuation() -> None:
    bookmarked = _result(title="《斗破苍穹》")

    outcome = select_match(_entry("斗破苍穹", "天蚕土豆"), [bookmarked])

    assert outcome.result is bookmarked


def test_select_match_multiple_candidates_prefers_exact_author() -> None:
    other = _result(book_id="1", author="别的作者")
    exact = _result(book_id="2", author="天蚕土豆")

    outcome = select_match(_entry("斗破苍穹", "天蚕土豆"), [other, exact])

    assert outcome.result is exact
    assert outcome.candidates == 2
    assert outcome.author_mismatch is False


def test_select_match_non_strict_uses_best_author_similarity() -> None:
    unrelated = _result(book_id="1", author="完全无关")
    similar = _result(book_id="2", author="天蚕土")

    outcome = select_match(_entry("斗破苍穹", "天蚕土豆"), [unrelated, similar])

    assert outcome.result is similar
    assert outcome.author_mismatch is True


def test_select_match_strict_rejects_author_mismatch() -> None:
    outcome = select_match(_entry("斗破苍穹", "别人"), [_result()], strict=True)

    assert outcome.result is None
    assert outcome.candidates == 1
    assert outcome.author_mismatch is True


def test_select_match_title_only_takes_top_result() -> None:
    first = _result(book_id="1", author="甲")
    second = _result(book_id="2", author="乙")

    outcome = select_match(_entry("斗破苍穹", None), [first, second])

    assert outcome.result is first
    assert outcome.candidates == 2


# ---------------------------------------------------------------------------
# Not-found file
# ---------------------------------------------------------------------------


def test_write_not_found_is_self_parseable(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "not_found_b520.txt"
    entries = [
        BatchEntry(line_no=2, title="书甲", author="作者甲"),
        BatchEntry(line_no=3, title="书乙", author=None),
    ]
    malformed = [batch.MalformedLine(1, "///", "title has no searchable characters")]

    batch._write_not_found(
        path,
        entries=entries,
        malformed=malformed,
        site="b520",
        matched=3,
        total=5,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert re.match(r"^# site=b520 searched_at=\S+ matches=3/5$", lines[0])
    assert lines[1:3] == ["书甲 作者甲", "书乙"]
    assert lines[3] == "# malformed line 1: ///"
    assert lines[-1] == "#"

    reparsed = parse_batch_text(path.read_text(encoding="utf-8"))
    assert [(e.title, e.author) for e in reparsed.entries] == [
        ("书甲", "作者甲"),
        ("书乙", None),
    ]
    assert reparsed.malformed == []
    assert reparsed.site_hint == "b520"


def test_default_not_found_path_uses_site_key(offline: Path) -> None:
    assert batch._not_found_path(None, "b520").name == "not_found_b520.txt"
    assert batch._not_found_path(None, None).name == "not_found_all.txt"


# ---------------------------------------------------------------------------
# run(): reporting and exit codes
# ---------------------------------------------------------------------------


def test_run_reports_not_found_and_exits_failure(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(
        tmp_path / "books.txt",
        "斗破苍穹 天蚕土豆\n消失的书 某作者\n",
    )
    calls = _install_search(
        monkeypatch,
        {"斗破苍穹": [_result()], "消失的书": []},
    )
    not_found = tmp_path / "not_found.txt"

    args = _parse_args(
        "--file", str(source), "--site", "b520", "--not-found", str(not_found)
    )
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1
    assert calls == [("斗破苍穹", "b520"), ("消失的书", "b520")]
    text = not_found.read_text(encoding="utf-8")
    assert "消失的书 某作者" in text
    assert "斗破苍穹" not in text
    assert text.splitlines()[-1] == "#"
    assert any(
        "[1/2] 斗破苍穹 (天蚕土豆) → matched b520" in line for line in messages.success
    )
    assert any("[2/2] 消失的书 (某作者) → not found" in line for line in messages.warn)
    assert any(
        "Total: 2 · matched: 1 · not found: 1 · malformed: 0" in line
        for line in messages.info
    )


def test_run_all_matched_exits_zero_and_writes_no_not_found_file(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    not_found = tmp_path / "not_found.txt"

    args = _parse_args(
        "--file", str(source), "--site", "b520", "--not-found", str(not_found)
    )
    BatchCmd.run(args)  # no SystemExit on a clean run

    assert not not_found.exists()
    assert any(
        "Total: 1 · matched: 1 · not found: 0 · malformed: 0" in line
        for line in messages.info
    )


def test_run_reports_malformed_lines_and_preserves_them(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "///\n斗破苍穹 天蚕土豆\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    not_found = tmp_path / "not_found.txt"

    args = _parse_args(
        "--file", str(source), "--site", "b520", "--not-found", str(not_found)
    )
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1
    assert any("Skipped malformed line 1" in line for line in messages.warn)
    text = not_found.read_text(encoding="utf-8")
    assert "# malformed line 1: ///" in text


def test_run_warns_for_title_only_entry_and_matches_by_title(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "独行月球\n")
    _install_search(monkeypatch, {"独行月球": [_result(title="独行月球")]})

    args = _parse_args(
        "--file", str(source), "--site", "b520", "--not-found", str(tmp_path / "nf")
    )
    BatchCmd.run(args)

    assert any("No author for '独行月球'" in line for line in messages.warn)


def test_run_counts_search_errors_as_not_found(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")

    async def _boom(_entry: BatchEntry, _site: str | None) -> list[SearchResult]:
        raise RuntimeError("site down")

    monkeypatch.setattr(batch, "_search_one", _boom)
    not_found = tmp_path / "not_found.txt"

    args = _parse_args(
        "--file", str(source), "--site", "b520", "--not-found", str(not_found)
    )
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1
    assert any("Search failed for '斗破苍穹'" in line for line in messages.warn)
    assert "斗破苍穹 天蚕土豆" in not_found.read_text(encoding="utf-8")


def test_missing_input_file_is_a_usage_error(
    tmp_path: Path,
    offline: Path,
) -> None:
    args = _parse_args("--file", str(tmp_path / "absent.txt"))

    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# run(): --from-not-found round trip
# ---------------------------------------------------------------------------


def test_from_not_found_reuses_the_header_site(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _write(
        tmp_path / "prev.txt",
        "# site=b909 searched_at=2026-01-01T00:00:00+00:00 matches=1/2\n"
        "斗破苍穹 天蚕土豆\n"
        "#\n",
    )
    calls = _install_search(monkeypatch, {"斗破苍穹": [_result(site="b909")]})

    args = _parse_args(
        "--from-not-found", str(previous), "--not-found", str(tmp_path / "nf.txt")
    )
    BatchCmd.run(args)

    assert calls == [("斗破苍穹", "b909")]
    assert any("Using site: b909" in line for line in messages.info)


def test_from_not_found_with_all_header_searches_every_site(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _write(
        tmp_path / "prev.txt",
        "# site=all searched_at=2026-01-01T00:00:00+00:00 matches=0/1\n"
        "斗破苍穹 天蚕土豆\n"
        "#\n",
    )
    calls = _install_search(monkeypatch, {"斗破苍穹": [_result()]})

    args = _parse_args(
        "--from-not-found", str(previous), "--not-found", str(tmp_path / "nf.txt")
    )
    BatchCmd.run(args)

    assert calls == [("斗破苍穹", None)]
    assert not any("Using site:" in line for line in messages.info)


def test_explicit_site_overrides_the_not_found_header(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _write(
        tmp_path / "prev.txt",
        "# site=b909 searched_at=2026-01-01T00:00:00+00:00 matches=0/1\n"
        "斗破苍穹 天蚕土豆\n"
        "#\n",
    )
    calls = _install_search(monkeypatch, {"斗破苍穹": [_result(site="other")]})

    args = _parse_args(
        "--from-not-found",
        str(previous),
        "--site",
        "other",
        "--not-found",
        str(tmp_path / "nf.txt"),
    )
    BatchCmd.run(args)

    assert calls == [("斗破苍穹", "other")]
    assert any("Using site: other" in line for line in messages.info)


# ---------------------------------------------------------------------------
# run(): matching details
# ---------------------------------------------------------------------------


def test_verbose_reports_candidate_count(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    _install_search(
        monkeypatch,
        {
            "斗破苍穹": [
                _result(book_id="1", author="别的作者"),
                _result(book_id="2", author="天蚕土豆"),
            ]
        },
    )

    args = _parse_args(
        "--file",
        str(source),
        "--site",
        "b520",
        "--verbose",
        "--not-found",
        str(tmp_path / "nf"),
    )
    BatchCmd.run(args)

    assert any("斗破苍穹 (天蚕土豆): 2 candidate(s)" in line for line in messages.info)


def test_strict_match_flag_turns_author_mismatch_into_not_found(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    calls = _install_search(monkeypatch, {"斗破苍穹": [_result(author="另一个作者")]})
    not_found = tmp_path / "nf.txt"

    args = _parse_args(
        "--file",
        str(source),
        "--site",
        "b520",
        "--strict-match",
        "--not-found",
        str(not_found),
    )
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1
    assert calls == [("斗破苍穹", "b520")]
    assert "斗破苍穹 天蚕土豆" in not_found.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# run(): --download plumbing
# ---------------------------------------------------------------------------


def test_download_groups_matches_by_site(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(
        tmp_path / "books.txt",
        "书甲 作者甲\n书乙 作者乙\n",
    )
    _install_search(
        monkeypatch,
        {
            "书甲": [_result(site="s1", book_id="1", title="书甲", author="作者甲")],
            "书乙": [_result(site="s2", book_id="2", title="书乙", author="作者乙")],
        },
    )
    downloaded: list[tuple[str, list[str]]] = []

    def _fake_download(_adapter: Any, site: str, book_ids: Any) -> batch.DownloadReport:
        downloaded.append((site, list(book_ids)))
        return batch.DownloadReport()

    monkeypatch.setattr(batch, "_download_site", _fake_download)

    args = _parse_args(
        "--file", str(source), "--download", "--not-found", str(tmp_path / "nf")
    )
    BatchCmd.run(args)

    assert downloaded == [("s1", ["1"]), ("s2", ["2"])]


def test_download_failure_exits_non_zero(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    monkeypatch.setattr(
        batch,
        "_download_site",
        lambda *_args: batch.DownloadReport(site_failures=1),
    )

    args = _parse_args(
        "--file", str(source), "--download", "--not-found", str(tmp_path / "nf")
    )
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1


def test_out_overrides_the_output_directory(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    seen: dict[str, Path] = {}

    def _fake_download(
        adapter: Any, _site: str, _book_ids: Any
    ) -> batch.DownloadReport:
        seen["out"] = adapter.get_output_dir()
        return batch.DownloadReport()

    monkeypatch.setattr(batch, "_download_site", _fake_download)
    out = tmp_path / "custom"

    args = _parse_args(
        "--file",
        str(source),
        "--download",
        "--out",
        str(out),
        "--not-found",
        str(tmp_path / "nf"),
    )
    BatchCmd.run(args)

    assert seen["out"] == out.resolve()
    assert out.is_dir()


# ---------------------------------------------------------------------------
# Adversarial review: input encodings, files and paths
# ---------------------------------------------------------------------------


def test_file_with_utf8_bom_is_parsed_without_a_bom_in_the_title(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows Notepad writes UTF-8 with BOM; the first title must stay clean."""
    source = tmp_path / "books.txt"
    source.write_bytes("\ufeff斗破苍穹 天蚕土豆\n".encode())
    calls = _install_search(monkeypatch, {"斗破苍穹": [_result()]})

    args = _parse_args("--file", str(source), "--not-found", str(tmp_path / "nf"))
    BatchCmd.run(args)

    assert calls == [("斗破苍穹", None)]


def test_gbk_file_fails_cleanly_instead_of_searching_mojibake(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy-Notepad GBK file is rejected with an error, not decoded as latin."""
    source = tmp_path / "gbk.txt"
    source.write_bytes("斗破苍穹 天蚕土豆\n".encode("gbk"))
    calls = _install_search(monkeypatch, {})

    args = _parse_args("--file", str(source))
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1
    assert calls == []
    assert any("Could not read" in line for line in messages.error)


def test_binary_input_fails_cleanly(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
) -> None:
    source = tmp_path / "noise.bin"
    source.write_bytes(b"\x00\x01\xff\xfe\x80binary")

    args = _parse_args("--file", str(source))
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1
    assert any("Could not read" in line for line in messages.error)


def test_parse_accepts_cr_crlf_lf_and_nbsp_separators() -> None:
    parsed = parse_batch_text(
        "书甲 作者甲\r\n书乙\t作者乙\r书丙\u00a0作者丙\n书丁\u3000作者丁\n"
    )

    assert [(e.title, e.author) for e in parsed.entries] == [
        ("书甲", "作者甲"),
        ("书乙", "作者乙"),
        ("书丙", "作者丙"),
        ("书丁", "作者丁"),
    ]


def test_parse_author_only_line_is_treated_as_a_title_only_entry() -> None:
    """Documents the known ambiguity: a lone token is always a title."""
    parsed = parse_batch_text("  天蚕土豆\n")

    assert [(e.title, e.author) for e in parsed.entries] == [("天蚕土豆", None)]
    assert parsed.malformed == []


def test_parse_title_containing_spaces_is_split_at_the_first_space() -> None:
    """Without a Tab the whitespace heuristic is unchanged (regression)."""
    parsed = parse_batch_text("斗破 苍穹 天蚕土豆\n")

    assert [(e.title, e.author) for e in parsed.entries] == [("斗破", "苍穹 天蚕土豆")]


def test_parse_tab_is_the_explicit_title_author_separator() -> None:
    parsed = parse_batch_text("斗破 苍穹\t天蚕土豆\n")

    assert [(e.title, e.author) for e in parsed.entries] == [("斗破 苍穹", "天蚕土豆")]


def test_parse_tab_form_strips_outer_whitespace_from_both_fields() -> None:
    parsed = parse_batch_text("  斗破 苍穹 \t 天蚕土豆 \n")

    assert [(e.title, e.author) for e in parsed.entries] == [("斗破 苍穹", "天蚕土豆")]


def test_parse_tab_with_empty_author_is_title_only() -> None:
    parsed = parse_batch_text("书名\t\n")

    assert [(e.title, e.author) for e in parsed.entries] == [("书名", None)]


def test_parse_tab_form_keeps_no_tab_lines_byte_identical() -> None:
    """The tab branch must not change any line that has no tab in it."""
    lines = [
        "斗破苍穹 天蚕土豆",
        "凡人修仙传   忘语",
        "独行月球",
        "  诡秘之主\u3000爱潜水的乌贼  ",
        "书三\u00a0作者三",
    ]
    parsed = parse_batch_text("\n".join(lines) + "\n")

    assert [(e.title, e.author) for e in parsed.entries] == [
        ("斗破苍穹", "天蚕土豆"),
        ("凡人修仙传", "忘语"),
        ("独行月球", None),
        ("诡秘之主", "爱潜水的乌贼"),
        ("书三", "作者三"),
    ]
    assert parsed.malformed == []


def test_parse_one_megabyte_line_is_clipped_not_kept_in_memory() -> None:
    parsed = parse_batch_text("长" * (1024 * 1024) + " 作者\n")

    assert parsed.entries == []
    assert len(parsed.malformed) == 1
    assert len(parsed.malformed[0].text) == 80


def test_not_found_round_trip_is_idempotent(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write, re-read and write again: the entry body must stay identical."""
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    current = _write(tmp_path / "in.txt", "斗破苍穹 天蚕土豆\n未找到书 作者\n")
    target = tmp_path / "nf.txt"
    seen: list[list[str]] = []

    # Cycle 1 reads the user file; cycles 2-3 use the documented
    # ``--from-not-found`` retry, including an in-place rewrite of ``target``.
    for cycle in range(3):
        flag = "--file" if cycle == 0 else "--from-not-found"
        try:
            BatchCmd.run(_parse_args(flag, str(current), "--not-found", str(target)))
        except SystemExit as exc:
            assert exc.code == 1  # the unmatched entry keeps the run partial
        body = [
            line
            for line in target.read_text(encoding="utf-8").splitlines()
            if not line.startswith("#")
        ]
        seen.append(body)
        current = target

    assert seen == [["未找到书 作者"]] * 3
    assert "# site=all" in target.read_text(encoding="utf-8").splitlines()[0]


def test_not_found_round_trip_preserves_titles_with_spaces(tmp_path: Path) -> None:
    """A Tab-parsed title must be written back in the Tab form, not re-split."""
    path = tmp_path / "nf.txt"
    entries = [
        BatchEntry(line_no=1, title="斗破 苍穹", author="天蚕土豆"),
        BatchEntry(line_no=2, title="独行 月球", author=None),
    ]

    batch._write_not_found(
        path,
        entries=entries,
        malformed=[],
        site="b520",
        matched=1,
        total=3,
    )

    body = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    assert body == ["斗破 苍穹\t天蚕土豆", "独行 月球\t"]
    reparsed = parse_batch_text(path.read_text(encoding="utf-8"))
    assert [(e.title, e.author) for e in reparsed.entries] == [
        ("斗破 苍穹", "天蚕土豆"),
        ("独行 月球", None),
    ]


def test_not_found_file_creates_missing_directories(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "未找到书 作者\n")
    _install_search(monkeypatch, {})
    target = tmp_path / "deep" / "nested" / "nf.txt"

    with pytest.raises(SystemExit):
        BatchCmd.run(_parse_args("--file", str(source), "--not-found", str(target)))

    assert "未找到书 作者" in target.read_text(encoding="utf-8")


def test_unwritable_not_found_path_is_reported_without_a_traceback(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "未找到书 作者\n")
    _install_search(monkeypatch, {})
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    downloaded: list[str] = []
    monkeypatch.setattr(
        batch,
        "_download_site",
        lambda *_args: downloaded.append("called") or batch.DownloadReport(),
    )

    args = _parse_args(
        "--file",
        str(source),
        "--download",
        "--not-found",
        str(blocker / "nf.txt"),
    )
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1
    assert any("Could not write" in line for line in messages.error)
    # Failing to persist the results aborts before the downloads start.
    assert downloaded == []


def test_refuses_to_overwrite_the_input_file(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "未找到书 作者\n"
    source = _write(tmp_path / "books.txt", original)
    _install_search(monkeypatch, {})

    args = _parse_args("--file", str(source), "--not-found", str(source))
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 2
    assert source.read_text(encoding="utf-8") == original
    assert any("Refusing to overwrite" in line for line in messages.error)


def test_existing_not_found_file_is_overwritten_without_force(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "新书 新作者\n")
    _install_search(monkeypatch, {})
    target = _write(
        tmp_path / "nf.txt",
        "# site=all searched_at=old matches=0/1\n旧书 旧作者\n#\n",
    )

    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(_parse_args("--file", str(source), "--not-found", str(target)))

    assert excinfo.value.code == 1
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# site=all searched_at=")
    assert "新书 新作者" in text
    assert "旧书" not in text


def test_foreign_existing_file_is_refused_and_force_overwrites_it(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "新书 新作者\n")
    _install_search(monkeypatch, {})
    original = "我自己的书单\n斗破苍穹 天蚕土豆\n"
    target = _write(tmp_path / "notes.txt", original)

    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(_parse_args("--file", str(source), "--not-found", str(target)))

    assert excinfo.value.code == 2
    assert target.read_text(encoding="utf-8") == original
    assert any("not a not-found file" in line for line in messages.error)

    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(
            _parse_args("--file", str(source), "--not-found", str(target), "--force")
        )

    assert excinfo.value.code == 1  # the entry is still unmatched
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# site=all searched_at=")
    assert "新书 新作者" in text
    assert "我自己的书单" not in text


def test_plain_file_used_as_from_not_found_is_not_truncated_in_place(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--from-not-found only grants in-place rewrite for real not-found files."""
    original = "普通书单 某作者\n"
    source = _write(tmp_path / "plain.txt", original)
    _install_search(monkeypatch, {})

    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(
            _parse_args("--from-not-found", str(source), "--not-found", str(source))
        )

    assert excinfo.value.code == 2
    assert source.read_text(encoding="utf-8") == original
    assert any("not a not-found file" in line for line in messages.error)


def test_unwritable_out_directory_is_reported_without_a_traceback(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    args = _parse_args(
        "--file",
        str(source),
        "--download",
        "--out",
        str(blocker / "out"),
        "--not-found",
        str(tmp_path / "nf"),
    )
    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(args)

    assert excinfo.value.code == 1
    assert any("Could not create" in line for line in messages.error)


def test_site_key_cannot_escape_the_not_found_directory(tmp_path: Path) -> None:
    for site in ("../evil", "..\\evil", "a/b", "CON"):
        path = batch._not_found_path(None, site)
        assert path.parent == batch._NOT_FOUND_DIR
        assert "/" not in path.name and "\\" not in path.name


# ---------------------------------------------------------------------------
# Adversarial review: matching edge cases
# ---------------------------------------------------------------------------


def test_select_match_strict_prefers_the_exact_author_among_same_titles() -> None:
    other = _result(book_id="1", author="别人")
    exact = _result(book_id="2", author="天蚕土豆")

    outcome = select_match(_entry("斗破苍穹", "天蚕土豆"), [other, exact], strict=True)

    assert outcome.result is exact
    assert outcome.author_mismatch is False


def test_select_match_empty_result_author_matches_only_when_not_strict() -> None:
    hit = _result(author="")

    loose = select_match(_entry("斗破苍穹", "天蚕土豆"), [hit])
    strict = select_match(_entry("斗破苍穹", "天蚕土豆"), [hit], strict=True)

    assert loose.result is hit
    assert loose.author_mismatch is True
    assert strict.result is None
    assert strict.author_mismatch is True


def test_select_match_does_not_fold_traditional_and_simplified_titles() -> None:
    """Documents the current limit: 繁简 variants do not match."""
    traditional = _result(title="鬥破蒼穹")

    outcome = select_match(_entry("斗破苍穹", "天蚕土豆"), [traditional])

    assert outcome.result is None
    assert outcome.candidates == 0


def test_select_match_takes_the_best_ranked_of_duplicate_sites() -> None:
    best = _result(site="b520", book_id="1")
    duplicate = _result(site="biquge", book_id="2")

    outcome = select_match(_entry("斗破苍穹", "天蚕土豆"), [best, duplicate])

    assert outcome.result is best
    assert outcome.candidates == 2


# ---------------------------------------------------------------------------
# Adversarial review: --download failure isolation and interruption
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def get_client_config(self, _site: str) -> object:
        return object()

    def get_login_required(self, _site: str) -> bool:
        return False

    def get_login_config(self, _site: str) -> dict[str, Any]:
        return {}

    def get_processor_configs(self, _site: str) -> list[Any]:
        return []

    def get_exporter_config(self, _site: str) -> Any:
        return None

    def get_export_fmt(self, _site: str) -> list[str]:
        return ["txt"]

    def get_output_dir(self) -> Path:
        return Path("out")

    def get_raw_data_dir(self) -> Path:
        return Path("raw")


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_download: frozenset[str] = frozenset(),
    fail_process: frozenset[str] = frozenset(),
    fail_export: frozenset[str] = frozenset(),
    incomplete: dict[str, int] | None = None,
) -> list[tuple[str, str]]:
    """
    Install a fake site client and record every per-book step.

    ``incomplete`` maps a book id to the number of failed chapters its
    ``download_book`` summary reports.
    """
    events: list[tuple[str, str]] = []
    chapters_missing = incomplete or {}

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def download_book(self, book: Any, ui: Any = None) -> Any:
            events.append(("download", book.book_id))
            if book.book_id in fail_download:
                raise RuntimeError(f"download boom {book.book_id}")
            if ui is not None:
                await ui.on_complete(book)
            return {"failed": chapters_missing.get(book.book_id, 0)}

        def process_book(
            self, book: Any, processors: Any = None, ui: Any = None
        ) -> None:
            events.append(("process", book.book_id))
            if book.book_id in fail_process:
                raise RuntimeError(f"process boom {book.book_id}")

        def export_book(
            self, book: Any, cfg: Any = None, formats: Any = None, ui: Any = None
        ) -> None:
            events.append(("export", book.book_id))
            if book.book_id in fail_export:
                raise RuntimeError(f"export boom {book.book_id}")

    monkeypatch.setattr(batch.registrar, "get_client", lambda _site, _cfg: _Client())
    return events


def test_download_exception_is_isolated_per_book(
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising book 3 must not stop books 4-10 from downloading."""
    events = _install_fake_client(monkeypatch, fail_download=frozenset({"3"}))

    report = batch._download_site(_FakeAdapter(), "s1", [str(i) for i in range(1, 11)])

    assert report.ok is False
    assert report.site_failures == 1
    assert report.incomplete_books == 0
    assert [bid for kind, bid in events if kind == "download"] == [
        str(i) for i in range(1, 11)
    ]
    assert [bid for kind, bid in events if kind == "export"] == [
        bid for bid in (str(i) for i in range(1, 11)) if bid != "3"
    ]
    assert any("Download failed for book 3" in line for line in messages.error)


def test_export_exception_is_isolated_per_book(
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising export for book 3 must not skip books 4 and 5."""
    events = _install_fake_client(monkeypatch, fail_export=frozenset({"3"}))

    report = batch._download_site(_FakeAdapter(), "s1", ["1", "2", "3", "4", "5"])

    assert report.ok is False
    assert report.site_failures == 1
    assert [bid for kind, bid in events if kind == "export"] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert any("Process/export failed for book 3" in line for line in messages.error)


def test_incomplete_chapters_are_reported_separately_from_a_failure(
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A summary with failed chapters is a warning, not a hard failure."""
    events = _install_fake_client(monkeypatch, incomplete={"2": 3})

    report = batch._download_site(_FakeAdapter(), "s1", ["1", "2", "3"])

    assert report.ok is False
    assert report.site_failures == 0
    assert report.incomplete_books == 1
    # The book still went through process and export.
    assert [bid for kind, bid in events if kind == "export"] == ["1", "2", "3"]
    assert any(
        "Book 2 downloaded with 3 incomplete chapter(s)." in line
        for line in messages.warn
    )
    assert not any("Download failed for book 2" in line for line in messages.error)


def test_incomplete_chapters_make_the_batch_exit_non_zero(
    tmp_path: Path,
    offline: Path,
    messages: _Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    monkeypatch.setattr(
        batch,
        "_download_site",
        lambda *_args: batch.DownloadReport(incomplete_books=2),
    )

    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(
            _parse_args(
                "--file",
                str(source),
                "--download",
                "--not-found",
                str(tmp_path / "nf"),
            )
        )

    assert excinfo.value.code == 1
    # The summary line distinguishes "downloaded but incomplete" from a miss.
    assert any(
        "Downloaded with incomplete chapters: 2 book(s)." in line
        for line in messages.warn
    )
    assert any("not found: 0" in line for line in messages.info)


def test_download_success_exits_zero(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    monkeypatch.setattr(batch, "_download_site", lambda *_args: batch.DownloadReport())

    BatchCmd.run(
        _parse_args(
            "--file",
            str(source),
            "--download",
            "--not-found",
            str(tmp_path / "nf"),
        )
    )  # no SystemExit


def test_interrupt_during_download_keeps_the_not_found_file(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n未找到书 作者\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    target = tmp_path / "nf.txt"

    def _interrupt(_adapter: Any, _matches: Any) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(batch, "_download_matches", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        BatchCmd.run(
            _parse_args(
                "--file",
                str(source),
                "--download",
                "--not-found",
                str(target),
            )
        )

    # The unmatched entry was persisted before the download started.
    assert "未找到书 作者" in target.read_text(encoding="utf-8")


def test_searches_run_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch must not fan out books: no two searches may overlap."""
    import asyncio

    active = 0
    peak = 0
    order: list[str] = []

    async def _search(entry: BatchEntry, _site: str | None) -> list[SearchResult]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        order.append(entry.title)
        await asyncio.sleep(0)
        active -= 1
        return []

    monkeypatch.setattr(batch, "_search_one", _search)
    entries = [_entry(f"书{i}", "作者") for i in range(5)]

    asyncio.run(batch._match_entries(entries, site=None, strict=False, verbose=False))

    assert peak == 1
    assert order == [f"书{i}" for i in range(5)]


def test_interval_sleeps_between_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def _search(_entry: BatchEntry, _site: str | None) -> list[SearchResult]:
        return []

    monkeypatch.setattr(batch.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(batch, "_search_one", _search)
    entries = [_entry(f"书{i}", "作者") for i in range(3)]

    asyncio.run(
        batch._match_entries(
            entries, site=None, strict=False, verbose=False, interval=0.25
        )
    )

    # Between entries only: no sleep before the first or after the last.
    assert sleeps == [0.25, 0.25]


def test_interval_defaults_to_no_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:  # pragma: no cover - unused
        sleeps.append(seconds)

    async def _search(_entry: BatchEntry, _site: str | None) -> list[SearchResult]:
        return []

    monkeypatch.setattr(batch.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(batch, "_search_one", _search)

    asyncio.run(
        batch._match_entries(
            [_entry("书甲", "作者"), _entry("书乙", "作者")],
            site=None,
            strict=False,
            verbose=False,
        )
    )

    assert sleeps == []


def test_negative_interval_is_a_usage_error(tmp_path: Path) -> None:
    source = _write(tmp_path / "books.txt", "书 作者\n")

    with pytest.raises(SystemExit) as excinfo:
        BatchCmd.run(_parse_args("--file", str(source), "--interval", "-1"))

    assert excinfo.value.code == 2


def test_out_moves_the_raw_data_directory_with_the_exports(
    tmp_path: Path,
    offline: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--out retargets both the exports and the raw cache (DIR/raw_data)."""
    source = _write(tmp_path / "books.txt", "斗破苍穹 天蚕土豆\n")
    _install_search(monkeypatch, {"斗破苍穹": [_result()]})
    seen: dict[str, Path] = {}

    def _fake_download(
        adapter: Any, _site: str, _book_ids: Any
    ) -> batch.DownloadReport:
        seen["out"] = adapter.get_output_dir()
        seen["raw"] = adapter.get_raw_data_dir()
        return batch.DownloadReport()

    monkeypatch.setattr(batch, "_download_site", _fake_download)
    out = tmp_path / "custom"

    BatchCmd.run(
        _parse_args(
            "--file",
            str(source),
            "--download",
            "--out",
            str(out),
            "--not-found",
            str(tmp_path / "nf"),
        )
    )

    assert seen["out"] == out.resolve()
    assert seen["raw"] == (out / "raw_data").resolve()


# ---------------------------------------------------------------------------
# Adversarial review: exit codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("--file", "a.txt", "--from-not-found", "b.txt"),
        (),
    ],
)
def test_conflicting_or_missing_source_is_a_usage_error(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse_args(*argv)

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_batch_is_registered_and_help_lists_its_options(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from novel_downloader.apps.cli import main as cli_main_module

    monkeypatch.setattr(sys, "argv", ["novel-cli", "batch", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for token in (
        "--file",
        "--from-not-found",
        "--site",
        "--out",
        "--strict-match",
        "--not-found",
        "--force",
        "--download",
        "--interval",
        "--verbose",
    ):
        assert token in out
    # The single-field / Tab rules are part of the --file contract.
    flat = " ".join(out.split())
    assert "single field is the title" in flat
    assert "Tab between title and author" in flat


def test_batch_is_offered_by_shell_completion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from novel_downloader.apps.cli import main as cli_main_module

    monkeypatch.setattr(sys, "argv", ["novel-cli", "completion", "--shell", "bash"])

    cli_main_module.cli_main()

    out = capsys.readouterr().out
    assert "batch" in out
    assert "--from-not-found" in out
