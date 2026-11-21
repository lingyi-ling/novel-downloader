#!/usr/bin/env python3
"""
tests.test_website_i18n
-----------------------

Content-completeness audit for the bilingual marketing site in ``website/``.

``tests/test_website_static.py`` already checks that the i18n hooks *exist*
and that no ``data-en`` value contains Chinese.  This module owns the other
half of the contract: that every translatable unit actually has both
languages, that the JS dictionaries and the static markup agree, that a
language switch leaves no residue in either direction, and that the
dictionaries carry no dead keys.

``website/`` uses two different mechanisms, so "key coverage" means two
different things:

* ``index.html`` is zh-authored.  Every translatable element carries a
  parallel ``data-en`` attribute; ``applyLang()`` in ``main.js`` swaps
  ``innerHTML`` between that attribute and the element's original zh markup
  (cached in ``data-zh`` on first switch).  The element *is* the dictionary
  entry: its key is the DOM position, its two values are the attribute and the
  inner markup.
* ``main.js`` holds the only real dictionaries: ``LABEL_SPECS`` (aria-labels,
  keyed by element id), ``PAGE_TITLE`` and ``META_DESC`` (each keyed by lang).
  Dead keys are silent here — ``applyLang()`` filters out specs whose id is
  absent from the DOM, so renaming a control would quietly stop translating it.

``404.html`` has no language toggle at all.  It ships both languages in the
markup (Chinese runs plus ``<span lang="en">`` halves) so it stays complete
with JavaScript disabled; its contract is "every English run has a Chinese
counterpart", covered below.

Static, stdlib only, no browser.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "website"
INDEX = SITE / "index.html"
ERROR_PAGE = SITE / "404.html"
JS_FILE = SITE / "main.js"

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Three or more consecutive Latin words is the cheapest reliable proxy for
# "an untranslated sentence": brand names and CLI flags never reach three.
LATIN_SENTENCE = re.compile(r"[A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){2,}")
TAG_NAME = re.compile(r"<\s*([a-zA-Z][\w-]*)")

VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

# Code comments that are byte-identical in both languages on purpose: the
# command is the same, only the surrounding prose differs.
IDENTICAL_BY_DESIGN = {"# macOS", "# Linux"}

# zh sources that legitimately contain no Han character: an OS name in Chinese
# typography, and two shell comments.
ZH_SOURCE_WITHOUT_HAN = {"Windows（PowerShell）", "# macOS", "# Linux"}

# Regions that are allowed to keep Chinese in the English view:
#   * pre/code   — CLI examples quote Chinese book and song titles verbatim
#   * noscript   — the note is bilingual by design; with JS off no switch can
#                  happen, so both halves must be visible
#   * brand      — 书音 is the product's Chinese name, kept in both languages
#   * copyright  — the legal line keeps the brand name
# head is skipped because document.title is replaced at runtime from
# PAGE_TITLE (asserted separately).
EN_VIEW_SKIP_TAGS = {"head", "script", "style", "pre", "code", "noscript"}
EN_VIEW_SKIP_CLASSES = {"brand-name", "foot-brand", "appwin-title"}
EN_VIEW_SKIP_PREFIXES = ("©",)

# The Chinese view may keep these regions in English: code samples are not
# prose, and the noscript note is bilingual by design.
ZH_VIEW_SKIP_TAGS = {"head", "script", "style", "pre", "code", "noscript"}

# Attributes whose value may legitimately be Chinese in index.html.  Anything
# else with Han characters (a new placeholder=, title=, alt= …) has no
# translation path and must fail this test.
#
# ``<meta content>`` is exempt wholesale: crawler metadata (description, Open
# Graph, Twitter, keywords) is *not* swapped at runtime — it is emitted once in
# the zh variant and paired with ``og:locale=zh_CN`` plus hreflang alternates.
# ``tests/test_website_seo.py`` pins those values and their budgets, and
# ``test_js_maps_agree_with_the_static_markup`` pins the one meta that main.js
# does rewrite (description).
ATTRIBUTE_EXEMPT = {("meta", "content")}


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []


class _Tree(HTMLParser):
    """Minimal DOM: enough to simulate the ``data-en`` innerHTML swap."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in VOID_ELEMENTS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def _parse(text: str) -> _Tree:
    tree = _Tree()
    tree.feed(text)
    tree.close()
    return tree


def _elements(node: _Node, ancestors: tuple[_Node, ...] = ()) -> list:
    """(node, ancestors) for every element, in document order."""
    found: list[tuple[_Node, tuple[_Node, ...]]] = []
    for child in node.children:
        if isinstance(child, _Node):
            found.append((child, ancestors))
            found.extend(_elements(child, (*ancestors, child)))
    return found


def _texts(node: _Node, ancestors: tuple[_Node, ...] = ()) -> list:
    """(text, ancestors) for every text node, in document order."""
    found: list[tuple[str, tuple[_Node, ...]]] = []
    for child in node.children:
        if isinstance(child, str):
            found.append((child, ancestors))
        else:
            found.extend(_texts(child, (*ancestors, child)))
    return found


def _en_leaves(
    node: _Node, ancestors: tuple[_Node, ...] = ()
) -> list[tuple[str, tuple[_Node, ...]]]:
    """Text as the English view shows it.

    An element with ``data-en`` has its ``innerHTML`` replaced wholesale, so it
    contributes its English value as one unit and its Chinese children are not
    walked.
    """
    if isinstance(node, str):
        return [(node, ancestors)]
    if "data-en" in node.attrs:
        return [(re.sub(r"<[^>]+>", "", node.attrs["data-en"]), (*ancestors, node))]
    found: list[tuple[str, tuple[_Node, ...]]] = []
    for child in node.children:
        found.extend(_en_leaves(child, (*ancestors, node)))
    return found


def _plain(node: _Node) -> str:
    """Concatenated text of a subtree, whitespace collapsed."""
    if isinstance(node, str):
        return node
    return re.sub(
        r"\s+", " ", "".join(_plain(child) for child in node.children)
    ).strip()


def _classes(node: _Node) -> set[str]:
    return set(node.attrs.get("class", "").split())


def _data_en_entries(
    tree: _Tree,
) -> list[tuple[_Node, tuple[_Node, ...], str, str]]:
    """(element, ancestors, zh inner text, en value) for each ``data-en``."""
    entries = []
    for node, ancestors in _elements(tree.root):
        if "data-en" not in node.attrs:
            continue
        entries.append((node, ancestors, _plain(node), node.attrs["data-en"]))
    return entries


def _js_map(js: str, name: str) -> dict[str, str]:
    """Pull a two-language ``{ zh, en }`` map out of main.js."""
    match = re.search(rf"const {name} = \{{(.*?)\}};", js, re.S)
    assert match is not None, f"{name} is missing from main.js"
    return dict(re.findall(r'(zh|en):\s*"([^"]*)"', match.group(1)))


def _label_specs(js: str) -> list[tuple[str, str, str]]:
    """(id, zh, en) rows of LABEL_SPECS."""
    match = re.search(r"const LABEL_SPECS = \[(.*?)\];", js, re.S)
    assert match is not None, "LABEL_SPECS is missing from main.js"
    rows = re.findall(
        r'\[\s*"([a-z0-9-]+)",\s*"([^"]*)",\s*"([^"]*)"\s*\]', match.group(1)
    )
    assert rows, "LABEL_SPECS parsed to zero rows"
    return [(row[0], row[1], row[2]) for row in rows]


@pytest.fixture(scope="module")
def index_text() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def error_text() -> str:
    return ERROR_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return JS_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_tree(index_text: str) -> _Tree:
    return _parse(index_text)


# --------------------------------------------------------------------------
# index.html: the data-en pairs
# --------------------------------------------------------------------------
def test_every_data_en_element_has_both_languages(index_tree: _Tree) -> None:
    """Both halves must exist, be non-empty and actually differ."""
    entries = _data_en_entries(index_tree)
    # A floor, not a target: it catches an accidental mass deletion.
    assert len(entries) >= 120, f"only {len(entries)} data-en entries"

    problems = []
    for _, _, zh, en in entries:
        if not en.strip():
            problems.append(f"empty data-en: {zh[:50]!r}")
        if not zh:
            problems.append(f"empty zh source: {en[:50]!r}")
        if zh and en.strip() == zh and zh not in IDENTICAL_BY_DESIGN:
            problems.append(f"data-en repeats the zh source: {zh[:50]!r}")
    assert not problems, problems[:5]


def test_every_zh_source_has_chinese_unless_allowlisted(
    index_tree: _Tree,
) -> None:
    """A data-en element whose zh side is English means a lost translation."""
    missing = [
        zh[:60]
        for _, _, zh, _ in _data_en_entries(index_tree)
        if not CJK.search(zh) and zh not in ZH_SOURCE_WITHOUT_HAN
    ]
    assert not missing, f"zh source has no Chinese: {missing[:5]}"


def test_english_side_of_every_pair_is_chinese_free(index_tree: _Tree) -> None:
    leaking = [
        en[:70] for _, _, _, en in _data_en_entries(index_tree) if CJK.search(en)
    ]
    assert not leaking, f"data-en still contains Chinese: {leaking[:5]}"


def test_markup_parity_between_the_two_languages(index_tree: _Tree) -> None:
    """``data-en`` is assigned to innerHTML, so inline tags must mirror the zh
    markup or the English view loses structure (e.g. <code> styling)."""
    mismatched = []
    for node, _, _, en in _data_en_entries(index_tree):
        zh_tags = [child.tag for child, _ in _elements(node)]
        en_tags = TAG_NAME.findall(en)
        if zh_tags != en_tags:
            mismatched.append((zh_tags, en_tags, en[:60]))
    assert not mismatched, mismatched[:5]


# --------------------------------------------------------------------------
# main.js: the real dictionaries
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["PAGE_TITLE", "META_DESC"])
def test_js_maps_define_exactly_zh_and_en(js: str, name: str) -> None:
    entries = _js_map(js, name)
    assert set(entries) == {"zh", "en"}, f"{name} keys: {sorted(entries)}"
    for lang, value in entries.items():
        assert value.strip(), f"{name}.{lang} is empty"
        assert "\n" not in value, f"{name}.{lang} spans lines"
    assert entries["zh"] != entries["en"]
    assert not CJK.search(entries["en"]), f"{name}.en contains Chinese"


def test_js_maps_agree_with_the_static_markup(
    js: str, index_text: str, index_tree: _Tree
) -> None:
    """The zh static document is the source of truth; JS must not drift."""
    titles = _js_map(js, "PAGE_TITLE")
    descriptions = _js_map(js, "META_DESC")

    static_title = re.search(r"<title>(.*?)</title>", index_text, re.S)
    assert static_title is not None
    assert titles["zh"] == static_title.group(1).strip()

    metas = [
        node.attrs["content"]
        for node, _ in _elements(index_tree.root)
        if node.tag == "meta" and node.attrs.get("name") == "description"
    ]
    assert len(metas) == 1
    assert descriptions["zh"] == metas[0].strip()


def test_switching_language_updates_title_description_and_document_lang(
    js: str,
) -> None:
    """Coverage is worthless if applyLang() never applies it."""
    assert re.search(r"document\.title\s*=\s*isEn\s*\?\s*PAGE_TITLE\.en", js)
    assert re.search(r"metaDesc\.setAttribute\(\s*[\"']content[\"'],\s*META_DESC\[", js)
    assert re.search(
        r"document\.documentElement\.lang\s*=\s*isEn\s*\?\s*\"en\"\s*:\s*\"zh-CN\"",
        js,
    )


def test_label_specs_have_no_dead_keys(js: str, index_tree: _Tree) -> None:
    """applyLang() silently drops specs whose id is absent from the DOM."""
    ids = {
        node.attrs["id"] for node, _ in _elements(index_tree.root) if "id" in node.attrs
    }
    specs = _label_specs(js)

    spec_ids = [row[0] for row in specs]
    duplicates = {value for value in spec_ids if spec_ids.count(value) > 1}
    assert not duplicates, f"duplicate LABEL_SPECS ids: {sorted(duplicates)}"

    dead = sorted(set(spec_ids) - ids)
    assert not dead, f"LABEL_SPECS ids not present in index.html: {dead}"


def test_label_specs_carry_both_languages(js: str) -> None:
    problems = []
    for spec_id, zh, en in _label_specs(js):
        if not zh.strip() or not en.strip():
            problems.append(f"{spec_id}: empty label")
        if zh == en:
            problems.append(f"{spec_id}: zh and en are identical")
        if CJK.search(en):
            problems.append(f"{spec_id}: en label contains Chinese: {en!r}")
    assert not problems, problems[:5]


def test_label_specs_cover_every_translated_aria_label(
    js: str, index_tree: _Tree
) -> None:
    """Every Chinese aria-label in the DOM needs a table row, and the table's
    zh value must be the one the static document ships."""
    specs = {spec_id: (zh, en) for spec_id, zh, en in _label_specs(js)}

    labelled = {}
    for node, _ in _elements(index_tree.root):
        label = node.attrs.get("aria-label", "")
        if not CJK.search(label):
            continue
        assert node.attrs.get("id"), f"translated aria-label without an id: {label!r}"
        labelled[node.attrs["id"]] = label

    missing = sorted(set(labelled) - set(specs))
    assert not missing, f"aria-label with no LABEL_SPECS row: {missing}"

    drifted = {
        key: (labelled[key], specs[key][0])
        for key in labelled
        if labelled[key] != specs[key][0]
    }
    assert not drifted, f"static aria-label drifted from LABEL_SPECS: {drifted}"


def test_burger_label_describes_the_open_action(index_tree: _Tree, js: str) -> None:
    """The button's static label is the closed state; it used to say just
    "菜单", which the table could not agree with."""
    burger = [
        node
        for node, _ in _elements(index_tree.root)
        if node.attrs.get("id") == "burger-btn"
    ]
    assert len(burger) == 1
    assert burger[0].attrs["aria-label"] == "打开菜单"
    assert '"关闭菜单"' in js and '"Close menu"' in js


# --------------------------------------------------------------------------
# attribute coverage: title / description / aria-label / placeholder
# --------------------------------------------------------------------------
def test_every_chinese_attribute_value_has_a_language_source(
    index_tree: _Tree,
) -> None:
    """A Chinese value in any *other* attribute has no switch path.

    This is what catches a future ``placeholder="搜索…"`` or ``title="…"``:
    neither is covered by ``data-en`` (which swaps innerHTML only) nor by
    ``LABEL_SPECS`` (which only writes ``aria-label``).
    """
    specs = {row[0] for row in _label_specs(JS_FILE.read_text(encoding="utf-8"))}
    uncovered = []
    for node, _ in _elements(index_tree.root):
        for attr, value in node.attrs.items():
            if attr == "data-en" or not CJK.search(value):
                continue
            if (node.tag, attr) in ATTRIBUTE_EXEMPT:
                continue
            if attr == "aria-label" and node.attrs.get("id") in specs:
                continue
            uncovered.append((node.tag, attr, value[:50]))
    assert not uncovered, f"Chinese attribute with no translation path: {uncovered[:5]}"


def test_no_placeholder_or_attribute_i18n_hooks(
    index_text: str, error_text: str
) -> None:
    """The site has no form controls, so the ``data-i18n-placeholder`` /
    ``data-i18n-aria`` variants have nothing to attach to.  Pin that: if one is
    introduced, this module's coverage model must be extended."""
    for text, name in ((index_text, "index.html"), (error_text, "404.html")):
        hooks = sorted(set(re.findall(r"\b(data-i18n[\w-]*)\b", text)))
        assert not hooks, f"{name} uses an unsupported i18n hook: {hooks}"
        assert not re.search(r"\bplaceholder\s*=", text), (
            f"{name} has a placeholder; add it to the i18n coverage"
        )


# --------------------------------------------------------------------------
# residue: neither language may leak into the other view
# --------------------------------------------------------------------------
def test_english_view_keeps_no_chinese(index_tree: _Tree) -> None:
    """Simulate applyLang("en"): swap every data-en element for its value."""
    leaks = []
    for text, ancestors in _en_leaves(index_tree.root):
        value = text.strip()
        if not value or not CJK.search(value):
            continue
        if any(node.tag in EN_VIEW_SKIP_TAGS for node in ancestors):
            continue
        if any(_classes(node) & EN_VIEW_SKIP_CLASSES for node in ancestors):
            continue
        if value.startswith(EN_VIEW_SKIP_PREFIXES):
            continue
        leaks.append(value[:80])
    assert not leaks, f"Chinese left in the English view: {leaks[:5]}"


def test_chinese_view_keeps_no_english_sentences(index_tree: _Tree) -> None:
    """The mirror check: untranslated prose left on the zh side."""
    leaks = []
    for text, ancestors in _texts(index_tree.root):
        value = re.sub(r"\s+", " ", text).strip()
        if not value or not LATIN_SENTENCE.search(value):
            continue
        if any(node.tag in ZH_VIEW_SKIP_TAGS for node in ancestors):
            continue
        leaks.append(value[:80])
    assert not leaks, f"English prose in the Chinese view: {leaks[:5]}"


def test_zh_sources_contain_no_english_sentences(index_tree: _Tree) -> None:
    """A data-en element whose Chinese half is English prose is a lost
    translation that the residue test above cannot see."""
    leaks = []
    for _, ancestors, zh, _ in _data_en_entries(index_tree):
        value = re.sub(r"\s+", " ", zh).strip()
        if not LATIN_SENTENCE.search(value):
            continue
        if any(node.tag in {"pre", "code", "noscript"} for node in ancestors):
            continue
        leaks.append(value[:80])
    assert not leaks, f"English prose in a zh source: {leaks[:5]}"


# --------------------------------------------------------------------------
# 404.html: bilingual markup, no toggle
# --------------------------------------------------------------------------
def test_404_has_no_language_toggle_to_cover(error_text: str) -> None:
    """If a toggle ever lands here it must come with its own coverage."""
    assert 'id="lang-btn"' not in error_text
    assert "data-en" not in error_text
    assert not re.search(r"\bdata-i18n[\w-]*", error_text)


def test_404_english_runs_are_english(error_text: str) -> None:
    tree = _parse(error_text)
    runs = [
        _plain(node)
        for node, _ in _elements(tree.root)
        if node.attrs.get("lang") == "en"
    ]
    assert len(runs) >= 15, f"only {len(runs)} English runs on the 404 page"

    problems = []
    for text in runs:
        if not text.strip():
            problems.append("empty lang=en run")
        if CJK.search(text):
            problems.append(f"lang=en run contains Chinese: {text[:60]!r}")
    assert not problems, problems[:5]


def test_404_every_english_run_has_a_chinese_counterpart(error_text: str) -> None:
    """The page shows both languages at once; a lone English half would mean a
    dropped Chinese string."""
    tree = _parse(error_text)
    orphans = []
    for node, ancestors in _elements(tree.root):
        if node.attrs.get("lang") != "en" or not ancestors:
            continue
        parent = ancestors[-1]
        chinese_half = "".join(
            child for child in parent.children if isinstance(child, str)
        )
        if not CJK.search(chinese_half):
            orphans.append(_plain(node)[:60])
    assert not orphans, f"English run without a Chinese half: {orphans[:5]}"


def test_404_description_is_bilingual(error_text: str) -> None:
    match = re.search(r'<meta name="description" content="(.*?)">', error_text, re.S)
    assert match is not None
    description = match.group(1)
    assert CJK.search(description), "404 description lost its Chinese half"
    assert LATIN_SENTENCE.search(description), "404 description lost its English half"


def test_404_title_is_present_and_single_line(error_text: str) -> None:
    match = re.search(r"<title>(.*?)</title>", error_text, re.S)
    assert match is not None
    title = match.group(1).strip()
    assert title and "\n" not in title
    assert "404" in title
