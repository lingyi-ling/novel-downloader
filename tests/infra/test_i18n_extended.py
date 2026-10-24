"""Extended coverage for :mod:`novel_downloader.infra.i18n`.

These tests intentionally never read the real ``.mo`` catalogs: every catalog
load is either served by a fake loader or made to fail, so the suite gives the
same result whether or not ``scripts/compile_mo.py`` has been run.
"""

from __future__ import annotations

import gettext
import types
from collections.abc import Iterator
from typing import Any

import pytest

from novel_downloader.infra import i18n


class FakeState:
    """In-memory stand-in for the persisted language preference."""

    def __init__(self, lang: str = "zh_CN") -> None:
        self.lang = lang
        self.writes: list[str] = []

    def get_language(self) -> str:
        return self.lang

    def set_language(self, lang: str) -> None:
        self.lang = lang
        self.writes.append(lang)


class MissingPath:
    """``Traversable``-like object whose ``open`` always reports a missing file."""

    def open(self, mode: str = "rb") -> Any:
        raise FileNotFoundError("no .mo file")


@pytest.fixture(autouse=True)
def _reset_catalogs() -> Iterator[None]:
    i18n.reload_catalogs()
    yield
    i18n.reload_catalogs()


def _use_language(monkeypatch: pytest.MonkeyPatch, lang: str = "zh_CN") -> FakeState:
    """Pin the active language to ``lang`` through a fake state manager."""
    monkeypatch.delenv(i18n.LANG_ENV_VAR, raising=False)
    state = FakeState(lang)
    monkeypatch.setattr(i18n, "state_mgr", state)
    return state


def _fake_translations(mapping: dict[str, str]) -> gettext.GNUTranslations:
    """Build a real ``GNUTranslations`` with an in-memory catalog.

    No ``.mo`` file is involved; ``plural`` is set because
    ``gettext.GNUTranslations`` only defines it while parsing a catalog.
    """
    translation = gettext.GNUTranslations()
    translation._catalog = dict(mapping)  # type: ignore[attr-defined]
    translation.plural = lambda n: n  # type: ignore[method-assign]
    return translation


def _install_loader(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, str] | None = None,
) -> list[str]:
    """Replace ``_load_catalog`` with a recorder and return the call log."""
    calls: list[str] = []

    def fake_load(code: str) -> gettext.NullTranslations:
        calls.append(code)
        if mapping is not None and code != i18n.SOURCE_LANGUAGE:
            return _fake_translations(mapping)
        return gettext.NullTranslations()

    monkeypatch.setattr(i18n, "_load_catalog", fake_load)
    return calls


def _missing_files(_package: str) -> Any:
    root = types.SimpleNamespace(joinpath=lambda *_parts: MissingPath())
    return root


def _missing_package(_package: str) -> Any:
    raise ModuleNotFoundError("No module named 'novel_downloader.locales'")


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    warnings: list[tuple[Any, ...]] = []

    def fake_warning(*args: Any, **kwargs: Any) -> None:
        warnings.append(args)

    monkeypatch.setattr(i18n.logger, "warning", fake_warning)
    return warnings


# ---------------------------------------------------------------------------
# normalize_language
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("alias", "expected"), sorted(i18n.LANGUAGE_ALIASES.items()))
def test_normalize_language_accepts_every_alias(alias: str, expected: str) -> None:
    assert expected in i18n.SUPPORTED_LANGUAGES
    assert i18n.normalize_language(alias) == expected
    assert i18n.normalize_language(alias.upper()) == expected
    assert i18n.normalize_language(f"  {alias}  ") == expected


@pytest.mark.parametrize("code", sorted(i18n.SUPPORTED_LANGUAGES))
def test_normalize_language_accepts_registered_codes(code: str) -> None:
    variants = {code, code.lower(), code.upper(), code.replace("_", "-")}
    for variant in variants:
        assert i18n.normalize_language(variant) == code


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "klingon", "zh_TW", "zh-Hans-CN", "en_AU", 0, 123],
)
def test_normalize_language_rejects_unsupported_values(value: Any) -> None:
    assert i18n.normalize_language(value) == ""


def test_aliases_only_target_registered_languages() -> None:
    assert set(i18n.LANGUAGE_ALIASES.values()) <= set(i18n.SUPPORTED_LANGUAGES)


def test_available_languages_returns_a_copy() -> None:
    languages = i18n.available_languages()
    languages["xx_XX"] = "Test"

    assert "xx_XX" not in i18n.SUPPORTED_LANGUAGES
    assert i18n.available_languages() == dict(i18n.SUPPORTED_LANGUAGES)


# ---------------------------------------------------------------------------
# get_language / set_language
# ---------------------------------------------------------------------------
def test_get_language_prefers_env_over_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(i18n.LANG_ENV_VAR, "en")
    monkeypatch.setattr(i18n, "state_mgr", FakeState("zh_CN"))

    assert i18n.get_language() == "en_US"


@pytest.mark.parametrize("env_value", ["", "   ", "klingon"])
def test_get_language_ignores_unusable_env_value(
    monkeypatch: pytest.MonkeyPatch, env_value: str
) -> None:
    monkeypatch.setenv(i18n.LANG_ENV_VAR, env_value)
    monkeypatch.setattr(i18n, "state_mgr", FakeState("zh_CN"))

    assert i18n.get_language() == "zh_CN"


@pytest.mark.parametrize("saved", ["", "ja_JP"])
def test_get_language_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, saved: str
) -> None:
    monkeypatch.delenv(i18n.LANG_ENV_VAR, raising=False)
    monkeypatch.setattr(i18n, "state_mgr", FakeState(saved))

    assert i18n.get_language() == i18n.DEFAULT_LANGUAGE


@pytest.mark.parametrize(
    ("value", "expected"),
    [("en", "en_US"), ("EN-us", "en_US"), ("POSIX", "en_US"), ("zh-Hans", "zh_CN")],
)
def test_set_language_persists_normalized_code(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    state = FakeState("zh_CN")
    monkeypatch.setattr(i18n, "state_mgr", state)

    assert i18n.set_language(value) == expected
    expected_writes = [] if expected == "zh_CN" else [expected]
    assert state.writes == expected_writes
    assert state.lang == expected


@pytest.mark.parametrize("value", ["klingon", "", "   ", None, "zh_TW"])
def test_set_language_rejects_illegal_value_without_persisting(
    monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    state = FakeState("zh_CN")
    monkeypatch.setattr(i18n, "state_mgr", state)

    assert i18n.set_language(value) == ""
    assert state.writes == []
    assert state.lang == "zh_CN"


# ---------------------------------------------------------------------------
# get_translation fallbacks and caching
# ---------------------------------------------------------------------------
def test_missing_mo_falls_back_to_msgid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(i18n, "files", _missing_files)
    _capture_warnings(monkeypatch)

    translation = i18n.get_translation("zh_CN")

    assert isinstance(translation, gettext.NullTranslations)
    assert translation.gettext("Search") == "Search"


def test_missing_locales_package_falls_back_to_msgid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packaged builds may omit ``locales``; that must not crash ``t()``."""
    monkeypatch.setattr(i18n, "files", _missing_package)
    _capture_warnings(monkeypatch)

    assert i18n.get_translation("zh_CN").gettext("Search") == "Search"


def test_source_language_never_touches_the_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(_package: str) -> Any:
        raise AssertionError("en_US must not load a catalog")

    monkeypatch.setattr(i18n, "files", explode)

    translation = i18n.get_translation(i18n.SOURCE_LANGUAGE)

    assert isinstance(translation, gettext.NullTranslations)


def test_unknown_language_uses_source_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_loader(monkeypatch)

    translation = i18n.get_translation("klingon")

    assert calls == [i18n.SOURCE_LANGUAGE]
    assert translation.gettext("Search") == "Search"


def test_aliases_are_normalized_before_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_loader(monkeypatch)

    first = i18n.get_translation("zh-cn")
    second = i18n.get_translation("zh_CN")

    assert calls == ["zh_CN"]
    assert first is second


def test_empty_language_argument_means_active_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_language(monkeypatch, "zh_CN")
    calls = _install_loader(monkeypatch)

    i18n.get_translation("")

    assert calls == ["zh_CN"]


def test_missing_catalog_warns_once_until_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(i18n, "files", _missing_files)
    warnings = _capture_warnings(monkeypatch)

    i18n.get_translation("zh_CN")
    i18n.get_translation("zh_CN")
    assert len(warnings) == 1

    i18n.reload_catalogs()
    i18n.get_translation("zh_CN")
    assert len(warnings) == 2


def test_reload_catalogs_drops_cached_translations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_loader(monkeypatch)

    assert i18n.get_translation("zh_CN") is i18n.get_translation("zh_CN")
    assert calls == ["zh_CN"]

    i18n.reload_catalogs()
    i18n.get_translation("zh_CN")

    assert calls == ["zh_CN", "zh_CN"]


# ---------------------------------------------------------------------------
# t()
# ---------------------------------------------------------------------------
def test_t_follows_runtime_language_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _use_language(monkeypatch, "zh_CN")
    mapping = {"Hello": "你好"}
    _install_loader(monkeypatch, mapping)

    assert i18n.t("Hello") == "你好"

    state.lang = "en_US"
    assert i18n.t("Hello") == "Hello"


def test_t_passes_percent_placeholders_through_untranslated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(i18n.LANG_ENV_VAR, i18n.SOURCE_LANGUAGE)
    msgid = "Run '%(prog)s %(command)s' for help."

    assert i18n.t(msgid) == msgid


def test_t_preserves_placeholders_in_translated_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(i18n.LANG_ENV_VAR, "zh_CN")
    msgid = "Hello %(name)s, you have {count} items"
    translated = "你好 %(name)s，你有 {count} 项"
    _install_loader(monkeypatch, {msgid: translated})

    assert i18n.t(msgid) == translated
    # gettext does not consume placeholders; the caller still applies them.
    assert i18n.t(msgid) % {"name": "Ada"} == "你好 Ada，你有 {count} 项"
    assert i18n.t(msgid).format(count=3) == "你好 %(name)s，你有 3 项"
