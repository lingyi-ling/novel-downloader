#!/usr/bin/env python3
"""Offline tests for :mod:`novel_downloader.plugins.base.parser` helpers.

No OCR model, network, or real site is involved.  The blank-page cases pin the
contract that an empty HTTP body makes a parser return ``None`` rather than
raising from the HTML parser.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser.biquge1 import Biquge1Parser
from novel_downloader.schemas import BookInfoDict, ChapterDict, ParserConfig


class _Parser(BaseParser):
    site_name = "demo"
    ADS: set[str] = set()

    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        return None

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        return None


class _Biquge1(Biquge1Parser):
    """Concrete site binding for the abstract common parser."""

    site_name = "biquge1"


def _config() -> ParserConfig:
    return ParserConfig(enable_ocr=False)


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def test_norm_space_collapses_all_whitespace() -> None:
    parser = _Parser(_config())

    assert parser._norm_space("  a\n\tb\u3000c  ") == "a b c"
    assert parser._norm_space("a\n b", c="-") == "a-b"


def test_first_str_takes_first_non_empty_value() -> None:
    assert BaseParser._first_str([]) == ""
    assert BaseParser._first_str(["  first  ", "second"]) == "first"
    assert BaseParser._first_str(["a-b"], [("-", "_")]) == "a_b"


def test_join_strs_preserves_line_breaks() -> None:
    # Parsers join lines with "\n" (searchers concatenate instead).
    assert BaseParser._join_strs([" a ", "", "  ", " b "]) == "a\nb"
    assert BaseParser._join_strs(["a", "b"], [("a", "x")]) == "x\nb"
    assert BaseParser._join_strs([]) == ""


def test_usable_pages_drops_missing_or_blank_payloads() -> None:
    pages = BaseParser._usable_pages(["", "   \n\t", "<html>x</html>", "y"])

    assert pages == ["<html>x</html>", "y"]
    assert BaseParser._usable_pages([]) == []
    assert BaseParser._usable_pages([""]) == []


# --------------------------------------------------------------------------- #
# ad filtering
# --------------------------------------------------------------------------- #
def test_is_ad_line_flags_ad_lines() -> None:
    parser = _Parser(_config())
    parser.ADS = {"广告", r"\bAD\b"}
    parser._ad_pattern = parser._compile_ads_pattern()

    assert parser._is_ad_line("这里是广告内容")
    assert parser._is_ad_line("visit AD now")
    assert not parser._is_ad_line("正常正文")


def test_invalid_ad_pattern_fails_at_construction() -> None:
    class _Broken(_Parser):
        ADS = {"("}  # unbalanced regex

    with pytest.raises(re.error):
        _Broken(_config())


# --------------------------------------------------------------------------- #
# blank pages are treated as "no content", not as a parse error
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pages", [[], [""], ["   "], ["\n\t"]])
def test_book_info_blank_pages_return_none(pages: list[str]) -> None:
    parser = _Biquge1(_config())

    assert parser.parse_book_info(pages) is None


@pytest.mark.parametrize("pages", [[], [""], ["   "], ["\n\t"]])
def test_chapter_content_blank_pages_return_none(pages: list[str]) -> None:
    parser = _Biquge1(_config())

    assert parser.parse_chapter_content(pages, chapter_id="1") is None


def test_chapter_content_skips_blank_pages_between_valid_ones() -> None:
    parser = _Biquge1(_config())
    html = "<html><body><article>正文段落</article></body></html>"

    chapter = parser.parse_chapter_content(["", html, "  "], chapter_id="1")

    assert chapter is not None
    assert chapter["content"] == "正文段落"


def test_book_info_parses_a_minimal_page() -> None:
    parser = _Biquge1(_config())
    html = (
        "<html><head>"
        '<meta property="og:novel:book_name" content="书名"/>'
        '<meta property="og:novel:author" content="作者"/>'
        "</head><body>"
        '<div class="book_list2"><a href="/book/1/9.html">第九章</a></div>'
        "</body></html>"
    )

    info = parser.parse_book_info([html])

    assert info is not None
    assert info["book_name"] == "书名"
    assert info["author"] == "作者"
    assert info["volumes"][0]["chapters"][0]["chapterId"] == "9"


# --------------------------------------------------------------------------- #
# OCR helper
# --------------------------------------------------------------------------- #
def test_extract_text_from_image_maps_predictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeModel:
        def __init__(self) -> None:
            self.calls: list[tuple[list[Any], int]] = []

        def predict(
            self, images: list[Any], batch_size: int = 1
        ) -> list[dict[str, Any]]:
            self.calls.append((images, batch_size))
            return [
                {"rec_text": "文字", "rec_score": 0.9},
                {"rec_text": "第二行", "rec_score": 0.8},
            ]

    model = _FakeModel()
    monkeypatch.setattr(BaseParser, "_OCR_MODEL", model)
    parser = _Parser(_config())

    result = parser._extract_text_from_image([object()], batch_size=2)  # type: ignore[list-item]

    assert result == [("文字", 0.9), ("第二行", 0.8)]
    assert model.calls[0][1] == 2
