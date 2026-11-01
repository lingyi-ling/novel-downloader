#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.engine
------------------------------------

Extraction primitives used by the rule-driven fetcher/parser/searcher.

Two selector dialects are supported for every ``selector`` string:

* XPath -- when it starts with ``/`` or ``(`` (e.g. ``//div[@id="list"]//a``).
* CSS   -- anything else (e.g. ``div#list a``), via ``lxml`` + ``cssselect``.

When a section declares ``"format": "json"`` the same ``selector`` strings are
instead read as dot paths over the decoded JSON document (see
:func:`select_json`); ``items[*].name`` selects a key of every element of
``items``, while a trailing key that resolves to an array is expanded into its
elements.

The extraction rules implemented here are intentionally simple and predictable;
anything beyond them should live in a dedicated site plugin.

Post-processing and hooks
-------------------------
:class:`~.model.Field` values run ``regex`` -> ``replace`` -> ``steps`` (in
that order).  ``steps`` is the only place where arbitrary logic is allowed, and
it is deliberately constrained:

* ``py`` steps call one of the pure ``str -> str`` functions in
  :data:`PY_STEP_FUNCTIONS` (an explicit whitelist -- unknown names are a load
  time :class:`RuleError`).
* ``js`` steps run on :class:`~novel_downloader.libs.mini_js.MiniJS`, which
  understands ``let`` / ``function`` / ``return`` / expression statements,
  arithmetic, ternary, object/array literals and *dict/list* member access.  It
  does **not** support ``if``, loops, regex literals, string prototypes
  (``.replace``, ``.trim``, ...) or global constructors.  The whitelisted
  ``py`` helpers are injected as unary functions, and two multi-argument
  helpers cover the HTML-rewriting case that motivated ``preprocess``:
  ``replace_all(text, old, new)`` and ``regex_replace(text, pattern, repl)``
  (Python ``re.sub`` semantics; a backslash passes through the JSON *and* JS
  string escaping, so a digit class is four backslashes in the rule file).
  ``input`` (alias ``result``) is the string being processed.  Unsupported code
  raises :class:`RuleError`, never a silent pass-through.

:func:`preprocess` rewrites a page's HTML before the selectors run.
:func:`should_stop_pagination` guards the extra-page walk.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import re
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote, unquote, urljoin

from lxml import etree
from lxml import html as lxml_html

from novel_downloader.libs.mini_js import MiniJS

from .model import Field, PagesRule, RuleError, Step, regex_backtracking_risk

__all__ = [
    "parse_page",
    "parse_json",
    "strip_nested_html",
    "select",
    "select_json",
    "node_to_str",
    "json_to_str",
    "extract",
    "extract_multi",
    "apply_steps",
    "preprocess",
    "body_fingerprint",
    "matches_stop_when",
    "should_stop_pagination",
    "PY_STEP_FUNCTIONS",
    "JS_STEP_HELPERS",
    "render",
    "abs_url",
]

_NESTED_HTML_RE = re.compile(r"<!DOCTYPE.*?>", re.I | re.S)

# a ``{name}`` placeholder used by :func:`render` (single pass)
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# a single dot-path segment: ``key``, ``key[2]``, ``key[*]`` or ``[0]``
_SEGMENT_RE = re.compile(r"^([^\[\]]*)(?:\[([^\]]*)\])?$")


def strip_nested_html(s: str) -> str:
    """Remove a nested document wrapper some sites embed inside a page."""
    s = _NESTED_HTML_RE.sub("", s)
    for tag in ("html", "body"):
        s = s.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
    return s


def parse_json(raw: str) -> Any:
    """Decode a JSON response body, raising :class:`RuleError` when unusable."""
    text = (raw or "").strip()
    if not text:
        raise RuleError("empty response cannot be parsed as JSON")
    try:
        return json.loads(text)
    except ValueError as e:
        raise RuleError(f"unparseable JSON response: {e}") from e


def parse_page(raw: str, fmt: str = "html") -> Any:
    """
    Parse a raw response into the document used by the extractors.

    Pages that are empty *after* removing a nested document wrapper (which
    happens on some sites' interstitial/blank responses) are reported as a
    :class:`RuleError` rather than leaking an ``lxml`` parser exception.

    :param fmt: ``"html"`` (default) returns an lxml tree, ``"json"`` returns
        the decoded Python object.
    """
    if fmt == "json":
        return parse_json(raw)

    text = strip_nested_html(raw or "")
    if not text.strip():
        raise RuleError("empty page cannot be parsed")
    try:
        return lxml_html.fromstring(text)
    except etree.ParserError as e:
        raise RuleError(f"unparseable page: {e}") from e


def select(root: Any, selector: str, fmt: str = "html") -> list[Any]:
    """Run a selector against ``root`` and return the matched nodes/values."""
    if fmt == "json":
        return select_json(root, selector)
    if not selector:
        return [root]
    if isinstance(root, str):
        # Bare string nodes (an XPath ``@attr`` / ``text()`` item selector)
        # cannot be descended into further; report "no match" instead of
        # leaking an ``AttributeError``.
        return []
    if selector.startswith(("/", "(", "./")):
        try:
            return list(root.xpath(selector))
        except etree.XPathError as e:
            raise RuleError(f"invalid selector {selector!r}: {e}") from e
    try:
        return list(root.cssselect(selector))
    except Exception as e:  # cssselect raises various errors on bad input
        raise RuleError(f"invalid selector {selector!r}: {e}") from e


def _path_segments(path: str) -> list[tuple[str, str | None]]:
    """Split ``$.a.b[*].c`` into ``[(key, index-or-None), ...]``."""
    body = path.strip()
    if body.startswith("$"):
        body = body[1:]
    body = body.lstrip(".")
    if not body:
        return []
    segments: list[tuple[str, str | None]] = []
    for piece in body.split("."):
        match = _SEGMENT_RE.match(piece)
        if match is None:  # pragma: no cover - regex is total over dot-pieces
            segments.append((piece, None))
        else:
            segments.append((match.group(1), match.group(2)))
    return segments


def _apply_index(values: list[Any], index: str) -> list[Any]:
    out: list[Any] = []
    if index == "*":
        for value in values:
            if isinstance(value, (list, tuple)):
                out.extend(value)
            elif isinstance(value, dict):
                out.extend(value.values())
        return out
    try:
        wanted = int(index)
    except ValueError:
        return []
    for value in values:
        if isinstance(value, (list, tuple)) and -len(value) <= wanted < len(value):
            out.append(value[wanted])
    return out


def _walk(values: list[Any], key: str, index: str | None) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if not key:
            out.append(value)
        elif isinstance(value, dict):
            if key in value:
                out.append(value[key])
        elif isinstance(value, (list, tuple)):
            # implicit fan-out: ``items.name`` means "name of every element"
            out.extend(
                elem[key] for elem in value if isinstance(elem, dict) and key in elem
            )
    return _apply_index(out, index) if index is not None else out


def select_json(root: Any, path: str) -> list[Any]:
    """
    Resolve a dot path against a decoded JSON document.

    ``key`` descends into an object, ``[n]`` picks an array element, ``[*]``
    expands every element (or every value of an object) and ``a.b`` fans out
    over arrays of objects.  Unless the final segment indexes explicitly, an
    array result is expanded into its elements so a ``item`` selector yields
    the collection to iterate over.
    """
    segments = _path_segments(path)
    if not segments:
        return [root]

    values: list[Any] = [root]
    for key, index in segments:
        values = _walk(values, key, index)

    if segments[-1][1] is not None:
        return values

    expanded: list[Any] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            expanded.extend(value)
        else:
            expanded.append(value)
    return expanded


def node_to_str(node: Any, attr: str) -> str:
    """Convert a matched node into a string according to ``attr``."""
    if isinstance(node, str):
        # xpath returned a bare string (e.g. ".../@href" or ".../text()")
        return node.strip()

    if attr == "text":
        return "".join(t.strip() for t in node.itertext() if t and t.strip())

    if attr == "inner_text":
        lines = [t.strip() for t in node.itertext() if t and t.strip()]
        return "\n".join(lines)

    if attr == "html":
        return "".join(lxml_html.tostring(child, encoding="unicode") for child in node)

    return (node.get(attr) or "").strip()


def json_to_str(value: Any, attr: str) -> str:
    """
    Convert a decoded JSON value into a string according to ``attr``.

    ``text`` / ``inner_text`` mean "the value itself" (numbers and booleans are
    stringified, arrays are joined element-wise); any other ``attr`` is looked
    up as a key of the matched object.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if attr in ("text", "inner_text", "html"):
            return json.dumps(value, ensure_ascii=False)
        found = value.get(attr)
        return json_to_str(found, "text")
    if isinstance(value, (list, tuple)):
        if attr in ("text", "html"):
            return "".join(json_to_str(item, "text") for item in value)
        if attr == "inner_text":
            return "\n".join(json_to_str(item, "text") for item in value)
        return json.dumps(value, ensure_ascii=False)
    return str(value)  # pragma: no cover - defensive


def _node_str(node: Any, attr: str, fmt: str) -> str:
    if fmt == "json":
        return json_to_str(node, attr)
    return node_to_str(node, attr)


# --------------------------------------------------------------------------- #
# post-processing steps
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]*>")
_TAG_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _py_strip_html(value: str) -> str:
    """Drop HTML comments and tags, leaving the text content."""
    return _TAG_RE.sub("", _TAG_COMMENT_RE.sub("", value))


def _py_normalize_space(value: str) -> str:
    """Collapse every whitespace run into a single space."""
    return " ".join(value.split())


def _py_dedupe_lines(value: str) -> str:
    """Drop blank and repeated lines, keeping the first occurrence order."""
    seen: set[str] = set()
    out: list[str] = []
    for line in value.splitlines():
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def _py_base64_decode(value: str) -> str:
    """Decode standard Base64 (whitespace tolerated, padding inferred)."""
    compact = "".join(value.split())
    padded = compact + "=" * (-len(compact) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"invalid base64 input: {e}") from e
    return raw.decode("utf-8", errors="replace")


#: Explicit whitelist of pure ``str -> str`` functions usable as ``py`` steps.
#: Every entry must be side-effect free, deterministic and accept/return a
#: ``str``; anything else does not belong in a rule file.
PY_STEP_FUNCTIONS: dict[str, Callable[[str], str]] = {
    "strip_html": _py_strip_html,
    "normalize_space": _py_normalize_space,
    "dedupe_lines": _py_dedupe_lines,
    "base64_decode": _py_base64_decode,
    "url_decode": unquote,
    "url_encode": quote,
    "html_unescape": html.unescape,
    "strip": lambda value: value.strip(),
    "lower": lambda value: value.lower(),
    "upper": lambda value: value.upper(),
    "reverse": lambda value: value[::-1],
}


def _step_where(step: Step, index: int) -> str:
    return step.where or f"steps[{index}]"


def _coerce_step_result(value: Any, *, where: str) -> str:
    """Require a step to produce text (``None`` -> empty, scalars stringified)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise RuleError(f"{where}: step must produce a string, got {type(value).__name__}")


def _js_helper(fn: Callable[[str], str]) -> Callable[[list[Any]], str]:
    """Adapt a ``py`` whitelist function to the mini-js calling convention."""

    def _call(args: list[Any]) -> str:
        if not args:
            raise TypeError("expected at least one argument")
        return fn(_coerce_step_result(args[0], where="js helper argument"))

    return _call


def _js_args(args: list[Any], count: int, name: str) -> list[str]:
    """Coerce exactly ``count`` mini-js call arguments to strings."""
    if len(args) != count:
        raise TypeError(f"{name} expects {count} arguments, got {len(args)}")
    return [_coerce_step_result(arg, where=f"{name} argument") for arg in args]


def _js_replace_all(args: list[Any]) -> str:
    """``replace_all(text, old, new)`` -- literal substring replacement."""
    value, old, new = _js_args(args, 3, "replace_all")
    return value.replace(old, new)


def _js_regex_replace(args: list[Any]) -> str:
    """``regex_replace(text, pattern, repl)`` -- Python ``re.sub`` semantics."""
    value, pattern, repl = _js_args(args, 3, "regex_replace")
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid pattern {pattern!r}: {e}") from e
    # ``re`` cannot be interrupted, so a pattern that backtracks exponentially
    # (``(a+)+$``) would hang the worker on one page of text.  The pattern is
    # only known at run time here, hence the check at this point.
    risk = regex_backtracking_risk(pattern)
    if risk is not None:
        raise ValueError(f"pattern {pattern!r} risks catastrophic backtracking: {risk}")
    return compiled.sub(repl, value)


#: Extra helpers available **only** inside ``js`` steps (they take several
#: arguments, so they are not ``py`` step names).  Keeping the HTML rewrite
#: capability here is what makes ``preprocess`` useful even though mini-js has
#: no string prototype methods.
JS_STEP_HELPERS: dict[str, Callable[[list[Any]], str]] = {
    "replace_all": _js_replace_all,
    "regex_replace": _js_regex_replace,
}


def _run_py_step(value: str, step: Step, *, where: str) -> str:
    fn = PY_STEP_FUNCTIONS.get(step.name)
    if fn is None:  # defensive: names are validated when the rule is parsed
        raise RuleError(
            f"{where}: unknown py step {step.name!r} "
            f"(allowed: {', '.join(sorted(PY_STEP_FUNCTIONS))})"
        )
    try:
        return _coerce_step_result(fn(value), where=where)
    except RuleError:
        raise
    except Exception as e:
        raise RuleError(f"{where}: py step {step.name!r} failed: {e}") from e


def _run_js_step(value: str, step: Step, *, where: str) -> str:
    js = MiniJS()
    # A fresh instance per step keeps variables from leaking between rules or
    # between pages.  Injection goes through the public ``bind`` API so this
    # module does not depend on mini-js internals.
    js.bind("input", value)
    js.bind("result", value)
    for name, fn in PY_STEP_FUNCTIONS.items():
        js.bind(name, _js_helper(fn))
    for name, helper in JS_STEP_HELPERS.items():
        js.bind(name, helper)
    try:
        result = js.eval(step.code)
    except RuleError:
        raise
    except Exception as e:
        raise RuleError(
            f"{where}: js step failed ({type(e).__name__}: {e}); "
            "mini-js supports expressions/let/function only -- see the "
            "engine module docstring for the exact subset"
        ) from e
    return _coerce_step_result(result, where=where)


def apply_steps(value: str, steps: Sequence[Step]) -> str:
    """
    Run ``steps`` in order, feeding each step's output to the next.

    :raises RuleError: when a step name is unknown, the code is unsupported by
        mini-js, or a step raises.  The message carries the rule location
        recorded in :attr:`~.model.Step.where`.
    """
    for index, step in enumerate(steps):
        where = _step_where(step, index)
        if step.lang == "py":
            value = _run_py_step(value, step, where=where)
        else:
            value = _run_js_step(value, step, where=where)
    return value


# --------------------------------------------------------------------------- #
# HTML pre-processing hook
# --------------------------------------------------------------------------- #
def preprocess(
    raw: str,
    steps: Sequence[Step],
    *,
    container: str | None = None,
    where: str = "preprocess",
) -> str:
    """
    Rewrite a page's HTML with ``steps`` before the selectors run.

    The program receives the *inner* HTML of ``container`` when it is set, or
    the whole raw response otherwise, and its output is re-parsed by the caller
    (see :meth:`~.parser.RuleParser`).  Typical uses: drop decoy chapters
    hidden with ``display:none``, turn ``onclick="read(123)"`` into a real
    ``href``, or absolutise relative links.

    :raises RuleError: when ``container`` matches nothing, the program errors,
        or the result is empty / not parsable as HTML.  A rewrite that silently
        yields nothing would otherwise produce a book with zero chapters.  The
        message names the section *and* the last step's location
        (``book.chapters.preprocess[1]``), because the damage is done by the
        final step even when the output looks fine in isolation.
    """
    if not steps:
        return raw
    tree = parse_page(raw, "html")
    if container:
        nodes = select(tree, container, "html")
        if not nodes:
            raise RuleError(
                f"{where}: container {container!r} matched nothing "
                f"(used by {_step_where(steps[0], 0)} and the steps after it)"
            )
        source = node_to_str(nodes[0], "html")
    else:
        source = raw
    result = apply_steps(source, steps)
    last = _step_where(steps[-1], len(steps) - 1)
    if not result.strip():
        raise RuleError(
            f"{where}: produced an empty document (output of {last} is blank)"
        )
    try:
        parse_page(result, "html")
    except RuleError as e:
        raise RuleError(
            f"{where}: produced invalid HTML (output of {last}): {e}"
        ) from e
    return result


# --------------------------------------------------------------------------- #
# pagination guard
# --------------------------------------------------------------------------- #
def body_fingerprint(body: str) -> str:
    """Digest a page body for duplicate-page detection."""
    return hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()


def matches_stop_when(pages: PagesRule, body: str) -> bool:
    """Whether ``body`` matches the rule's optional ``stop_when`` regex."""
    pattern = pages.stop_when
    if not pattern:
        return False
    return re.search(pattern, body, re.S) is not None


def should_stop_pagination(
    pages: PagesRule,
    body: str,
    url: str,
    seen_urls: set[str],
    seen_bodies: set[str],
) -> bool:
    """
    Decide whether fetching more pages must stop after consuming ``body``.

    ``url`` should be the URL of ``body`` and must *not* be in ``seen_urls``
    yet: this function records it (and the body digest) as visited.  The two
    sets are the caller's pagination state, so a repeated page is detected
    across the whole walk without any module-level mutable state.

    * ``pages.stop_when`` matching the body stops immediately (page kept).
    * with ``pages.loop_guard`` (default) an already-seen URL or an identical
      body digest also stops, which is what prevents the classic
      "last page links to itself forever" infinite loop.

    The URL is recorded as visited even when ``loop_guard`` is off: the
    duplicate-URL guard predates the body-digest guard and must keep working
    when only the latter is disabled.
    """
    matched = matches_stop_when(pages, body)
    if not pages.loop_guard:
        seen_urls.add(url)
        return matched
    fingerprint = body_fingerprint(body)
    repeated = url in seen_urls or fingerprint in seen_bodies
    seen_urls.add(url)
    seen_bodies.add(fingerprint)
    return matched or repeated


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def _postprocess(value: str, fld: Field) -> str:
    if fld._pattern is not None:
        m = fld._pattern.search(value)
        if m:
            group = fld.group
            # ``group`` is validated >= 0 at load time; stay defensive anyway
            # because a negative index raises a raw ``IndexError``.
            value = m.group(group) if 0 < group <= len(m.groups()) else m.group(0)
    value = fld.apply_replace(value).strip()
    # Steps only run on a non-empty value: otherwise a constant-producing
    # program could invent content for a selector that matched nothing.
    if value and fld.steps:
        value = apply_steps(value, fld.steps).strip()
    return value


def _extract_one(node: Any, fld: Field, fmt: str) -> str:
    return _postprocess(_node_str(node, fld.attr, fmt), fld)


def _nodes(root: Any, fld: Field, fmt: str) -> list[Any]:
    return select(root, fld.selector, fmt) if fld.selector else [root]


def _extract_chain(root: Any, fld: Field, fmt: str) -> str:
    """First non-empty value from ``fld`` and, if empty, its fallback chain."""
    for node in _nodes(root, fld, fmt):
        value = _extract_one(node, fld, fmt)
        if value:
            return value
    for alt in fld.fallback:
        value = _extract_chain(root, alt, fmt)
        if value:
            return value
    return ""


def extract(root: Any, fld: Field, fmt: str = "html") -> str:
    """
    Extract a single string for ``fld`` from ``root`` (or a child node).

    The main selector is tried first; when it yields nothing the ``fallback``
    chain is tried in order.  Only after every option failed is the field's
    ``default`` used (a fallback's own ``default`` is ignored so the main
    field always has the final say).
    """
    value = _extract_chain(root, fld, fmt)
    if value:
        return value
    return fld.apply_replace(fld.default).strip()


def _extract_all(root: Any, fld: Field, fmt: str) -> list[str]:
    out: list[str] = []
    for node in _nodes(root, fld, fmt):
        value = _extract_one(node, fld, fmt)
        if value:
            out.append(value)
    return out


def extract_multi(root: Any, fld: Field, fmt: str = "html") -> list[str]:
    """Extract every non-empty match for ``fld`` from ``root``.

    As with :func:`extract`, if the main selector matches nothing the first
    fallback that yields values wins.
    """
    out = _extract_all(root, fld, fmt)
    if out:
        return out
    for alt in fld.fallback:
        out = extract_multi(root, alt, fmt)
        if out:
            return out
    return out


def render(template: str, **kv: Any) -> str:
    """
    Substitute ``{name}`` placeholders without touching other braces.

    The substitution is a *single pass* over the template: inserted values are
    never rescanned, so a value that itself contains placeholder text (e.g. a
    ``book_id`` scraped from a hostile page URL) cannot smuggle in another
    variable's expansion.  Unknown braces are left untouched.
    """
    if not kv:
        return template

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(kv[key]) if key in kv else match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template)


def abs_url(base: str, url: str) -> str:
    """Resolve ``url`` against ``base`` and normalize protocol-relative URLs."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base, url)
