#!/usr/bin/env python3
"""Tests for the extended rule features.

Covers ``Field.steps`` / ``Field.fallback``, section ``preprocess``, the
pagination guards, per-source ``limits`` and ``search.single_result_fallback``.
Every feature is optional, so a few tests pin the legacy default behaviour.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from novel_downloader.plugins.rule import _make_fetcher
from novel_downloader.plugins.rule.engine import (
    PY_STEP_FUNCTIONS,
    apply_steps,
    body_fingerprint,
    extract,
    extract_multi,
    matches_stop_when,
    parse_page,
    preprocess,
    should_stop_pagination,
)
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.model import (
    Field,
    Limits,
    PagesRule,
    Rule,
    RuleError,
    Step,
    apply_limits,
    parse_rule,
)
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.schemas import FetcherConfig, ParserConfig

BASE = "https://demo.example"

MINIMAL: dict[str, Any] = {
    "site_key": "demo",
    "base_url": BASE,
    "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
}


# --------------------------------------------------------------------------- #
# Field.steps
# --------------------------------------------------------------------------- #
def test_steps_run_after_regex_and_replace() -> None:
    fld = Field.from_dict(
        {
            "selector": "span",
            "regex": r"前缀：(.*)",
            "group": 1,
            "replace": [["，", ","]],
            "steps": [
                {"lang": "py", "name": "normalize_space"},
                {"lang": "js", "code": 'input + "!"'},
            ],
        }
    )

    tree = parse_page("<span>前缀：  甲，乙 </span>")

    # regex -> replace -> py step -> js step
    assert extract(tree, fld) == "甲,乙!"


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("strip_html", "<b>x</b><!--c-->y", "xy"),
        ("normalize_space", " a \n\t b  ", "a b"),
        ("dedupe_lines", "a\n\nb\na", "a\nb"),
        ("base64_decode", "5L2g5aW9", "你好"),
        ("url_decode", "a%20b", "a b"),
        ("url_encode", "a b", "a%20b"),
        ("html_unescape", "&lt;x&gt;", "<x>"),
        ("strip", "  x  ", "x"),
        ("upper", "abc", "ABC"),
    ],
)
def test_py_step_whitelist_is_pure_string_functions(
    name: str, raw: str, expected: str
) -> None:
    assert name in PY_STEP_FUNCTIONS
    assert apply_steps(raw, [Step.from_dict({"lang": "py", "name": name})]) == expected


def test_py_step_unknown_name_rejected_at_parse_time() -> None:
    with pytest.raises(RuleError, match="unknown py step 'evil'"):
        Field.from_dict({"selector": "a", "steps": [{"lang": "py", "name": "evil"}]})


def test_js_step_syntax_error_rejected_at_parse_time() -> None:
    with pytest.raises(RuleError, match="unsupported JavaScript"):
        Field.from_dict(
            {"selector": "a", "steps": [{"lang": "js", "code": "if (x) { x }"}]}
        )


def test_js_step_without_code_is_rejected() -> None:
    with pytest.raises(RuleError, match="non-empty 'code'"):
        Field.from_dict({"selector": "a", "steps": [{"lang": "js"}]})


def test_js_step_unsupported_syntax_raises_at_runtime() -> None:
    fld = Field.from_dict(
        {"selector": "p", "steps": [{"lang": "js", "code": "input.trim()"}]}
    )

    with pytest.raises(RuleError, match="js step failed"):
        extract(parse_page("<p>hi</p>"), fld)


def test_js_step_can_call_injected_helpers() -> None:
    fld = Field.from_dict(
        {
            "selector": "p",
            "attr": "html",
            "steps": [
                {
                    "lang": "js",
                    "code": (
                        'strip_html(input) + "|" + '
                        'normalize_space("  a   b  ") + "|" + '
                        'replace_all("x-y", "-", "+")'
                    ),
                }
            ],
        }
    )

    tree = parse_page("<p><b>hi</b></p>")

    assert extract(tree, fld) == "hi|a b|x+y"


def test_js_regex_replace_helper() -> None:
    fld = Field.from_dict(
        {
            "selector": "p",
            "attr": "html",
            "steps": [
                {
                    "lang": "js",
                    "code": 'regex_replace(input, "<b>.*?</b>", "[b]")',
                }
            ],
        }
    )

    assert extract(parse_page("<p><b>hi</b> there</p>"), fld) == "[b] there"


def test_js_step_must_return_a_string() -> None:
    fld = Field.from_dict(
        {"selector": "p", "steps": [{"lang": "js", "code": "[1, 2]"}]}
    )

    with pytest.raises(RuleError, match="must produce a string"):
        extract(parse_page("<p>hi</p>"), fld)


def test_steps_do_not_run_on_an_empty_value() -> None:
    fld = Field.from_dict(
        {
            "selector": "p.missing",
            "default": "fallback",
            "steps": [{"lang": "js", "code": '"invented"'}],
        }
    )

    assert extract(parse_page("<p>hi</p>"), fld) == "fallback"


def test_step_failure_reports_the_rule_location() -> None:
    fld = Field.from_dict(
        {
            "selector": "p",
            "steps": [{"lang": "js", "code": "input.trim()"}],
        },
        where="book.fields.summary",
    )

    with pytest.raises(RuleError, match=r"book\.fields\.summary\.steps\[0\]"):
        extract(parse_page("<p>hi</p>"), fld)


def test_step_language_is_validated() -> None:
    with pytest.raises(RuleError, match="unsupported step language"):
        Field.from_dict({"selector": "a", "steps": [{"lang": "lua", "code": "x"}]})
    with pytest.raises(RuleError, match="step must be an object"):
        Field.from_dict({"selector": "a", "steps": ["input"]})


# --------------------------------------------------------------------------- #
# Field.fallback
# --------------------------------------------------------------------------- #
def test_fallback_string_reuses_attr_and_steps() -> None:
    fld = Field.from_dict(
        {
            "selector": "div.missing",
            "attr": "html",
            "steps": [{"lang": "py", "name": "strip_html"}],
            "fallback": ["div.alt"],
        }
    )

    tree = parse_page('<div class="alt"><i>ok</i></div>')

    assert extract(tree, fld) == "ok"
    assert fld.fallback[0].attr == "html"
    assert fld.fallback[0].steps == fld.steps
    # the inherited copy must not carry the parent chain (no recursion)
    assert fld.fallback[0].fallback == []


def test_fallback_dict_is_an_independent_field() -> None:
    fld = Field.from_dict(
        {
            "selector": "#none",
            "fallback": [{"selector": "span", "attr": "text"}],
        }
    )

    assert extract(parse_page("<span> hi </span>"), fld) == "hi"


def test_fallback_chain_order_and_default_last() -> None:
    fld = Field.from_dict(
        {
            "selector": "#none",
            "default": "D",
            "fallback": ["#also-none", {"selector": "span"}],
        }
    )

    assert extract(parse_page("<span> second </span>"), fld) == "second"
    assert extract(parse_page("<p>nothing</p>"), fld) == "D"


def test_fallback_rejects_bad_shapes() -> None:
    with pytest.raises(RuleError, match="must be a list"):
        Field.from_dict({"selector": "a", "fallback": "b"})
    with pytest.raises(RuleError, match="selector string or object"):
        Field.from_dict({"selector": "a", "fallback": [3]})
    with pytest.raises(RuleError, match="non-empty selector"):
        Field.from_dict({"selector": "a", "fallback": [""]})


def test_fallback_fields_are_validated_at_parse_time() -> None:
    with pytest.raises(RuleError, match="empty 'attr'"):
        parse_rule(
            {
                **MINIMAL,
                "book": {
                    "url": "{base_url}/{book_id}/",
                    "fields": {
                        "summary": {
                            "selector": "p",
                            "fallback": [{"selector": "div", "attr": ""}],
                        }
                    },
                    "chapters": {"item": "a"},
                },
            }
        )


def test_extract_multi_uses_first_non_empty_fallback() -> None:
    fld = Field.from_dict({"selector": "#none", "multi": True, "fallback": ["li"]})

    tree = parse_page("<ul><li>a</li><li>b</li></ul>")

    assert extract_multi(tree, fld) == ["a", "b"]


def test_field_defaults_keep_the_legacy_shape() -> None:
    fld = Field.from_dict({"selector": "p"})

    assert fld.steps == []
    assert fld.fallback == []
    assert fld.attr == "text"


# --------------------------------------------------------------------------- #
# section preprocess
# --------------------------------------------------------------------------- #
CHAPTER_PREPROCESS = [
    {
        "lang": "js",
        "code": 'regex_replace(input, "<div class=\\"fake\\">.*?</div>", "")',
    },
    {
        "lang": "js",
        "code": 'replace_all(input, "onclick=\\"read(1)\\"", "href=\\"/c/1.html\\"")',
    },
]


def _book_rule(**chapters_extra: Any) -> dict[str, Any]:
    chapters: dict[str, Any] = {"item": "a.chapter", **chapters_extra}
    return {
        "site_key": "demo",
        "base_url": BASE,
        "book": {
            "url": "{base_url}/book/{book_id}/",
            "chapters": chapters,
        },
    }


def make_parser(rule: Rule) -> RuleParser:
    cls = type(
        f"RuleParser_{rule.site_key}",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    return cast(RuleParser, cls(ParserConfig()))


def test_chapters_preprocess_drops_decoys_and_rewrites_href() -> None:
    page = (
        '<div id="list">'
        '<div class="fake"><a class="chapter" href="/fake">假章节</a></div>'
        '<div><a class="chapter" onclick="read(1)">真章节</a></div>'
        "</div>"
    )
    rule = parse_rule(_book_rule(container="div#list", preprocess=CHAPTER_PREPROCESS))

    info = make_parser(rule).parse_book_info([page])

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["title"] for c in chapters] == ["真章节"]
    assert chapters[0]["url"] == f"{BASE}/c/1.html"


def test_chapter_preprocess_runs_on_the_content_container() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "chapter": {
                "url": "{base_url}/{book_id}/{chapter_id}.html",
                "container": "div.body",
                "content": {"selector": "article"},
                "preprocess": [
                    {
                        "lang": "js",
                        "code": (
                            'replace_all(input, "PLACEHOLDER", '
                            '"<article>正文</article>")'
                        ),
                    }
                ],
            },
        }
    )
    page = '<div class="body"><div>PLACEHOLDER</div></div>'

    chapter = make_parser(rule).parse_chapter_content([page], "1")

    assert chapter is not None
    assert chapter["content"] == "正文"


def test_preprocess_without_container_uses_the_whole_page() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "chapter": {
                "url": "{base_url}/{book_id}/{chapter_id}.html",
                "content": {"selector": "article"},
                "preprocess": [
                    {
                        "lang": "js",
                        "code": (
                            'replace_all(input, "PLACEHOLDER", '
                            '"<article>正文</article>")'
                        ),
                    }
                ],
            },
        }
    )

    chapter = make_parser(rule).parse_chapter_content(["<div>PLACEHOLDER</div>"], "1")

    assert chapter is not None
    assert chapter["content"] == "正文"


def test_preprocess_empty_result_is_a_hard_error() -> None:
    with pytest.raises(RuleError, match="produced an empty document"):
        preprocess(
            "<div>x</div>",
            [Step.from_dict({"lang": "js", "code": '""'})],
        )


def test_preprocess_missing_container_is_a_hard_error() -> None:
    with pytest.raises(RuleError, match="matched nothing"):
        preprocess(
            "<div>x</div>",
            [Step.from_dict({"lang": "js", "code": "input"})],
            container="div#missing",
        )


def test_preprocess_on_json_section_is_rejected() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "book": {
                "url": "{base_url}/{book_id}/",
                "format": "json",
                "chapters": {
                    "item": "data[*]",
                    "preprocess": [{"lang": "py", "name": "normalize_space"}],
                },
            },
        }
    )

    with pytest.raises(RuleError, match="requires 'format': 'html'"):
        make_parser(rule).parse_book_info(['{"data": []}'])


def test_preprocess_failure_propagates_for_chapter_content() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "chapter": {
                "url": "{base_url}/{book_id}/{chapter_id}.html",
                "content": {"selector": "article"},
                "preprocess": [{"lang": "js", "code": '""'}],
            },
        }
    )

    with pytest.raises(RuleError, match="produced an empty document"):
        make_parser(rule).parse_chapter_content(["<article>x</article>"], "1")


def test_preprocess_is_optional_and_keeps_legacy_parsing() -> None:
    rule = parse_rule(_book_rule())
    page = '<a class="chapter" href="/c/1.html">第一章</a>'

    info = make_parser(rule).parse_book_info([page])

    assert info is not None
    assert info["volumes"][0]["chapters"][0]["title"] == "第一章"


# --------------------------------------------------------------------------- #
# pagination guards
# --------------------------------------------------------------------------- #
def _pages(**extra: Any) -> PagesRule:
    return PagesRule.from_dict({"url": "{base_url}/p/{page}", **extra})


def test_stop_when_matches_the_current_page() -> None:
    pages = _pages(stop_when="THE-END")

    assert matches_stop_when(pages, "x THE-END y")
    assert not matches_stop_when(pages, "nothing here")


def test_invalid_stop_when_regex_is_rejected() -> None:
    with pytest.raises(RuleError, match="invalid regex"):
        _pages(stop_when="(")


def test_loop_guard_rejects_non_boolean() -> None:
    with pytest.raises(RuleError, match="must be a boolean"):
        _pages(loop_guard="false")


def test_loop_guard_stops_on_repeated_body() -> None:
    seen_urls: set[str] = set()
    seen_bodies = {body_fingerprint("<p>same</p>")}

    assert should_stop_pagination(
        _pages(), "<p>same</p>", f"{BASE}/p/2", seen_urls, seen_bodies
    )
    assert f"{BASE}/p/2" in seen_urls


def test_loop_guard_allows_a_new_body() -> None:
    seen_urls: set[str] = set()
    seen_bodies = {body_fingerprint("<p>one</p>")}

    assert not should_stop_pagination(
        _pages(), "<p>two</p>", f"{BASE}/p/2", seen_urls, seen_bodies
    )


def test_loop_guard_stops_on_repeated_url() -> None:
    seen_urls = {f"{BASE}/p/2"}

    assert should_stop_pagination(
        _pages(), "<p>fresh</p>", f"{BASE}/p/2", seen_urls, set()
    )


def test_loop_guard_can_be_disabled() -> None:
    seen_bodies = {body_fingerprint("<p>same</p>")}

    assert not should_stop_pagination(
        _pages(loop_guard=False),
        "<p>same</p>",
        f"{BASE}/p/2",
        set(),
        seen_bodies,
    )


class _StubFetcher(RuleFetcher):
    """A :class:`RuleFetcher` whose HTTP layer is a URL -> HTML map."""

    site_name = "demo"

    def __init__(self, rule: Rule, pages: dict[str, str]) -> None:
        self._rule = rule  # type: ignore[misc]  # per-test rule binding
        self._base_url = ""
        self.pages = pages
        self.fetched: list[str] = []

    async def fetch(self, url: str, encoding: str = "utf-8", **kwargs: Any) -> str:
        self.fetched.append(url)
        return self.pages[url]


PAGINATED_RULE: dict[str, Any] = {
    "site_key": "demo",
    "base_url": BASE,
    "book": {
        "url": "{base_url}/book/{book_id}/",
        "pages": {"url": "{base_url}/book/{book_id}/index_{page}.html", "start": 2},
        "chapters": {"item": "div.list a"},
    },
}

BOOK_URL = f"{BASE}/book/7/"


def _index(index: int, body: str) -> str:
    return f"{BASE}/book/7/index_{index}.html"


@pytest.mark.asyncio
async def test_fetcher_loop_guard_stops_on_duplicate_page() -> None:
    pages = {
        BOOK_URL: '<a href="index_2.html">next</a>',
        _index(2, "body"): 'same <a href="index_3.html">next</a>',
        _index(3, "body"): 'same <a href="index_3.html">next</a>',
        _index(4, "body"): 'same <a href="index_4.html">next</a>',
    }
    fetcher = _StubFetcher(parse_rule(PAGINATED_RULE), pages)

    await fetcher.fetch_book_info("7")

    assert fetcher.fetched == [BOOK_URL, _index(2, "body"), _index(3, "body")]


@pytest.mark.asyncio
async def test_fetcher_loop_guard_disabled_fetches_to_max_pages() -> None:
    rule = parse_rule(
        {
            **PAGINATED_RULE,
            "book": {
                **PAGINATED_RULE["book"],
                "pages": {
                    "url": "{base_url}/book/{book_id}/index_{page}.html",
                    "start": 2,
                    "max_pages": 4,
                    "loop_guard": False,
                },
            },
        }
    )
    pages = {
        BOOK_URL: '<a href="index_2.html">next</a>',
        _index(2, "body"): 'same <a href="index_3.html">next</a>',
        _index(3, "body"): 'same <a href="index_4.html">next</a>',
        _index(4, "body"): "same last",
    }
    fetcher = _StubFetcher(rule, pages)

    await fetcher.fetch_book_info("7")

    assert fetcher.fetched == [
        BOOK_URL,
        _index(2, "body"),
        _index(3, "body"),
        _index(4, "body"),
    ]


@pytest.mark.asyncio
async def test_fetcher_stop_when_stops_pagination() -> None:
    rule = parse_rule(
        {
            **PAGINATED_RULE,
            "book": {
                **PAGINATED_RULE["book"],
                "pages": {
                    "url": "{base_url}/book/{book_id}/index_{page}.html",
                    "start": 2,
                    "stop_when": "THE-END",
                },
            },
        }
    )
    pages = {
        BOOK_URL: '<a href="index_2.html">next</a>',
        _index(2, "body"): 'THE-END <a href="index_3.html">next</a>',
        _index(3, "body"): '<a href="index_4.html">next</a>',
    }
    fetcher = _StubFetcher(rule, pages)

    await fetcher.fetch_book_info("7")

    assert fetcher.fetched == [BOOK_URL, _index(2, "body")]


@pytest.mark.asyncio
async def test_fetcher_stop_when_on_entry_page_fetches_nothing_extra() -> None:
    rule = parse_rule(
        {
            **PAGINATED_RULE,
            "book": {
                **PAGINATED_RULE["book"],
                "pages": {
                    "url": "{base_url}/book/{book_id}/index_{page}.html",
                    "start": 2,
                    "stop_when": "THE-END",
                },
            },
        }
    )
    pages = {BOOK_URL: "THE-END"}
    fetcher = _StubFetcher(rule, pages)

    await fetcher.fetch_book_info("7")

    assert fetcher.fetched == [BOOK_URL]


# --------------------------------------------------------------------------- #
# Rule.limits
# --------------------------------------------------------------------------- #
def test_apply_limits_is_pure_and_copies() -> None:
    config = FetcherConfig()

    merged = apply_limits(
        config,
        Limits(
            concurrency=2,
            min_interval=0.2,
            max_interval=0.4,
            max_attempts=4,
            retry_min_interval=1.0,
            retry_max_interval=3.0,
        ),
    )

    # the shared object is untouched
    assert config.max_connections == 10
    assert config.request_interval == 0.5
    assert config.retry_times == 3
    assert config.backoff_factor == 2.0
    # ... and the copy carries the limits
    assert merged is not config
    assert merged.max_connections == 2
    assert merged.request_interval == pytest.approx(0.3)
    assert merged.retry_times == 3
    assert merged.backoff_factor == pytest.approx(2.0)


def test_apply_limits_none_returns_the_same_object() -> None:
    config = FetcherConfig()

    assert apply_limits(config, None) is config


def test_apply_limits_partial_update_only_touches_given_fields() -> None:
    merged = apply_limits(FetcherConfig(), Limits(concurrency=5))

    assert merged.max_connections == 5
    assert merged.request_interval == 0.5
    assert merged.retry_times == 3


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"concurrency": 0}, "concurrency must be >= 1"),
        ({"concurrency": -1}, "concurrency must be >= 1"),
        ({"min_interval": -0.1}, "must be non-negative"),
        ({"max_attempts": 0}, "max_attempts must be >= 1"),
        ({"min_interval": 2, "max_interval": 1}, "greater than max"),
        ({"retry_min_interval": 5, "retry_max_interval": 1}, "greater than max"),
        ({"min_interval": True}, "must be a number"),
        ({"nope": 1}, "unknown key"),
    ],
)
def test_limits_validation(limits: dict[str, Any], message: str) -> None:
    with pytest.raises(RuleError, match=message):
        parse_rule({**MINIMAL, "limits": limits})


def test_limits_default_to_absent() -> None:
    assert parse_rule(MINIMAL).limits is None


def test_limits_are_applied_when_a_rule_fetcher_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, FetcherConfig] = {}

    def fake_init(
        self: RuleFetcher,
        config: FetcherConfig,
        cookies: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        captured["config"] = config

    monkeypatch.setattr(RuleFetcher, "__init__", fake_init)
    rule = parse_rule({**MINIMAL, "limits": {"concurrency": 2}})
    cls = _make_fetcher(rule, "demo")

    cls.__init__(object.__new__(cls), FetcherConfig(), None)

    assert captured["config"].max_connections == 2
    assert FetcherConfig().max_connections == 10


def test_rules_without_limits_keep_the_plain_fetcher_init() -> None:
    cls = _make_fetcher(parse_rule(MINIMAL), "demo")

    assert "__init__" not in cls.__dict__


# --------------------------------------------------------------------------- #
# search.single_result_fallback
# --------------------------------------------------------------------------- #
SEARCH_RULE: dict[str, Any] = {
    "site_key": "demo",
    "base_url": BASE,
    "search": {
        "url": "https://demo.example/search.php?q={keyword}",
        "item": "div.result",
        "single_result_fallback": True,
    },
    "book": {
        "url": "{base_url}/book/{book_id}/",
        "fields": {
            "book_name": {"selector": "h1", "attr": "text"},
            "author": {"selector": "span.author", "attr": "text"},
            "cover_url": {"selector": "img.cover", "attr": "src"},
            "word_count": {"selector": "span.words", "attr": "text"},
        },
        "chapters": {"item": "a"},
    },
}

DETAIL_PAGE = """
<html><body>
  <h1>斗破苍穹</h1>
  <span class="author">天蚕土豆</span>
  <img class="cover" src="/covers/7.jpg">
  <span class="words">1024</span>
</body></html>
"""

SEARCH_URL = f"{BASE}/search.php?q=test"
DETAIL_URL = f"{BASE}/book/7/"


def make_searcher(rule: Rule) -> RuleSearcher:
    cls = type(
        f"RuleSearcher_{rule.site_key}",
        (RuleSearcher,),
        {
            "_rule": rule,
            "site_name": rule.site_key,
            "BASE_URL": rule.base_url,
            "priority": rule.search.priority if rule.search else rule.priority,
        },
    )
    return cast(RuleSearcher, cls(session=None))


def test_single_result_fallback_uses_the_final_url() -> None:
    searcher = make_searcher(parse_rule(SEARCH_RULE))
    searcher._request_url = SEARCH_URL
    searcher._final_url = DETAIL_URL

    results = searcher._parse_html(DETAIL_PAGE)

    assert len(results) == 1
    result = results[0]
    assert result["book_url"] == DETAIL_URL
    assert result["book_id"] == "7"
    assert result["title"] == "斗破苍穹"
    assert result["author"] == "天蚕土豆"
    assert result["cover_url"] == f"{BASE}/covers/7.jpg"
    assert result["word_count"] == "1024"
    assert result["site"] == "demo"


def test_single_result_fallback_requires_book_name_without_redirect() -> None:
    searcher = make_searcher(parse_rule(SEARCH_RULE))
    searcher._request_url = DETAIL_URL
    searcher._final_url = DETAIL_URL

    results = searcher._parse_html(DETAIL_PAGE)

    assert [r["title"] for r in results] == ["斗破苍穹"]
    assert results[0]["book_url"] == DETAIL_URL


def test_single_result_fallback_needs_evidence() -> None:
    searcher = make_searcher(parse_rule(SEARCH_RULE))
    searcher._request_url = SEARCH_URL
    searcher._final_url = SEARCH_URL

    assert searcher._parse_html("<html><body>没有结果</body></html>") == []


def test_single_result_fallback_without_any_url_returns_nothing() -> None:
    searcher = make_searcher(parse_rule(SEARCH_RULE))

    assert searcher._parse_html(DETAIL_PAGE) == []


def test_single_result_fallback_is_off_by_default() -> None:
    rule_dict = {
        **SEARCH_RULE,
        "search": {
            k: v
            for k, v in SEARCH_RULE["search"].items()
            if k != "single_result_fallback"
        },
    }
    searcher = make_searcher(parse_rule(rule_dict))
    searcher._request_url = SEARCH_URL
    searcher._final_url = DETAIL_URL

    assert searcher._parse_html(DETAIL_PAGE) == []


def test_single_result_fallback_rejects_non_boolean() -> None:
    rule_dict = {
        **SEARCH_RULE,
        "search": {**SEARCH_RULE["search"], "single_result_fallback": "yes"},
    }

    with pytest.raises(RuleError, match="must be a boolean"):
        parse_rule(rule_dict)


class _FakeResponse:
    url = DETAIL_URL
    charset = "utf-8"

    async def read(self) -> bytes:
        return b"<html>ok</html>"


@pytest.mark.asyncio
async def test_fetch_html_records_the_final_url() -> None:
    searcher = make_searcher(parse_rule(SEARCH_RULE))

    text = await searcher._text_of(_FakeResponse())

    assert text == "<html>ok</html>"
    assert searcher._final_url == DETAIL_URL
