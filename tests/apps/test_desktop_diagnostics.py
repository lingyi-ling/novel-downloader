#!/usr/bin/env python3
"""
tests.apps.test_desktop_diagnostics
-----------------------------------

Behavioural tests for the desktop "Copy diagnostics" support report.

The report is a plain-text artifact users paste into bug reports, so the
contract is narrow:

* it names the application, runtime, desktop UI layer, relevant paths, key
  preferences and the newest log **path**;
* every path is redacted to ``~`` instead of exposing the account name;
* values the bridge never reads (proxy password/address, site cookies, music
  tokens, search history) cannot leak into it;
* the settings page exposes the card and button, the JS reuses ``copyText``
  and falls back to a selectable read-only box when the clipboard is denied;
* every front-end string is published through ``_i18n_catalog()``.

Nothing here opens a window or touches the network.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.apps.desktop.webview_app import (
    ApiBridge,
    _i18n_catalog,
    _redact_home,
)
from novel_downloader.infra.i18n import t

#: Strings rendered by the HTML card, so each needs a ``data-i18n`` attribute.
_DIAGNOSTICS_HTML_KEYS = (
    "Diagnostics",
    "Copy a short report for bug reports: version, runtime, paths and key "
    "preferences. Tokens, cookies, proxy passwords and account details are "
    "never included.",
    "Copy diagnostics",
)

#: Strings only the copy handler looks up through ``t()``.
_DIAGNOSTICS_SCRIPT_KEYS = (
    "Diagnostics copied to the clipboard",
    "Could not build the diagnostics report",
    "Could not copy; the report is selected below",
)

#: Values planted in the preference file that must never reach the report.
_SECRETS = (
    "proxy-SECRET-111",
    "proxy-SECRET-222",
    "cookie-SECRET-333",
    "token-SECRET-444",
    "private-search-555",
    "LOG-SECRET-CONTENT",
)


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """
    Point every path the report resolves at a fake home for user ``alice``.

    :return: The paths and the loaded bridge module, for assertions.
    """
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.paths as paths

    home = tmp_path / "users" / "alice"
    logs = home / "logs"
    prefs = home / "music" / "desktop_prefs.json"
    config = home / "settings.toml"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    config.parent.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(wa, "_prefs_path", lambda: prefs)
    monkeypatch.setattr(wa, "_settings_config_path", lambda: config)
    monkeypatch.setattr(wa, "_desktop_log_dir", lambda: logs)
    monkeypatch.setattr(wa, "_home_directories", lambda: (str(home),))
    monkeypatch.setattr(wa, "_desktop_ui_version", lambda: "pywebview 9.9.9")
    monkeypatch.setattr(paths, "app_home", lambda: home)
    monkeypatch.setattr(
        wa.ApiBridge, "get_data_dir", lambda _self: str(home / "downloads")
    )
    return {"home": home, "logs": logs, "prefs": prefs}


def _write_prefs(path: Path, values: dict[str, Any]) -> None:
    path.write_text(json.dumps(values), encoding="utf-8")


def test_report_lists_identity_runtime_paths_and_preferences(
    isolated: dict[str, Any],
) -> None:
    """All required fields are present and the prefs are rendered as values."""
    _write_prefs(
        isolated["prefs"],
        {
            "theme": "dark",
            "auto_check_updates": False,
            "auto_download_updates": True,
            "auto_install_on_exit": False,
            "window_x": 120,
            "window_y": 80,
        },
    )

    report = ApiBridge().diagnostics_text()

    assert report.startswith("BookTone desktop diagnostics\n")
    assert "Generated: " in report
    assert "Version: " in report
    assert "Desktop UI: pywebview 9.9.9" in report
    assert f"Python: {platform.python_version()}" in report
    assert f"OS: {platform.system()} {platform.release()}" in report
    assert f"Platform: {sys.platform}" in report
    for label in (
        "Data directory: ",
        "Output directory: ",
        "Log directory: ",
        "Config file: ",
        "Preferences file: ",
        "Recent log file: ",
    ):
        assert label in report, label
    assert "Theme: dark" in report
    assert "Auto-check updates: off" in report
    assert "Auto-download updates: on" in report
    assert "Auto-install on exit: off" in report
    assert "Window geometry saved: yes" in report


def test_paths_redact_the_account_name(isolated: dict[str, Any]) -> None:
    """Every resolved path uses ``~``; the OS account name is not exposed."""
    home: Path = isolated["home"]
    _write_prefs(isolated["prefs"], {"theme": "light"})

    report = ApiBridge().diagnostics_text()

    assert str(home) not in report
    assert "alice" not in report.lower()
    assert "Data directory: ~" in report
    assert "Output directory: ~" in report
    assert "Log directory: ~" in report
    assert "Config file: ~" in report
    assert "Preferences file: ~" in report
    assert report.count("~") >= 5


def test_redact_home_accepts_both_separator_styles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launcher may spell the same home with forward slashes."""
    import novel_downloader.apps.desktop.webview_app as wa

    home = tmp_path / "users" / "alice"
    monkeypatch.setattr(wa, "_home_directories", lambda: (str(home),))

    mixed = str(home).replace(os.sep, "/") + "/books"
    assert _redact_home(mixed) == "~/books"
    assert _redact_home(str(home)) == "~"
    # A longer sibling name is not a home prefix and must not be mangled.
    sibling = str(home) + "by"
    assert _redact_home(sibling) == sibling


def test_report_never_contains_credentials_or_history(
    isolated: dict[str, Any],
) -> None:
    """Secrets in the preferences file are neither read nor echoed."""
    _write_prefs(
        isolated["prefs"],
        {
            "theme": "dark",
            "proxy": "http://bob:proxy-SECRET-222@127.0.0.1:7890",
            "proxy_pass": "proxy-SECRET-111",
            "sites": {"qidian": {"cookies": "cookie-SECRET-333"}},
            "music_tokens": {"tidal": "token-SECRET-444"},
            "search_history": ["private-search-555"],
        },
    )

    report = ApiBridge().diagnostics_text().lower()

    for secret in _SECRETS:
        assert secret.lower() not in report, secret
    for key in ("proxy_pass", "cookies", "music_tokens", "search_history", "token"):
        assert key not in report, key


def test_report_names_the_newest_log_without_reading_it(
    isolated: dict[str, Any],
) -> None:
    """Only the newest log path is reported; the file body is never read."""
    logs: Path = isolated["logs"]
    older = logs / "novel-desktop.log.1"
    newest = logs / "novel-desktop.log"
    older.write_text("LOG-SECRET-CONTENT older", encoding="utf-8")
    newest.write_text("LOG-SECRET-CONTENT newest", encoding="utf-8")
    os.utime(older, (1_600_000_000, 1_600_000_000))
    os.utime(newest, (1_700_000_000, 1_700_000_000))

    report = ApiBridge().diagnostics_text()

    assert "Recent log file: " in report
    assert "_redact" not in report
    assert "novel-desktop.log" in report
    assert "novel-desktop.log.1" not in report
    assert "LOG-SECRET-CONTENT" not in report


def test_report_says_none_when_no_log_exists(isolated: dict[str, Any]) -> None:
    """A fresh install has no log yet; the field still exists."""
    for path in isolated["logs"].iterdir():
        path.unlink()

    report = ApiBridge().diagnostics_text()

    assert "Recent log file: (none)" in report


def test_unresolvable_paths_degrade_instead_of_failing(
    isolated: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One broken path cannot take down the whole report."""
    import novel_downloader.apps.desktop.webview_app as wa

    def boom() -> Path:
        raise OSError("no home")

    monkeypatch.setattr(wa, "_settings_config_path", boom)

    report = ApiBridge().diagnostics_text()

    assert "Config file: (unavailable)" in report
    assert "Preferences file: ~" in report


def test_diagnostics_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bridge method is total, like every other public bridge method."""
    import novel_downloader.apps.desktop.webview_app as wa

    def explode(_self: wa.ApiBridge) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(wa.ApiBridge, "_diagnostics_text", explode)

    assert ApiBridge().diagnostics_text() == (
        "BookTone desktop diagnostics\n(unavailable)\n"
    )


def test_settings_page_exposes_the_diagnostics_dom_and_catalog() -> None:
    """The card, the button and every string are wired into the page."""
    assert 'id="btn-copy-diagnostics"' in APP_HTML
    assert 'id="set-diagnostics-text"' in APP_HTML
    assert 'id="set-diagnostics-note"' in APP_HTML
    tag = APP_HTML[APP_HTML.index("<textarea") :]
    tag = tag[: tag.index(">") + 1]
    assert 'id="set-diagnostics-text"' in tag
    assert "readonly" in tag
    assert "hidden" in tag

    catalog = _i18n_catalog()
    for key in _DIAGNOSTICS_HTML_KEYS:
        assert f'data-i18n="{key}"' in APP_HTML, key
        assert key in catalog, key
    for key in _DIAGNOSTICS_SCRIPT_KEYS:
        assert f't("{key}")' in APP_HTML, key
        assert key in catalog, key


def _js_handler(needle: str) -> str:
    """Return the JS statement block starting at ``needle``."""
    start = APP_HTML.index(needle)
    end = APP_HTML.index("\n\n", start)
    return APP_HTML[start:end]


def test_copy_handler_reuses_the_shared_clipboard_helper() -> None:
    """The button asks the bridge for text and reuses ``copyText``."""
    handler = _js_handler('$("#btn-copy-diagnostics").onclick')

    assert 'api("diagnostics_text")' in handler
    assert "await copyText(text)" in handler
    assert 't("Diagnostics copied to the clipboard")' in handler
    assert 'toast(message, "ok")' in handler


def test_copy_failure_falls_back_to_a_selectable_box() -> None:
    """A denied clipboard still leaves the report selectable by hand."""
    handler = _js_handler('$("#btn-copy-diagnostics").onclick')

    assert 'if (!text || typeof text !== "string")' in handler
    assert 't("Could not build the diagnostics report")' in handler
    assert "box.value = text" in handler
    assert "box.hidden = false" in handler
    assert "box.select()" in handler
    assert 't("Could not copy; the report is selected below")' in handler
    assert 'toast(message, "warn", 7000)' in handler


def test_copy_text_keeps_the_legacy_clipboard_fallback() -> None:
    """``copyText`` must still resolve to a boolean when the API is denied."""
    start = APP_HTML.index("async function copyText(text)")
    end = APP_HTML.index("\n\n", start)
    helper = APP_HTML[start:end]

    assert "navigator.clipboard" in helper
    assert 'document.execCommand("copy")' in helper
    assert "return copied;" in helper


def test_diagnostics_copy_is_translated() -> None:
    """The card's copy resolves through ``t()`` in the English build."""
    # The conftest pins en_US, so these are identity lookups; the zh_CN
    # completeness itself is enforced by ``test_i18n_coverage``.
    assert t("Copy diagnostics") == "Copy diagnostics"
    assert t("Diagnostics") == "Diagnostics"
