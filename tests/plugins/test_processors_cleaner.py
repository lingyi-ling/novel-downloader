#!/usr/bin/env python3
"""
Offline, deterministic tests for
:mod:`novel_downloader.plugins.processors.cleaner`.

Covers the documented behaviour from ``docs/guide/processors-reference.md``
(JSON rule files, invisible-character removal), the edge cases that matter for
the pipeline (empty/whitespace/very long/special-character text, malformed
configs) plus idempotence and input immutability.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins.processors.cleaner import CleanerProcessor
from tests.plugins._processor_test_utils import make_book_info, make_chapter

CLEANER_LOGGER = "novel_downloader.plugins.processors.cleaner"


def write_json(tmp_path: Path, name: str, payload: Any) -> str:
    """Write ``payload`` as UTF-8 JSON and return the file path as a string."""
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# documented examples
# --------------------------------------------------------------------------- #
def test_docs_example_title_removes_and_content_replace(tmp_path: Path) -> None:
    """The exact rule files from the reference doc drive the documented flow."""
    title_removes = write_json(
        tmp_path,
        "title-remove.json",
        [r"\[广告\]", r"\(无弹窗小说网\)", "PS：.*$"],
    )
    content_replace = write_json(
        tmp_path,
        "content-replace.json",
        {
            "请记住本书首发网址": "",
            "(本章完)": "",
            "li子": "例子",
            "pinbi词": "屏蔽词",
        },
    )
    proc = CleanerProcessor(
        {
            "remove_invisible": True,
            "title_removes": title_removes,
            "content_replace": content_replace,
        }
    )

    chapter = proc.process_chapter(
        make_chapter(
            title="[广告]第1章 开始(无弹窗小说网)",
            content="请记住本书首发网址：起点\nli子与pinbi词(本章完)",
        )
    )

    assert chapter["title"] == "第1章 开始"
    # Replacement keys are literal, not regexes: only the prefix is cut.
    assert chapter["content"] == "：起点\n例子与屏蔽词"


def test_title_remove_regex_is_anchored_to_line(tmp_path: Path) -> None:
    title_removes = write_json(tmp_path, "title-remove.json", ["PS：.*$"])
    proc = CleanerProcessor({"title_removes": title_removes})

    chapter = proc.process_chapter(make_chapter(title="第一章PS：求月票"))
    assert chapter["title"] == "第一章"


def test_content_removes_are_multiline(tmp_path: Path) -> None:
    content_removes = write_json(tmp_path, "content-remove.json", ["PS：.*$"])
    proc = CleanerProcessor({"content_removes": content_removes})

    chapter = proc.process_chapter(make_chapter(content="第一行\nPS：求推荐票\n第三行"))
    assert chapter["content"] == "第一行\n\n第三行"


def test_content_and_title_rules_do_not_leak_into_each_other(
    tmp_path: Path,
) -> None:
    title_removes = write_json(tmp_path, "t.json", ["广告"])
    content_removes = write_json(tmp_path, "c.json", ["正文"])
    proc = CleanerProcessor(
        {"title_removes": title_removes, "content_removes": content_removes}
    )

    chapter = proc.process_chapter(make_chapter(title="广告正文", content="广告正文"))
    assert chapter["title"] == "正文"
    assert chapter["content"] == "广告"


# --------------------------------------------------------------------------- #
# invisible characters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("char", ["\ufeff", "\u200b", "\u200c", "\u200d", "\u2060"])
def test_remove_invisible_is_on_by_default(char: str) -> None:
    proc = CleanerProcessor({})

    assert (
        proc.process_chapter(make_chapter(content=f"前{char}后"))["content"] == "前后"
    )
    assert proc.process_chapter(make_chapter(title=f"前{char}后"))["title"] == "前后"


def test_remove_invisible_can_be_disabled() -> None:
    proc = CleanerProcessor({"remove_invisible": False})

    chapter = proc.process_chapter(make_chapter(content="\u200b正文 "))
    # Whitespace is still stripped, but the zero-width character survives.
    assert chapter["content"] == "\u200b正文"


def test_remove_invisible_removes_stray_bom_mid_text() -> None:
    proc = CleanerProcessor({})

    book_info = proc.process_book_info(make_book_info(book_name="书名\ufeff续"))
    assert book_info["book_name"] == "书名续"


# --------------------------------------------------------------------------- #
# empty / whitespace / long / special text
# --------------------------------------------------------------------------- #
def test_empty_strings_are_untouched() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title="", content=""))
    assert chapter["title"] == ""
    assert chapter["content"] == ""

    book_info = proc.process_book_info(make_book_info(book_name="", summary=""))
    assert book_info["book_name"] == ""
    assert book_info["summary"] == ""


@pytest.mark.parametrize("blank", [" ", "\n", "\t\r\n  ", "\u3000\u3000"])
def test_whitespace_only_text_collapses_to_empty(blank: str) -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title=blank, content=blank))
    assert chapter["title"] == ""
    assert chapter["content"] == ""


def test_very_long_text_is_cleaned_deterministically(tmp_path: Path) -> None:
    content_removes = write_json(tmp_path, "c.json", ["广告"])
    proc = CleanerProcessor({"content_removes": content_removes})

    payload = ("广告" * 5_000) + "\n" + ("正文" * 50_000)
    chapter = make_chapter(content=payload)

    first = proc.process_chapter(chapter)
    second = proc.process_chapter(chapter)

    assert first == second
    # The leading newline is removed by the trailing ``strip()``.
    assert first["content"] == "正文" * 50_000
    assert len(first["content"]) == 100_000


def test_replacement_keys_are_escaped_regex_literals(tmp_path: Path) -> None:
    content_replace = write_json(tmp_path, "c.json", {"a.b*c": "X"})
    proc = CleanerProcessor({"content_replace": content_replace})

    # The literal sequence is replaced ...
    assert proc.process_chapter(make_chapter(content="a.b*c"))["content"] == "X"
    # ... but a text that merely *matches* it as a regex is not.
    assert proc.process_chapter(make_chapter(content="aXbbc"))["content"] == "aXbbc"


def test_newlines_and_special_characters_survive_cleaning() -> None:
    proc = CleanerProcessor({})

    content = "第一行\n\n第二行\r\n\t缩进 $^.*+?()[]{}\nemoji 🐉 组合 é"
    assert proc.process_chapter(make_chapter(content=content))["content"] == (
        content.strip()
    )


def test_combining_marks_are_preserved() -> None:
    proc = CleanerProcessor({})

    text = "e\u0301cole"
    assert proc.process_chapter(make_chapter(content=text))["content"] == text


# --------------------------------------------------------------------------- #
# book info structure
# --------------------------------------------------------------------------- #
def test_book_info_title_and_content_rules_apply_to_nested_fields(
    tmp_path: Path,
) -> None:
    title_replace = write_json(tmp_path, "tr.json", {"广告": "", "[卷]": ""})
    content_replace = write_json(tmp_path, "cr.json", {"简介": "提要"})
    proc = CleanerProcessor(
        {"title_replace": title_replace, "content_replace": content_replace}
    )

    book_info = proc.process_book_info(
        make_book_info(
            book_name="广告书名",
            author="广告作者",
            summary="简介文本",
            tags=["广告", "正常"],
            volumes=[
                {
                    "volume_name": "[卷]第一卷",
                    "volume_intro": "简介",
                    "chapters": [
                        {
                            "title": "广告第1章",
                            "url": "https://example.com/1",
                            "chapterId": "1",
                        }
                    ],
                }
            ],
        )
    )

    assert book_info["book_name"] == "书名"
    assert book_info["author"] == "作者"
    assert book_info["summary"] == "提要文本"
    assert book_info["tags"] == ["", "正常"]
    volume = book_info["volumes"][0]
    assert volume["volume_name"] == "第一卷"
    assert volume["volume_intro"] == "提要"
    assert volume["chapters"][0]["title"] == "第1章"


def test_non_string_fields_are_left_alone(tmp_path: Path) -> None:
    title_removes = write_json(tmp_path, "r.json", ["广告"])
    proc = CleanerProcessor({"title_removes": title_removes})

    book_info = proc.process_book_info(
        make_book_info(book_name=None, summary=None, tags=[None, "广告"])
    )

    assert book_info["book_name"] is None
    assert book_info["summary"] is None
    assert book_info["tags"] == [None, ""]


# --------------------------------------------------------------------------- #
# malformed configuration
# --------------------------------------------------------------------------- #
def test_missing_rule_file_is_ignored_with_warning(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    missing = str(tmp_path / "does-not-exist.json")

    with caplog.at_level(logging.WARNING, logger=CLEANER_LOGGER):
        proc = CleanerProcessor({"title_removes": missing})

    assert "cannot load removal list" in caplog.text
    assert proc.process_chapter(make_chapter(title="原样"))["title"] == "原样"


def test_invalid_json_is_ignored_with_warning(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=CLEANER_LOGGER):
        proc = CleanerProcessor({"content_replace": str(bad)})

    assert "cannot load replacement map" in caplog.text
    assert proc.process_chapter(make_chapter(content="原样"))["content"] == "原样"


def test_directory_instead_of_file_is_ignored_with_warning(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with caplog.at_level(logging.WARNING, logger=CLEANER_LOGGER):
        proc = CleanerProcessor({"title_removes": str(tmp_path)})

    assert "cannot load removal list" in caplog.text
    assert proc.process_chapter(make_chapter(title="原样"))["title"] == "原样"


def test_json_object_where_array_expected_is_ignored(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    path = write_json(tmp_path, "wrong.json", {"广告": ""})

    with caplog.at_level(logging.WARNING, logger=CLEANER_LOGGER):
        proc = CleanerProcessor({"title_removes": path})

    assert "must contain a JSON array" in caplog.text
    assert proc.process_chapter(make_chapter(title="广告"))["title"] == "广告"


def test_json_array_where_object_expected_is_ignored(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    path = write_json(tmp_path, "wrong.json", ["广告"])

    with caplog.at_level(logging.WARNING, logger=CLEANER_LOGGER):
        proc = CleanerProcessor({"content_replace": path})

    assert "must contain a JSON object" in caplog.text
    assert proc.process_chapter(make_chapter(content="广告"))["content"] == "广告"


def test_non_string_entries_are_dropped(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A malformed array must not blow up ``re.compile`` with a TypeError."""
    path = write_json(tmp_path, "mixed.json", ["广告", 1, None, {"a": 1}])

    with caplog.at_level(logging.WARNING, logger=CLEANER_LOGGER):
        proc = CleanerProcessor({"content_removes": path})

    assert "ignoring non-string entry" in caplog.text
    assert proc.process_chapter(make_chapter(content="广告正文"))["content"] == "正文"


def test_non_string_replacement_values_are_dropped(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A JSON ``null`` must never become the literal text ``"None"``."""
    path = write_json(tmp_path, "mixed.json", {"a": None, "b": "B", "c": 3})

    with caplog.at_level(logging.WARNING, logger=CLEANER_LOGGER):
        proc = CleanerProcessor({"content_replace": path})

    assert "ignoring non-string mapping entry" in caplog.text
    result = proc.process_chapter(make_chapter(content="a b c"))["content"]
    assert result == "a B c"
    assert "None" not in result


def test_invalid_removal_regex_raises_clear_value_error(tmp_path: Path) -> None:
    path = write_json(tmp_path, "bad-regex.json", ["[unclosed"])

    with pytest.raises(ValueError, match="invalid removal regex"):
        CleanerProcessor({"title_removes": path})


def test_invalid_replacement_never_breaks_compilation(tmp_path: Path) -> None:
    """Replacement keys are escaped, so regex metacharacters stay safe."""
    path = write_json(tmp_path, "repl.json", {"[": "[", "*": "*", "\\": "\\"})

    proc = CleanerProcessor({"content_replace": path})
    assert proc.process_chapter(make_chapter(content="a[b*c\\d"))["content"] == (
        "a[b*c\\d"
    )


def test_default_config_performs_only_basic_cleanup() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title="  标题  ", content=" 正文 "))
    assert chapter["title"] == "标题"
    assert chapter["content"] == "正文"


def test_blank_path_values_are_treated_as_unset() -> None:
    proc = CleanerProcessor({"title_removes": "", "content_replace": ""})

    assert proc.process_chapter(make_chapter(title="广告"))["title"] == "广告"


# --------------------------------------------------------------------------- #
# rule precedence
# --------------------------------------------------------------------------- #
def test_longest_literal_wins(tmp_path: Path) -> None:
    path = write_json(tmp_path, "repl.json", {"ab": "X", "abc": "Y"})
    proc = CleanerProcessor({"title_replace": path})

    assert proc.process_chapter(make_chapter(title="abc"))["title"] == "Y"
    assert proc.process_chapter(make_chapter(title="ab"))["title"] == "X"


def test_replacement_map_wins_over_removal_for_the_same_literal(
    tmp_path: Path,
) -> None:
    """Existing precedence: the substitution lookup happens before deletion."""
    removes = write_json(tmp_path, "removes.json", ["AB"])
    replaces = write_json(tmp_path, "replaces.json", {"AB": "Z"})
    proc = CleanerProcessor({"title_removes": removes, "title_replace": replaces})

    assert proc.process_chapter(make_chapter(title="AB"))["title"] == "Z"


def test_removal_patterns_still_win_for_non_replacement_text(
    tmp_path: Path,
) -> None:
    removes = write_json(tmp_path, "removes.json", ["AB", "CD"])
    replaces = write_json(tmp_path, "replaces.json", {"AB": "Z"})
    proc = CleanerProcessor({"title_removes": removes, "title_replace": replaces})

    assert proc.process_chapter(make_chapter(title="ABCD"))["title"] == "Z"


def test_duplicate_removal_patterns_are_deduped(tmp_path: Path) -> None:
    path = write_json(tmp_path, "dup.json", ["广告", "广告", "广告"])
    proc = CleanerProcessor({"content_removes": path})

    assert proc.process_chapter(make_chapter(content="广告"))["content"] == ""


# --------------------------------------------------------------------------- #
# idempotence / purity
# --------------------------------------------------------------------------- #
def test_cleaning_is_idempotent(tmp_path: Path) -> None:
    removes = write_json(tmp_path, "removes.json", ["广告"])
    replaces = write_json(tmp_path, "replaces.json", {"li子": "例子"})
    proc = CleanerProcessor({"content_removes": removes, "content_replace": replaces})

    once = proc.process_chapter(make_chapter(content="广告li子"))
    twice = proc.process_chapter(once)

    assert once == twice


def test_two_processor_instances_agree(tmp_path: Path) -> None:
    config = {"content_removes": write_json(tmp_path, "r.json", ["广告"])}

    first = CleanerProcessor(config).process_chapter(make_chapter(content="广告A"))
    second = CleanerProcessor(config).process_chapter(make_chapter(content="广告A"))

    assert first == second


def test_input_dicts_are_not_mutated(tmp_path: Path) -> None:
    removes = write_json(tmp_path, "r.json", ["广告"])
    proc = CleanerProcessor({"content_removes": removes})

    chapter = make_chapter(title="广告题", content="广告文")
    snapshot = json.loads(json.dumps(chapter))
    proc.process_chapter(chapter)
    assert chapter == snapshot

    book_info = make_book_info(book_name="广告书")
    book_snapshot = json.loads(json.dumps(book_info))
    proc.process_book_info(book_info)
    assert book_info == book_snapshot


def test_deeply_nested_structures_are_copied_not_shared(tmp_path: Path) -> None:
    proc = CleanerProcessor({})

    book_info = make_book_info()
    processed = proc.process_book_info(book_info)

    assert processed is not book_info
    processed["volumes"][0]["chapters"][0]["title"] = "改过的"
    assert book_info["volumes"][0]["chapters"][0]["title"] == "第1章 开始"


# --------------------------------------------------------------------------- #
# format characters (Cf / Co) via unicodedata
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "char",
    [
        "\ue000",  # private use, BMP start
        "\uf8ff",  # private use, BMP end
        "\u200b",  # zero width space (Cf)
        "\u200e",  # left-to-right mark (Cf)
        "\u202e",  # right-to-left override (Cf)
        "\ufeff",  # BOM / zero width no-break space (Cf)
        "\U000e0001",  # language tag (Cf, outside the BMP)
    ],
)
def test_format_chars_are_removed_by_default(char: str) -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(content=f"前{char}后"))
    assert chapter["content"] == "前后"
    assert proc.process_chapter(make_chapter(title=f"前{char}后"))["title"] == "前后"


def test_remove_format_chars_can_be_disabled_without_disabling_invisible() -> None:
    proc = CleanerProcessor({"remove_invisible": True, "remove_format_chars": False})

    # The legacy fixed set is still stripped ...
    assert proc.process_chapter(make_chapter(content="前\u200b后"))["content"] == "前后"
    # ... but private-use glyphs survive.
    cleaned = proc.process_chapter(make_chapter(content="前\ue000后"))
    assert cleaned["content"] == "前\ue000后"


def test_remove_format_chars_inherits_remove_invisible_false() -> None:
    """``remove_format_chars`` defaults to the value of ``remove_invisible``."""
    proc = CleanerProcessor({"remove_invisible": False})

    chapter = proc.process_chapter(make_chapter(content="a\ue000b\u200bc"))
    assert chapter["content"] == "a\ue000b\u200bc"


def test_remove_format_chars_can_be_enabled_on_its_own() -> None:
    proc = CleanerProcessor({"remove_invisible": False, "remove_format_chars": True})

    chapter = proc.process_chapter(make_chapter(content="a\ue000b\u200bc"))
    assert chapter["content"] == "abc"


def test_format_removal_leaves_normal_text_untouched() -> None:
    proc = CleanerProcessor({})

    text = "中文，标点！emoji 🐉 组合 e\u0301cole\t带空格 world"
    assert proc.process_chapter(make_chapter(content=text))["content"] == text


def test_format_removal_keeps_newlines_and_tabs() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(content="一\n\t二\r\n三"))
    assert chapter["content"] == "一\n\t二\r\n三"


# --------------------------------------------------------------------------- #
# duplicated leading chapter title
# --------------------------------------------------------------------------- #
def test_leading_duplicate_title_is_stripped_by_default() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(
        make_chapter(
            title="第1章 开始",
            content="第1章 开始\n\n正文第一段。",
        )
    )
    assert chapter["content"] == "正文第一段。"


def test_leading_title_strip_allows_leading_whitespace() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(
        make_chapter(title="第1章 开始", content="\n\n  第1章 开始\n正文")
    )
    assert chapter["content"] == "正文"


def test_leading_title_strip_matches_whitespace_removed_variant() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(
        make_chapter(title="第1章 开始", content="第1章开始\n正文")
    )
    assert chapter["content"] == "正文"


def test_leading_title_strip_escapes_regex_metacharacters() -> None:
    proc = CleanerProcessor({})

    title = "第1章 (上)[序] *"
    chapter = proc.process_chapter(make_chapter(title=title, content=f"{title}\n正文"))
    assert chapter["content"] == "正文"


def test_leading_title_strip_removes_following_colon() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(
        make_chapter(title="第1章 开始", content="第1章 开始：正文")
    )
    assert chapter["content"] == "正文"


def test_leading_title_strip_only_touches_the_start() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(
        make_chapter(title="第1章 开始", content="前面\n第1章 开始\n后面")
    )
    assert chapter["content"] == "前面\n第1章 开始\n后面"


def test_leading_title_strip_requires_a_boundary() -> None:
    """Prose starting with the title word must not lose its opening."""
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(
        make_chapter(title="开始", content="开始的时候，他还不知道。")
    )
    assert chapter["content"] == "开始的时候，他还不知道。"


def test_short_title_followed_by_punctuation_is_kept() -> None:
    """A 2-character title + comma is probably the first sentence, not a heading."""
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(
        make_chapter(title="开始", content="开始，他还不知道。")
    )
    assert chapter["content"] == "开始，他还不知道。"


@pytest.mark.parametrize(
    ("title", "content"),
    [
        ("第一次相遇", "第一次相遇，他紧张得说不出话。"),
        ("第一次相遇", "第一次相遇。他推开门。"),
        ("第1章 开始", "第1章 开始，他推开门。"),
        ("第1章 开始", "第1章 开始！他推开门。"),
        ("第1章 开始", "第1章 开始；然后。"),
    ],
)
def test_long_title_followed_by_sentence_punctuation_is_kept(
    title: str, content: str
) -> None:
    """Sentence punctuation means the title text opens the first sentence."""
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title=title, content=content))
    assert chapter["content"] == content


@pytest.mark.parametrize(
    "content",
    ["第一章 相遇", "第一章 相遇：", "第一章 相遇\n\n", "第一章 相遇：\n"],
)
def test_title_only_chapter_is_never_emptied(content: str) -> None:
    """Stripping the duplicate heading must not produce an empty body."""
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title="第一章 相遇", content=content))
    assert chapter["content"] != ""
    assert chapter["content"] == content.strip()


@pytest.mark.parametrize(
    ("title", "content"),
    [
        ("第1章\u3000相遇", "第1章 相遇\n正文"),
        ("第1章 相遇", "第1章\u3000相遇\n正文"),
        ("第1章  相遇", "第1章 相遇\n正文"),
        ("第1章 相遇", "第1章相遇\n正文"),
    ],
)
def test_leading_title_strip_ignores_whitespace_variants(
    title: str, content: str
) -> None:
    """Heading spacing is not stable across a site's own pages."""
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title=title, content=content))
    assert chapter["content"] == "正文"


def test_short_title_followed_by_newline_is_still_stripped() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title="序", content="序\n正文"))
    assert chapter["content"] == "正文"


def test_leading_title_strip_keeps_title_suffix_brackets() -> None:
    """``（上）`` after the title means the heading continues; keep it."""
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(
        make_chapter(title="第1章 开始", content="第1章 开始（上）\n正文")
    )
    assert chapter["content"] == "第1章 开始（上）\n正文"


def test_leading_title_strip_can_be_disabled() -> None:
    proc = CleanerProcessor({"strip_leading_title": False})

    chapter = proc.process_chapter(
        make_chapter(title="第1章 开始", content="第1章 开始\n正文")
    )
    assert chapter["content"] == "第1章 开始\n正文"


def test_strip_leading_title_helper_is_callable_directly() -> None:
    assert (
        CleanerProcessor.strip_leading_title("第1章 开始\n正文", "第1章 开始") == "正文"
    )
    assert CleanerProcessor.strip_leading_title("正文", "第1章 开始") == "正文"
    assert CleanerProcessor.strip_leading_title("正文", "") == "正文"


# --------------------------------------------------------------------------- #
# chapter title normalization (off by default)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1. 名字", "第1章 名字"),
        ("1、名字", "第1章 名字"),
        ("12.名字", "第12章 名字"),
        ("一、名字", "第一章 名字"),
        ("十二、名字", "第十二章 名字"),
        ("第1章名字", "第1章 名字"),
        ("第一章名字", "第一章 名字"),
    ],
)
def test_normalize_chapter_title_forms(raw: str, expected: str) -> None:
    proc = CleanerProcessor({"normalize_chapter_title": True})

    chapter = proc.process_chapter(make_chapter(title=raw))
    assert chapter["title"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        "第1章 名字",
        "第一章 名字",
        "第1章：名字",
        "3.14 圆周率",
        "1.2.3 版本",
        "名字",
    ],
)
def test_normalize_chapter_title_leaves_non_matches_alone(raw: str) -> None:
    proc = CleanerProcessor({"normalize_chapter_title": True})

    chapter = proc.process_chapter(make_chapter(title=raw))
    assert chapter["title"] == raw


def test_normalize_chapter_title_is_off_by_default() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title="1. 名字"))
    assert chapter["title"] == "1. 名字"


def test_normalize_chapter_title_is_idempotent() -> None:
    proc = CleanerProcessor({"normalize_chapter_title": True})

    once = proc.process_chapter(make_chapter(title="1、名字"))
    twice = proc.process_chapter(once)
    assert once["title"] == twice["title"] == "第1章 名字"


def test_normalize_chapter_title_applies_to_chapter_list() -> None:
    proc = CleanerProcessor({"normalize_chapter_title": True})

    book_info = proc.process_book_info(
        make_book_info(
            volumes=[
                {
                    "volume_name": "1. 第一卷",
                    "volume_intro": "简介",
                    "chapters": [
                        {
                            "title": "2、名字",
                            "url": "https://example.com/2",
                            "chapterId": "2",
                        }
                    ],
                }
            ]
        )
    )

    assert book_info["volumes"][0]["chapters"][0]["title"] == "第2章 名字"
    # Volume names are not chapter titles and must be left alone.
    assert book_info["volumes"][0]["volume_name"] == "1. 第一卷"


# --------------------------------------------------------------------------- #
# whitespace fidelity (regression: no collapsing / no removal)
# --------------------------------------------------------------------------- #
def test_mixed_cjk_and_english_keeps_word_spaces() -> None:
    proc = CleanerProcessor({})

    text = "hello world 世界"
    assert proc.process_chapter(make_chapter(content=text))["content"] == text


def test_internal_whitespace_is_not_collapsed() -> None:
    proc = CleanerProcessor({})

    text = "a  b\t\tc\n\n\nd"
    chapter = proc.process_chapter(make_chapter(content=text))
    assert chapter["content"] == text


def test_only_surrounding_whitespace_is_stripped() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(content="  hello world 世界  "))
    assert chapter["content"] == "hello world 世界"


# --------------------------------------------------------------------------- #
# HTML entities (regression: never delete ``&...;`` wholesale)
# --------------------------------------------------------------------------- #
def test_html_entities_are_preserved() -> None:
    proc = CleanerProcessor({})

    text = "A &amp; B &lt;tag&gt; &quot;q&quot; &#39;s&#39; &bogus;"
    chapter = proc.process_chapter(make_chapter(content=text))
    assert chapter["content"] == text


def test_html_entity_in_title_is_preserved() -> None:
    proc = CleanerProcessor({})

    chapter = proc.process_chapter(make_chapter(title="A &amp; B"))
    assert chapter["title"] == "A &amp; B"


# --------------------------------------------------------------------------- #
# empty tag cleanup (opt-in; content is plain text at this stage)
# --------------------------------------------------------------------------- #
def test_empty_tag_cleanup_is_off_by_default() -> None:
    """By default, literal tags in prose/code samples must survive."""
    proc = CleanerProcessor({})

    text = "源码里写 <p></p>，换行写 <br><br>。"
    chapter = proc.process_chapter(make_chapter(content=text))
    assert chapter["content"] == text


def test_empty_paragraph_tags_are_removed() -> None:
    proc = CleanerProcessor({"remove_empty_tags": True})

    chapter = proc.process_chapter(
        make_chapter(content="<p></p><p>第一段</p><p> </p><p><br/></p>结尾")
    )
    assert chapter["content"] == "<p>第一段</p>结尾"


def test_empty_self_closing_paragraph_is_removed() -> None:
    proc = CleanerProcessor({"remove_empty_tags": True})

    chapter = proc.process_chapter(make_chapter(content="甲<p/>乙"))
    assert chapter["content"] == "甲乙"


def test_single_line_break_is_kept() -> None:
    proc = CleanerProcessor({"remove_empty_tags": True})

    chapter = proc.process_chapter(make_chapter(content="第一行<br/>第二行"))
    assert chapter["content"] == "第一行<br/>第二行"


def test_repeated_line_breaks_collapse_to_one_newline() -> None:
    proc = CleanerProcessor({"remove_empty_tags": True})

    chapter = proc.process_chapter(make_chapter(content="第一行<br/><br/>第二行"))
    assert chapter["content"] == "第一行\n第二行"


def test_paragraph_with_content_is_never_touched() -> None:
    text = "<p>有内容的段落</p><p>另一段</p>"
    for proc in (
        CleanerProcessor({}),
        CleanerProcessor({"remove_empty_tags": True}),
    ):
        chapter = proc.process_chapter(make_chapter(content=text))
        assert chapter["content"] == text


def test_empty_tag_cleanup_handles_pathological_unterminated_tags() -> None:
    """A body full of ``<p`` prefixes must stay linear, not rescan repeatedly."""
    proc = CleanerProcessor({"remove_empty_tags": True})

    text = "<p" * 10_000
    chapter = proc.process_chapter(make_chapter(content=text))
    assert chapter["content"] == text


def test_empty_paragraph_with_attributes_is_removed() -> None:
    proc = CleanerProcessor({"remove_empty_tags": True})

    chapter = proc.process_chapter(
        make_chapter(content='甲<p class="x" data-y="1"> </p>乙')
    )
    assert chapter["content"] == "甲乙"


def test_empty_tag_cleanup_can_be_disabled() -> None:
    proc = CleanerProcessor({"remove_empty_tags": False})

    chapter = proc.process_chapter(make_chapter(content="甲<p></p>乙<br/><br/>丙"))
    assert chapter["content"] == "甲<p></p>乙<br/><br/>丙"
