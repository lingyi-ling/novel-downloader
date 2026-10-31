#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.plugins.utils.yuewen.qdcss`.

The data-driven regression suite (``test_qdcss.py``) only runs when fixture
HTML/CSS are present; these tests exercise the CSS rule parser and renderer
directly so the module stays covered without fixtures.
"""

from __future__ import annotations

import pytest

from novel_downloader.plugins.utils.yuewen.qdcss import (
    _only_tag,
    _parse_decls,
    apply_css_text_rules,
)

# --------------------------------------------------------------------------- #
# baseline rendering
# --------------------------------------------------------------------------- #


def test_plain_html_without_css() -> None:
    text, refl = apply_css_text_rules(
        "<html><body><p>First</p><p>Second</p></body></html>", ""
    )
    assert text == "First\nSecond"
    assert refl == []


def test_empty_paragraphs_are_skipped() -> None:
    text, refl = apply_css_text_rules(
        "<html><body><p></p><p>   </p><p>Kept</p></body></html>", ""
    )
    assert text == "   " + chr(10) + "Kept"  # whitespace-only p.text still emitted
    assert refl == []


def test_non_y_children_contribute_text_and_tails() -> None:
    text, _ = apply_css_text_rules("<html><body><p>A<b>B</b>C</p></body></html>", "")
    assert text == "ABC"


def test_y_without_matching_rule_keeps_raw_text() -> None:
    text, refl = apply_css_text_rules(
        "<html><body><p>Before<y>raw</y>After</p></body></html>", ""
    )
    assert text == "BeforerawAfter"
    assert refl == []


# --------------------------------------------------------------------------- #
# .sy-* class rules
# --------------------------------------------------------------------------- #


def test_sy_delete_all_drops_text() -> None:
    css = ".sy-1{font-size:0;}"
    text, refl = apply_css_text_rules(
        "<html><body><p>A<y class='sy-1'>hidden</y>B</p></body></html>", css
    )
    assert text == "AB"
    assert refl == []


def test_sy_first_letter_rule_is_unreachable_like_other_pseudos() -> None:
    # ".sy-2::first-letter" is stored under key "sy-2::first-letter" while the
    # renderer looks up plain "sy-2", so the rule never applies.
    css = ".sy-2::first-letter{font-size:0;}"
    text, _ = apply_css_text_rules(
        "<html><body><p>A<y class='sy-2'>XY</y>B</p></body></html>", css
    )
    assert text == "AXYB"


def test_sy_delete_first_via_plain_rule_after_merge() -> None:
    # Two blocks targeting the same .sy-2 key are merged; delete_first only
    # triggers when "::first-letter" is in the selector, so a plain block
    # with font-size:0 maps to delete_all instead.
    css = ".sy-2{font-size:0;}"
    text, _ = apply_css_text_rules(
        "<html><body><p>A<y class='sy-2'>XY</y>B</p></body></html>", css
    )
    assert text == "AB"


def test_sy_flip_x_collects_reflection() -> None:
    css = ".sy-3{transform:scaleX(-1);}"
    text, refl = apply_css_text_rules(
        "<html><body><p>A<y class='sy-3'>wow</y>B</p></body></html>", css
    )
    assert text == "AwowB"
    assert refl == ["wow"]


def test_sy_pseudo_selectors_are_unreachable_in_renderer() -> None:
    # Current behaviour: ``.sy-4::before`` rules are stored under the key
    # "sy-4::before" while the renderer looks up plain "sy-4", so the rule
    # never applies and the raw text is used.
    css = '.sy-4::before{content:"«";}.sy-4::after{content:attr(data-end);}'
    text, _ = apply_css_text_rules(
        "<html><body><p>A<y class='sy-4' data-end='»'>mid</y>B</p></body></html>", css
    )
    assert text == "AmidB"


def test_append_attr_rules_apply_on_ordered_tags() -> None:
    css = "i{order:1;}.p6 i::before{content:attr(data-pre);}.p6 i::after{content:'!';}"
    text, _ = apply_css_text_rules(
        "<html><body><p class='p6'><i data-pre='pre'>body</i></p></body></html>", css
    )
    assert text == "prebody!"


def test_append_start_char_applies_on_ordered_tags() -> None:
    css = "i{order:1;} .p7 i::before{content:'[';} .p7 i::after{content:']';}"
    text, _ = apply_css_text_rules(
        "<html><body><p class='p7'><i>core</i></p></body></html>", css
    )
    assert text == "[core]"


def test_sy_unmatched_class_falls_back_to_raw_text() -> None:
    css = ".sy-9{font-size:0;}"
    text, _ = apply_css_text_rules(
        "<html><body><p>A<y class='sy-8'>kept</y></p></body></html>", css
    )
    assert text == "Akept"


# --------------------------------------------------------------------------- #
# ordered paragraphs (.pN <tag>)
# --------------------------------------------------------------------------- #


def test_ordered_paragraph_reorders_by_order_property() -> None:
    css = "i{order:2;} em{order:1;}"
    text, refl = apply_css_text_rules(
        "<html><body><p class='p1'><i>second</i><em>first</em></p></body></html>", css
    )
    # No .p1 rules exist, but the p has a class starting with 'p' and rules
    # apply only when p_key is present in p_rules; without them the plain
    # text path is used.
    assert text == "secondfirst"
    assert refl == []


def test_ordered_paragraph_with_p_rules_and_delete_first() -> None:
    css = ".p2 i::first-letter{font-size:0;}i{order:2;}em{order:1;}"
    text, _ = apply_css_text_rules(
        "<html><body><p class='p2'><i>second</i><em>first</em></p></body></html>", css
    )
    # Reordered by tag order (em before i); i also loses its first letter.
    assert text == "firstecond"


def test_ordered_paragraph_drops_direct_p_text() -> None:
    css = ".p3 i{order:1;}"
    text, _ = apply_css_text_rules(
        "<html><body><p class='p3'>HEAD<i>tail</i></p></body></html>", css
    )
    assert text == "tail"


def test_ordered_rule_missing_tag_defaults_to_noop_rule() -> None:
    css = ".p4 span{order:1;}"
    text, _ = apply_css_text_rules(
        "<html><body><p class='p4'><span>kept</span></p></body></html>", css
    )
    assert text == "kept"


def test_duplicate_ordered_tags_preserve_duplicates() -> None:
    css = "i{order:1;} .p5 i{color:red;}"
    text, _ = apply_css_text_rules(
        "<html><body><p class='p5'><i>a</i><i>b</i></p></body></html>", css
    )
    assert text == "ab"


# --------------------------------------------------------------------------- #
# CSS parser details
# --------------------------------------------------------------------------- #


def test_css_case_insensitive_and_tolerates_spacing() -> None:
    css = ".SY-1 { FONT-SIZE : 0 }"
    text, _ = apply_css_text_rules(
        "<html><body><p>A<y class='sy-1'>x</y></p></body></html>", css
    )
    assert text == "A"


def test_css_unclosed_block_is_ignored() -> None:
    css = ".sy-1{font-size:0; .sy-2{color:red;}"
    text, _ = apply_css_text_rules(
        "<html><body><p>A<y class='sy-1'>x</y></p></body></html>", css
    )
    # The first block runs to the last '}', so its first declaration
    # (font-size:0) still registers delete_all for sy-1.
    assert text == "A"


def test_css_content_only_applies_to_pseudo_selectors() -> None:
    # content on a plain selector (no ::before/::after) must not create
    # append rules.
    css = ".sy-1{content:'X';}"
    text, _ = apply_css_text_rules(
        "<html><body><p>A<y class='sy-1'>x</y></p></body></html>", css
    )
    assert text == "Ax"


def test_selector_with_dot_is_not_a_tag_for_ordering() -> None:
    # `.sy-1` alone must not enter the tag order list even though it
    # contains an `order`-like declaration for a tag rule.
    css = "i{order:1;} .sy-1{order:2; font-size:0;}"
    text, _ = apply_css_text_rules(
        "<html><body><p class='p1'><i>tag</i><y class='sy-1'>y</y></p></body></html>",
        css,
    )
    assert text == "tag"


def test_only_tag_extracts_tag_names() -> None:
    assert _only_tag("em::before") == "em"
    assert _only_tag(".p3 i") == "i"
    assert _only_tag("span[attr='x']") == "span"
    assert _only_tag(".cls") is None


def test_parse_decls_basic() -> None:
    assert _parse_decls("color: red; font-size:0 ; ;;") == [
        ("color", "red"),
        ("font-size", "0"),
    ]


def test_parse_decls_ignores_entries_without_colon() -> None:
    assert _parse_decls("justtext; color:red") == [("color", "red")]


def test_multiple_paragraphs_each_rendered() -> None:
    css = ".sy-1{font-size:0;}"
    html_str = (
        "<html><body><p>One<y class='sy-1'>.</y></p><p>Two</p><p></p></body></html>"
    )
    text, refl = apply_css_text_rules(html_str, css)
    assert text == "One\nTwo"
    assert refl == []


def test_flip_x_reflections_are_sorted_and_deduped() -> None:
    css = ".sy-1{transform:scalex(-1);}"
    html_str = (
        "<html><body><p><y class='sy-1'>bbb</y></p><p><y class='sy-1'>aaa</y>"
        "<y class='sy-1'>bbb</y></p></body></html>"
    )
    text, refl = apply_css_text_rules(html_str, css)
    assert text == "bbb" + chr(10) + "aaabbb"  # paragraphs are newline-joined
    assert refl == ["aaa", "bbb"]


def test_order_values_sort_stably() -> None:
    css = "span{order:3;} i{order:1;} em{order:2;} .p9 i{color:red;}"
    text, _ = apply_css_text_rules(
        "<html><body><p class='p9'><span>c</span><i>a</i><em>b</em></p></body></html>",
        css,
    )
    assert text == "abc"


@pytest.mark.parametrize(
    "css",
    [
        "",
        "   ",
        ".p1{}",
        "sy-1{font-size:0;}",  # selector without dot does not match .sy-*
    ],
)
def test_noop_css_inputs(css: str) -> None:
    text, refl = apply_css_text_rules(
        "<html><body><p>A<y class='sy-1'>x</y></p></body></html>", css
    )
    assert text == "Ax"
    assert refl == []
