#!/usr/bin/env python3
"""Extraction primitive tests for :mod:`novel_downloader.plugins.rule.engine`."""

from __future__ import annotations

import pytest

from novel_downloader.plugins.rule.engine import (
    abs_url,
    extract,
    extract_multi,
    json_to_str,
    node_to_str,
    parse_json,
    parse_page,
    render,
    select,
    select_json,
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


def test_extract_regex_without_match_leaves_value_unchanged() -> None:
    """Documented lenient behaviour: a non-matching regex is a no-op.

    (Rules such as ``author`` with ``regex: "作者：(\\S+)"`` rely on this to
    keep plain values that simply lack the prefix.)
    """
    tree = parse_page("<div>天蚕土豆</div>")
    fld = Field(selector="div", regex=r"作者：(\S+)", group=1)
    assert extract(tree, fld) == "天蚕土豆"


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


def test_select_on_bare_string_root_returns_no_matches() -> None:
    """Bare string nodes (XPath ``@attr``/``text()``) cannot be descended."""
    assert select("/book/7/", "div.item", "html") == []
    assert select("/book/7/", "//div", "html") == []


def test_extract_negative_group_falls_back_to_whole_match() -> None:
    """A negative ``group`` used to leak a raw ``IndexError``."""
    tree = parse_page(PAGE)
    fld = Field(selector="span.author", regex=r"作者：(\S+)", group=-1)
    assert extract(tree, fld) == "作者：天蚕土豆"


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


def test_render_does_not_rescan_substituted_values() -> None:
    """A value containing ``{placeholder}`` text must stay literal.

    ``book_id`` / ``chapter_id`` come from (possibly hostile) page URLs; the
    old sequential ``str.replace`` let a value smuggle another variable's
    expansion (``chapter_id="{page}"`` produced ``.../2_2.html``).
    """
    out = render(
        "{base_url}/{book_id}/{chapter_id}_{page}.html",
        base_url="https://demo.example",
        book_id="0_1",
        chapter_id="{page}",
        page=2,
    )
    assert out == "https://demo.example/0_1/{page}_2.html"


def test_render_expansion_is_order_independent() -> None:
    """No variable may feed another, regardless of insertion order."""
    out = render(
        "https://demo.example/s?q={keyword}&p={page}",
        keyword="{page}",
        page=3,
    )
    assert out == "https://demo.example/s?q={page}&p=3"

    out2 = render("{base_url}/book/{book_id}/", book_id="{base_url}/x")
    assert out2 == "{base_url}/book/{base_url}/x/"


def test_abs_url_variants() -> None:
    base = "https://demo.example/book/7/"
    assert abs_url(base, "/x.html") == "https://demo.example/x.html"
    assert abs_url(base, "i.html") == "https://demo.example/book/7/i.html"
    assert abs_url(base, "//cdn.example/a.png") == "https://cdn.example/a.png"
    assert abs_url(base, "http://other.example/a") == "http://other.example/a"
    assert abs_url(base, "   ") == ""
    assert abs_url(base, "") == ""


# --------------------------------------------------------------------------- #
# JSON mode
# --------------------------------------------------------------------------- #
DOC = {
    "code": 0,
    "data": {
        "list": [
            {"title": "斗破苍穹", "bookId": 11, "tags": ["玄幻"], "author": "天蚕土豆"},
            {"title": "武动乾坤", "bookId": 22, "tags": [], "author": "天蚕土豆"},
        ],
        "total": 2,
    },
}


def test_parse_json_rejects_empty_and_malformed() -> None:
    with pytest.raises(RuleError, match="empty response"):
        parse_json("  ")
    with pytest.raises(RuleError, match="unparseable JSON"):
        parse_json("{not json")


def test_parse_page_dispatches_on_format() -> None:
    assert parse_page('{"a": 1}', "json") == {"a": 1}
    assert select(parse_page("<div id='x'>hi</div>"), "#x")[0].text == "hi"


def test_select_json_empty_path_returns_root() -> None:
    assert select_json(DOC, "") == [DOC]


def test_select_json_expands_trailing_array() -> None:
    assert len(select_json(DOC, "data.list")) == 2


def test_select_json_leading_dollar_is_optional() -> None:
    assert select_json(DOC, "$.data.total") == [2]
    assert select_json(DOC, "data.total") == [2]


def test_select_json_wildcard_over_nested_objects() -> None:
    assert select_json(DOC, "items[*].name") == []
    assert select_json(DOC, "data.list[*].title") == ["斗破苍穹", "武动乾坤"]


def test_select_json_fans_out_over_arrays_of_objects() -> None:
    assert select_json(DOC, "data.list.title") == ["斗破苍穹", "武动乾坤"]


def test_select_json_index_and_missing_key() -> None:
    assert select_json(DOC, "data.list[1].title") == ["武动乾坤"]
    assert select_json(DOC, "data.nope") == []
    assert select_json(DOC, "data.list[9]") == []


def test_select_json_returns_key_of_first_element_via_index() -> None:
    value = select_json(DOC, "data.list[0]")
    assert value == [DOC["data"]["list"][0]]


def test_json_to_str_scalars() -> None:
    assert json_to_str(None, "text") == ""
    assert json_to_str(True, "text") == "true"
    assert json_to_str(False, "text") == "false"
    assert json_to_str(11, "text") == "11"
    assert json_to_str("  hi  ", "text") == "hi"


def test_json_to_str_object_and_array() -> None:
    assert json_to_str({"a": 1}, "a") == "1"
    assert json_to_str({"a": 1}, "missing") == ""
    assert json_to_str(["x", "y"], "text") == "xy"
    assert json_to_str(["x", "y"], "inner_text") == "x\ny"
    assert json_to_str({"a": 1}, "text") == '{"a": 1}'


def test_extract_json_scalar_and_attribute_lookup() -> None:
    item = select_json(DOC, "data.list[0]")[0]
    assert extract(item, Field(selector="title"), "json") == "斗破苍穹"
    assert extract(item, Field(selector="bookId", attr="bookId"), "json") == "11"


def test_extract_json_multi_collects_every_item() -> None:
    fld = Field(selector="data.list[*].title")
    assert extract_multi(DOC, fld, "json") == ["斗破苍穹", "武动乾坤"]
    assert extract(DOC, fld, "json") == "斗破苍穹"


def test_extract_json_applies_regex_and_default() -> None:
    assert extract(DOC, Field(selector="data.nope", default="n/a"), "json") == "n/a"
    fld = Field(selector="data.list[*].title", regex=r"(\S+)苍穹", group=1)
    assert extract(DOC, fld, "json") == "斗破"
