#!/usr/bin/env python3
"""Behavioural tests for the ``scripts/update_*.py`` and ``compile_mo.py`` tools.

Every test works on a throw-away locale tree; the real catalogs are never
opened for writing.
"""

from __future__ import annotations

import gettext
import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

_BABEL = pytest.importorskip("babel")
_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> ModuleType:
    """Import a repo script by path (``scripts/`` is not a package)."""
    path = _ROOT / "scripts" / f"{name}.py"
    module_name = f"_i18n_script_test_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` resolves annotations through ``sys.modules``.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _header(*, language: str = "zh_CN") -> str:
    return (
        'msgid ""\n'
        'msgstr ""\n'
        '"POT-Creation-Date: 2025-01-01 00:00+0000\\n"\n'
        f'"Language: {language}\\n"\n'
        '"Content-Type: text/plain; charset=utf-8\\n"\n'
    )


def _pot(entries: str) -> str:
    return _header() + "\n" + entries


def _po(entries: str, *, language: str = "zh_CN") -> str:
    return _header(language=language) + "\n" + entries


def _escape(text: str) -> str:
    """Escape a value for a single-line PO string."""
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _entry(msgid: str, msgstr: str = "") -> str:
    return f'msgid "{_escape(msgid)}"\nmsgstr "{_escape(msgstr)}"\n'


@pytest.fixture()
def po_script() -> ModuleType:
    return _load_script("update_po")


@pytest.fixture()
def pot_script() -> ModuleType:
    return _load_script("update_pot")


@pytest.fixture()
def mo_script() -> ModuleType:
    return _load_script("compile_mo")


def _write_locale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    po_text: str,
    pot_text: str,
) -> tuple[Path, Path]:
    locales = tmp_path / "locales"
    po_file = locales / "zh_CN" / "LC_MESSAGES" / "messages.po"
    po_file.parent.mkdir(parents=True)
    po_file.write_text(po_text, encoding="utf-8")
    pot_file = locales / "messages.pot"
    pot_file.write_text(pot_text, encoding="utf-8")
    monkeypatch.setattr(module, "LOCALES_DIR", locales)
    monkeypatch.setattr(module, "POT_FILE", pot_file)
    monkeypatch.setattr(module, "LANGS", ("zh_CN",))
    return po_file, pot_file


def _read_catalog(module: ModuleType, path: Path):  # noqa: ANN202
    from babel.messages.pofile import read_po

    with path.open("rb") as handle:
        return read_po(handle, locale="zh_CN")


# ---------------------------------------------------------------------------
# update_po: preservation and validation
# ---------------------------------------------------------------------------


def test_existing_translation_is_preserved_and_new_entry_is_empty(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hello", "你好")),
        pot_text=_pot(_entry("Hello") + _entry("Bye")),
    )

    assert po_script.main([]) == 0

    catalog = _read_catalog(po_script, po_file)
    hello = catalog.get("Hello")
    bye = catalog.get("Bye")
    assert hello is not None and hello.string == "你好"
    assert bye is not None and bye.string == ""
    assert not any(message.fuzzy for message in catalog if message.id)


def test_placeholder_mismatch_is_kept_but_marked_fuzzy(
    po_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hi {name}", "你好 {nom}")),
        pot_text=_pot(_entry("Hi {name}")),
    )

    assert po_script.main([]) == 0

    catalog = _read_catalog(po_script, po_file)
    message = catalog.get("Hi {name}")
    assert message is not None
    # The human translation is preserved for review instead of being replaced.
    assert message.string == "你好 {nom}"
    assert message.fuzzy
    assert "placeholder" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("msgid", "translation"),
    [
        ("Hello {name}", "你好 {name}"),
        ("%s item(s)", "%s 个项目"),
        ("%(count)d item(s)", "%(count)d 个项目"),
        ("{0} of {1}", "{0}/{1}"),
        ("literal %% and %s", "字面 %% 和 %s"),
    ],
)
def test_matching_placeholders_are_not_marked_fuzzy(
    po_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    msgid: str,
    translation: str,
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry(msgid, translation)),
        pot_text=_pot(_entry(msgid)),
    )

    assert po_script.main([]) == 0
    message = _read_catalog(po_script, po_file).get(msgid)
    assert message is not None
    assert message.string == translation
    assert not message.fuzzy


def test_changed_msgid_does_not_inherit_or_fuzzy_match_translation(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Open file", "打开文件")),
        pot_text=_pot(_entry("Open the file")),
    )

    assert po_script.main([]) == 0

    catalog = _read_catalog(po_script, po_file)
    message = catalog.get("Open the file")
    assert message is not None
    assert message.string == ""
    assert not any(entry.fuzzy for entry in catalog if entry.id)


def test_html_newline_and_whitespace_drift_is_flagged(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(
            _entry("<b>Bold</b>", "粗体")
            + _entry("Line one\nLine two", "第一行第二行")
            + _entry(" padded ", "无空格")
        ),
        pot_text=_pot(
            _entry("<b>Bold</b>") + _entry("Line one\nLine two") + _entry(" padded ")
        ),
    )

    assert po_script.main([]) == 0

    catalog = _read_catalog(po_script, po_file)
    for msgid in ("<b>Bold</b>", "Line one\nLine two", " padded "):
        message = catalog.get(msgid)
        assert message is not None, msgid
        assert message.fuzzy, msgid


def test_check_mode_reports_without_writing(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hello", "你好")),
        pot_text=_pot(_entry("Hello") + _entry("Bye")),
    )
    original = po_file.read_bytes()

    assert po_script.main(["--check"]) == 1
    assert po_file.read_bytes() == original

    assert po_script.main([]) == 0
    after_update = po_file.read_bytes()
    assert after_update != original

    # "Bye" still has an empty msgstr, so the strict check keeps failing while
    # still leaving the file untouched.
    assert po_script.main(["--check"]) == 1
    assert po_file.read_bytes() == after_update


def test_check_mode_is_clean_when_everything_is_translated(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hello", "你好") + _entry("Bye", "再见")),
        pot_text=_pot(_entry("Hello") + _entry("Bye")),
    )

    assert po_script.main([]) == 0
    stable = po_file.read_bytes()

    assert po_script.main(["--check"]) == 0
    assert po_file.read_bytes() == stable


def test_check_mode_flags_fuzzy_residue(
    po_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po("#, fuzzy\n" + _entry("World", "世界")),
        pot_text=_pot(_entry("World")),
    )

    assert po_script.main([]) == 0
    stable = po_file.read_bytes()

    assert po_script.main(["--check"]) == 1
    assert po_file.read_bytes() == stable
    assert "fuzzy" in capsys.readouterr().err


def test_two_runs_are_byte_identical(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hello", "你好") + _entry("Hi {name}", "你好 {name}")),
        pot_text=_pot(_entry("Hello") + _entry("Hi {name}") + _entry("Bye")),
    )

    assert po_script.main([]) == 0
    first = po_file.read_bytes()
    assert po_script.main([]) == 0
    assert po_file.read_bytes() == first


def test_crlf_line_endings_are_preserved(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hello", "你好")),
        pot_text=_pot(_entry("Hello")),
    )
    # Normalize the hand-written header first, then introduce Windows line
    # endings. A no-op update must leave those bytes untouched.
    assert po_script.main([]) == 0
    data = po_file.read_bytes().replace(b"\r\n", b"\n")
    po_file.write_bytes(data.replace(b"\n", b"\r\n"))
    original = po_file.read_bytes()

    assert po_script.main(["--check"]) == 0
    assert po_file.read_bytes() == original

    assert po_script.main([]) == 0
    updated = po_file.read_bytes()
    assert updated == original
    assert updated.count(b"\n") == updated.count(b"\r\n")


def test_bom_is_reported_and_removed(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hello", "你好")),
        pot_text=_pot(_entry("Hello")),
    )
    po_file.write_bytes(b"\xef\xbb\xbf" + po_file.read_bytes())

    assert po_script.main(["--check"]) == 1
    assert po_file.read_bytes().startswith(b"\xef\xbb\xbf")

    assert po_script.main([]) == 0
    assert not po_file.read_bytes().startswith(b"\xef\xbb\xbf")
    assert _read_catalog(po_script, po_file).get("Hello").string == "你好"
    assert po_script.main(["--check"]) == 0


def test_duplicate_msgid_is_a_hard_error_and_file_is_not_written(
    po_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hello", "你好") + _entry("Hello", "您好")),
        pot_text=_pot(_entry("Hello")),
    )
    original = po_file.read_bytes()

    assert po_script.main([]) == 1
    assert po_file.read_bytes() == original
    assert "duplicate" in capsys.readouterr().err


def test_non_utf8_catalog_is_rejected(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(_entry("Hello", "你好")),
        pot_text=_pot(_entry("Hello")),
    )
    po_file.write_bytes(b"\xff\xfe\x00invalid")

    assert po_script.main([]) == 1
    assert po_file.read_bytes() == b"\xff\xfe\x00invalid"


def test_context_and_plural_translations_are_preserved(
    po_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context_entry = 'msgctxt "menu"\nmsgid "Open"\nmsgstr "打开"\n'
    plural_entry = (
        'msgid "One file"\nmsgid_plural "{n} files"\nmsgstr[0] "{n} 个文件"\n'
    )
    po_file, _ = _write_locale(
        tmp_path,
        monkeypatch,
        po_script,
        po_text=_po(context_entry + plural_entry),
        pot_text=_pot(
            'msgctxt "menu"\nmsgid "Open"\nmsgstr ""\n'
            + 'msgid "One file"\nmsgid_plural "{n} files"\nmsgstr[0] ""\n'
        ),
    )

    assert po_script.main([]) == 0

    catalog = _read_catalog(po_script, po_file)
    contextual = catalog.get("Open", context="menu")
    assert contextual is not None and contextual.string == "打开"
    plural = catalog.get(("One file", "{n} files"))
    assert plural is not None
    assert plural.string == ("{n} 个文件",)
    assert not contextual.fuzzy and not plural.fuzzy


def test_placeholder_helpers(po_script: ModuleType) -> None:
    placeholders = po_script._placeholders

    assert placeholders("Hi {name}") == Counter({("brace", "name"): 1})
    assert placeholders("{0} and {0}") == Counter({("brace", "0"): 2})
    assert placeholders("{name!r:>10}") == Counter({("brace", "name"): 1})
    assert placeholders("{{literal}}") == Counter()
    assert placeholders("%(x)s %s %% 100% 50% complete") == Counter(
        {("printf", "x:s"): 1, ("printf", ":s"): 1}
    )
    assert placeholders("Save 20%off today") == Counter()
    assert po_script._html_tags("Path: <output directory>/<keyword>") == Counter()
    assert po_script._html_tags("<b>x</b><br>") == Counter({"b": 2, "br": 1})


# ---------------------------------------------------------------------------
# compile_mo: fuzzy entries never reach the .mo
# ---------------------------------------------------------------------------


def test_compile_mo_skips_fuzzy_entries(
    mo_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locales = tmp_path / "locales"
    po_file = locales / "zh_CN" / "LC_MESSAGES" / "messages.po"
    po_file.parent.mkdir(parents=True)
    po_file.write_text(
        _po(_entry("Hello", "你好") + "#, fuzzy\n" + _entry("World", "世界")),
        encoding="utf-8",
    )
    monkeypatch.setattr(mo_script, "LOCALES_DIR", locales)

    assert mo_script.main([]) == 0

    mo_file = po_file.with_suffix(".mo")
    with mo_file.open("rb") as handle:
        translations = gettext.GNUTranslations(handle)
    assert translations.gettext("Hello") == "你好"
    assert translations.gettext("World") == "World"


def test_compile_mo_check_mode(
    mo_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locales = tmp_path / "locales"
    po_file = locales / "zh_CN" / "LC_MESSAGES" / "messages.po"
    po_file.parent.mkdir(parents=True)
    po_file.write_text(_po(_entry("Hello", "你好")), encoding="utf-8")
    monkeypatch.setattr(mo_script, "LOCALES_DIR", locales)

    assert mo_script.main(["--check"]) == 1
    assert not po_file.with_suffix(".mo").exists()

    assert mo_script.main([]) == 0
    assert mo_script.main(["--check"]) == 0


# ---------------------------------------------------------------------------
# update_pot: context/plural, duplicate conflicts, --check, idempotency
# ---------------------------------------------------------------------------


@pytest.fixture()
def pot_source(tmp_path: Path, pot_script: ModuleType) -> Path:
    source = tmp_path / "src"
    package = source / "pkg"
    package.mkdir(parents=True)
    (package / "module.py").write_text(
        't("Hello {name}")\nt("Bye")\n',
        encoding="utf-8",
    )
    return source


def test_update_pot_check_mode_and_idempotency(
    pot_script: ModuleType,
    pot_source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_file = tmp_path / "messages.pot"
    monkeypatch.setattr(pot_script, "OUT_FILE", out_file)
    monkeypatch.setattr(
        pot_script,
        "_SOURCES",
        ((pot_source, [("**.py", "python")], "src"),),
    )

    assert pot_script.main(["--check"]) == 1
    assert not out_file.exists()

    assert pot_script.main([]) == 0
    first = out_file.read_bytes()
    assert b'msgid "Hello {name}"' in first
    assert b"python-brace-format" in first

    assert pot_script.main(["--check"]) == 0
    assert pot_script.main([]) == 0
    assert out_file.read_bytes() == first

    # A Windows checkout has CRLF; regenerating must not rewrite every line.
    out_file.write_bytes(first.replace(b"\n", b"\r\n"))
    assert pot_script.main(["--check"]) == 0
    assert out_file.read_bytes().count(b"\n") == out_file.read_bytes().count(b"\r\n")


def test_update_pot_keeps_context_and_plural(
    pot_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_file = tmp_path / "messages.pot"
    monkeypatch.setattr(pot_script, "OUT_FILE", out_file)

    catalog = pot_script._build_catalog(
        [
            pot_script.ExtractedMessage(
                message="Open",
                location=("src/a.py", 1),
                flags=(),
                context="menu",
            ),
            pot_script.ExtractedMessage(
                message=("One file", "{n} files"),
                location=("src/a.py", 2),
                flags=("python-brace-format",),
                context=None,
            ),
        ]
    )
    rendered = pot_script._render(catalog)

    assert b'msgctxt "menu"' in rendered
    assert b'msgid_plural "{n} files"' in rendered


def test_update_pot_flags_plural_and_detects_conflicts(
    pot_script: ModuleType,
) -> None:
    assert pot_script._flags(("One file", "{n} files")) == ["python-brace-format"]

    entries = [
        pot_script.ExtractedMessage(
            message="Hello",
            location=("src/a.py", 1),
            flags=(),
            context=None,
        ),
        pot_script.ExtractedMessage(
            message=("Hello", "Hellos"),
            location=("src/a.py", 2),
            flags=(),
            context=None,
        ),
    ]
    problems = pot_script._conflicting_messages(entries)
    assert problems and "conflicts" in problems[0]

    same_plural = [
        entries[1],
        pot_script.ExtractedMessage(
            message=("Hello", "Hellos"),
            location=("src/b.py", 3),
            flags=(),
            context=None,
        ),
    ]
    assert pot_script._conflicting_messages(same_plural) == []


def test_update_pot_removes_bom(
    pot_script: ModuleType,
    pot_source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_file = tmp_path / "messages.pot"
    out_file.write_bytes(b"\xef\xbb\xbfstale")
    monkeypatch.setattr(pot_script, "OUT_FILE", out_file)
    monkeypatch.setattr(
        pot_script,
        "_SOURCES",
        ((pot_source, [("**.py", "python")], "src"),),
    )

    assert pot_script.main([]) == 0
    assert not out_file.read_bytes().startswith(b"\xef\xbb\xbf")
