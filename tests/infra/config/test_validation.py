"""Tests for ``novel_downloader.infra.config.validation``."""

from __future__ import annotations

import copy
import os
from pathlib import Path

from novel_downloader.infra.config import (
    Problem,
    migrate_config,
    validate_config,
)
from novel_downloader.infra.config import validation as validation_module
from novel_downloader.infra.config.file_io import load_config


def _paths(problems: list[Problem], level: str | None = None) -> set[str]:
    return {p.path for p in problems if level is None or p.level == level}


def _full_config(tmp_path: Path) -> dict:
    """A representative configuration that must validate cleanly."""
    return {
        "general": {
            "raw_data_dir": str(tmp_path / "raw"),
            "output_dir": str(tmp_path / "out"),
            "cache_dir": str(tmp_path / "cache"),
            "request_interval": 0.5,
            "workers": 4,
            "max_connections": 10,
            "max_rps": 1000.0,
            "retry_times": 3,
            "backoff_factor": 2.0,
            "timeout": 10.0,
            "storage_batch_size": 1,
            "cache_book_info": True,
            "cache_chapter": True,
            "fetch_inaccessible": False,
            "backend": "aiohttp",
            "locale_style": "simplified",
            "login_required": False,
            "http2": True,
            "impersonate": None,
            "verify_ssl": True,
            "user_agent": None,
            "headers": {"X-Test": "yes"},
            "proxy": None,
            "proxy_user": None,
            "proxy_pass": None,
            "trust_env": False,
            "download_retry_times": 2,
            "parser": {
                "enable_ocr": False,
                "batch_size": 32,
                "remove_watermark": False,
                "model_name": None,
                "model_dir": None,
                "input_shape": [3, 32, 320],
                "device": None,
                "precision": "fp32",
                "cpu_threads": 10,
                "enable_hpi": False,
                "use_truncation": True,
                "cut_mode": "none",
            },
            "output": {
                "formats": ["txt", "epub"],
                "render_missing_chapter": True,
                "append_timestamp": True,
                "filename_template": "{title}_{author}",
                "include_picture": True,
                "split_mode": "book",
            },
            "debug": {
                "save_html": False,
                "log_level": "INFO",
                "log_dir": str(tmp_path / "logs"),
            },
            "processors": [
                {"name": "cleaner", "overwrite": False, "remove_invisible": True}
            ],
        },
        "sites": {
            "example": {
                "book_ids": [
                    "123",
                    456,
                    {
                        "book_id": "789",
                        "start_id": 1,
                        "end_id": 2,
                        "ignore_ids": [3, "4"],
                    },
                ],
                "login_required": True,
                "username": "user",
                "password": "pass",
                "cookies": "a=1",
                "request_interval": 2.0,
                "retry_times": 5,
                "backoff_factor": 3.0,
                "timeout": 15.0,
                "max_connections": 20,
                "max_rps": 500.0,
                "workers": 8,
                "storage_batch_size": 2,
                "cache_book_info": True,
                "cache_chapter": True,
                "fetch_inaccessible": False,
                "backend": "httpx",
                "locale_style": "traditional",
                "http2": True,
                "impersonate": "chrome",
                "verify_ssl": True,
                "user_agent": "UA",
                "headers": {"X-Site": "yes"},
                "proxy": None,
                "proxy_user": None,
                "proxy_pass": None,
                "trust_env": False,
                "parser": {
                    "enable_ocr": True,
                    "batch_size": 8,
                    "remove_watermark": True,
                    "model_name": "m",
                    "model_dir": str(tmp_path / "model"),
                    "input_shape": [3, 64, 640],
                    "device": "cpu",
                    "precision": "int8",
                    "cpu_threads": 4,
                    "enable_hpi": True,
                    "use_truncation": False,
                    "cut_mode": "paragraph",
                },
                "output": {
                    "formats": ["epub"],
                    "render_missing_chapter": False,
                    "append_timestamp": False,
                    "filename_template": "{title}",
                    "include_picture": False,
                    "split_mode": "volume",
                },
                "processors": [{"name": "site", "overwrite": True, "x": 1}],
            },
            "empty": {},
        },
        "plugins": {
            "enable_local_plugins": True,
            "override_builtins": True,
            "local_plugins_path": str(tmp_path / "plugins"),
        },
    }


# ---------------------------------------------------------------------------
# Valid configurations
# ---------------------------------------------------------------------------


def test_valid_minimal_config_has_no_problems():
    assert validate_config({}) == []
    assert validate_config({"general": {}, "sites": {}, "plugins": {}}) == []


def test_valid_full_config_has_no_problems(tmp_path):
    problems = validate_config(_full_config(tmp_path))
    assert problems == [], "\n".join(str(p) for p in problems)


def test_valid_book_ids_shapes():
    assert validate_config({"sites": {"s": {"book_ids": "123"}}}) == []
    assert validate_config({"sites": {"s": {"book_ids": 123}}}) == []
    assert validate_config({"sites": {"s": {"book_ids": {"book_id": "1"}}}}) == []
    assert validate_config({"sites": {"s": {"book_ids": ["1", 2]}}}) == []


# ---------------------------------------------------------------------------
# Unknown keys and suggestions
# ---------------------------------------------------------------------------


def test_unknown_root_key_suggests_close_match():
    problems = validate_config({"generall": {}})
    assert _paths(problems, "warning") == {"generall"}
    assert "general" in problems[0].fix_hint


def test_unknown_nested_key_suggests_close_match():
    problems = validate_config({"general": {"timeuot": 10}})
    assert _paths(problems, "warning") == {"general.timeuot"}
    assert "timeout" in problems[0].fix_hint


def test_unknown_nested_section_key_suggests_close_match():
    problems = validate_config({"general": {"output": {"formats": ["txt"], "xmob": 1}}})
    assert _paths(problems, "warning") == {"general.output.xmob"}
    assert "unknown configuration key" in problems[0].message


def test_unknown_key_without_close_match_has_generic_hint():
    problems = validate_config({"general": {"zzzzzzzz": 1}})
    assert len(problems) == 1
    assert "settings-reference.md" in problems[0].fix_hint


# ---------------------------------------------------------------------------
# Type errors
# ---------------------------------------------------------------------------


def test_type_errors_are_reported_as_errors():
    problems = validate_config(
        {
            "general": {
                "timeout": "10",
                "workers": True,
                "headers": [],
                "debug": "nope",
                "parser": [],
                "output": [],
                "processors": {},
            },
            "sites": [],
            "plugins": "off",
        }
    )
    errors = _paths(problems, "error")
    assert {
        "general.timeout",
        "general.workers",
        "general.headers",
        "general.debug",
        "general.parser",
        "general.output",
        "general.processors",
        "sites",
        "plugins",
    } <= errors


def test_bool_is_not_accepted_as_int_or_number():
    problems = validate_config(
        {"general": {"retry_times": True, "request_interval": False}}
    )
    assert _paths(problems, "error") == {
        "general.retry_times",
        "general.request_interval",
    }


def test_site_must_be_a_table():
    problems = validate_config({"sites": {"bad": "nope"}})
    assert _paths(problems, "error") == {"sites.bad"}


def test_empty_site_name_is_rejected():
    problems = validate_config({"sites": {"": {}}})
    assert _paths(problems, "error") == {"sites"}


# ---------------------------------------------------------------------------
# Range checks
# ---------------------------------------------------------------------------


def test_out_of_range_values_are_errors():
    problems = validate_config(
        {
            "general": {
                "timeout": 0,
                "request_interval": -1,
                "max_rps": -1,
                "backoff_factor": -1,
                "workers": 0,
                "max_connections": -1,
                "retry_times": -1,
                "download_retry_times": -1,
                "storage_batch_size": 0,
                "parser": {"batch_size": 0, "cpu_threads": 0},
            }
        }
    )
    errors = _paths(problems, "error")
    assert {
        "general.timeout",
        "general.request_interval",
        "general.max_rps",
        "general.backoff_factor",
        "general.workers",
        "general.max_connections",
        "general.retry_times",
        "general.download_retry_times",
        "general.storage_batch_size",
        "general.parser.batch_size",
        "general.parser.cpu_threads",
    } <= errors


def test_site_out_of_range_values_are_errors():
    problems = validate_config(
        {"sites": {"s": {"timeout": 0, "workers": 0, "max_connections": 0}}}
    )
    assert {
        "sites.s.timeout",
        "sites.s.workers",
        "sites.s.max_connections",
    } <= _paths(problems, "error")


def test_input_shape_must_have_three_positive_integers():
    problems = validate_config(
        {
            "general": {
                "parser": {
                    "input_shape": [3, 32],
                }
            }
        }
    )
    assert _paths(problems, "error") == {"general.parser.input_shape"}

    problems = validate_config({"general": {"parser": {"input_shape": [3, 0, -1]}}})
    assert _paths(problems, "error") == {
        "general.parser.input_shape[1]",
        "general.parser.input_shape[2]",
    }


def test_unsupported_enum_values():
    problems = validate_config(
        {
            "general": {
                "backend": "requests",
                "locale_style": "zh",
                "debug": {"log_level": "LOUD"},
            }
        }
    )
    assert _paths(problems, "error") == {
        "general.backend",
        "general.locale_style",
    }
    # Unknown log levels are a warning: the logger falls back to INFO.
    assert _paths(problems, "warning") == {"general.debug.log_level"}


def test_unsupported_export_format_is_a_warning(tmp_path):
    cfg = _full_config(tmp_path)
    cfg["general"]["output"]["formats"] = ["epub", "mobi"]
    problems = validate_config(cfg)
    assert _paths(problems, "warning") == {"general.output.formats[1]"}
    assert problems[0].level == "warning"


# ---------------------------------------------------------------------------
# Directory writability
# ---------------------------------------------------------------------------


def test_output_dir_pointing_at_a_file_is_an_error(tmp_path):
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")

    problems = validate_config({"general": {"output_dir": str(file_path)}})
    assert _paths(problems, "error") == {"general.output_dir"}
    assert "not a directory" in problems[0].message


def test_unwritable_output_dir_is_an_error(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()

    def _deny_access(path: os.PathLike[str] | str, mode: int) -> bool:
        return False

    monkeypatch.setattr(validation_module.os, "access", _deny_access)

    problems = validate_config({"general": {"output_dir": str(out)}})
    assert _paths(problems, "error") == {"general.output_dir"}
    assert "not writable" in problems[0].message


def test_missing_output_dir_under_writable_parent_is_fine(tmp_path):
    problems = validate_config(
        {"general": {"output_dir": str(tmp_path / "new" / "nested")}}
    )
    assert problems == []


def test_missing_output_dir_under_file_is_an_error(tmp_path):
    file_path = tmp_path / "blocker"
    file_path.write_text("x", encoding="utf-8")

    problems = validate_config({"general": {"output_dir": str(file_path / "child")}})
    assert _paths(problems, "error") == {"general.output_dir"}
    assert "cannot create" in problems[0].message


def test_empty_directory_value_is_an_error():
    problems = validate_config({"general": {"cache_dir": "   "}})
    assert _paths(problems, "error") == {"general.cache_dir"}


def test_plugins_directory_is_checked_only_when_enabled(tmp_path):
    path = str(tmp_path / "blocker" / "plugins")
    (tmp_path / "blocker").write_text("x", encoding="utf-8")

    assert (
        validate_config(
            {"plugins": {"enable_local_plugins": False, "local_plugins_path": path}}
        )
        == []
    )
    problems = validate_config(
        {"plugins": {"enable_local_plugins": True, "local_plugins_path": path}}
    )
    assert _paths(problems, "error") == {"plugins.local_plugins_path"}


# ---------------------------------------------------------------------------
# Deprecated / misplaced keys
# ---------------------------------------------------------------------------


def test_site_cookie_is_reported_as_deprecated():
    problems = validate_config({"sites": {"s": {"cookie": "a=1"}}})
    assert _paths(problems, "warning") == {"sites.s.cookie"}
    assert "cookies" in problems[0].fix_hint


def test_site_level_use_truncation_is_reported_as_misplaced():
    problems = validate_config({"sites": {"s": {"use_truncation": True}}})
    assert _paths(problems, "warning") == {"sites.s.use_truncation"}
    assert "sites.s.parser.use_truncation" in problems[0].fix_hint


# ---------------------------------------------------------------------------
# book_ids / processors
# ---------------------------------------------------------------------------


def test_invalid_book_ids_entries():
    problems = validate_config({"sites": {"s": {"book_ids": [1.5, True]}}})
    assert _paths(problems, "error") == {
        "sites.s.book_ids[0]",
        "sites.s.book_ids[1]",
    }

    problems = validate_config({"sites": {"s": {"book_ids": {"start_id": "1"}}}})
    assert _paths(problems, "error") == {"sites.s.book_ids"}

    problems = validate_config(
        {"sites": {"s": {"book_ids": {"book_id": "1", "ignore_ids": "123"}}}}
    )
    assert _paths(problems, "error") == {"sites.s.book_ids.ignore_ids"}


def test_unknown_book_entry_key_is_a_warning():
    problems = validate_config(
        {"sites": {"s": {"book_ids": {"book_id": "1", "strat_id": "2"}}}}
    )
    assert _paths(problems, "warning") == {"sites.s.book_ids.strat_id"}
    assert "start_id" in problems[0].fix_hint


def test_processor_validation():
    problems = validate_config(
        {
            "general": {
                "processors": [
                    {"overwrite": False},
                    {"name": ""},
                    {"name": "ok", "overwrite": "yes"},
                    "not-a-table",
                ]
            }
        }
    )
    assert _paths(problems, "error") == {
        "general.processors[0]",
        "general.processors[1].name",
        "general.processors[2].overwrite",
        "general.processors[3]",
    }


# ---------------------------------------------------------------------------
# Path input and purity
# ---------------------------------------------------------------------------


def test_validate_config_accepts_a_toml_path(tmp_path):
    cfg = tmp_path / "settings.toml"
    cfg.write_text(
        '[general]\ntimeout = 10.0\n[sites.example]\nbook_ids = ["1"]\n',
        encoding="utf-8",
    )
    assert validate_config(cfg) == []


def test_validate_config_returns_problem_for_invalid_toml(tmp_path):
    cfg = tmp_path / "bad.toml"
    cfg.write_text("timeout = ", encoding="utf-8")
    problems = validate_config(cfg)
    assert len(problems) == 1
    assert problems[0].level == "error"
    assert problems[0].path == "<file>"


def test_validate_config_returns_problem_for_missing_file(tmp_path):
    problems = validate_config(tmp_path / "missing.toml")
    assert len(problems) == 1
    assert problems[0].path == "<file>"


def test_validate_config_does_not_mutate_input(tmp_path):
    cfg = _full_config(tmp_path)
    before = copy.deepcopy(cfg)
    validate_config(cfg)
    assert cfg == before


def test_problem_string_includes_hint():
    problem = Problem("warning", "general.timeout", "bad", "fix it")
    assert str(problem) == "[warning] general.timeout: bad Hint: fix it"


# ---------------------------------------------------------------------------
# load_config integration: warn, never crash
# ---------------------------------------------------------------------------


def test_load_config_logs_validation_problems_but_returns_data(
    tmp_path, monkeypatch, caplog
):
    cfg = tmp_path / "settings.json"
    cfg.write_text('{"generall": {}, "general": {"timeout": "oops"}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with caplog.at_level("WARNING"):
        data = load_config(None)

    assert data == {"generall": {}, "general": {"timeout": "oops"}}
    assert "unknown configuration key" in caplog.text
    assert "expected number" in caplog.text


# ---------------------------------------------------------------------------
# migrate_config
# ---------------------------------------------------------------------------


def test_migrate_renames_cookie_and_moves_use_truncation():
    original = {
        "sites": {
            "qidian": {
                "cookie": "a=1",
                "use_truncation": False,
            }
        },
    }
    migrated, changes = migrate_config(original)

    assert migrated["sites"]["qidian"] == {
        "cookies": "a=1",
        "parser": {"use_truncation": False},
    }
    assert changes == [
        "sites.qidian.cookie -> sites.qidian.cookies",
        "sites.qidian.use_truncation -> sites.qidian.parser.use_truncation",
    ]
    # The caller's mapping is untouched.
    assert original["sites"]["qidian"] == {
        "cookie": "a=1",
        "use_truncation": False,
    }


def test_migrate_merges_into_existing_parser():
    original = {
        "sites": {"s": {"use_truncation": True, "parser": {"enable_ocr": True}}}
    }
    migrated, changes = migrate_config(original)
    assert migrated["sites"]["s"]["parser"] == {
        "enable_ocr": True,
        "use_truncation": True,
    }
    assert changes == ["sites.s.use_truncation -> sites.s.parser.use_truncation"]


def test_migrate_fills_download_retry_times_default():
    migrated, changes = migrate_config({"general": {"retry_times": 3}})
    assert migrated["general"]["download_retry_times"] == 2
    assert changes == ["general.download_retry_times not set; filled default 2"]


def test_migrate_keeps_explicit_download_retry_times():
    migrated, changes = migrate_config({"general": {"download_retry_times": 0}})
    assert migrated["general"]["download_retry_times"] == 0
    assert changes == []


def test_migrate_does_not_create_general_section():
    migrated, changes = migrate_config({"sites": {"s": {}}})
    assert "general" not in migrated
    assert changes == []


def test_migrate_leaves_conflicting_cookie_untouched():
    original = {"sites": {"s": {"cookie": "old", "cookies": "new"}}}
    migrated, changes = migrate_config(original)
    assert migrated == original
    assert changes == []


def test_migrate_leaves_parser_conflict_untouched():
    original = {
        "sites": {"s": {"use_truncation": True, "parser": {"use_truncation": False}}}
    }
    migrated, changes = migrate_config(original)
    assert migrated == original
    assert changes == []


def test_migrate_skips_non_table_site_and_parser():
    original = {"sites": {"bad": "nope", "s": {"use_truncation": True, "parser": []}}}
    migrated, changes = migrate_config(original)
    assert migrated == original
    assert changes == []


def test_migrate_is_idempotent():
    original = {
        "general": {},
        "sites": {"qidian": {"cookie": "a=1", "use_truncation": True}},
    }
    once, first_changes = migrate_config(original)
    twice, second_changes = migrate_config(once)

    assert first_changes
    assert second_changes == []
    assert twice == once


def test_migrate_empty_config():
    migrated, changes = migrate_config({})
    assert migrated == {}
    assert changes == []
