#!/usr/bin/env python3
"""
tests.apps.test_desktop_preferences
-----------------------------------

Behavioural tests for how the desktop client persists and switches its two
user-facing preferences.

The two preferences deliberately live in different files:

* **theme** is a desktop-only preference in ``desktop_prefs.json`` inside
  :func:`novel_downloader.apps.music.paths.music_data_dir` (``NOVEL_MUSIC_HOME``
  overrides it), and
* **language** is shared with the CLI in ``state.json`` under
  :func:`novel_downloader.infra.paths.STATE_PATH` (``NOVEL_HOME`` overrides the
  directory), resolved by :mod:`novel_downloader.infra.i18n` as
  ``NOVEL_LANG`` > persisted ``lang`` > ``zh_CN``.

Nothing here touches the network or opens a window; the JS contract is checked
against the embedded page source.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

import novel_downloader.infra.config as config_mod
from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.infra.persistence.state import StateManager

ROOT = Path(__file__).resolve().parents[2]

#: ``data-i18n`` keys the Appearance card depends on.  Each one must reach the
#: page through ``_i18n_catalog()`` for a switch to relabel anything.
_APPEARANCE_KEYS = ("Theme", "Language", "Appearance", "Light", "Dark")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """
    Point both preference stores at ``tmp_path`` and cut the config lookup.

    :return: ``(prefs_path, state_path)``.
    """
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.infra import i18n

    home = tmp_path / "profile"
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(home))
    # The suite pins NOVEL_LANG=en_US globally; these tests exercise the
    # persisted value instead, so the override is removed for the duration.
    monkeypatch.delenv(i18n.LANG_ENV_VAR, raising=False)
    # No dependency on the repository's settings.toml or the user's profile.
    monkeypatch.setattr(
        config_mod, "get_config_value", lambda _keys, default: default, raising=True
    )
    monkeypatch.setattr(wa, "_settings_config_path", lambda: tmp_path / "settings.toml")

    prefs = home / wa._PREFS_FILENAME
    state_path = tmp_path / "home" / "state.json"
    monkeypatch.setattr(i18n, "state_mgr", StateManager(state_path))
    return prefs, state_path


def _restart(monkeypatch: pytest.MonkeyPatch, state_path: Path) -> tuple[Any, Any]:
    """
    Simulate a new process: fresh bridge, fresh state manager, same files.

    :return: ``(bridge, state_manager)``.
    """
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.infra import i18n

    manager = StateManager(state_path)
    monkeypatch.setattr(i18n, "state_mgr", manager)
    return wa.ApiBridge(), manager


def _write_prefs(prefs: Path, payload: object) -> None:
    """Write a raw preference payload (str/bytes are written verbatim)."""
    prefs.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        prefs.write_bytes(payload)
    elif isinstance(payload, str):
        prefs.write_text(payload, encoding="utf-8")
    else:
        prefs.write_text(json.dumps(payload), encoding="utf-8")


def _read_prefs(prefs: Path) -> dict[str, Any]:
    loaded = json.loads(prefs.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _js_block(marker: str) -> str:
    """Return the JS statement block starting at ``marker``."""
    start = APP_HTML.index(marker)
    end = APP_HTML.index("\n\n", start)
    return APP_HTML[start:end]


def _js_handler(marker: str) -> str:
    """Return one event handler: from ``marker`` to the next handler."""
    start = APP_HTML.index(marker)
    end = APP_HTML.find("\n\n$(", start)
    if end == -1:
        end = APP_HTML.index("\n\n</script>", start)
    return APP_HTML[start:end]


def _settings_document(**overrides: Any) -> dict[str, Any]:
    """A valid export document with optional per-test overrides."""
    settings: dict[str, Any] = {
        "theme": "dark",
        "auto_check_updates": False,
        "auto_download_updates": False,
        "auto_install_on_exit": False,
        "search_filters": {},
        "search_sort": {"sort": "quality", "order": "desc"},
    }
    settings.update(overrides)
    return {
        "app": "novel-downloader",
        "kind": "desktop-preferences",
        "format_version": 1,
        "app_version": "1.36.0",
        "exported_at": "2026-11-16T00:00:00Z",
        "settings": settings,
    }


# ---------------------------------------------------------------------------
# theme: persistence, restart and fallbacks
# ---------------------------------------------------------------------------


def test_theme_defaults_to_light_without_a_preference_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A first launch reports light and marks the value as not stored."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)

    state = wa.ApiBridge().get_initial_state()

    assert state["theme"] == "light"
    assert state["theme_stored"] is False
    assert not prefs.exists()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_round_trips_across_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, theme: str
) -> None:
    """Both accepted themes survive a save and a fresh launch."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, state_path = _isolate(monkeypatch, tmp_path)
    assert wa.ApiBridge().save_prefs({"theme": theme})["ok"] is True
    assert _read_prefs(prefs) == {"theme": theme}

    bridge, _manager = _restart(monkeypatch, state_path)
    restored = bridge.get_initial_state()

    assert restored["theme"] == theme
    assert restored["theme_stored"] is True


def test_theme_save_is_case_and_whitespace_insensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The select's value may arrive padded; the stored value is canonical."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)

    assert wa.ApiBridge().save_prefs({"theme": "  DARK "})["ok"] is True
    assert _read_prefs(prefs) == {"theme": "dark"}


def test_theme_rejects_an_unknown_value_without_touching_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rejected theme must not be persisted, nor overwrite a valid one."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": "dark"})

    result = wa.ApiBridge().save_prefs({"theme": "purple"})

    assert result["ok"] is False
    assert result["errors"] == {"theme": wa.t("Unknown theme")}
    assert _read_prefs(prefs) == {"theme": "dark"}


def test_system_theme_is_not_a_supported_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pin the current contract: only light/dark exist, "system" is rejected.

    The desktop page has no ``prefers-color-scheme`` handling and the settings
    select ships two options, so "system" is an unknown value like any other.
    This test is the place to update if that ever becomes a real feature.
    """
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)

    result = wa.ApiBridge().save_prefs({"theme": "system"})

    assert result["ok"] is False
    assert result["errors"] == {"theme": wa.t("Unknown theme")}
    assert not prefs.exists()


@pytest.mark.parametrize(
    "stored",
    [
        "system",
        "PURPLE",
        "",
        "  dark  ",
        123,
        True,
        None,
        ["dark"],
        {"mode": "dark"},
    ],
)
def test_stored_theme_junk_falls_back_to_light(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: object
) -> None:
    """A hand-edited value is normalised away, not trusted or fatal."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": stored})

    state = wa.ApiBridge().get_initial_state()

    assert state["theme"] == "light"
    # The page then keeps its per-window localStorage choice instead of
    # overwriting it with the default.
    assert state["theme_stored"] is False


def test_unstored_theme_defers_to_localstorage_in_the_page() -> None:
    """The boot script only applies the file value when it was really stored."""
    boot = _js_block("function boot() {")
    assert (
        'applyTheme(state.theme_stored ? state.theme : store.get("theme", "light"));'
        in boot
    )


# ---------------------------------------------------------------------------
# language: persistence, restart and fallbacks
# ---------------------------------------------------------------------------


def test_language_defaults_to_zh_cn_without_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, state_path = _isolate(monkeypatch, tmp_path)

    state = wa.ApiBridge().get_initial_state()

    assert state["language"] == "zh_CN"
    assert sorted(state["languages"]) == ["en_US", "zh_CN"]
    assert not state_path.exists()


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("en_US", "en_US"), ("en", "en_US"), ("zh_CN", "zh_CN"), ("zh-cn", "zh_CN")],
)
def test_language_save_persists_and_survives_a_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requested: str,
    expected: str,
) -> None:
    """Aliases are normalised before they reach ``state.json``."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    if expected == "zh_CN":
        # zh_CN is the default, and an unchanged language is deliberately not
        # rewritten; start from the other language so a write really happens.
        assert wa.ApiBridge().save_settings({"language": "en"})["ok"] is True

    result = wa.ApiBridge().save_settings({"language": requested})

    assert result["ok"] is True, result["errors"]
    assert result["saved"] == ["language"]
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"lang": expected}
    assert result["settings"]["language"] == expected

    bridge, _manager = _restart(monkeypatch, state_path)
    assert bridge.get_initial_state()["language"] == expected


def test_language_rejects_an_unknown_value_and_keeps_the_stored_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    assert wa.ApiBridge().save_settings({"language": "en"})["ok"] is True
    before = state_path.read_bytes()

    result = wa.ApiBridge().save_settings({"language": "klingon"})

    assert result["ok"] is False
    assert result["errors"] == {"language": wa.t("Unknown language")}
    assert result["saved"] == []
    assert state_path.read_bytes() == before
    assert result["settings"]["language"] == "en_US"


def test_blank_language_means_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty select (catalogue failed to load) must not fail the save."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, state_path = _isolate(monkeypatch, tmp_path)

    result = wa.ApiBridge().save_settings({"language": ""})

    assert result["ok"] is True, result["errors"]
    assert result["saved"] == []
    assert not state_path.exists()


@pytest.mark.parametrize("stored", ["klingon", "", 123, None, ["en_US"]])
def test_stored_language_junk_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: object
) -> None:
    from novel_downloader.infra import i18n

    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"lang": stored}), encoding="utf-8")

    bridge, _manager = _restart(monkeypatch, state_path)

    assert i18n.get_language() == i18n.DEFAULT_LANGUAGE == "zh_CN"
    assert bridge.get_initial_state()["language"] == "zh_CN"
    assert bridge.get_settings()["language"] == "zh_CN"


def test_environment_language_outranks_the_persisted_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``NOVEL_LANG`` is the highest-priority source, per i18n's contract."""
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.infra import i18n

    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"lang": "en_US"}), encoding="utf-8")
    monkeypatch.setenv(i18n.LANG_ENV_VAR, "zh")

    assert wa.ApiBridge().get_initial_state()["language"] == "zh_CN"
    assert wa.ApiBridge().get_settings()["language"] == "zh_CN"


def test_corrupt_state_file_does_not_break_the_boot_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken ``state.json`` is quarantined; the window still starts."""
    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json", encoding="utf-8")

    bridge, _manager = _restart(monkeypatch, state_path)
    state = bridge.get_initial_state()

    assert state["language"] == "zh_CN"
    assert state["theme"] == "light"
    assert (
        state_path.with_name(state_path.name + ".corrupt").read_text(encoding="utf-8")
        == "{not json"
    )


# ---------------------------------------------------------------------------
# theme and language are independent
# ---------------------------------------------------------------------------


def test_saving_the_theme_leaves_the_language_file_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    assert wa.ApiBridge().save_settings({"language": "en"})["ok"] is True
    before = state_path.read_bytes()

    assert wa.ApiBridge().save_prefs({"theme": "dark"})["ok"] is True

    assert state_path.read_bytes() == before
    assert wa.ApiBridge().get_initial_state()["language"] == "en_US"


def test_switching_the_language_leaves_the_theme_file_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    assert wa.ApiBridge().save_prefs({"theme": "dark"})["ok"] is True
    before = prefs.read_bytes()

    assert wa.ApiBridge().save_settings({"language": "zh"})["ok"] is True

    assert prefs.read_bytes() == before
    assert wa.ApiBridge().get_initial_state()["theme"] == "dark"


def test_a_language_key_in_the_prefs_file_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Language has exactly one source of truth; prefs copies do nothing."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": "dark", "language": "en_US"})

    state = wa.ApiBridge().get_initial_state()

    assert state["theme"] == "dark"
    assert state["language"] == "zh_CN"


def test_a_theme_key_in_the_state_file_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"lang": "en_US", "theme": "dark"}), "utf-8")

    bridge, _manager = _restart(monkeypatch, state_path)
    state = bridge.get_initial_state()

    assert state["theme"] == "light"
    assert state["theme_stored"] is False
    assert state["language"] == "en_US"


def test_both_preferences_are_restored_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The combined restart path: one save each, one fresh launch."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    bridge = wa.ApiBridge()
    assert bridge.save_prefs({"theme": "dark"})["ok"] is True
    assert bridge.save_settings({"language": "en"})["ok"] is True

    restarted, _manager = _restart(monkeypatch, state_path)
    state = restarted.get_initial_state()

    assert state["theme"] == "dark"
    assert state["theme_stored"] is True
    assert state["language"] == "en_US"


# ---------------------------------------------------------------------------
# language switch: catalogue and DOM contract
# ---------------------------------------------------------------------------


def test_language_switch_publishes_a_translated_catalogue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reply carries the new strings so the page can re-render at once."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, _state = _isolate(monkeypatch, tmp_path)

    english = wa.ApiBridge().save_settings({"language": "en"})
    assert english["ok"] is True, english["errors"]
    assert english["i18n"], "a language change must return a catalogue"
    assert english["i18n"]["Theme"] == "Theme"
    assert english["i18n"]["Language"] == "Language"

    chinese = wa.ApiBridge().save_settings({"language": "zh"})
    assert chinese["ok"] is True, chinese["errors"]
    assert chinese["i18n"]["Theme"] == "主题"
    assert chinese["i18n"]["Language"] == "语言"
    assert chinese["i18n"]["Appearance"] == "外观"


def test_initial_state_ships_the_catalogue_for_the_active_language(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boot uses the same translated strings as a later switch."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, _state = _isolate(monkeypatch, tmp_path)

    state = wa.ApiBridge().get_initial_state()

    assert state["language"] == "zh_CN"
    assert state["i18n"]["Theme"] == "主题"
    assert state["i18n"]["Language"] == "语言"


def test_the_page_rerenders_text_titles_and_descriptions() -> None:
    """Static contract: the labels exist and ``applyI18n`` rewrites them."""
    for key in _APPEARANCE_KEYS:
        assert f'data-i18n="{key}"' in APP_HTML, key
    # The icon-only theme button is labelled through title/aria attributes.
    assert 'data-i18n-title="Theme"' in APP_HTML
    assert 'data-i18n-aria="Theme"' in APP_HTML

    apply_body = _js_block("function applyI18n(root) {")
    assert "el.textContent = t(el.dataset.i18n);" in apply_body
    assert "el.title = text;" in apply_body
    assert 'el.setAttribute("aria-label", text);' in apply_body
    assert 'el.setAttribute("aria-label", t(el.dataset.i18nAria));' in apply_body


def test_boot_applies_the_catalogue_before_the_first_paint() -> None:
    boot = _js_block("function boot() {")
    assert "Object.assign(I18N, state.i18n || {});" in boot
    assert boot.index("Object.assign(I18N") < boot.index("applyI18n();")


def test_the_settings_save_handler_reapplies_a_new_catalogue() -> None:
    """Saving a language must repaint the page without a reload."""
    handler = _js_handler('$("#btn-set-save").onclick')

    assert 'call("save_settings", values)' in handler
    assert "if (r.i18n) {" in handler
    assert "Object.assign(I18N, r.i18n);" in handler
    assert "applyI18n();" in handler
    assert 'language: $("#set-language").value,' in handler


def test_the_theme_control_offers_only_light_and_dark() -> None:
    """Documents the missing "system" option rather than leaving it implicit."""
    start = APP_HTML.index('id="set-theme"')
    select = APP_HTML[start : APP_HTML.index("</select>", start)]

    assert '<option value="light"' in select
    assert '<option value="dark"' in select
    assert '<option value="system"' not in select
    assert "prefers-color-scheme" not in APP_HTML


# ---------------------------------------------------------------------------
# export / import: the whitelist never carries the language
# ---------------------------------------------------------------------------


def test_export_whitelist_excludes_the_language(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": "dark", "last_update_check": "1700000000"})
    target = tmp_path / "export.json"

    result = wa.ApiBridge().export_settings(str(target))

    assert result["ok"] is True, result["error"]
    document = json.loads(target.read_text(encoding="utf-8"))
    assert set(document["settings"]) == set(wa._SETTINGS_FILE_KEYS)
    assert "language" not in document["settings"]
    assert "lang" not in document
    # Throttle state and device-specific paths stay local too.
    assert "last_update_check" not in document["settings"]


def test_import_rejects_a_smuggled_language_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whitelist is a hard error, so an import cannot change language."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, state_path = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": "light"})
    assert wa.ApiBridge().save_settings({"language": "en"})["ok"] is True
    state_before = state_path.read_bytes()

    document = _settings_document()
    document["settings"]["language"] = "zh_CN"
    source = tmp_path / "import.json"
    source.write_text(json.dumps(document), encoding="utf-8")

    result = wa.ApiBridge().import_settings(str(source))

    assert result["ok"] is False
    assert "language" in str(result["error"])
    assert state_path.read_bytes() == state_before
    assert _read_prefs(prefs) == {"theme": "light"}
    assert wa.ApiBridge().get_initial_state()["language"] == "en_US"


def test_import_applies_other_preferences_without_touching_the_language(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, state_path = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": "light", "last_update_check": "1700000000"})
    assert wa.ApiBridge().save_settings({"language": "en"})["ok"] is True
    state_before = state_path.read_bytes()
    source = tmp_path / "import.json"
    source.write_text(json.dumps(_settings_document(theme="dark")), encoding="utf-8")

    result = wa.ApiBridge().import_settings(str(source))

    assert result["ok"] is True, result["error"]
    assert result["settings"]["theme"] == "dark"
    assert result["settings"]["language"] == "en_US"
    assert state_path.read_bytes() == state_before
    # The import merges into the stored map: unrelated keys survive.
    assert _read_prefs(prefs)["last_update_check"] == "1700000000"


# ---------------------------------------------------------------------------
# damaged / partial preference files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[1, 2, 3]",
        '"just a string"',
        "42",
        "null",
        b"\xff\xfe\x00not utf-8",
        "",
    ],
)
def test_a_damaged_prefs_file_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: object
) -> None:
    """Boot must survive anything that can be sitting at that path."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, payload)

    state = wa.ApiBridge().get_initial_state()
    settings = wa.ApiBridge().get_settings()

    for payload_state in (state, settings):
        assert payload_state["theme"] == "light"
        assert payload_state["search_filters"] == {}
    # ``get_initial_state`` also publishes the sort pair; ``get_settings`` does
    # not render it, so only the boot payload can be checked for it.
    assert state["search_sort"] == {"sort": "quality", "order": "desc"}
    assert state["theme_stored"] is False
    assert state["language"] == "zh_CN"
    for key in (
        "auto_check_updates",
        "auto_download_updates",
        "auto_install_on_exit",
    ):
        assert state[key] is True
        assert settings[key] is True


def test_a_damaged_prefs_file_is_preserved_before_being_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, "{not json")

    assert wa.ApiBridge().save_prefs({"theme": "dark"})["ok"] is True

    assert _read_prefs(prefs) == {"theme": "dark"}
    broken = prefs.with_name(prefs.name + ".broken")
    assert broken.read_text(encoding="utf-8") == "{not json"


def test_partial_and_unknown_keys_are_filled_and_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing keys take their defaults; unknown keys survive a save."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    _write_prefs(
        prefs,
        {"theme": "dark", "from_a_future_build": {"nested": [1, 2]}},
    )

    state = wa.ApiBridge().get_initial_state()
    assert state["theme"] == "dark"
    assert state["auto_check_updates"] is True
    assert state["search_filters"] == {}

    assert wa.ApiBridge().save_prefs({"theme": "light"})["ok"] is True
    stored = _read_prefs(prefs)
    assert stored["theme"] == "light"
    assert stored["from_a_future_build"] == {"nested": [1, 2]}


def test_an_unresolvable_prefs_path_is_a_save_failure_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unusable profile directory must answer ``ok: false``.

    ``music_data_dir()`` creates its directory on demand, so a file sitting
    where the folder belongs (or an unresolvable ``~``) raises while the path
    is resolved.  That used to escape ``_save_prefs`` and take the bridge with
    it.
    """
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, _state = _isolate(monkeypatch, tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(blocker / "music"))

    assert wa._load_prefs() == {}
    assert wa._save_prefs({"theme": "dark"}) is False
    assert wa._remove_prefs_keys("theme") is False
    result = wa.ApiBridge().save_prefs({"theme": "dark"})
    assert result["ok"] is False
    assert result["error"] == wa.t("Could not save the settings")


def test_an_unresolvable_home_is_absorbed_by_the_preference_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``Path.expanduser`` raises ``RuntimeError`` when HOME is unresolvable."""
    import novel_downloader.apps.desktop.webview_app as wa

    def boom() -> Path:
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(wa, "_prefs_path", boom)

    assert wa._load_prefs() == {}
    assert wa._save_prefs({"theme": "dark"}) is False


# ---------------------------------------------------------------------------
# concurrency and atomic writes
# ---------------------------------------------------------------------------


def test_concurrent_thread_saves_keep_the_file_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every writer's key survives and the file is never half-written."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    failures: list[int] = []

    def writer(index: int) -> None:
        theme = "dark" if index % 2 else "light"
        if wa._save_prefs({f"k{index}": index, "theme": theme}) is False:
            failures.append(index)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    stored = _read_prefs(prefs)
    assert {f"k{i}": i for i in range(10)} == {
        key: stored[key] for key in stored if key.startswith("k")
    }
    assert stored["theme"] in ("light", "dark")
    assert list(prefs.parent.glob("*.tmp")) == []


def _mp_writer(
    prefs_path: str, keys: list[str], theme: str, start: Any, errors: Any
) -> None:
    """Worker for the cross-process test (module level: ``spawn`` pickles it)."""
    import novel_downloader.apps.desktop.webview_app as wa

    target = Path(prefs_path)
    wa._prefs_path = lambda: target  # type: ignore[assignment]
    try:
        if not start.wait(60):
            errors.put("start event timed out")
            return
        for key in keys:
            if not wa._save_prefs({key: True}):
                errors.put(f"save failed for {key}")
                return
        if not wa._save_prefs({"theme": theme}):
            errors.put("save failed for theme")
    except Exception as exc:  # noqa: BLE001 - reported through the queue
        errors.put(f"{type(exc).__name__}: {exc}")


def test_two_processes_leave_one_complete_prefs_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two instances writing at once must not truncate or interleave bytes."""
    prefs, _state = _isolate(monkeypatch, tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    errors = context.Queue()
    keys_a = [f"proc_a_{index}" for index in range(6)]
    keys_b = [f"proc_b_{index}" for index in range(6)]
    procs = [
        context.Process(
            target=_mp_writer,
            args=(str(prefs), keys_a, "dark", start, errors),
        ),
        context.Process(
            target=_mp_writer,
            args=(str(prefs), keys_b, "light", start, errors),
        ),
    ]

    try:
        for proc in procs:
            proc.start()
        start.set()
        for proc in procs:
            proc.join(120)
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)

    assert all(proc.exitcode == 0 for proc in procs), [p.exitcode for p in procs]
    messages: list[str] = []
    while not errors.empty():
        messages.append(str(errors.get_nowait()))
    assert messages == []

    stored = _read_prefs(prefs)
    assert all(stored.get(key) is True for key in keys_a + keys_b)
    assert stored["theme"] in ("light", "dark")
    assert list(prefs.parent.glob("*.tmp")) == []


def test_a_failed_atomic_write_keeps_the_previous_theme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old value stays visible and no temp file is left behind."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _state = _isolate(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": "dark"})
    before = prefs.read_bytes()

    def boom(_src: Any, _dst: Any) -> None:
        raise OSError("disk is full")

    with monkeypatch.context() as failing:
        failing.setattr(wa.os, "replace", boom)

        assert wa._save_prefs({"theme": "light"}) is False
        assert prefs.read_bytes() == before
        assert list(prefs.parent.glob("*.tmp")) == []

    # Once the replace works again the untouched old value is what boots.
    assert wa.ApiBridge().get_initial_state()["theme"] == "dark"


def test_a_failed_language_write_is_not_cached_as_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed ``state.json`` write must not poison the in-memory language.

    The state manager keeps a decoded copy.  If ``set_language`` updated it
    before the write failed, ``get_language`` would report a language that is
    not on disk, the reply would show it, and a retry would be skipped as
    "already saved".
    """
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.persistence.state as state_mod
    from novel_downloader.infra import i18n

    _prefs, state_path = _isolate(monkeypatch, tmp_path)
    manager = i18n._resolve_state_mgr()
    manager.set_language("zh_CN")
    real_replace = state_mod.os.replace

    def fail(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if str(dst).endswith("state.json"):
            raise OSError("disk full")
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(state_mod.os, "replace", fail)

    result = wa.ApiBridge().save_settings({"theme": "dark", "language": "en"})

    assert result["ok"] is False
    assert "language" in result["errors"]
    # The theme still landed; only the language failed.
    assert result["saved"] == ["theme"]
    assert result["settings"]["language"] == "zh_CN"
    assert i18n.get_language() == "zh_CN"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"lang": "zh_CN"}

    # Once the disk recovers, the same request succeeds instead of being
    # skipped as a redundant write.
    monkeypatch.setattr(state_mod.os, "replace", real_replace)
    retry = wa.ApiBridge().save_settings({"language": "en"})
    assert retry["ok"] is True, retry["errors"]
    assert i18n.get_language() == "en_US"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"lang": "en_US"}


# ---------------------------------------------------------------------------
# path overrides
# ---------------------------------------------------------------------------


def test_the_prefs_file_follows_novel_music_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.apps.music.paths import music_data_dir

    home = tmp_path / "custom-music-home"
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(home))

    assert wa._prefs_path() == home / wa._PREFS_FILENAME
    assert wa._prefs_path().parent == music_data_dir()
    assert wa._save_prefs({"theme": "dark"}) is True
    assert (home / wa._PREFS_FILENAME).is_file()


def test_both_overrides_are_honoured_in_a_fresh_process(tmp_path: Path) -> None:
    """``NOVEL_HOME``/``NOVEL_MUSIC_HOME`` must work from a cold start.

    ``STATE_PATH`` is computed when ``persistence.state`` is imported, so this
    runs in a subprocess where the environment is set before that import.
    """
    script = textwrap.dedent(
        """
        import json
        import novel_downloader.apps.desktop.webview_app as wa
        from novel_downloader.infra.i18n import set_language
        from novel_downloader.infra.paths import STATE_PATH

        prefs = wa._prefs_path()
        assert wa._save_prefs({"theme": "dark"}) is True
        assert set_language("en") == "en_US"
        print(json.dumps({
            "prefs": str(prefs),
            "state": str(STATE_PATH),
            "theme": wa.ApiBridge().get_initial_state()["theme"],
            "language": wa.ApiBridge().get_initial_state()["language"],
            "prefs_exists": prefs.is_file(),
            "state_exists": STATE_PATH.is_file(),
            "state_lang": json.loads(STATE_PATH.read_text(encoding="utf-8"))["lang"],
        }))
        """
    )
    env = os.environ.copy()
    env["NOVEL_HOME"] = str(tmp_path / "app-home")
    env["NOVEL_MUSIC_HOME"] = str(tmp_path / "music-home")
    env.pop("NOVEL_LANG", None)
    env["PYTHONPATH"] = str(ROOT / "src")

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["prefs"] == str(tmp_path / "music-home" / "desktop_prefs.json")
    assert payload["state"] == str(tmp_path / "app-home" / "state.json")
    assert payload["prefs_exists"] is True
    assert payload["state_exists"] is True
    assert payload["theme"] == "dark"
    assert payload["language"] == "en_US"
    assert payload["state_lang"] == "en_US"


# ---------------------------------------------------------------------------
# the stored-key inventory stays explicit
# ---------------------------------------------------------------------------


def test_the_prefs_writer_only_accepts_documented_keys() -> None:
    """Pin the write surface: theme plus the three update switches."""
    import novel_downloader.apps.desktop.webview_app as wa

    assert wa._PREF_BOOL_KEYS == (
        "auto_check_updates",
        "auto_download_updates",
        "auto_install_on_exit",
    )
    assert set(wa._SETTINGS_FILE_KEYS) == {
        "theme",
        *wa._PREF_BOOL_KEYS,
        "search_filters",
        "search_sort",
    }
    assert "language" not in wa._SETTINGS_FILE_KEYS
    # The theme is a desktop preference; the language is not.
    assert wa._PREFS_FILENAME == "desktop_prefs.json"


def test_no_theme_or_language_key_is_written_outside_its_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One save of everything must not duplicate either preference."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, state_path = _isolate(monkeypatch, tmp_path)

    result = wa.ApiBridge().save_settings(
        {
            "theme": "dark",
            "language": "en",
            "auto_check_updates": False,
            "auto_download_updates": False,
            "auto_install_on_exit": False,
        }
    )

    assert result["ok"] is True, result["errors"]
    stored = _read_prefs(prefs)
    assert stored == {
        "theme": "dark",
        "auto_check_updates": False,
        "auto_download_updates": False,
        "auto_install_on_exit": False,
    }
    assert "language" not in stored and "lang" not in stored
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"lang": "en_US"}


def test_the_theme_labels_are_extractable_translation_units() -> None:
    """Every label the Appearance card shows is a literal ``t()`` call."""
    source = (
        ROOT / "src" / "novel_downloader" / "apps" / "desktop" / "webview_app.py"
    ).read_text(encoding="utf-8")
    for key in _APPEARANCE_KEYS:
        assert re.search(rf'"\s*{key}"\s*:\s*t\(\s*"{key}"\s*\)', source), key
