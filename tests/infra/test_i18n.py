import gettext
import types

import pytest

from novel_downloader.infra import i18n


class FakePath:
    """Fake path object to simulate .mo file behavior."""

    def __init__(self, exists: bool):
        self.exists = exists

    def joinpath(self, *_parts):
        return self

    def open(self, mode="rb"):
        if not self.exists:
            raise FileNotFoundError("no mo file")
        return FakeFile()


class FakeFile:
    def read(self, *a, **k):
        # Minimal MO header bytes
        return (
            b"\xde\x12\x04\x95"  # magic
            b"\x00\x00\x00\x00"  # version
            b"\x00\x00\x00\x00"  # nstrings
            b"\x00\x00\x00\x00"  # orig_tab_offset
            b"\x00\x00\x00\x00"  # trans_tab_offset
        )

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeStateManager:
    """In-memory stand-in for the persisted language preference."""

    def __init__(self, lang: str = "zh_CN") -> None:
        self.lang = lang
        self.writes: list[str] = []

    def get_language(self) -> str:
        return self.lang

    def set_language(self, lang: str) -> None:
        self.lang = lang
        self.writes.append(lang)


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    """Drop cached catalogs around every test so monkeypatching is effective."""
    i18n.reload_catalogs()
    yield
    i18n.reload_catalogs()


def _stub_files(monkeypatch: pytest.MonkeyPatch, *, exists: bool) -> None:
    def fake_files(_package):
        obj = types.SimpleNamespace()
        obj.joinpath = lambda *p: FakePath(exists=exists)
        return obj

    monkeypatch.setattr(i18n, "files", fake_files)


def test_get_translation_missing_mo(monkeypatch):
    """If .mo file does not exist -> NullTranslations."""
    _stub_files(monkeypatch, exists=False)

    tr = i18n.get_translation("zh_CN")
    assert isinstance(tr, gettext.NullTranslations)
    assert not isinstance(tr, gettext.GNUTranslations)


def test_get_translation_valid_mo(monkeypatch):
    """If .mo exists -> return GNUTranslations."""
    _stub_files(monkeypatch, exists=True)

    tr = i18n.get_translation("zh_CN")
    assert isinstance(tr, gettext.GNUTranslations)


def test_missing_catalog_warns_only_once(monkeypatch, caplog):
    _stub_files(monkeypatch, exists=False)

    with caplog.at_level("WARNING", logger=i18n.__name__):
        i18n.get_translation("zh_CN")
        i18n.get_translation("zh_CN")

    assert len([r for r in caplog.records if r.levelno == 30]) == 1


def test_t_follows_runtime_language_switch(monkeypatch):
    """Regression: the active language must be resolved on every call."""
    monkeypatch.delenv(i18n.LANG_ENV_VAR, raising=False)
    fake_state = FakeStateManager("zh_CN")
    monkeypatch.setattr(i18n, "state_mgr", fake_state)

    assert i18n.t("Search") == "搜索"

    fake_state.lang = "en_US"
    assert i18n.t("Search") == "Search"


def test_env_var_overrides_persisted_language(monkeypatch):
    monkeypatch.setenv(i18n.LANG_ENV_VAR, "zh")
    monkeypatch.setattr(i18n, "state_mgr", FakeStateManager("en_US"))

    assert i18n.get_language() == "zh_CN"


def test_env_var_with_unknown_value_is_ignored(monkeypatch):
    monkeypatch.setenv(i18n.LANG_ENV_VAR, "tlh")
    monkeypatch.setattr(i18n, "state_mgr", FakeStateManager("en_US"))

    assert i18n.get_language() == "en_US"


def test_persisted_language_falls_back_to_default(monkeypatch):
    monkeypatch.delenv(i18n.LANG_ENV_VAR, raising=False)
    monkeypatch.setattr(i18n, "state_mgr", FakeStateManager("ja_JP"))

    assert i18n.get_language() == i18n.DEFAULT_LANGUAGE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("zh", "zh_CN"),
        ("ZH", "zh_CN"),
        ("zh-cn", "zh_CN"),
        ("zh_CN", "zh_CN"),
        ("zh-Hans", "zh_CN"),
        ("en", "en_US"),
        ("en-gb", "en_US"),
        ("en_US", "en_US"),
        ("  en  ", "en_US"),
        ("", ""),
        (None, ""),
        ("klingon", ""),
    ],
)
def test_normalize_language(raw, expected):
    assert i18n.normalize_language(raw) == expected


def test_unknown_language_translates_to_source_text(monkeypatch):
    monkeypatch.delenv(i18n.LANG_ENV_VAR, raising=False)

    assert i18n.get_translation("klingon").gettext("Search") == "Search"


def test_get_translation_caches_catalogs(monkeypatch):
    calls: list[str] = []

    def fake_load(code: str) -> gettext.NullTranslations:
        calls.append(code)
        return gettext.NullTranslations()

    monkeypatch.setattr(i18n, "_load_catalog", fake_load)

    i18n.get_translation("zh_CN")
    i18n.get_translation("zh_CN")
    i18n.get_translation(i18n.SOURCE_LANGUAGE)
    i18n.get_translation(i18n.SOURCE_LANGUAGE)

    assert calls == ["zh_CN", i18n.SOURCE_LANGUAGE]


def test_set_language_persists_normalized_code(monkeypatch):
    fake_state = FakeStateManager("zh_CN")
    monkeypatch.setattr(i18n, "state_mgr", fake_state)

    assert i18n.set_language("en") == "en_US"
    assert fake_state.writes == ["en_US"]


def test_set_language_skips_redundant_write(monkeypatch):
    fake_state = FakeStateManager("zh_CN")
    monkeypatch.setattr(i18n, "state_mgr", fake_state)

    assert i18n.set_language("zh-cn") == "zh_CN"
    assert fake_state.writes == []


def test_set_language_rejects_unknown_value(monkeypatch):
    fake_state = FakeStateManager("zh_CN")
    monkeypatch.setattr(i18n, "state_mgr", fake_state)

    assert i18n.set_language("klingon") == ""
    assert fake_state.writes == []


def test_available_languages_lists_registry():
    assert i18n.available_languages() == {
        "zh_CN": "简体中文",
        "en_US": "English",
    }
