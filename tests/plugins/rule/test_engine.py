#!/usr/bin/env python3
"""Extraction primitive tests for :mod:`novel_downloader.plugins.rule.engine`."""

from __future__ import annotations

import pytest

from novel_downloader.plugins.rule.engine import (
    abs_url,
    extract,
    extract_multi,
    node_to_str,
    parse_page,
    render,
    select,
    strip_nested_html,
)
from novel_downloader.plugins.rule.model import Field, RuleError

PAGE = """
<html>
  <body>
    <div class="wrap">
      <div class="item">
        <a href="/book/1/"> 斗破苍穹 </a>
        <span class="author">作者：天蚕土豆</span>
      </div>
      <div class="item">
        <a href="//cdn.example/book/2/"> 武动乾坤 </a>
        <span class="author">作者：天蚕土豆</span>
      </div>
    </div>
  </body>
</html>
"""


# --------------------------------------------------------------------------- #
# page handling
# --------------------------------------------------------------------------- #
def test_parse_page_rejects_empty_html() -> None:
    for raw in ("", "   ", "\n"):
        with pytest.raises(RuleError, match="empty page"):
            parse_page(raw)


def test_parse_page_accepts_plain_fragment() -> None:
    tree = parse_page("<div id='x'>hi</div>")
    assert select(tree, "#x")[0].text == "hi"


def test_strip_nested_html_removes_document_wrapper() -> None:
    raw = "<!DOCTYPE html><html><body><div id='x'>hi</div></body></html>"
    stripped = strip_nested_html(raw)

    assert "<!DOCTYPE" not in stripped
    assert "<html>" not in stripped
    assert "<body>" not in stripped
    assert "<div id='x'>hi</div>" in stripped


# --------------------------------------------------------------------------- #
# selectors
# --------------------------------------------------------------------------- #
def test_empty_selector_returns_root() -> None:
    tree = parse_page(PAGE)
    assert select(tree, "") == [tree]


def test_css_selector() -> None:
    tree = parse_page(PAGE)
    assert len(select(tree, "div.item")) == 2
    assert len(select(tree, "div.wrap div.item a")) == 2


def test_xpath_selector() -> None:
    tree = parse_page(PAGE)
    assert len(select(tree, "//div[@class='item']")) == 2
    assert len(select(tree, "(//div[@class='item'])[2]")) == 1


def test_invalid_css_selector_raises_rule_error() -> None:
    tree = parse_page(PAGE)
    with pytest.raises(RuleError, match="invalid selector"):
        select(tree, "div[[")


# --------------------------------------------------------------------------- #
# node_to_str
# --------------------------------------------------------------------------- #
def test_node_to_str_text_concatenates_without_separator() -> None:
    node = select(parse_page(PAGE), "div.item a")[0]
    assert node_to_str(node, "text") == "斗破苍穹"


def test_node_to_str_inner_text_joins_with_newlines() -> None:
    node = select(parse_page(PAGE), "div.item")[0]
    assert node_to_str(node, "inner_text") == "斗破苍穹\n作者：天蚕土豆"


def test_node_to_str_html_keeps_markup() -> None:
    node = select(parse_page(PAGE), "div.item")[0]
    assert node_to_str(node, "html").startswith('<a href="/book/1/">')


def test_node_to_str_attribute() -> None:
    node = select(parse_page(PAGE), "div.item a")[0]
    assert node_to_str(node, "href") == "/book/1/"
    assert node_to_str(node, "missing-attr") == ""


def test_node_to_str_bare_string_node() -> None:
    assert node_to_str("  hello  ", "text") == "hello"


# --------------------------------------------------------------------------- #
# extract / extract_multi
# --------------------------------------------------------------------------- #
def test_extract_first_matching_node() -> None:
    tree = parse_page(PAGE)
    fld = Field(selector="div.item a", attr="href")
    assert extract(tree, fld) == "/book/1/"


def test_extract_without_selector_uses_root() -> None:
    tree = parse_page("<div id='x'> 正文 </div>")
    assert extract(tree, Field(attr="text")) == "正文"


def test_extract_applies_regex_and_group() -> None:
    tree = parse_page(PAGE)
    fld = Field(selector="span.author", regex=r"作者：(\S+)", group=1)
    assert extract(tree, fld) == "天蚕土豆"


def test_extract_falls_back_to_whole_match_when_group_missing() -> None:
    tree = parse_page(PAGE)
    fld = Field(selector="span.author", regex=r"作者：\S+", group=3)
    assert extract(tree, fld) == "作者：天蚕土豆"


def test_extract_uses_default_when_nothing_matches() -> None:
    tree = parse_page(PAGE)
    fld = Field(selector="div.nope", default="  n/a  ")
    assert extract(tree, fld) == "n/a"


def test_extract_applies_replace() -> None:
    tree = parse_page("<div id='x'>A\u00a0B</div>")
    fld = Field(selector="#x", replace=[("\u00a0", "")])
    assert extract(tree, fld) == "AB"


def test_extract_multi_returns_every_match() -> None:
    tree = parse_page(PAGE)
    fld = Field(selector="div.item a", attr="text", multi=True)
    assert extract_multi(tree, fld) == ["斗破苍穹", "武动乾坤"]


def test_extract_multi_skips_empty_values() -> None:
    tree = parse_page("<div class='t'></div><div class='t'>ok</div>")
    fld = Field(selector="div.t", attr="text", multi=True)
    assert extract_multi(tree, fld) == ["ok"]


# --------------------------------------------------------------------------- #
# render / abs_url
# --------------------------------------------------------------------------- #
def test_render_substitutes_known_placeholders_only() -> None:
    out = render(
        "{base_url}/{book_id}/index_{page}.html?q={keyword}&x={unknown}",
        base_url="https://demo.example",
        book_id="7",
        page=2,
        keyword="a%20b",
    )
    assert out == "https://demo.example/7/index_2.html?q=a%20b&x={unknown}"


def test_render_leaves_unknown_braces_untouched() -> None:
    assert render("a {b} c", c=1) == "a {b} c"
    assert render("a {b} c", b=1) == "a 1 c"


def test_abs_url_variants() -> None:
    base = "https://demo.example/book/7/"
    assert abs_url(base, "/x.html") == "https://demo.example/x.html"
    assert abs_url(base, "i.html") == "https://demo.example/book/7/i.html"
    assert abs_url(base, "//cdn.example/a.png") == "https://cdn.example/a.png"
    assert abs_url(base, "http://other.example/a") == "http://other.example/a"
    assert abs_url(base, "   ") == ""
    assert abs_url(base, "") == ""
