#!/usr/bin/env python3
"""
tests.apps.test_cli_search_validation
-------------------------------------

Boundary matrix for the ``novel-cli search`` option contract.

``test_cli_search.py`` covers the happy path and the machine-readable shape;
this module pins down what happens at the *edges* of every argument, because
those edges are where a silently accepted value turns into a wrong answer:

* numeric options (``--limit`` / ``--site-limit`` / ``--timeout``) at 0,
  negative, non-integer, floating and absurdly large values;
* the ``--min-words`` / ``--max-words`` pair (order, equality, range, type);
* ``--updated-after`` spellings that look like dates but are not;
* ``--site`` selections (unknown, blank, comma, duplicate, case, padding);
* ``--sort`` / ``--order`` whitelists;
* the error channel and exit code of each failure in ``--json`` and in the
  human-readable mode;
* keyword normalization edges (blank, punctuation-only, NFKC, very long);
* the "no results" document (``[]`` plus exit 0).

The aggregate search, the config loader, logging and prompts are stubbed, so
every case is offline and deterministic.  Each case asserts the *contract*:
usage errors are exit 2 with an empty stdout (so a ``--json`` consumer never
receives half a document), and only a value the engine can actually honour is
forwarded.

Two contract notes that are easy to mistake for bugs:

* ``--limit`` / ``--site-limit`` clamp non-positive values to 1 rather than
  rejecting them (documented as "最小 1"); this module locks that clamp.
* ``--updated-after`` accepts a future date.  It is a valid inclusive lower
  bound that simply matches nothing, and both the REST API and the desktop
  accept it; rejecting it here would make the three surfaces disagree.
"""

from __future__ import annotations

import argparse
import json
import math
from contextlib import nullcontext
from datetime import date
from typing import Any

import pytest

from novel_downloader.apps.cli.commands.base import EXIT_FAILURE, EXIT_USAGE
from novel_downloader.apps.cli.commands.search import SearchCmd
from novel_downloader.plugins.search import SearchEvent
from novel_downloader.plugins.search_cache import build_key

#: Word-count bounds shared by the CLI, the REST schema and the embedded
#: engine (``plugins.search.MAX_WORD_FILTER`` / ``schemas.MAX_SEARCH_WORDS``).
MAX_WORDS = 1_000_000_000

#: A representative selection used by the stubbed registry: two real-looking
#: keys plus a numeric-prefixed one (the registry prefixes ``n``).
_KNOWN_SITES = ("b520", "n23qb", "n37yq")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    SearchCmd.register(subparsers)
    return parser


def _args(argv: list[str]) -> argparse.Namespace:
    return _parser().parse_args(["search", *argv])


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "site": "b520",
        "book_id": "b520-1",
        "book_url": "https://example.invalid/b520/1",
        "cover_url": "",
        "title": "三体",
        "author": "刘慈欣",
        "latest_chapter": "第一章",
        "update_date": "2026-01-01",
        "word_count": "30万字",
        "priority": 100,
        "rank_score": 0.875,
        "match_tier": 4,
        "query_intent": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def search_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub config/logging/status so a case never touches disk or a tty."""
    from novel_downloader.apps.cli.commands import search as module

    monkeypatch.setattr(
        module, "load_or_init_config", lambda *_a, **_k: {"general": {}}
    )
    monkeypatch.setattr(module.ui, "setup_logging", lambda **_k: None)
    monkeypatch.setattr(module.ui, "status", lambda _message: nullcontext())
    monkeypatch.setattr(module.prompts, "select_search_result", lambda _r: None)
    return module


def _install_search(
    monkeypatch: pytest.MonkeyPatch,
    results: list[dict[str, Any]],
    *,
    events: tuple[SearchEvent, ...] = (),
    captured: dict[str, Any] | None = None,
) -> None:
    async def fake_search(**kwargs: Any) -> list[dict[str, Any]]:
        if captured is not None:
            captured.update(kwargs)
        for event in events:
            kwargs["on_event"](event)
        return list(results)

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)


def _forbid_search(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the aggregate with a stub that fails the test if it is called."""
    calls: list[Any] = []

    async def fake_search(**kwargs: Any) -> list[Any]:  # pragma: no cover
        calls.append(kwargs)
        raise AssertionError("validation must reject this before searching")

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    return calls


def _run(
    args: argparse.Namespace,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    code = 0
    try:
        args.func(args)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    out, err = capsys.readouterr()
    return code, out, err


def _parse_usage_error(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> str:
    """Assert argparse rejected ``argv`` as a usage error; return stderr."""
    with pytest.raises(SystemExit) as excinfo:
        _args(argv)
    assert excinfo.value.code == EXIT_USAGE
    out, err = capsys.readouterr()
    assert out == ""
    return err


@pytest.fixture
def site_registry(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Stub the plugin registry with a small, deterministic site set.

    The real registry imports every plugin (~1.5 s per lookup) and is already
    exercised by ``test_cli_search.py``; the matrix here only needs the
    normalization rules: strip, lowercase, ``n`` prefix for a leading digit.
    """

    def _unknown(sites: Any) -> list[str]:
        unknown: list[str] = []
        for raw in sites:
            key = str(raw).strip().lower()
            if key and key[0].isdigit():
                key = f"n{key}"
            if not key or key not in _KNOWN_SITES:
                unknown.append(str(raw))
        return unknown

    monkeypatch.setattr(search_module, "_unknown_search_sites", _unknown)
    monkeypatch.setattr(
        search_module, "_available_search_sites", lambda: list(_KNOWN_SITES)
    )
    return search_module


# ---------------------------------------------------------------------------
# --limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "-999999"])
def test_limit_at_or_below_zero_clamps_to_one(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Documented as "最小 1": 0/negative are clamped, not rejected."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, out, _err = _run(_args(["--json", "--limit", value, "三体"]), capsys)

    assert code == 0
    assert captured["limit"] == 1
    assert json.loads(out)


@pytest.mark.parametrize("value", ["1.5", "2.0", "1e3", "", "one", "0x10"])
def test_limit_rejects_non_integer_spellings(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``type=int`` rejects floats, exponents, blanks and non-numeric text."""
    calls = _forbid_search(monkeypatch)

    err = _parse_usage_error(["--json", "--limit", value, "三体"], capsys)

    assert "--limit" in err
    assert calls == []


def test_limit_accepts_python_int_underscore_separators(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``int("1_000")`` is 1000: argparse inherits Python's literal grammar.

    Locked as-is rather than rejected: it is unambiguous, and adding a custom
    ``type=`` would change every other integer option's contract too.
    """
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "--limit", "1_000", "三体"]), capsys)

    assert code == 0
    assert captured["limit"] == 1000


def test_limit_accepts_surrounding_whitespace(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "--limit", " 5 ", "三体"]), capsys)

    assert code == 0
    assert captured["limit"] == 5


def test_limit_huge_value_is_forwarded_without_allocation(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A huge cap is just a slice bound; it must not be materialised.

    ``limit`` is only ever used as ``rows[:limit]`` after ranking, so a value
    far beyond any result set costs nothing.  The documented contract has no
    upper bound (unlike the REST API's 500), so it is forwarded verbatim.
    """
    captured: dict[str, Any] = {}
    huge = 10**15
    _install_search(monkeypatch, [_row()], captured=captured)

    code, out, _err = _run(_args(["--json", "--limit", str(huge), "三体"]), capsys)

    assert code == 0
    assert captured["limit"] == huge
    assert len(json.loads(out)) == 1


def test_limit_beyond_int_conversion_guard_is_rejected(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CPython's int/str digit limit turns a 5000-digit value into exit 2."""
    calls = _forbid_search(monkeypatch)

    err = _parse_usage_error(["--json", "--limit", "9" * 5000, "三体"], capsys)

    assert "--limit" in err
    assert calls == []


def test_limit_default_is_no_cap(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert captured["limit"] is None


# ---------------------------------------------------------------------------
# --site-limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "-42"])
def test_site_limit_at_or_below_zero_clamps_to_one(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "--site-limit", value, "三体"]), capsys)

    assert code == 0
    assert captured["per_site_limit"] == 1


@pytest.mark.parametrize("value", ["1.5", "0.5", "1e2", "ten"])
def test_site_limit_rejects_non_integer_spellings(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _forbid_search(monkeypatch)

    err = _parse_usage_error(["--json", "--site-limit", value, "三体"], capsys)

    assert "--site-limit" in err
    assert calls == []


def test_site_limit_defaults_to_ten(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert captured["per_site_limit"] == 10


def test_site_limit_huge_value_is_forwarded(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The per-site cap is bounded by the site's own page and the deadline."""
    captured: dict[str, Any] = {}
    huge = 10**12
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(
        _args(["--json", "--site-limit", str(huge), "三体"]), capsys
    )

    assert code == 0
    assert captured["per_site_limit"] == huge


# ---------------------------------------------------------------------------
# --timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "-0.0", "-5.5"])
def test_finite_non_positive_timeout_clamps_to_a_positive_floor(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A zero/negative budget is clamped to 0.1 s, never passed to aiohttp."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "--timeout", value, "三体"]), capsys)

    assert code == 0
    assert captured["timeout"] == 0.1


@pytest.mark.parametrize("value", ["0.1", "1.5", "30", "120"])
def test_positive_finite_timeout_is_forwarded_verbatim(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "--timeout", value, "三体"]), capsys)

    assert code == 0
    assert captured["timeout"] == pytest.approx(float(value))


@pytest.mark.parametrize("value", ["0.001", "0.09"])
def test_sub_floor_timeout_clamps_to_the_0_1s_floor(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Anything below 0.1 s (including a positive fraction) hits the floor."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "--timeout", value, "三体"]), capsys)

    assert code == 0
    assert captured["timeout"] == 0.1


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "Infinity", "-inf"])
def test_non_finite_timeout_is_a_usage_error(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nan``/``inf`` are not durations and must not reach the transport.

    ``aiohttp.ClientTimeout(total=inf)`` raises ``OverflowError`` from
    ``ceil(inf)`` on the first request, so every site was reported as an
    outage (exit 1 with an empty array) although the network was fine; ``nan``
    silently collapsed the budget to 0.1 s.  Both are rejected as usage
    errors, matching ``follow --timeout``.
    """
    calls = _forbid_search(monkeypatch)

    # ``--timeout=-inf`` is the only spelling argparse accepts for a negative
    # infinity: as a separate token it looks like an option.
    code, out, err = _run(_args(["--json", f"--timeout={value}", "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "--timeout" in err
    assert calls == []


def test_non_finite_timeout_is_rejected_in_human_mode_too(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _forbid_search(monkeypatch)

    code, out, err = _run(_args(["--timeout=inf", "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "--timeout" in err
    assert "\x1b" not in err
    assert calls == []


@pytest.mark.parametrize("value", ["abc", "", "5s", "1,5"])
def test_timeout_rejects_non_numeric_spellings(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _forbid_search(monkeypatch)

    err = _parse_usage_error(["--json", "--timeout", value, "三体"], capsys)

    assert "--timeout" in err
    assert calls == []


def test_huge_finite_timeout_is_accepted_and_forwarded(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A finite 1e300 is absurd but usable: aiohttp converts it to a timer."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "--timeout", "1e300", "三体"]), capsys)

    assert code == 0
    assert math.isfinite(captured["timeout"])
    assert captured["timeout"] == 1e300


# ---------------------------------------------------------------------------
# --min-words / --max-words
# ---------------------------------------------------------------------------


def test_min_words_greater_than_max_words_is_a_usage_error(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Contradictory bounds can never match; reporting them as "no results"
    would hide a swapped pair behind a successful exit 0."""
    calls = _forbid_search(monkeypatch)

    code, out, err = _run(
        _args(["--json", "--min-words", "6", "--max-words", "5", "三体"]), capsys
    )

    assert code == EXIT_USAGE
    assert out == ""
    assert "Min words cannot be greater than max words" in err
    assert calls == []


@pytest.mark.parametrize("value", ["0", "1", "12345", str(MAX_WORDS)])
def test_equal_min_and_max_words_is_accepted(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A single-value window (min == max) is a legitimate exact filter."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(
        _args(["--json", "--min-words", value, "--max-words", value, "三体"]),
        capsys,
    )

    assert code == 0
    assert captured["min_words"] == int(value)
    assert captured["max_words"] == int(value)


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--min-words", "-1"),
        ("--max-words", "-1"),
        ("--min-words", str(MAX_WORDS + 1)),
        ("--max-words", str(MAX_WORDS + 1)),
        ("--min-words", str(10**30)),
    ],
)
def test_word_bounds_outside_the_documented_range_are_usage_errors(
    flag: str,
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _forbid_search(monkeypatch)

    code, out, err = _run(_args(["--json", flag, value, "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert flag in err
    assert calls == []


@pytest.mark.parametrize("flag", ["--min-words", "--max-words"])
@pytest.mark.parametrize("value", ["1.0", "1e5", "abc", ""])
def test_word_bounds_reject_non_integer_spellings(
    flag: str,
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _forbid_search(monkeypatch)

    err = _parse_usage_error(["--json", flag, value, "三体"], capsys)

    assert flag in err
    assert calls == []


def test_omitted_word_bounds_mean_no_filtering(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert captured["min_words"] is None
    assert captured["max_words"] is None


# ---------------------------------------------------------------------------
# --updated-after
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "2026-13-01",  # month 13
        "2026-00-10",  # month 0
        "2026-02-30",  # April 31-style impossible day
        "2025-02-29",  # not a leap year
        "2026-04-31",
        "0000-01-01",  # year 0 is not a valid ISO date
        "2024/01/01",  # not ISO
        "20240101",  # compact form is not accepted
        "2024-1-1",  # not zero padded
        "2024-01-01T00:00:00",  # datetime, not a date
        "2024-01-01Z",
        "soon",
        "",
        "  ",
    ],
)
def test_updated_after_rejects_invalid_dates(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _forbid_search(monkeypatch)

    code, out, err = _run(_args(["--json", "--updated-after", value, "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "--updated-after" in err
    assert calls == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-02-29", date(2024, 2, 29)),  # leap day is valid
        ("2024-01-01", date(2024, 1, 1)),
        (" 2024-01-01 ", date(2024, 1, 1)),  # surrounding blanks are stripped
        ("2999-12-31", date(2999, 12, 31)),  # future: valid, matches nothing
    ],
)
def test_updated_after_accepts_valid_dates_as_a_date_object(
    value: str,
    expected: date,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A future bound is accepted on every surface (REST, embedded, desktop):
    it is a legitimate filter that simply yields an empty set."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, err = _run(_args(["--json", "--updated-after", value, "三体"]), capsys)

    assert code == 0, err
    assert captured["updated_after"] == expected


def test_updated_after_is_absent_by_default(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert captured["updated_after"] is None


# ---------------------------------------------------------------------------
# --site
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "echo"),
    [
        ("qiidan", "qiidan"),  # typo
        ("b520x", "b520x"),
        ("", "''"),  # empty string
        ("   ", "   "),  # blank
        ("b520,", "b520,"),  # stray comma
        ("b520,n23qb", "b520,n23qb"),  # comma list is not a valid key
        ("b520/../x", "b520/../x"),  # path-shaped key
    ],
)
def test_unknown_site_selection_is_a_usage_error(
    value: str,
    echo: str,
    site_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A key no searcher is registered for used to search nothing and exit 0,
    which is indistinguishable from "no such book"."""
    calls = _forbid_search(monkeypatch)

    code, out, err = _run(_args(["--json", "--site", value, "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert echo in err
    # The diagnostic names the alternatives, so a typo is fixable from it.
    assert "b520" in err
    assert "n23qb" in err
    assert calls == []


def test_option_like_site_key_is_rejected_when_spelled_with_equals(
    site_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--site -b520`` is read as a missing value; ``--site=-b520`` reaches
    validation and is rejected as an unknown key (no searcher matches it)."""
    calls = _forbid_search(monkeypatch)

    err = _parse_usage_error(["--json", "--site", "-b520", "三体"], capsys)
    assert "--site" in err
    assert calls == []

    code, out, err = _run(_args(["--json", "--site=-b520", "三体"]), capsys)
    assert code == EXIT_USAGE
    assert out == ""
    assert "-b520" in err
    assert calls == []


@pytest.mark.parametrize("value", ["b520", "B520", " b520 ", "B520 ", "N23QB"])
def test_site_case_and_padding_resolve_like_the_registry(
    value: str,
    site_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validation normalizes; the spelling handed to the engine is untouched."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, err = _run(_args(["--json", "--site", value, "三体"]), capsys)

    assert code == 0, err
    assert captured["sites"] == [value]


def test_repeated_site_flags_are_forwarded_in_order(
    site_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Duplicates are not an error: the engine searches each site once."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, err = _run(
        _args(
            [
                "--json",
                "--site",
                "b520",
                "--site",
                "B520",
                "--site",
                "n23qb",
                "三体",
            ]
        ),
        capsys,
    )

    assert code == 0, err
    assert captured["sites"] == ["b520", "B520", "n23qb"]


def test_site_validation_does_not_run_without_explicit_selection(
    site_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _unexpected(_sites: Any) -> list[str]:
        raise AssertionError("no --site means every site; nothing to validate")

    monkeypatch.setattr(site_registry, "_unknown_search_sites", _unexpected)
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, _err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert captured["sites"] is None


def test_unknown_site_error_never_prints_a_json_document(
    site_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A script must see either a document or an exit code, never half of one."""
    _forbid_search(monkeypatch)

    code, out, err = _run(_args(["--json", "--site", "nope", "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "nope" in err


# ---------------------------------------------------------------------------
# --sort / --order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["QUALITY", "Quality", "quality ", "best", "", "rank_score", "update_date"],
)
def test_invalid_sort_key_is_a_usage_error(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _forbid_search(monkeypatch)

    err = _parse_usage_error(["--json", "--sort", value, "三体"], capsys)

    assert "--sort" in err
    # The whitelist is echoed, so the user can pick a valid key.
    assert "quality" in err
    assert calls == []


@pytest.mark.parametrize("value", ["ASC", "Desc", "ascending", "up", ""])
def test_invalid_order_is_a_usage_error(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _forbid_search(monkeypatch)

    err = _parse_usage_error(["--json", "--order", value, "三体"], capsys)

    assert "--order" in err
    assert calls == []


@pytest.mark.parametrize(
    "value", ["quality", "priority", "site", "title", "author", "words", "update"]
)
def test_every_documented_sort_key_is_accepted(
    value: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "--sort", value, "三体"]), capsys)

    assert code == 0
    assert captured["sort"] == value


def test_default_sort_and_order_are_the_ranking_layer_defaults(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``order=None`` lets the ranking layer pick the key's natural direction."""
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [_row()], captured=captured)

    code, _out, _err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert captured["sort"] == "quality"
    assert captured["order"] is None


# ---------------------------------------------------------------------------
# Error channel / exit code in both output modes
# ---------------------------------------------------------------------------


def test_usage_error_in_json_mode_keeps_stdout_empty(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _forbid_search(monkeypatch)

    code, out, err = _run(
        _args(["--json", "--min-words", "10", "--max-words", "1", "三体"]), capsys
    )

    assert code == EXIT_USAGE
    assert out == ""
    assert err.strip()
    assert "\x1b" not in err


def test_usage_error_in_human_mode_uses_stderr_too(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _forbid_search(monkeypatch)

    code, out, err = _run(_args(["--updated-after", "2026-02-30", "三体"]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "--updated-after" in err


def test_argparse_error_in_human_mode_uses_stderr(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _forbid_search(monkeypatch)

    err = _parse_usage_error(["--limit", "1.5", "三体"], capsys)

    assert "--limit" in err


def test_json_no_results_is_an_empty_array_with_exit_zero(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [],
        events=(SearchEvent(site="b520", kind="completed", result_count=0),),
    )

    code, out, err = _run(_args(["--json", "三体"]), capsys)

    assert code == 0
    assert out == "[]\n"  # exactly one compact document plus a newline
    assert len(out.splitlines()) == 1
    assert "No results found for '三体'." in err


def test_human_no_results_keeps_stdout_empty(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [],
        events=(SearchEvent(site="b520", kind="completed", result_count=0),),
    )

    code, out, err = _run(_args(["三体"]), capsys)

    assert code == 0
    assert out == ""
    assert "No results found for '三体'." in err


def test_all_sites_failed_still_emits_the_document_then_exits_one(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [],
        events=(
            SearchEvent(site="b520", kind="failed", message="timeout"),
            SearchEvent(site="n23qb", kind="failed", message="refused"),
        ),
    )

    code, out, err = _run(_args(["--json", "三体"]), capsys)

    assert code == EXIT_FAILURE
    assert json.loads(out) == []
    assert "0 site(s) completed / 2 failed / 0 skipped" in err
    assert "No results found" not in err


def test_all_sites_failed_in_human_mode_exits_one_without_document(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_search(
        monkeypatch,
        [],
        events=(SearchEvent(site="b520", kind="failed", message="timeout"),),
    )

    code, out, err = _run(_args(["三体"]), capsys)

    assert code == EXIT_FAILURE
    assert out == ""
    assert "No results found" not in err


# ---------------------------------------------------------------------------
# Keyword edges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n", "\u3000", "\u00a0"])
def test_blank_keyword_is_a_usage_error_before_any_work(
    blank: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Includes the ideographic and no-break spaces: ``str.strip`` folds them,
    so the CLI must not send a query made only of invisible characters."""
    calls = _forbid_search(monkeypatch)
    loaded: list[Any] = []
    monkeypatch.setattr(
        search_module,
        "load_or_init_config",
        lambda *a, **k: loaded.append((a, k)) or {"general": {}},
    )

    code, out, err = _run(_args(["--json", blank]), capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "No keyword entered." in err
    assert calls == []
    assert loaded == []


@pytest.mark.parametrize("keyword", ["!!!", "???", "，。！", "---"])
def test_punctuation_only_keyword_is_a_real_query(
    keyword: str,
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Punctuation is stripped for ranking, not for the blank check.

    Such a query is still a search the user asked for; the cache key falls
    back to the NFKC-folded raw text so two different punctuation-only
    queries do not share a snapshot.  A leading ``-`` makes argparse read the
    token as an option, so it needs the ``--`` end-of-options marker.
    """
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    argv = ["--json", "--", keyword] if keyword.startswith("-") else ["--json", keyword]
    code, _out, err = _run(_args(argv), capsys)

    assert code == 0, err
    assert captured["keyword"] == keyword
    assert f"No results found for '{keyword}'." in err


def test_punctuation_only_queries_have_distinct_cache_identities() -> None:
    assert build_key("!!!", None).keyword != build_key("???", None).keyword


def test_full_width_keyword_is_forwarded_raw_but_folds_for_identity(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NFKC equivalence is a ranking/cache concern, not a CLI rewrite.

    The site is queried with exactly what the user typed (a site may index the
    full-width spelling), while the cache identity folds both spellings.
    """
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, _err = _run(_args(["--json", "ＡＢＣ"]), capsys)

    assert code == 0
    assert captured["keyword"] == "ＡＢＣ"
    assert build_key(captured["keyword"], None) == build_key("ABC", None)


def test_multi_word_query_is_joined_with_single_spaces(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, _err = _run(_args(["--json", "斗破苍穹", "天蚕土豆"]), capsys)

    assert code == 0
    assert captured["keyword"] == "斗破苍穹 天蚕土豆"


def test_surrounding_whitespace_is_stripped_from_the_keyword(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)

    code, _out, err = _run(_args(["--json", "  三体  "]), capsys)

    assert code == 0
    assert captured["keyword"] == "三体"
    assert "No results found for '三体'." in err


def test_very_long_keyword_is_forwarded_unchanged(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI has no keyword length cap (the REST/embedded 200-char bound is a
    server-side quota guard).  Locking the pass-through keeps this test honest
    about the current contract; see the module docstring for the risk note."""
    captured: dict[str, Any] = {}
    long_keyword = "长" * 3000
    _install_search(monkeypatch, [], captured=captured)

    code, _out, _err = _run(_args(["--json", long_keyword]), capsys)

    assert code == 0
    assert captured["keyword"] == long_keyword
    assert len(captured["keyword"]) == 3000


def test_keyword_starting_with_a_dash_needs_the_end_of_options_marker(
    search_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-三体`` is read as an option unless ``--`` precedes it."""
    calls = _forbid_search(monkeypatch)

    _parse_usage_error(["--json", "-三体"], capsys)
    assert calls == []

    captured: dict[str, Any] = {}
    _install_search(monkeypatch, [], captured=captured)
    code, _out, _err = _run(_args(["--json", "--", "-三体"]), capsys)

    assert code == 0
    assert captured["keyword"] == "-三体"
