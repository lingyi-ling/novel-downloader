#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.plugins.utils.js_eval`.

Covers the pure-Python IIFE fast path (string/token/argument parsing) and the
Node fallback decision logic.  ``subprocess`` is always monkeypatched, so no
real Node.js process is ever started.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from novel_downloader.plugins.utils.js_eval import (
    JsEvaluator,
    _parse_js_string,
    _parse_js_token,
    _split_args,
    _tokenize_object,
)

# --------------------------------------------------------------------------- #
# _parse_js_string
# --------------------------------------------------------------------------- #


def test_parse_js_string_plain() -> None:
    assert _parse_js_string("'abc'") == "abc"
    assert _parse_js_string('"abc"') == "abc"


def test_parse_js_string_without_quotes_raises() -> None:
    with pytest.raises(ValueError, match="Invalid JS string literal"):
        _parse_js_string("abc")
    with pytest.raises(ValueError, match="Invalid JS string literal"):
        _parse_js_string("'abc")


def test_parse_js_string_standard_escapes() -> None:
    assert _parse_js_string(r"'a\nb'") == "a\nb"
    assert _parse_js_string(r"'a\tb'") == "a\tb"
    assert _parse_js_string(r"'a\rb'") == "a\rb"
    assert _parse_js_string(r"'a\bb'") == "a\bb"
    assert _parse_js_string(r"'a\fb'") == "a\fb"
    assert _parse_js_string(r"'a\vb'") == "a\vb"
    assert _parse_js_string(r"'a\0b'") == "a\0b"


def test_parse_js_string_quote_and_backslash_escapes() -> None:
    assert _parse_js_string(r"'a\'b'") == "a'b"
    assert _parse_js_string(r'''"a\"b"''') == 'a"b'
    assert _parse_js_string(r"'a\\b'") == "a\\b"


def test_parse_js_string_hex_and_unicode_escapes() -> None:
    assert _parse_js_string(r"'\x41'") == "A"
    assert _parse_js_string(r"'\u4e2d'") == "中"


def test_parse_js_string_lenient_unknown_escape() -> None:
    assert _parse_js_string(r"'a\qb'") == "aqb"


def test_parse_js_string_trailing_lone_backslash() -> None:
    # A dangling backslash ends parsing instead of raising.
    assert _parse_js_string(r"'ab\'") == "ab"


def test_parse_js_string_empty_body() -> None:
    assert _parse_js_string("''") == ""
    with pytest.raises(ValueError, match="Invalid JS string literal"):
        _parse_js_string("")


# --------------------------------------------------------------------------- #
# _parse_js_token
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tok", "expected"),
    [
        ("null", None),
        ("undefined", None),
        ("true", True),
        ("false", False),
        ("42", 42),
        ("-7", -7),
        ("+3", 3),
        ("1.5", 1.5),
        (".5", 0.5),
        ("1e3", 1000.0),
        ("-2.5E-2", -0.025),
        ("'hi'", "hi"),
        ('"hi"', "hi"),
        ("some_ident", "some_ident"),
    ],
)
def test_parse_js_token(tok: str, expected: object) -> None:
    assert _parse_js_token(tok) == expected


# --------------------------------------------------------------------------- #
# _split_args
# --------------------------------------------------------------------------- #


def test_split_args_flat() -> None:
    assert _split_args("1, 'a', b") == ["1", "'a'", "b"]


def test_split_args_nested_brackets() -> None:
    assert _split_args("{a: 1, b: 2}, [3, 4], (5, 6)") == [
        "{a: 1, b: 2}",
        "[3, 4]",
        "(5, 6)",
    ]


def test_split_args_commas_inside_strings() -> None:
    assert _split_args("'x,y', \"a,b\", z") == ["'x,y'", '"a,b"', "z"]


def test_split_args_escaped_quote_inside_string() -> None:
    assert _split_args(r"'a\'b, c', d") == [r"'a\'b, c'", "d"]


def test_split_args_empty_and_whitespace_only() -> None:
    assert _split_args("") == []
    assert _split_args("  ") == []


# --------------------------------------------------------------------------- #
# _tokenize_object
# --------------------------------------------------------------------------- #


def test_tokenize_object_basic() -> None:
    toks = _tokenize_object('{a: 1, "b c": [2, 3]}')
    assert toks == ["{", "a", ":", "1", ",", '"b c"', ":", "[", "2", ",", "3", "]", "}"]


def test_tokenize_object_line_comment() -> None:
    toks = _tokenize_object("{a: 1, // comment\n b: 2}")
    assert "//" not in toks
    assert "comment" not in toks
    assert toks[-1] == "}"


def test_tokenize_object_block_comment() -> None:
    toks = _tokenize_object("{a: /* hidden */ 1}")
    assert toks == ["{", "a", ":", "1", "}"]


def test_tokenize_object_string_with_escapes() -> None:
    toks = _tokenize_object(r"{'a\'b': 'c\nd'}")
    assert toks == ["{", r"'a\'b'", ":", r"'c\nd'", "}"]


# --------------------------------------------------------------------------- #
# JsEvaluator._eval_direct (IIFE fast path)
# --------------------------------------------------------------------------- #


def test_eval_direct_simple_iife() -> None:
    code = '(function(a, b){ return {x: a, y: b} }(1, "two"))'
    assert JsEvaluator._eval_direct(code) == {"x": 1, "y": "two"}


def test_eval_direct_nested_structures() -> None:
    code = '(function(n){ return {list: [1, 2, {deep: n}], s: "x"} }(true))'
    assert JsEvaluator._eval_direct(code) == {
        "list": [1, 2, {"deep": True}],
        "s": "x",
    }


def test_eval_direct_no_params() -> None:
    code = "(function(){ return {v: null} }())"
    assert JsEvaluator._eval_direct(code) == {"v": None}


def test_eval_direct_invalid_format_raises() -> None:
    with pytest.raises(ValueError, match="Invalid IIFE format"):
        JsEvaluator._eval_direct("var x = 1;")


def test_eval_direct_param_arg_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        JsEvaluator._eval_direct("(function(a, b){ return {x: a} }(1))")


def test_eval_direct_missing_colon_raises() -> None:
    with pytest.raises(ValueError, match="Expected :"):
        JsEvaluator._eval_direct("(function(){ return {a 1} }())")


# --------------------------------------------------------------------------- #
# JsEvaluator fallback decision logic
# --------------------------------------------------------------------------- #


def _make_evaluator(tmp_path: Path) -> JsEvaluator:
    return JsEvaluator(
        script_dir=tmp_path,
        script_src=tmp_path / "src.js",
        node_bin="node",
    )


def test_eval_empty_input_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.utils.js_eval.shutil.which", lambda name: None
    )
    ev = _make_evaluator(tmp_path)
    # Empty and whitespace-only inputs never reach Node.
    assert ev.eval("") is None
    assert ev.eval("   ") is None


def test_eval_uses_direct_fast_path_without_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.utils.js_eval.shutil.which", lambda name: None
    )

    def _fail(self: JsEvaluator, js_code: str) -> subprocess.CompletedProcess[str]:
        raise AssertionError("Node must not be invoked for a parseable IIFE")

    monkeypatch.setattr(JsEvaluator, "_run_node", _fail)
    ev = _make_evaluator(tmp_path)
    assert ev.has_node is False
    code = "(function(a){ return {v: a} }(7))"
    assert ev.eval(code) == {"v": 7}


def _fake_node_result(
    returncode: int, stdout: str, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["node"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_eval_falls_back_to_node_and_parses_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.utils.js_eval.shutil.which",
        lambda name: "/usr/bin/node",
    )

    calls: list[str] = []

    def _run(self: JsEvaluator, js_code: str) -> subprocess.CompletedProcess[str]:
        calls.append(js_code)
        return _fake_node_result(0, json.dumps({"ok": True}))

    monkeypatch.setattr(JsEvaluator, "_run_node", _run)
    ev = _make_evaluator(tmp_path)
    assert ev.eval("window.cfg = 1") == {"ok": True}
    assert calls == ["window.cfg = 1"]


def test_eval_node_unavailable_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.utils.js_eval.shutil.which", lambda name: None
    )
    ev = _make_evaluator(tmp_path)
    assert ev.eval("not an iife") is None


def test_eval_node_nonzero_exit_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.utils.js_eval.shutil.which",
        lambda name: "/usr/bin/node",
    )

    def _run(self: JsEvaluator, js_code: str) -> subprocess.CompletedProcess[str]:
        return _fake_node_result(1, "", stderr="boom")

    monkeypatch.setattr(JsEvaluator, "_run_node", _run)
    ev = _make_evaluator(tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        ev.eval("anything")


def test_eval_node_empty_stdout_returns_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.utils.js_eval.shutil.which",
        lambda name: "/usr/bin/node",
    )

    def _run(self: JsEvaluator, js_code: str) -> subprocess.CompletedProcess[str]:
        return _fake_node_result(0, "")

    monkeypatch.setattr(JsEvaluator, "_run_node", _run)
    ev = _make_evaluator(tmp_path)
    assert ev.eval("anything") == {}


def test_eval_node_invalid_json_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.utils.js_eval.shutil.which",
        lambda name: "/usr/bin/node",
    )

    def _run(self: JsEvaluator, js_code: str) -> subprocess.CompletedProcess[str]:
        return _fake_node_result(0, "not json")

    monkeypatch.setattr(JsEvaluator, "_run_node", _run)
    ev = _make_evaluator(tmp_path)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        ev.eval("anything")


def test_run_node_materialises_script_and_invokes_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.js"
    src.write_bytes(b"console.log(1)")
    ev = _make_evaluator(tmp_path)

    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _fake_node_result(0, "{}")

    monkeypatch.setattr(
        "novel_downloader.plugins.utils.js_eval.subprocess.run", _fake_run
    )

    result = ev._run_node("input-code")
    assert result.returncode == 0

    dst = tmp_path / "expr_to_json.js"
    assert dst.is_file()
    assert dst.read_bytes() == b"console.log(1)"
    assert captured["cmd"] == ["node", str(dst)]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["input"] == "input-code"
    assert kwargs["cwd"] == tmp_path

    # Second call reuses the extracted script.
    ev._run_node("again")
    assert dst.read_bytes() == b"console.log(1)"
