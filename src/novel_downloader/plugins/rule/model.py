#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.model
-----------------------------------

Data model for the rule-driven ("book source") plugin system.

A *rule* is a small JSON document that describes how to search, parse and
download novels from a website.  The idea is deliberately kept close to the
"book source" JSON formats used by other community downloaders so that new
sources can be added by writing configuration instead of Python code.

The public surface is intentionally tiny:

* :class:`Rule` and friends -- plain dataclasses, one per JSON section.
* :func:`parse_rule` -- validate a raw ``dict`` (already ``json.loads``-ed)
  into a :class:`Rule`, raising :class:`RuleError` on malformed input.

Field extraction semantics (see :class:`Field`):

* ``attr`` values are mutually exclusive:
    - ``text``       : whitespace-stripped concatenation of descendant text
    - ``inner_text`` : descendant text lines joined by ``"\\n"``
    - ``html``       : inner HTML of the selected node(s)
    - anything else  : the value of that attribute (``href``, ``src``, ``content``, ...)
* ``regex`` + ``group`` optionally post-process the extracted string.
* ``replace`` applies literal ``[old, new]`` pairs after the regex.
* ``steps`` runs an explicit post-processing chain after ``regex`` + ``replace``
  (see :class:`Step`).
* ``fallback`` is a selector fallback chain tried when the main selector yields
  an empty string (see :func:`~.engine.extract`).
* ``multi`` returns every match instead of only the first.

Post-processing chains are explicit JSON arrays, *not* an ``@js:`` suffix
----------------------------------------------------------------------
Other "book source" formats encode a post-processing program in the selector
string itself, e.g. ``"selector@js:code"``.  We deliberately do **not** copy
that DSL: ``@`` is also the attribute-predicate marker in XPath
(``//div[@class="x"]``), so a naive ``split("@")`` truncates perfectly valid
selectors and silently yields nothing.  (``@`` can also appear in CSS
attribute selectors, ``a[href*="@"]``.)  Instead every hook is a separate,
typed JSON list (``steps`` / ``preprocess``), which keeps the selector usable
verbatim, lets the loader validate the whole program *before* a single request
is made, and gives every failure a rule location.  This is an intentional
improvement over the upstream design.

Accepted step languages (see :class:`Step`):

* ``py``  -- a name from the explicit whitelist in
  :data:`novel_downloader.plugins.rule.engine.PY_STEP_FUNCTIONS`; unknown names
  are rejected at load time.
* ``js``  -- a program for :mod:`novel_downloader.libs.mini_js`.  That runtime
  is deliberately tiny: ``let`` / ``function`` / ``return`` / expression
  statements, arithmetic, ternary, object/array literals and *dict/list*
  member access.  It has **no** ``if`` / loops, no regex literals, no string
  prototypes (``.replace``/``.split``/``.toUpperCase`` all raise), and no
  global constructors.  The current string is bound to ``input`` (also
  ``result``), every whitelisted ``py`` function is injected as a callable
  (``strip_html(input)`` works, ``input.trim()`` does not), and two
  multi-argument helpers cover HTML rewriting:
  ``replace_all(text, old, new)`` and ``regex_replace(text, pattern, repl)``
  (Python ``re.sub`` semantics; backslashes pass through two escaping layers
  -- the JSON file *and* the JS string literal -- so a digit class is four
  backslashes in the rule file).
  Unsupported constructs raise :class:`RuleError` instead of returning the
  input unchanged.

Response formats
----------------
The ``search`` / ``book`` / ``chapter`` sections may declare
``"format": "json"`` to consume an AJAX/JSON endpoint instead of HTML.  When a
section leaves it out, the site-wide :attr:`Rule.format` (default ``"html"``)
applies.  In JSON mode a ``selector`` is a dot path (``data.list``,
``items[*].name``) rather than a CSS/XPath expression, and ``attr`` selects a
key of the matched object (``text`` / ``inner_text`` mean "the value itself").

A JSON request body can be sent by adding ``"json_body": {...}`` to the
``search`` section; its string values accept the same ``{keyword}`` /
``{page}`` templates as URLs.

Per-source request limits
-------------------------
The optional top-level ``"limits"`` object overrides the global downloader
tuning **for this source only** (see :class:`Limits` and :func:`apply_limits`).
All intervals are in **seconds** and are never mutated in place: a new
``FetcherConfig`` is produced with :func:`dataclasses.replace`, so a shared
global config object is untouched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from novel_downloader.infra.i18n import t
from novel_downloader.schemas import FetcherConfig

__all__ = [
    "Field",
    "Step",
    "Limits",
    "SearchRule",
    "ChaptersRule",
    "PagesRule",
    "BookRule",
    "ChapterRule",
    "ResolveRule",
    "Rule",
    "RuleError",
    "parse_rule",
    "loads_rule_json",
    "apply_limits",
]


class RuleError(ValueError):
    """Raised when a rule document is structurally invalid."""


# --------------------------------------------------------------------------- #
# JSON decoding (used by the loader, installer and remote fetcher)
# --------------------------------------------------------------------------- #
def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` hook that rejects duplicate keys instead of last-wins."""
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def loads_rule_json(text: str | bytes, *, source: str = "<memory>") -> dict[str, Any]:
    """
    Decode a rule document, requiring a JSON object as the root.

    Unlike a plain ``json.loads``, duplicate object keys are rejected: for a
    rule document a duplicate key is a copy/paste error, and silently keeping
    the last value hides the conflict from the author.

    :raises RuleError: on invalid JSON, a non-object root, or duplicate keys.
    """
    try:
        data: Any = json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except ValueError as e:
        raise RuleError(
            t("{source}: invalid JSON: {error}").format(source=source, error=e)
        ) from e
    if not isinstance(data, dict):
        raise RuleError(t("{source}: rule must be a JSON object").format(source=source))
    return data


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_replace(value: Any) -> list[tuple[str, str]]:
    """Normalize a list of ``[old, new]`` pairs."""
    if not value:
        return []
    out: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((str(item[0]), str(item[1])))
    return out


#: Control characters that must never appear in a header name or value.
_HEADER_CTRL_RE = re.compile(r"[\r\n\x00]")


def _as_headers(value: Any) -> dict[str, str]:
    """Normalize a request header mapping into ``{str: str}``."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        key, val = str(k), _as_str(v)
        if _HEADER_CTRL_RE.search(key) or _HEADER_CTRL_RE.search(val):
            raise RuleError(
                f"header {key!r} must not contain control characters "
                "(CR/LF/NUL are rejected to prevent header injection)"
            )
        out[key] = val
    return out


def _as_int(value: Any, default: int, *, where: str) -> int:
    """
    Coerce an optional integer field, raising :class:`RuleError` on bad input.

    Accepts ``None`` (-> ``default``), a real ``int`` or a numeric string.
    Anything else (a float, a list, a non-numeric string, a bool) is rejected
    instead of leaking a raw ``ValueError``/``TypeError`` to the caller.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuleError(f"{where} must be an integer, got {value!r}")
    try:
        return int(value)
    except ValueError as e:
        raise RuleError(f"{where} must be an integer, got {value!r}") from e


def _as_bool(value: Any, default: bool, *, where: str) -> bool:
    """Coerce an optional boolean field, rejecting strings/numbers."""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RuleError(f"{where} must be a boolean, got {value!r}")
    return value


def _as_number(value: Any, *, where: str) -> float | None:
    """
    Coerce an optional non-negative floating point seconds value.

    ``bool`` is rejected explicitly (it is an ``int`` subclass) so
    ``"min_interval": true`` does not silently mean ``1.0`` seconds.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuleError(f"{where} must be a number, got {value!r}")
    try:
        number = float(value)
    except ValueError as e:
        raise RuleError(f"{where} must be a number, got {value!r}") from e
    if number < 0:
        raise RuleError(f"{where} must be non-negative, got {value!r}")
    if number != number or number == float("inf"):
        raise RuleError(f"{where} must be a finite number, got {value!r}")
    return number


# --------------------------------------------------------------------------- #
# regex safety (catastrophic backtracking)
# --------------------------------------------------------------------------- #
# Python's ``re`` has no timeout, so a pattern such as ``(a+)+$`` can pin a
# worker for hours on a few dozen bytes of page text.  Rule regexes come from
# a configuration file (and, for remote rules, from the network), which makes
# them *untrusted*: an accidentally evil pattern would otherwise hang the whole
# download with no diagnostic.  The helpers below reject the canonical
# "quantified group whose body can repeat ambiguously" shape at load time.
#
# This is a best-effort static check, not a proof: it catches ``(a+)+``,
# ``(a*)*``, ``(\d+)+``, ``(a*b?)+`` and friends, but not overlapping
# alternations such as ``(a|aa)+`` or backreference blow-ups.  Rule authors
# must still keep patterns linear.


def _skip_char_class(pattern: str, i: int) -> int:
    """Return the index just past the ``[...]`` class starting at ``i``."""
    n = len(pattern)
    i += 1  # past '['
    if i < n and pattern[i] == "^":
        i += 1
    if i < n and pattern[i] == "]":  # a ']' first in a class is literal
        i += 1
    while i < n:
        if pattern[i] == "\\":
            i += 2
            continue
        if pattern[i] == "]":
            return i + 1
        i += 1
    return n


def _read_quantifier(pattern: str, i: int) -> tuple[bool, bool, int]:
    """
    Read the quantifier starting at ``i``.

    :return: ``(min_zero, unbounded, next_index)`` -- ``min_zero`` is True when
        the atom may be skipped (``*``, ``?``, ``{0,...}``), ``unbounded`` is
        True when it may repeat forever (``*``, ``+``, ``{n,}``).  With no
        quantifier the result is ``(False, False, i)``.
    """
    n = len(pattern)
    if i >= n:
        return False, False, i
    c = pattern[i]
    if c in "*+":
        j = i + 1
        # a lazy ('?') or possessive ('+') suffix does not change the bounds
        if j < n and pattern[j] in "?+":
            j += 1
        return c == "*", True, j
    if c == "?":
        j = i + 1
        if j < n and pattern[j] in "?+":
            j += 1
        return True, False, j
    if c == "{":
        close = pattern.find("}", i)
        if close == -1:
            return False, False, i
        parts = pattern[i + 1 : close].split(",")
        if (
            not parts[0].isdigit()
            or len(parts) > 2
            or (len(parts) == 2 and parts[1] and not parts[1].isdigit())
        ):
            return False, False, i  # a literal '{', not a quantifier
        j = close + 1
        if j < n and pattern[j] in "?+":
            j += 1
        if len(parts) == 2:
            return parts[0] == "0", parts[1] == "", j
        return False, False, j
    return False, False, i


def _atom_end(pattern: str, i: int) -> int:
    """Return the index just past the single regex atom starting at ``i``."""
    n = len(pattern)
    if i >= n:
        return i
    c = pattern[i]
    if c == "\\":
        nxt = pattern[i + 1] if i + 1 < n else ""
        if nxt == "x":
            return min(i + 4, n)
        if nxt == "u":
            return min(i + 6, n)
        if nxt == "N" and pattern[i + 2 : i + 3] == "{":
            close = pattern.find("}", i + 3)
            return n if close == -1 else close + 1
        return min(i + 2, n)
    if c == "[":
        return _skip_char_class(pattern, i)
    if c == "(":
        depth = 0
        j = i
        while j < n:
            ch = pattern[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "[":
                j = _skip_char_class(pattern, j)
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    return i + 1


def _split_alternatives(body: str) -> list[str]:
    """Split a group body on top-level ``|`` (ignoring classes and groups)."""
    out: list[str] = []
    start = 0
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i = _skip_char_class(body, i)
            continue
        if c == "(":
            i = _atom_end(body, i)
            continue
        if c == "|":
            out.append(body[start:i])
            start = i + 1
        i += 1
    out.append(body[start:])
    return out


def _group_body(pattern: str, start: int, end: int) -> str:
    """Strip the ``(?:`` / ``(?P<name>`` / ``(?i:`` prefix from a group body."""
    body = pattern[start:end]
    if not body.startswith("?"):
        return body
    if body.startswith("?P<") or body.startswith("?<"):
        close = body.find(">")
        return body[close + 1 :] if close != -1 else body
    if body.startswith("?:"):
        return body[2:]
    colon = body.find(":")
    if colon != -1:
        return body[colon + 1 :]
    return ""  # a flag-only group such as ``(?i)``


def _can_match_empty(body: str) -> bool:
    """Whether ``body`` can match the empty string (conservative)."""
    return any(_alternative_matches_empty(alt) for alt in _split_alternatives(body))


def _alternative_matches_empty(alt: str) -> bool:
    i = 0
    n = len(alt)
    while i < n:
        end = _atom_end(alt, i)
        if end <= i:
            return False
        atom = alt[i:end]
        min_zero, _unbounded, after = _read_quantifier(alt, end)
        if min_zero:
            i = after
            continue
        if atom.startswith("(") and _can_match_empty(
            _group_body(atom, 1, len(atom) - 1)
        ):
            i = after
            continue
        return False
    return True


def _is_quantified_single_atom(body: str) -> bool:
    """Whether ``body`` is exactly one atom with an unbounded quantifier."""
    end = _atom_end(body, 0)
    if end <= 0 or end >= len(body):
        return False
    _min_zero, unbounded, after = _read_quantifier(body, end)
    return unbounded and after == len(body)


def _is_lookaround(pattern: str, start: int) -> bool:
    """
    Whether the group whose body starts at ``start`` is a lookaround.

    Lookarounds are zero-width, so repeating one cannot backtrack
    exponentially (CPython refuses to loop on a zero-width match).
    """
    head = pattern[start : start + 3]
    if head.startswith("?<"):
        return head in ("?<=", "?<!")
    return head[:2] in ("?=", "?!")


def regex_backtracking_risk(pattern: str) -> str | None:
    """
    Describe the catastrophic-backtracking shape of ``pattern``, if any.

    A group quantified by ``*``/``+``/``{n,}`` whose body is a single repeated
    atom (``(a+)+``) or can match the empty string (``(a*b?)+``) makes the
    matcher try exponentially many splits.  Returned text is meant for a
    :class:`RuleError` message; ``None`` means "no known risk".
    """
    n = len(pattern)
    stack: list[int] = []
    i = 0
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i = _skip_char_class(pattern, i)
            continue
        if c == "(":
            stack.append(i + 1)
            i += 1
            continue
        if c != ")":
            i += 1
            continue
        if not stack:
            i += 1
            continue
        start = stack.pop()
        _min_zero, unbounded, after = _read_quantifier(pattern, i + 1)
        if unbounded and not _is_lookaround(pattern, start):
            body = _group_body(pattern, start, i)
            # unwrap a body that is itself a single group, e.g. ``((a+))+``
            while body.startswith("(") and _atom_end(body, 0) == len(body):
                body = _group_body(body, 1, len(body) - 1)
            if not body or _can_match_empty(body) or _is_quantified_single_atom(body):
                return (
                    f"group {pattern[start - 1 : i + 1]!r} is repeated "
                    "(unbounded quantifier) but its body can match the empty "
                    "string or a single repeated atom, so matching backtracks "
                    "exponentially"
                )
        i = after
    return None


def _check_regex(pattern: Any, *, where: str) -> str | None:
    """
    Validate an optional regex string, returning it unchanged.

    A non-string or syntactically invalid pattern is a hard :class:`RuleError`
    so a bad rule is rejected when it is *parsed* rather than crashing later
    inside the fetcher/parser/searcher (where the exception would be opaque).
    A pattern with a known catastrophic-backtracking shape is rejected too:
    ``re`` has no timeout, so accepting it would let one page of text hang the
    download indefinitely.
    """
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise RuleError(f"{where} must be a regex string, got {type(pattern).__name__}")
    try:
        re.compile(pattern)
    except re.error as e:
        raise RuleError(f"{where}: invalid regex {pattern!r}: {e}") from e
    risk = regex_backtracking_risk(pattern)
    if risk is not None:
        raise RuleError(
            f"{where}: regex {pattern!r} risks catastrophic backtracking: {risk}"
        )
    return pattern


def _as_str_list(value: Any, *, where: str) -> list[str]:
    """Normalize an optional list-of-strings field, rejecting other shapes."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise RuleError(f"{where} must be a list")
    return [str(item) for item in value]


_VALID_FORMATS = {"html", "json"}


def _check_format(fmt: str) -> str:
    """Validate a response format name."""
    if fmt not in _VALID_FORMATS:
        raise RuleError(
            t("unsupported format {fmt!r} (expected one of {formats})").format(
                fmt=fmt, formats=sorted(_VALID_FORMATS)
            )
        )
    return fmt


def _section_format(data: Any) -> str | None:
    """Read an optional per-section ``format`` override (``None`` = inherit)."""
    raw = data.get("format") if isinstance(data, dict) else None
    if raw in (None, ""):
        return None
    return _check_format(_as_str(raw).lower())


# --------------------------------------------------------------------------- #
# schema keys (typo guard: unknown keys fail at load time, not silently)
# --------------------------------------------------------------------------- #
# Keys starting with ``_`` are a comment/annotation convention (see the
# shipped ``_example-template.json``) and are always ignored.
_TOP_LEVEL_KEYS = {
    "site_key",
    "site_name",
    "priority",
    "base_url",
    "encoding",
    "format",
    "hosts",
    "headers",
    "resolve",
    "search",
    "book",
    "chapter",
    "limits",
}
_SEARCH_KEYS = {
    "url",
    "method",
    "data",
    "json_body",
    "headers",
    "format",
    "item",
    "fields",
    "book_id_regex",
    "book_id_group",
    "priority",
    "single_result_fallback",
}
_BOOK_KEYS = {"url", "fields", "chapters", "pages", "headers", "format"}
_CHAPTERS_KEYS = {
    "item",
    "title",
    "url",
    "id_regex",
    "id_group",
    "volume_name",
    "container",
    "preprocess",
    "is_desc",
}
_PAGES_KEYS = {"url", "start", "max_pages", "stop_when", "loop_guard"}
_CHAPTER_KEYS = {
    "url",
    "content",
    "title",
    "replace",
    "drop",
    "pages",
    "headers",
    "format",
    "container",
    "preprocess",
}
_FIELD_KEYS = {
    "selector",
    "attr",
    "regex",
    "group",
    "multi",
    "replace",
    "default",
    "steps",
    "fallback",
}
_STEP_KEYS = {"lang", "code", "name"}
_LIMIT_KEYS = {
    "concurrency",
    "min_interval",
    "max_interval",
    "max_attempts",
    "retry_min_interval",
    "retry_max_interval",
}
_RESOLVE_KEYS = {"book", "chapter"}
#: Field names actually consumed by the searcher / parser.
_SEARCH_FIELD_NAMES = {"url", "cover", "title", "author", "latest", "update", "word"}
_BOOK_FIELD_NAMES = {
    "book_name",
    "author",
    "cover_url",
    "update_time",
    "summary",
    "summary_brief",
    "serial_status",
    "word_count",
    "tags",
}


def _reject_unknown(data: dict[str, Any], allowed: set[str], *, where: str) -> None:
    """Reject keys outside ``allowed`` (typos) unless prefixed with ``_``."""
    unknown = sorted(k for k in data if k not in allowed and not k.startswith("_"))
    if unknown:
        raise RuleError(
            f"{where}: unknown key {unknown[0]!r} "
            f"(possible typo; allowed keys: {', '.join(sorted(allowed))})"
        )


def _check_field_names(
    fields: dict[str, Any], allowed: set[str], *, where: str
) -> None:
    """Reject field names the engine would silently ignore."""
    unknown = sorted(k for k in fields if k not in allowed and not k.startswith("_"))
    if unknown:
        raise RuleError(
            f"{where}: unknown field {unknown[0]!r} "
            f"(possible typo; consumed fields: {', '.join(sorted(allowed))}; "
            "prefix custom keys with '_' to keep them as a comment)"
        )


def _non_negative(value: int, *, where: str) -> int:
    """Reject negative capture-group indices (they raise ``IndexError``)."""
    if value < 0:
        raise RuleError(f"{where} must be a non-negative integer, got {value}")
    return value


# --------------------------------------------------------------------------- #
# post-processing steps
# --------------------------------------------------------------------------- #
#: Languages accepted by :class:`Step`.
_VALID_STEP_LANGS = ("js", "py")


def _known_py_steps() -> set[str]:
    """Return the whitelisted ``py`` step names (lazy import: avoids a cycle)."""
    from .engine import PY_STEP_FUNCTIONS

    return set(PY_STEP_FUNCTIONS)


def _validate_js_source(code: str, *, where: str) -> None:
    """
    Reject code the mini-js parser cannot even compile.

    This is a *syntax* check only: constructs the parser accepts but the
    evaluator cannot execute (string methods, ``new``, regex literals, ...)
    fail later at run time with a :class:`RuleError` that carries the same
    location.  Validating here means a broken rule is rejected at load time,
    before any request is made, instead of producing silently empty output.
    """
    from novel_downloader.libs.mini_js.parser import parse_code

    try:
        parse_code(code)
    except Exception as e:  # mini-js raises SyntaxError/AssertionError alike
        raise RuleError(
            f"{where}: unsupported JavaScript ({type(e).__name__}: {e})"
        ) from e


@dataclass
class Step:
    """
    One entry of a post-processing chain (``steps`` / ``preprocess``).

    ``lang="py"`` names a whitelisted, side-effect-free ``str -> str`` function
    from :data:`novel_downloader.plugins.rule.engine.PY_STEP_FUNCTIONS`
    (``strip_html``, ``normalize_space``, ``base64_decode``, ``url_decode``,
    ``dedupe_lines``, ...).  Unknown names are rejected at load time.

    ``lang="js"`` runs ``code`` in :class:`~novel_downloader.libs.mini_js.MiniJS`.
    That engine is a strict subset of JavaScript -- see the module docstring for
    the exact limits.  The current string is available as ``input`` (and
    ``result``), and the whitelisted ``py`` helpers are injected as callables.

    ``where`` records the rule location (``"book.fields.summary.steps[0]"``)
    so a runtime failure can point at the offending step.
    """

    lang: str
    code: str = ""
    name: str = ""
    where: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.lang == "js":
            _validate_js_source(self.code, where=self.where or "step")
        elif self.lang == "py":
            known = _known_py_steps()
            if self.name not in known:
                raise RuleError(
                    f"{self.where or 'step'}: unknown py step {self.name!r} "
                    f"(allowed: {', '.join(sorted(known))})"
                )
        else:
            raise RuleError(
                f"{self.where or 'step'}: unsupported step language "
                f"{self.lang!r} (expected one of {', '.join(_VALID_STEP_LANGS)})"
            )

    @classmethod
    def from_dict(cls, data: Any, *, where: str = "step") -> Step:
        if not isinstance(data, dict):
            raise RuleError(f"{where}: step must be an object")
        _reject_unknown(data, _STEP_KEYS, where=where)
        lang = data.get("lang")
        if not isinstance(lang, str) or not lang.strip():
            raise RuleError(f"{where}: step requires a 'lang' string")
        lang = lang.strip().lower()
        code = data.get("code")
        name = data.get("name")
        if lang == "js":
            if not isinstance(code, str) or not code.strip():
                raise RuleError(f"{where}: js step requires a non-empty 'code'")
            return cls(lang=lang, code=code, where=where)
        if lang == "py":
            if not isinstance(name, str) or not name.strip():
                raise RuleError(f"{where}: py step requires a 'name'")
            return cls(lang=lang, name=name.strip(), where=where)
        raise RuleError(
            f"{where}: unsupported step language {lang!r} "
            f"(expected one of {', '.join(_VALID_STEP_LANGS)})"
        )


def _as_steps(value: Any, *, where: str) -> list[Step]:
    """Normalize an optional ``steps`` / ``preprocess`` list."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise RuleError(f"{where} must be a list of steps")
    return [
        Step.from_dict(item, where=f"{where}[{index}]")
        for index, item in enumerate(value)
    ]


# --------------------------------------------------------------------------- #
# per-source request limits
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Limits:
    """
    Optional per-source download tuning (all intervals are in **seconds**).

    * ``concurrency`` -- parallel requests; must be ``>= 1``.  ``0`` is a hard
      error: a zero-permit semaphore deadlocks forever instead of failing
      loudly.
    * ``min_interval`` / ``max_interval`` -- desired spacing between requests.
    * ``max_attempts`` -- total attempts (including the first one).
    * ``retry_min_interval`` / ``retry_max_interval`` -- desired spacing
      between retries.

    The underlying :class:`~novel_downloader.schemas.FetcherConfig` only
    exposes one nominal interval per concern, so a band is collapsed to its
    midpoint by :func:`apply_limits` (see that function's docstring).  The
    upper bound is therefore **not** enforced: the fetcher sleeps the midpoint
    plus up to +10% jitter, so ``min=0.2, max=5.0`` really means "about 2.6 s
    between requests".  Use a single value (``min_interval`` only) when an
    exact spacing matters.
    """

    concurrency: int | None = None
    min_interval: float | None = None
    max_interval: float | None = None
    max_attempts: int | None = None
    retry_min_interval: float | None = None
    retry_max_interval: float | None = None

    @classmethod
    def from_dict(cls, data: Any, *, where: str = "limits") -> Limits:
        if not isinstance(data, dict):
            raise RuleError(f"{where} must be an object")
        _reject_unknown(data, _LIMIT_KEYS, where=where)
        raw_concurrency = data.get("concurrency")
        concurrency: int | None = None
        if raw_concurrency is not None:
            concurrency = _as_int(raw_concurrency, 1, where=f"{where}.concurrency")
            if concurrency < 1:
                raise RuleError(
                    f"{where}.concurrency must be >= 1, got {concurrency} "
                    "(0 would create a zero-permit semaphore and deadlock)"
                )
        max_attempts: int | None = None
        if data.get("max_attempts") is not None:
            max_attempts = _as_int(
                data.get("max_attempts"), 1, where=f"{where}.max_attempts"
            )
            if max_attempts < 1:
                raise RuleError(
                    f"{where}.max_attempts must be >= 1, got {max_attempts}"
                )
        limits = cls(
            concurrency=concurrency,
            min_interval=_as_number(
                data.get("min_interval"), where=f"{where}.min_interval"
            ),
            max_interval=_as_number(
                data.get("max_interval"), where=f"{where}.max_interval"
            ),
            max_attempts=max_attempts,
            retry_min_interval=_as_number(
                data.get("retry_min_interval"), where=f"{where}.retry_min_interval"
            ),
            retry_max_interval=_as_number(
                data.get("retry_max_interval"), where=f"{where}.retry_max_interval"
            ),
        )
        _check_band(limits.min_interval, limits.max_interval, where=f"{where}.interval")
        _check_band(
            limits.retry_min_interval,
            limits.retry_max_interval,
            where=f"{where}.retry_interval",
        )
        return limits


def _check_band(low: float | None, high: float | None, *, where: str) -> None:
    """Reject an inverted ``min``/``max`` interval pair."""
    if low is not None and high is not None and low > high:
        raise RuleError(f"{where}: min {low} is greater than max {high}")


def _band_midpoint(low: float | None, high: float | None) -> float | None:
    """Collapse an optional ``[low, high]`` band to a single nominal value."""
    if low is None:
        return high
    if high is None:
        return low
    return (low + high) / 2.0


#: ``BaseFetcher._sleep`` jitters by at most this factor, and only upward.
_SLEEP_JITTER_FACTOR = 1.1


def _request_interval(low: float | None, high: float | None) -> float | None:
    """
    Pick a nominal ``request_interval`` that stays inside the declared band.

    The fetcher sleeps ``interval * uniform(1.0, 1.1)``, so the upper edge is
    the constraint that matters (exceeding it means hitting the site harder
    than the rule allows).  Capping at ``high / 1.1`` guarantees the jittered
    sleep never overshoots; on a narrow band that can land slightly *below*
    ``low``, which errs on the safe side and is deliberately not clamped back
    up.
    """
    interval = _band_midpoint(low, high)
    if interval is None:
        return None
    if high is not None:
        interval = min(interval, high / _SLEEP_JITTER_FACTOR)
    return interval


def apply_limits(config: FetcherConfig, limits: Limits | None) -> FetcherConfig:
    """
    Return a **new** config with ``limits`` applied (pure, no mutation).

    ``limits is None`` returns ``config`` unchanged.  Otherwise a copy is built
    with :func:`dataclasses.replace`, so a config object shared between sites
    is never modified in place (the failure mode of the upstream design).

    Mapping onto :class:`~novel_downloader.schemas.FetcherConfig` -- which has
    one value per concern, not a band:

    * ``concurrency`` -> ``max_connections``
    * ``min_interval`` / ``max_interval`` -> ``request_interval`` (band
      midpoint, further capped at ``max_interval / 1.1`` because
      ``BaseFetcher._sleep`` multiplies by ``random.uniform(1.0, 1.1)`` -- it
      only ever jitters **upward**, so a raw midpoint could overshoot
      ``max_interval`` on a narrow band).  With ``min=0.2, max=5.0`` the
      spacing stays ~2.6-2.86 s; with ``min=max=1.0`` it now stays at
      ~0.91-1.0 s instead of exceeding the band.
    * ``max_attempts`` -> ``retry_times = max_attempts - 1`` (``retry_times``
      counts *retries after* the first attempt)
    * ``retry_min_interval`` / ``retry_max_interval`` -> ``backoff_factor``
      (band midpoint)

    Unset fields leave the corresponding config value untouched.  Intervals are
    in **seconds**, matching ``FetcherConfig``.
    """
    if limits is None:
        return config

    updates: dict[str, Any] = {}
    if limits.concurrency is not None:
        updates["max_connections"] = limits.concurrency
    interval = _request_interval(limits.min_interval, limits.max_interval)
    if interval is not None:
        updates["request_interval"] = interval
    if limits.max_attempts is not None:
        updates["retry_times"] = max(0, limits.max_attempts - 1)
    backoff = _band_midpoint(limits.retry_min_interval, limits.retry_max_interval)
    if backoff is not None:
        updates["backoff_factor"] = backoff
    # ``replace`` always returns a fresh dataclass instance, even with no
    # updates, so callers cannot observe (or mutate) the caller's object.
    return replace(config, **updates)


# --------------------------------------------------------------------------- #
# field
# --------------------------------------------------------------------------- #
@dataclass
class Field:
    """A single extraction instruction.

    Execution order: selector -> ``attr`` -> ``regex`` (``group``) ->
    ``replace`` -> ``steps``.  The final value is whitespace-stripped.
    ``fallback`` entries are only consulted when that whole chain yields an
    empty string; the first non-empty fallback wins and its own default is not
    applied (the main field's ``default`` is still the last resort).
    """

    selector: str | None = None
    attr: str = "text"
    regex: str | None = None
    group: int = 1
    multi: bool = False
    replace: list[tuple[str, str]] = field(default_factory=list)
    default: str = ""
    steps: list[Step] = field(default_factory=list)
    fallback: list[Field] = field(default_factory=list)

    _pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.regex:
            try:
                self._pattern = re.compile(self.regex, re.S)
            except (re.error, TypeError) as e:
                # TypeError covers a non-string ``regex`` (e.g. a number/list).
                raise RuleError(
                    t("invalid field regex {regex!r}: {err}").format(
                        regex=self.regex, err=e
                    )
                ) from e
            # ``Field`` is the one place that compiles a rule regex without
            # going through ``_check_regex``; apply the same DoS guard here.
            risk = (
                regex_backtracking_risk(self.regex)
                if isinstance(self.regex, str)
                else None
            )
            if risk is not None:
                raise RuleError(
                    f"field regex {self.regex!r} risks catastrophic backtracking: "
                    f"{risk}"
                )

    @classmethod
    def from_dict(cls, data: Any, *, where: str = "field") -> Field:
        if isinstance(data, str):
            return cls(selector=data)
        if not isinstance(data, dict):
            raise RuleError(
                t("field must be a string or object, got {type!r}").format(
                    type=type(data)
                )
            )
        _reject_unknown(data, _FIELD_KEYS, where=where)
        selector = data.get("selector")
        if selector is not None and not isinstance(selector, str):
            raise RuleError(
                f"{where}: 'selector' must be a string, got {type(selector).__name__}"
            )
        raw_attr = data.get("attr", "text")
        if raw_attr is not None and not isinstance(raw_attr, str):
            raise RuleError(
                f"{where}: 'attr' must be a string, got {type(raw_attr).__name__}"
            )
        fld = cls(
            selector=selector,
            attr=_as_str(raw_attr, "text"),
            regex=data.get("regex"),
            group=_non_negative(
                _as_int(data.get("group"), 1, where=f"{where} 'group'"),
                where=f"{where} 'group'",
            ),
            multi=bool(data.get("multi", False)),
            replace=_as_replace(data.get("replace")),
            default=_as_str(data.get("default")),
            steps=_as_steps(data.get("steps"), where=f"{where}.steps"),
        )
        fld.fallback = _as_fallback(
            data.get("fallback"), fld, where=f"{where}.fallback"
        )
        return fld

    def apply_replace(self, value: str) -> str:
        for old, new in self.replace:
            value = value.replace(old, new)
        return value


def _as_fallback(value: Any, base: Field, *, where: str) -> list[Field]:
    """
    Normalize a ``fallback`` chain.

    A string inherits every option of ``base`` (``attr``, ``regex``,
    ``group``, ``replace``, ``steps``) and only swaps the selector; a dict is
    an independent :class:`Field`.  The inherited copy deliberately drops
    ``base.fallback`` so a self-referencing chain cannot recurse forever.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise RuleError(f"{where} must be a list")
    out: list[Field] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            if not item.strip():
                raise RuleError(f"{where}[{index}] must be a non-empty selector")
            out.append(replace(base, selector=item, fallback=[]))
        elif isinstance(item, dict):
            out.append(Field.from_dict(item, where=f"{where}[{index}]"))
        else:
            raise RuleError(
                f"{where}[{index}] must be a selector string or object, "
                f"got {type(item).__name__}"
            )
    return out


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
@dataclass
class SearchRule:
    url: str
    method: str = "GET"
    data: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    format: str | None = None
    item: str = ""
    fields: dict[str, Field] = field(default_factory=dict)
    book_id_regex: str | None = None
    book_id_group: int = 1
    priority: int = 200
    single_result_fallback: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> SearchRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError(t("search section requires a 'url'"))
        _reject_unknown(data, _SEARCH_KEYS, where="search")
        raw_fields = data.get("fields")
        if raw_fields is None:
            raw_fields = {}
        elif not isinstance(raw_fields, dict):
            raise RuleError("search.fields must be an object")
        _check_field_names(raw_fields, _SEARCH_FIELD_NAMES, where="search.fields")
        fields = {
            k: Field.from_dict(v, where=f"search.fields.{k}")
            for k, v in raw_fields.items()
        }
        json_body = data.get("json_body")
        if json_body is not None and not isinstance(json_body, dict):
            raise RuleError(t("search.json_body must be an object"))
        raw_data = data.get("data")
        if raw_data is not None and not isinstance(raw_data, dict):
            raise RuleError("search.data must be an object")
        method = _as_str(data.get("method"), "GET")
        if data.get("method") is not None and not isinstance(data["method"], str):
            raise RuleError("search.method must be a string")
        return cls(
            url=_as_str(data["url"]),
            method=method.upper(),
            data={str(k): _as_str(v) for k, v in (raw_data or {}).items()},
            json_body=json_body,
            headers=_as_headers(data.get("headers")),
            format=_section_format(data),
            item=_as_str(data.get("item")),
            fields=fields,
            book_id_regex=_check_regex(
                data.get("book_id_regex"), where="search.book_id_regex"
            ),
            book_id_group=_non_negative(
                _as_int(data.get("book_id_group"), 1, where="search.book_id_group"),
                where="search.book_id_group",
            ),
            priority=_as_int(data.get("priority"), 200, where="search.priority"),
            single_result_fallback=_as_bool(
                data.get("single_result_fallback"),
                False,
                where="search.single_result_fallback",
            ),
        )


# --------------------------------------------------------------------------- #
# book (info + catalog)
# --------------------------------------------------------------------------- #
@dataclass
class ChaptersRule:
    """
    Catalog ("table of contents") extraction.

    ``is_desc`` handles sites that list the newest chapter first.  The pages
    are still concatenated in page order first and the resulting **flat**
    chapter list is reversed once at the end (see
    :meth:`~.parser.RuleParser._parse_chapters`), so the order is correct for
    multi-page catalogs as well.  The current parser emits a single volume, so
    "reverse the flat list" and "reverse the whole book" are the same thing;
    should volumes ever be supported, the reversal must stay on the final
    flattened order rather than per volume.
    """

    item: str
    title: Field
    url: Field
    id_regex: str | None = None
    id_group: int = 1
    volume_name: str = "正文"
    container: str | None = None
    preprocess: list[Step] = field(default_factory=list)
    is_desc: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> ChaptersRule:
        if not isinstance(data, dict) or not data.get("item"):
            raise RuleError(t("book.chapters requires an 'item' selector"))
        _reject_unknown(data, _CHAPTERS_KEYS, where="book.chapters")
        container = data.get("container")
        if container is not None and not isinstance(container, str):
            raise RuleError("book.chapters.container must be a string")
        return cls(
            item=_as_str(data["item"]),
            title=Field.from_dict(
                data.get("title", {"attr": "text"}), where="book.chapters.title"
            ),
            url=Field.from_dict(
                data.get("url", {"attr": "href"}), where="book.chapters.url"
            ),
            id_regex=_check_regex(data.get("id_regex"), where="book.chapters.id_regex"),
            id_group=_non_negative(
                _as_int(data.get("id_group"), 1, where="book.chapters.id_group"),
                where="book.chapters.id_group",
            ),
            volume_name=_as_str(data.get("volume_name"), "正文"),
            container=container or None,
            preprocess=_as_steps(
                data.get("preprocess"), where="book.chapters.preprocess"
            ),
            is_desc=_as_bool(data.get("is_desc"), False, where="book.chapters.is_desc"),
        )


@dataclass
class PagesRule:
    """Extra pagination pages for info / catalog / chapter pages.

    By default pagination stops as soon as the *next* page is no longer
    referenced from the current page's payload.  For endpoints that carry no
    such link (typically JSON APIs) set ``max_pages`` to fetch a fixed range
    instead.

    Two optional guards make "walk until the link disappears" safe:

    * ``stop_when`` -- a regex matched against the **current page body**; on a
      hit, pagination stops (the current page is kept).
    * ``loop_guard`` -- default ``True``.  A page whose URL was already fetched
      or whose body digest equals an earlier page's stops the loop, so a site
      that keeps serving the last page forever cannot produce an infinite (or
      50-page) fetch.  Set it to ``False`` to restore the unguarded heuristic.
    """

    url: str
    start: int = 2
    max_pages: int | None = None
    stop_when: str | None = None
    loop_guard: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> PagesRule:
        if isinstance(data, str):
            return cls(url=data)
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError(t("pages rule requires a 'url'"))
        _reject_unknown(data, _PAGES_KEYS, where="pages")
        raw_max = data.get("max_pages")
        return cls(
            url=_as_str(data["url"]),
            start=_as_int(data.get("start"), 2, where="pages.start"),
            max_pages=(
                None
                if raw_max is None
                else _as_int(raw_max, 2, where="pages.max_pages")
            ),
            stop_when=_check_regex(data.get("stop_when"), where="pages.stop_when"),
            loop_guard=_as_bool(data.get("loop_guard"), True, where="pages.loop_guard"),
        )


@dataclass
class BookRule:
    url: str
    fields: dict[str, Field]
    chapters: ChaptersRule
    pages: PagesRule | None = None
    headers: dict[str, str] = field(default_factory=dict)
    format: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> BookRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError(t("book section requires a 'url'"))
        _reject_unknown(data, _BOOK_KEYS, where="book")
        pages = data.get("pages")
        raw_fields = data.get("fields")
        if raw_fields is None:
            raw_fields = {}
        elif not isinstance(raw_fields, dict):
            raise RuleError("book.fields must be an object")
        _check_field_names(raw_fields, _BOOK_FIELD_NAMES, where="book.fields")
        return cls(
            url=_as_str(data["url"]),
            fields={
                k: Field.from_dict(v, where=f"book.fields.{k}")
                for k, v in raw_fields.items()
            },
            chapters=ChaptersRule.from_dict(data.get("chapters") or {}),
            pages=PagesRule.from_dict(pages) if pages else None,
            headers=_as_headers(data.get("headers")),
            format=_section_format(data),
        )


# --------------------------------------------------------------------------- #
# chapter (content)
# --------------------------------------------------------------------------- #
@dataclass
class ChapterRule:
    url: str
    content: Field
    title: Field | None = None
    replace: list[tuple[str, str]] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    pages: PagesRule | None = None
    headers: dict[str, str] = field(default_factory=dict)
    format: str | None = None
    container: str | None = None
    preprocess: list[Step] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> ChapterRule:
        if not isinstance(data, dict) or not data.get("url"):
            raise RuleError(t("chapter section requires a 'url'"))
        _reject_unknown(data, _CHAPTER_KEYS, where="chapter")
        pages = data.get("pages")
        drop = [
            _check_regex(p, where="chapter.drop") or ""
            for p in _as_str_list(data.get("drop"), where="chapter.drop")
        ]
        container = data.get("container")
        if container is not None and not isinstance(container, str):
            raise RuleError("chapter.container must be a string")
        return cls(
            url=_as_str(data["url"]),
            content=Field.from_dict(
                data.get("content", {"selector": "body"}), where="chapter.content"
            ),
            title=(
                Field.from_dict(data["title"], where="chapter.title")
                if data.get("title")
                else None
            ),
            replace=_as_replace(data.get("replace")),
            drop=drop,
            pages=PagesRule.from_dict(pages) if pages else None,
            headers=_as_headers(data.get("headers")),
            format=_section_format(data),
            container=container or None,
            preprocess=_as_steps(data.get("preprocess"), where="chapter.preprocess"),
        )


# --------------------------------------------------------------------------- #
# url -> site resolution
# --------------------------------------------------------------------------- #
@dataclass
class ResolveRule:
    book: str | None = None
    chapter: str | None = None


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #
@dataclass
class Rule:
    site_key: str
    site_name: str = ""
    priority: int = 200
    base_url: str = ""
    encoding: str = "utf-8"
    format: str = "html"
    hosts: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    resolve: ResolveRule | None = None
    search: SearchRule | None = None
    book: BookRule | None = None
    chapter: ChapterRule | None = None
    limits: Limits | None = None

    @property
    def display_name(self) -> str:
        return self.site_name or self.site_key

    def format_of(self, section: str | None) -> str:
        """Resolve the effective format for a section (``None`` = inherit)."""
        return section or self.format

    @property
    def search_format(self) -> str:
        return self.format_of(self.search.format if self.search else None)

    @property
    def book_format(self) -> str:
        return self.format_of(self.book.format if self.book else None)

    @property
    def chapter_format(self) -> str:
        return self.format_of(self.chapter.format if self.chapter else None)


_VALID_ATTRS = {"text", "inner_text", "html"}


def _validate_field(fld: Field, where: str) -> None:
    """An empty ``attr`` is always a copy/paste bug (silently empty output)."""
    if fld.attr not in _VALID_ATTRS and not fld.attr:
        raise RuleError(t("{where}: empty 'attr'").format(where=where))
    for index, alt in enumerate(fld.fallback):
        _validate_field(alt, f"{where}.fallback[{index}]")


def _validate_fields(fields: dict[str, Field], where: str) -> None:
    for name, fld in fields.items():
        _validate_field(fld, f"{where}.{name}")


def parse_rule(data: dict[str, Any], *, source: str = "<memory>") -> Rule:
    """Validate a raw rule mapping and return a :class:`Rule`."""
    if not isinstance(data, dict):
        raise RuleError(t("{source}: rule must be a JSON object").format(source=source))
    _reject_unknown(data, _TOP_LEVEL_KEYS, where=source)

    site_key = _as_str(data.get("site_key"))
    if not site_key:
        raise RuleError(t("{source}: missing 'site_key'").format(source=source))

    resolve_raw = data.get("resolve")
    resolve = None
    if resolve_raw is not None:
        if not isinstance(resolve_raw, dict):
            raise RuleError(f"{source}: 'resolve' must be an object")
        _reject_unknown(resolve_raw, _RESOLVE_KEYS, where="resolve")
        resolve = ResolveRule(
            book=_check_regex(resolve_raw.get("book"), where="resolve.book"),
            chapter=_check_regex(resolve_raw.get("chapter"), where="resolve.chapter"),
        )

    book = BookRule.from_dict(data["book"]) if data.get("book") else None
    chapter = ChapterRule.from_dict(data["chapter"]) if data.get("chapter") else None
    search = SearchRule.from_dict(data["search"]) if data.get("search") else None
    limits_raw = data.get("limits")
    limits = (
        Limits.from_dict(limits_raw, where=f"{source}.limits")
        if limits_raw is not None
        else None
    )

    if book is None and chapter is None:
        raise RuleError(
            t("{source}: rule needs at least a 'book' or 'chapter' section").format(
                source=source
            )
        )
    if book is not None:
        _validate_fields(book.fields, "book.fields")
        _validate_field(book.chapters.title, "book.chapters.title")
        _validate_field(book.chapters.url, "book.chapters.url")
    if search is not None:
        _validate_fields(search.fields, "search.fields")
    if chapter is not None:
        _validate_field(chapter.content, "chapter.content")
        if chapter.title is not None:
            _validate_field(chapter.title, "chapter.title")

    return Rule(
        site_key=site_key,
        site_name=_as_str(data.get("site_name")),
        priority=_as_int(data.get("priority"), 200, where="priority"),
        base_url=_as_str(data.get("base_url")),
        encoding=_as_str(data.get("encoding"), "utf-8"),
        format=_check_format(_as_str(data.get("format"), "html").lower()),
        hosts=[h.lower() for h in _as_str_list(data.get("hosts"), where="hosts")],
        headers=_as_headers(data.get("headers")),
        resolve=resolve,
        search=search,
        book=book,
        chapter=chapter,
        limits=limits,
    )
