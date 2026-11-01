#!/usr/bin/env python3
"""Adversarial-audit regression tests for the rule engine.

These tests pin the behaviour of the features added in the "steps / fallback /
preprocess / container / pagination guard / limits / single_result_fallback"
rewrite that are *risky* rather than merely new:

* the default ``pages.loop_guard`` must never lose a page that the old
  (unguarded) walk would have collected with unique content;
* ``Field`` without ``steps``/``fallback`` must return exactly what the
  hand-written selector+attr+regex+replace path returns;
* rule-supplied regexes must not be able to hang the process (ReDoS);
* every ``py`` step must fail as ``RuleError`` (never a raw ``TypeError``)
  for hostile input;
* ``preprocess`` failures must name the rule location;
* ``apply_limits`` must be pure and reject deadlocking/inverted values;
* ``chapters.is_desc`` must reverse the *final flat* chapter order.
"""

from __future__ import annotations

import dataclasses
import inspect
import time
from typing import Any, cast

import pytest

from novel_downloader.libs.mini_js import MiniJS
from novel_downloader.plugins.rule import engine
from novel_downloader.plugins.rule.engine import (
    PY_STEP_FUNCTIONS,
    apply_steps,
    body_fingerprint,
    extract,
    extract_multi,
    parse_page,
    preprocess,
    select,
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
    regex_backtracking_risk,
)
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.schemas import FetcherConfig, ParserConfig

BASE = "https://demo.example"
BOOK = f"{BASE}/book/7"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def make_parser(rule: Rule) -> RuleParser:
    cls = type(
        f"RuleParser_{rule.site_key}",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    return cast(RuleParser, cls(ParserConfig()))


def _book_rule(**chapters_extra: Any) -> dict[str, Any]:
    return {
        "site_key": "demo",
        "base_url": BASE,
        "book": {
            "url": "{base_url}/book/{book_id}/",
            "chapters": {"item": "a.chapter", **chapters_extra},
        },
    }


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
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
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


def _pager(next_page: int) -> str:
    return f'<a href="index_{next_page}.html">下一页</a>'


def _catalog_pages(count: int) -> dict[str, str]:
    """``count`` catalog pages, each linking to the next one."""
    pages = {f"{BOOK}/": _pager(2)}
    for number in range(2, count + 1):
        pages[f"{BOOK}/index_{number}.html"] = _pager(number + 1)
    return pages


# --------------------------------------------------------------------------- #
# A. loop_guard: default must not lose pages
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("loop_guard", [None, True, False])
async def test_identical_shell_pages_are_not_cut_short(
    loop_guard: bool | None,
) -> None:
    """
    A shell page (same bytes for every page number, correct next link) is
    already stopped by the *pre-existing* suffix heuristic: page N+1's name is
    not in a body that only links to page N+2.  ``loop_guard`` must not make it
    stop any earlier, so the page count is identical with and without it.
    """
    shell = {n: _pager(n + 1) for n in range(2, 6)}
    pages = {
        f"{BOOK}/": _pager(2),
        **{f"{BOOK}/index_{n}.html": shell[n] for n in shell},
    }
    pages_rule: dict[str, Any] = {
        "url": "{base_url}/book/{book_id}/index_{page}.html",
        "start": 2,
    }
    if loop_guard is not None:
        pages_rule["loop_guard"] = loop_guard
    rule = parse_rule(
        {
            **PAGINATED_RULE,
            "book": {**PAGINATED_RULE["book"], "pages": pages_rule},
        }
    )

    fetched = await _StubFetcher(rule, pages).fetch_book_info("7")

    assert len(fetched) == 5


@pytest.mark.asyncio
async def test_loop_guard_stops_a_duplicate_last_page() -> None:
    """
    The site serves page 2 byte-for-byte for every later page number, and the
    pager lists all page links (so the pre-existing "next link is present"
    heuristic keeps walking).  Without the digest guard that is 50 identical
    fetches; with it, one duplicate is kept and the walk stops.
    """
    pager = "".join(f'<a href="index_{n}.html">{n}</a>' for n in range(2, 21))
    last_page = f"<p>last</p>{pager}"
    pages = {f"{BOOK}/": _pager(2)}
    for number in range(2, 21):
        pages[f"{BOOK}/index_{number}.html"] = last_page

    guarded = await _StubFetcher(parse_rule(PAGINATED_RULE), pages).fetch_book_info("7")

    # entry + page 2 + the first duplicate, then stop
    assert len(guarded) == 3
    # everything after the entry is the same body, so no unique content is lost
    assert set(guarded[1:]) == {last_page}


@pytest.mark.asyncio
async def test_loop_guard_off_keeps_fetching_identical_pages() -> None:
    """The opt-out restores the old behaviour (identical pages, capped at 50)."""
    pager = "".join(f'<a href="index_{n}.html">{n}</a>' for n in range(2, 21))
    last_page = f"<p>last</p>{pager}"
    pages = {f"{BOOK}/": _pager(2)}
    for number in range(2, 21):
        pages[f"{BOOK}/index_{number}.html"] = last_page
    rule = parse_rule(
        {
            **PAGINATED_RULE,
            "book": {
                **PAGINATED_RULE["book"],
                "pages": {
                    "url": "{base_url}/book/{book_id}/index_{page}.html",
                    "start": 2,
                    "loop_guard": False,
                },
            },
        }
    )

    fetched = await _StubFetcher(rule, pages).fetch_book_info("7")

    assert len(fetched) == 20


@pytest.mark.asyncio
async def test_loop_guard_keeps_walking_after_a_single_duplicate() -> None:
    """
    A duplicate only stops the walk when the *next* page name is still present
    in the repeated body.  A page that repeats an earlier body but links one
    page further does not trigger the guard.
    """
    pages = {
        f"{BOOK}/": _pager(2),
        f"{BOOK}/index_2.html": "<p>dup</p>" + _pager(3),
        f"{BOOK}/index_3.html": "<p>dup</p>" + _pager(4),
        f"{BOOK}/index_4.html": "<p>UNIQUE</p>",
    }

    fetched = await _StubFetcher(parse_rule(PAGINATED_RULE), pages).fetch_book_info("7")

    assert fetched[-1] == "<p>UNIQUE</p>"
    assert len(fetched) == 4


@pytest.mark.asyncio
async def test_loop_guard_does_not_stop_on_a_per_page_token() -> None:
    """A random token per page defeats the digest guard (no false stop)."""
    pages = {
        f"{BOOK}/": _pager(2) + "<!--tok-1-->",
        f"{BOOK}/index_2.html": _pager(3) + "<!--tok-2-->",
        f"{BOOK}/index_3.html": _pager(4) + "<!--tok-3-->",
        f"{BOOK}/index_4.html": "<p>end</p>" + "<!--tok-4-->",
    }

    fetched = await _StubFetcher(parse_rule(PAGINATED_RULE), pages).fetch_book_info("7")

    assert len(fetched) == 4


@pytest.mark.asyncio
async def test_loop_guard_default_is_true_and_opt_out_works() -> None:
    assert PagesRule.from_dict({"url": "x"}).loop_guard is True
    assert PagesRule.from_dict({"url": "x", "loop_guard": False}).loop_guard is False


def test_opt_out_still_records_the_url_as_visited() -> None:
    """``loop_guard: false`` disables the digest check, not the URL check."""
    seen_urls: set[str] = set()

    assert not engine.should_stop_pagination(
        PagesRule.from_dict({"url": "x", "loop_guard": False}),
        "<p>fresh</p>",
        f"{BASE}/p/2",
        seen_urls,
        set(),
    )
    assert seen_urls == {f"{BASE}/p/2"}


@pytest.mark.asyncio
async def test_stop_when_on_the_entry_page_skips_follow_ups() -> None:
    rule = parse_rule(
        {
            **PAGINATED_RULE,
            "book": {
                **PAGINATED_RULE["book"],
                "pages": {
                    "url": "{base_url}/book/{book_id}/index_{page}.html",
                    "stop_when": "THE-END",
                },
            },
        }
    )

    fetched = await _StubFetcher(rule, {f"{BOOK}/": "THE-END"}).fetch_book_info("7")

    assert len(fetched) == 1


# --------------------------------------------------------------------------- #
# A. legacy Field parity (no steps / no fallback)
# --------------------------------------------------------------------------- #
PAGE = """
<html><body>
  <div id="list">
    <a class="c" href="/book/7/1.html">  第一章   陨落的天才 </a>
    <a class="c" href="/book/7/2.html">第二章&nbsp;斗之气</a>
    <a class="c" href="/book/7/3.html">第三章</a>
  </div>
  <span class="author">天蚕土豆</span>
</body></html>
"""


def _legacy_text(root: Any, selector: str, attr: str) -> str:
    """The pre-steps implementation: select -> node_to_str -> strip."""
    from lxml import html as lxml_html

    nodes = select(root, selector, "html")
    if not nodes:
        return ""
    node = nodes[0]
    if attr == "text":
        return "".join(t.strip() for t in node.itertext() if t and t.strip())
    if attr == "inner_text":
        return "\n".join(t.strip() for t in node.itertext() if t and t.strip())
    if attr == "html":
        return "".join(lxml_html.tostring(child, encoding="unicode") for child in node)
    return (node.get(attr) or "").strip()


def test_field_without_steps_matches_the_hand_written_path() -> None:
    tree = parse_page(PAGE)
    cases = [
        (Field(selector="span.author", attr="text"), "天蚕土豆"),
        (Field(selector="span.author", attr="missing"), ""),
        (Field(selector="a.c", attr="href"), "/book/7/1.html"),
        (Field(selector="div#list", attr="inner_text"), None),
        (Field(selector="div#list", attr="html"), None),
        (Field(selector="a.nope", attr="text", default="兜底"), "兜底"),
    ]
    for fld, expected in cases:
        got = extract(tree, fld, "html")
        if expected is None:
            legacy = _legacy_text(tree, fld.selector or "", fld.attr).strip()
        else:
            legacy = expected
        assert got == legacy, f"{fld.selector}/{fld.attr}"


def test_field_regex_group_replace_matches_the_hand_written_path() -> None:
    tree = parse_page('<div><a class="c" href="/1.html">第1章 陨落的天才</a></div>')
    fld = Field(
        selector="a.c",
        attr="text",
        regex=r"^第(\d+)章\s+(.+)$",
        group=2,
        replace=[("陨落", "崛起")],
    )

    got = extract(tree, fld, "html")

    node = select(tree, "a.c", "html")[0]
    legacy = "".join(t.strip() for t in node.itertext() if t and t.strip())
    legacy = fld._pattern.search(legacy).group(2)  # type: ignore[union-attr]
    legacy = fld.apply_replace(legacy).strip()
    assert got == legacy == "崛起的天才"


def test_extract_multi_matches_the_hand_written_path() -> None:
    tree = parse_page(PAGE)
    fld = Field(selector="a.c", attr="href", multi=True)

    got = extract_multi(tree, fld, "html")

    legacy = [(n.get("href") or "").strip() for n in select(tree, "a.c", "html")]
    assert (
        got
        == legacy
        == [
            "/book/7/1.html",
            "/book/7/2.html",
            "/book/7/3.html",
        ]
    )


def test_extract_without_steps_raises_the_same_errors() -> None:
    tree = parse_page(PAGE)

    # an invalid CSS selector is a RuleError, exactly as before
    with pytest.raises(RuleError, match="invalid selector"):
        extract(tree, Field(selector="div[[", attr="text"), "html")
    # an invalid XPath is a RuleError
    with pytest.raises(RuleError, match="invalid selector"):
        extract(tree, Field(selector="//div[", attr="text"), "html")
    # a missing selector is *not* an error: empty string
    assert extract(tree, Field(selector="div#nope", attr="text"), "html") == ""
    assert extract_multi(tree, Field(selector="div#nope", attr="text"), "html") == []


# --------------------------------------------------------------------------- #
# B. steps safety: ReDoS
# --------------------------------------------------------------------------- #
EVIL_PATTERNS = [
    r"(a+)+$",
    r"(a*)*",
    r"(\d+)+",
    r"(a+){2,}",
    r"(a*b?)+",
    r"((a+))+",
    r"(a|)+",
]

SAFE_PATTERNS = [
    r"^/([\d_]+)/?$",
    r"/(\d+)\.html",
    r"^(.+?)\s*[-—]\s*《",
    r"^第\(\d+/\d+\)页$",
    r"(\d+\.)+",
    r"(?:foo|bar)+",
    r"(?P<name>\d+)",
    r"\d{2,3}-\d+",
    # zero-width lookarounds cannot backtrack exponentially
    r"(?=a)+",
    r"(?<=a)+",
    r"(?:ab|cd)*",
    r"(a){0,}",
]


@pytest.mark.parametrize("pattern", EVIL_PATTERNS)
def test_catastrophic_patterns_are_flagged(pattern: str) -> None:
    assert regex_backtracking_risk(pattern) is not None


@pytest.mark.parametrize("pattern", SAFE_PATTERNS)
def test_real_world_patterns_are_not_flagged(pattern: str) -> None:
    assert regex_backtracking_risk(pattern) is None


@pytest.mark.parametrize("pattern", EVIL_PATTERNS)
def test_evil_field_regex_is_rejected_at_parse_time(pattern: str) -> None:
    with pytest.raises(RuleError, match="catastrophic backtracking"):
        Field.from_dict({"selector": "a", "regex": pattern})


@pytest.mark.parametrize("pattern", EVIL_PATTERNS)
def test_evil_stop_when_and_drop_are_rejected_at_parse_time(pattern: str) -> None:
    with pytest.raises(RuleError, match="catastrophic backtracking"):
        PagesRule.from_dict({"url": "x", "stop_when": pattern})
    with pytest.raises(RuleError, match="catastrophic backtracking"):
        parse_rule(
            {
                "site_key": "demo",
                "chapter": {"url": "u", "drop": [pattern]},
            }
        )


def test_regex_replace_rejects_an_evil_pattern_quickly() -> None:
    """5000 chars of ``a`` must fail fast instead of backtracking for hours."""
    step = Step.from_dict(
        {
            "lang": "js",
            "code": 'regex_replace(input, "(a+)+$", "")',
        }
    )
    started = time.perf_counter()
    with pytest.raises(RuleError, match="catastrophic backtracking"):
        apply_steps("a" * 5000, [step])
    assert time.perf_counter() - started < 0.5


def test_regex_replace_still_works_for_safe_patterns() -> None:
    step = Step.from_dict({"lang": "js", "code": r'regex_replace(input, "\\d+", "#")'})

    assert apply_steps("a1b22c333", [step]) == "a#b#c#"


# --------------------------------------------------------------------------- #
# B. steps safety: py whitelist
# --------------------------------------------------------------------------- #
def test_base64_decode_rejects_non_base64_loudly() -> None:
    step = Step.from_dict({"lang": "py", "name": "base64_decode"})

    with pytest.raises(RuleError, match="base64"):
        apply_steps("这不是 base64!!", [step])
    with pytest.raises(RuleError, match="base64"):
        apply_steps("abc\u0000def", [step])


def test_base64_decode_accepts_long_input_and_empty_input() -> None:
    import base64

    step = Step.from_dict({"lang": "py", "name": "base64_decode"})
    payload = "长" * 100_000
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")

    assert apply_steps(encoded, [step]) == payload
    assert apply_steps("", [step]) == ""


def test_base64_decode_replaces_undecodable_bytes_instead_of_crashing() -> None:
    import base64

    step = Step.from_dict({"lang": "py", "name": "base64_decode"})
    encoded = base64.b64encode(b"\xff\xfe").decode("ascii")

    assert apply_steps(encoded, [step]) == "\ufffd\ufffd"


@pytest.mark.parametrize("name", sorted(PY_STEP_FUNCTIONS))
@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param(cast(Any, None), id="none"),
        pytest.param(cast(Any, b"bytes"), id="bytes"),
        pytest.param("x\u0000y", id="nul"),
        pytest.param("a" * 200_000, id="long"),
    ],
)
def test_every_py_step_fails_as_rule_error_on_hostile_input(
    name: str, hostile: Any
) -> None:
    """
    No raw TypeError/AttributeError may escape, and no multi-page traceback may
    reach the user.  A step that happens to accept the input (``unquote``
    tolerates bytes) may still succeed, but must return a ``str``.
    """
    step = Step(lang="py", name=name, where="book.fields.summary.steps[0]")

    try:
        result = apply_steps(hostile, [step])
    except RuleError as e:
        assert "book.fields.summary.steps[0]" in str(e)
        assert len(str(e)) < 400  # no multi-page traceback in the message
    else:
        assert isinstance(result, str)


def test_apply_steps_rejects_non_string_values_from_a_step() -> None:
    with pytest.raises(RuleError, match="must produce a string"):
        apply_steps("x", [Step.from_dict({"lang": "js", "code": "[1, 2]"})])


# --------------------------------------------------------------------------- #
# B. steps safety: mini-js public bind API + no loops
# --------------------------------------------------------------------------- #
def test_minijs_bind_is_public_and_works() -> None:
    js = MiniJS()
    js.bind("greeting", "hi")

    assert js.eval("greeting") == "hi"
    assert "greeting" in js.env()


def test_minijs_bind_rejects_bad_names() -> None:
    js = MiniJS()

    with pytest.raises(TypeError):
        js.bind(cast(Any, None), 1)
    with pytest.raises(ValueError):
        js.bind("not a name", 1)


def test_engine_injects_through_bind_not_the_private_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = MiniJS.bind

    def spy(self: MiniJS, name: str, value: Any) -> None:
        calls.append(name)
        original(self, name, value)

    monkeypatch.setattr(MiniJS, "bind", spy)

    assert apply_steps("x", [Step.from_dict({"lang": "js", "code": "input"})]) == "x"
    assert {"input", "result", "strip", "replace_all"} <= set(calls)


def test_engine_never_touches_the_private_env() -> None:
    assert "_env" not in inspect.getsource(engine)


@pytest.mark.parametrize(
    "code",
    [
        "while (true) {}",
        "for (let i = 0; i < 10; i = i + 1) {}",
        "do {} while (1)",
        "while (1) { x = 1 }",
    ],
)
def test_minijs_has_no_loop_construct(code: str) -> None:
    """
    mini-js has no ``while``/``for``/``do`` statement, so a step can never
    spin forever.  ``for`` is a hard syntax error; ``while``/``do`` are not
    keywords, so they parse as identifiers and fail at run time with a
    ``RuleError`` (never a hang).
    """
    try:
        Step.from_dict({"lang": "js", "code": code})
    except RuleError as e:
        assert "unsupported JavaScript" in str(e)
        return
    with pytest.raises(RuleError, match="js step failed"):
        apply_steps("x", [Step.from_dict({"lang": "js", "code": code})])


# --------------------------------------------------------------------------- #
# C. preprocess
# --------------------------------------------------------------------------- #
def test_preprocess_missing_container_names_the_rule_location() -> None:
    rule = parse_rule(
        _book_rule(
            container="div#missing", preprocess=[{"lang": "js", "code": "input"}]
        )
    )

    with pytest.raises(RuleError) as excinfo:
        make_parser(rule).parse_book_info(["<div>x</div>"])

    message = str(excinfo.value)
    assert "book.chapters.preprocess" in message
    assert "matched nothing" in message


def test_preprocess_empty_output_names_the_failing_step() -> None:
    rule = parse_rule(
        _book_rule(
            preprocess=[
                {"lang": "py", "name": "strip"},
                {"lang": "js", "code": '""'},
            ]
        )
    )

    with pytest.raises(RuleError) as excinfo:
        make_parser(rule).parse_book_info(["<div>x</div>"])

    message = str(excinfo.value)
    assert "produced an empty document" in message
    assert "book.chapters.preprocess[1]" in message


def test_preprocess_invalid_html_names_the_failing_step() -> None:
    """A comment-only document is not parsable: report which step produced it."""
    rule = parse_rule(
        _book_rule(preprocess=[{"lang": "js", "code": '"<!-- only a comment -->"'}])
    )

    with pytest.raises(RuleError) as excinfo:
        make_parser(rule).parse_book_info(["<div>x</div>"])

    message = str(excinfo.value)
    assert "produced invalid HTML" in message
    assert "book.chapters.preprocess[0]" in message


def test_preprocess_on_a_json_chapter_section_is_rejected() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "chapter": {
                "url": "{base_url}/{book_id}/{chapter_id}.html",
                "format": "json",
                "content": {"selector": "data.content"},
                "preprocess": [{"lang": "py", "name": "normalize_space"}],
            },
        }
    )

    with pytest.raises(RuleError, match="requires 'format': 'html'"):
        make_parser(rule).parse_chapter_content(['{"data": {"content": "x"}}'], "1")


def test_preprocess_returns_the_input_untouched_without_steps() -> None:
    assert preprocess("<div>x</div>", []) == "<div>x</div>"


# --------------------------------------------------------------------------- #
# D. limits
# --------------------------------------------------------------------------- #
def test_apply_limits_does_not_mutate_the_config_deeply() -> None:
    config = FetcherConfig(headers={"User-Agent": "ua"}, request_interval=0.5)
    before = dataclasses.asdict(config)
    snapshot = dict(config.headers or {})

    merged = apply_limits(
        config,
        Limits(
            concurrency=1,
            min_interval=0.1,
            max_interval=0.3,
            max_attempts=2,
            retry_min_interval=0.5,
            retry_max_interval=1.5,
        ),
    )

    assert dataclasses.asdict(config) == before
    assert config.headers == snapshot
    assert merged is not config
    assert merged.headers == snapshot


def test_apply_limits_with_no_limits_returns_the_same_object() -> None:
    config = FetcherConfig()

    assert apply_limits(config, None) is config
    assert apply_limits(config, Limits()) is not config


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        # Wide band: the midpoint already sits under high / 1.1.
        (0.2, 5.0, (0.2 + 5.0) / 2.0),
        # Flat band: capped so the upward jitter cannot overshoot ``high``.
        (1.0, 1.0, 1.0 / 1.1),
        # Only a lower bound: used verbatim.
        (0.7, None, 0.7),
        # Only an upper bound: capped for the same reason.
        (None, 2.0, 2.0 / 1.1),
    ],
)
def test_apply_limits_keeps_the_jittered_sleep_inside_the_band(
    low: float | None, high: float | None, expected: float
) -> None:
    merged = apply_limits(FetcherConfig(), Limits(min_interval=low, max_interval=high))

    assert merged.request_interval == pytest.approx(expected)
    if high is not None:
        # ``BaseFetcher._sleep`` multiplies by uniform(1.0, 1.1).
        assert merged.request_interval * 1.1 <= high + 1e-9


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"concurrency": 0}, "concurrency must be >= 1"),
        ({"concurrency": -3}, "concurrency must be >= 1"),
        ({"min_interval": 2.0, "max_interval": 1.0}, "greater than max"),
        ({"retry_min_interval": 5.0, "retry_max_interval": 1.0}, "greater than max"),
        ({"max_attempts": 0}, "max_attempts must be >= 1"),
    ],
)
def test_limits_reject_deadlocking_or_inverted_values(
    limits: dict[str, Any], message: str
) -> None:
    with pytest.raises(RuleError, match=message):
        parse_rule(
            {
                "site_key": "demo",
                "chapter": {"url": "u"},
                "limits": limits,
            }
        )


def test_interval_band_collapses_to_its_midpoint() -> None:
    merged = apply_limits(FetcherConfig(), Limits(min_interval=0.2, max_interval=5.0))

    assert merged.request_interval == pytest.approx(2.6)
    # The fetcher only jitters upward (random.uniform(1.0, 1.1)), so the real
    # spacing is 2.6 .. 2.86 -- inside the declared band here, but a band
    # narrower than 10% can be exceeded.  This is documented in
    # ``apply_limits``/``Limits``; the assertion pins the documented mapping.
    assert merged.request_interval * 1.1 <= 5.0


def test_single_interval_value_is_used_verbatim() -> None:
    merged = apply_limits(FetcherConfig(), Limits(min_interval=1.25))

    assert merged.request_interval == pytest.approx(1.25)


# --------------------------------------------------------------------------- #
# E. performance / empty-value handling
# --------------------------------------------------------------------------- #
def test_body_fingerprint_cost_is_acceptable_for_fifty_pages() -> None:
    body = "<div>" + ("x" * 500_000) + "</div>"
    started = time.perf_counter()
    for _ in range(50):
        body_fingerprint(body)
    elapsed = time.perf_counter() - started

    # ~500 KB * 50 = 25 MB of SHA-256; well under a second on any machine.
    assert elapsed < 2.0


def test_steps_are_not_applied_to_an_empty_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    original = engine.apply_steps

    def spy(value: str, steps: Any) -> str:
        called.append(value)
        return original(value, steps)

    monkeypatch.setattr(engine, "apply_steps", spy)
    tree = parse_page("<div>only</div>")
    fld = Field(
        selector="span.missing",
        attr="text",
        steps=[Step.from_dict({"lang": "js", "code": '"constant"'})],
    )

    assert extract(tree, fld, "html") == ""
    assert called == []


def test_apply_steps_receives_only_strings() -> None:
    step = Step.from_dict({"lang": "py", "name": "upper"})

    assert apply_steps("abc", [step]) == "ABC"
    with pytest.raises(RuleError):
        apply_steps(cast(Any, None), [step])


# --------------------------------------------------------------------------- #
# chapters.is_desc
# --------------------------------------------------------------------------- #
def _catalog_page(titles: list[str], *, start: int = 1) -> str:
    links = "".join(
        f'<a class="chapter" href="/book/7/{index}.html">{title}</a>'
        for index, title in enumerate(titles, start=start)
    )
    return f"<div>{links}</div>"


def test_is_desc_defaults_to_false_and_requires_a_bool() -> None:
    assert parse_rule(_book_rule()).book is not None
    rule = parse_rule(_book_rule())
    assert rule.book is not None
    assert rule.book.chapters.is_desc is False

    for bad in ("true", 1, 0, "yes"):
        with pytest.raises(RuleError, match="must be a boolean"):
            parse_rule(_book_rule(is_desc=bad))


def test_is_desc_reverses_a_single_page_catalog() -> None:
    ascending = ["第一章", "第二章", "第三章"]
    rule = parse_rule(_book_rule(is_desc=True))

    info = make_parser(rule).parse_book_info([_catalog_page(ascending)])

    assert info is not None
    titles = [c["title"] for c in info["volumes"][0]["chapters"]]
    assert titles == list(reversed(ascending))


@pytest.mark.asyncio
async def test_is_desc_composes_with_pagination_end_to_end() -> None:
    """
    Fetcher walk (loop_guard on) -> parser: pages are appended in page order,
    then the finished list is reversed once.
    """
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "book": {
                "url": "{base_url}/book/{book_id}/",
                "pages": {
                    "url": "{base_url}/book/{book_id}/index_{page}.html",
                    "start": 2,
                },
                "chapters": {
                    "item": "a.chapter",
                    "title": {"attr": "text"},
                    "url": {"attr": "href"},
                    "is_desc": True,
                },
            },
        }
    )
    pages = {
        f"{BOOK}/": _catalog_page(["第一章", "第二章"], start=1) + _pager(2),
        f"{BOOK}/index_2.html": _catalog_page(["第三章", "第四章"], start=3),
    }

    raw_pages = await _StubFetcher(rule, pages).fetch_book_info("7")
    info = make_parser(rule).parse_book_info(raw_pages)

    assert len(raw_pages) == 2
    assert info is not None
    assert [c["title"] for c in info["volumes"][0]["chapters"]] == [
        "第四章",
        "第三章",
        "第二章",
        "第一章",
    ]


def test_is_desc_reverses_the_final_flat_order_across_pages() -> None:
    """
    Page order stays 1..N (the concatenation is not reversed per page) and the
    finished flat list is reversed once, so chapter 6 comes first.
    """
    pages = [
        _catalog_page(["第一章", "第二章"], start=1),
        _catalog_page(["第三章", "第四章"], start=3),
        _catalog_page(["第五章", "第六章"], start=5),
    ]
    rule = parse_rule(_book_rule(is_desc=True))

    info = make_parser(rule).parse_book_info(pages)

    assert info is not None
    chapters = info["volumes"][0]["chapters"]
    assert [c["title"] for c in chapters] == [
        "第六章",
        "第五章",
        "第四章",
        "第三章",
        "第二章",
        "第一章",
    ]
    # the parser emits no ordinal field, so "renumbering" means the positional
    # order is continuous: the list is exactly the reverse of the ascending one
    assert [c["url"] for c in chapters] == [
        f"{BOOK}/6.html",
        f"{BOOK}/5.html",
        f"{BOOK}/4.html",
        f"{BOOK}/3.html",
        f"{BOOK}/2.html",
        f"{BOOK}/1.html",
    ]


def test_is_desc_on_an_ascending_page_yields_a_reversed_catalog() -> None:
    """A user misconfiguration reverses the book; it must not crash."""
    rule = parse_rule(_book_rule(is_desc=True))
    page = (
        '<a class="chapter" href="/c/1.html">第一章</a>'
        '<a class="chapter" href="/c/2.html">第二章</a>'
    )

    info = make_parser(rule).parse_book_info([page])

    assert info is not None
    assert [c["title"] for c in info["volumes"][0]["chapters"]] == ["第二章", "第一章"]


def test_is_desc_keeps_dedupe_before_reversal() -> None:
    """The same chapter on two pages is dropped once, then the list reversed."""
    pages = [
        _catalog_page(["第一章", "第二章"], start=1),
        _catalog_page(["第二章", "第三章"], start=2),
    ]
    rule = parse_rule(_book_rule(is_desc=True))

    info = make_parser(rule).parse_book_info(pages)

    assert info is not None
    assert [c["title"] for c in info["volumes"][0]["chapters"]] == [
        "第三章",
        "第二章",
        "第一章",
    ]


def test_is_desc_applies_to_the_volume_order_too() -> None:
    rule = parse_rule(_book_rule(is_desc=True, volume_name="正文"))

    info = make_parser(rule).parse_book_info(
        [_catalog_page(["第一章", "第二章", "第三章"])]
    )

    assert info is not None
    volumes = info["volumes"]
    assert len(volumes) == 1
    assert volumes[0]["volume_name"] == "正文"
    assert [c["title"] for c in volumes[0]["chapters"]] == [
        "第三章",
        "第二章",
        "第一章",
    ]
