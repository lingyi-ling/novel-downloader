"""Boundary tests for hand-edited values consumed by ``ConfigAdapter``.

The adapter must never turn a typo in ``settings.toml`` into a startup crash,
and it must not silently reinterpret strings such as ``"false"`` as truthy.
Invalid scalar values fall back to the documented default (or are clamped to
the documented lower bound); genuinely invalid structural values raise the
same ``ValueError`` the public API already documented.
"""

from __future__ import annotations

import pytest

from novel_downloader.infra.config.adapter import ConfigAdapter


def test_sites_and_general_non_mapping_are_ignored():
    adapter = ConfigAdapter({"general": "oops", "sites": ["not", "a", "table"]})

    assert adapter._gen_cfg() == {}
    assert adapter._site_cfg("example") == {}
    assert adapter.get_log_level() == "INFO"


def test_non_mapping_adapter_input_is_empty():
    adapter = ConfigAdapter(["not", "a", "mapping"])  # type: ignore[arg-type]

    assert adapter.get_config() == {}


def test_bool_strings_are_not_treated_as_truthy():
    adapter = ConfigAdapter(
        {
            "general": {"verify_ssl": "false", "http2": "no", "trust_env": "0"},
            "sites": {"s": {"cache_book_info": "off", "cache_chapter": "TRUE"}},
        }
    )

    fetcher = adapter.get_fetcher_config("s")
    client = adapter.get_client_config("s")

    assert fetcher.verify_ssl is False
    assert fetcher.http2 is False
    assert fetcher.trust_env is False
    assert client.cache_book_info is False
    assert client.cache_chapter is True


def test_login_required_string_false_is_false():
    adapter = ConfigAdapter({"general": {"login_required": "false"}})
    assert adapter.get_login_required("missing") is False


def test_numeric_strings_are_coerced():
    adapter = ConfigAdapter(
        {
            "general": {
                "workers": "8",
                "retry_times": "5",
                "timeout": "2.5",
                "max_connections": "20.0",
                "storage_batch_size": "3",
            }
        }
    )

    client = adapter.get_client_config("s")
    fetcher = adapter.get_fetcher_config("s")

    assert client.workers == 8
    assert client.retry_times == 5
    assert client.storage_batch_size == 3
    assert fetcher.retry_times == 5
    assert fetcher.timeout == 2.5
    assert fetcher.max_connections == 20


def test_invalid_and_out_of_range_numbers_are_clamped():
    adapter = ConfigAdapter(
        {
            "general": {
                "workers": 0,
                "max_connections": -5,
                "retry_times": -1,
                "request_interval": -2,
                "backoff_factor": -1,
                "timeout": 0,
                "max_rps": -10,
                "storage_batch_size": 0,
                "parser": {"batch_size": "0", "cpu_threads": -3},
            }
        }
    )

    client = adapter.get_client_config("s")
    fetcher = adapter.get_fetcher_config("s")
    parser = adapter.get_parser_config("s")

    assert client.workers == 1
    assert client.retry_times == 0
    assert client.storage_batch_size == 1
    assert fetcher.max_connections == 1
    assert fetcher.request_interval == 0.0
    assert fetcher.backoff_factor == 0.0
    assert fetcher.timeout == 10.0
    assert fetcher.max_rps == 0.0
    assert parser.batch_size == 1
    assert parser.ocr_cfg.cpu_threads == 1


def test_non_numeric_values_fall_back_to_defaults():
    adapter = ConfigAdapter(
        {
            "general": {
                "workers": "many",
                "retry_times": None,
                "timeout": "soon",
                "max_connections": [1],
                "request_interval": float("nan"),
            }
        }
    )

    client = adapter.get_client_config("s")
    fetcher = adapter.get_fetcher_config("s")

    assert client.workers == 4
    assert fetcher.retry_times == 3
    assert fetcher.timeout == 10.0
    assert fetcher.max_connections == 10
    assert fetcher.request_interval == 0.5


def test_invalid_enums_fall_back_to_defaults():
    adapter = ConfigAdapter(
        {"general": {"backend": "made-up", "locale_style": "klingon"}}
    )

    fetcher = adapter.get_fetcher_config("s")
    assert fetcher.backend == "aiohttp"
    assert fetcher.locale_style == "simplified"


def test_invalid_log_level_and_dir_types_fall_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))

    adapter = ConfigAdapter({"general": {"debug": {"log_level": "LOUD", "log_dir": 3}}})

    assert adapter.get_log_level() == "INFO"
    assert adapter.get_log_dir() == (tmp_path / "logs").resolve()

    adapter = ConfigAdapter({"general": {"debug": "not-a-table"}})
    assert adapter.get_log_level() == "INFO"
    assert adapter.get_log_dir() == (tmp_path / "logs").resolve()


def test_non_string_cache_dir_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))

    adapter = ConfigAdapter({"general": {"cache_dir": 123}})

    assert adapter.get_cache_dir() == (tmp_path / "home" / "cache").resolve()
    assert adapter.get_fetcher_config("s").cache_dir


def test_plugins_section_non_mapping_is_ignored():
    adapter = ConfigAdapter({"plugins": ["not", "a", "table"]})

    assert adapter.get_plugins_config() == {
        "enable_local_plugins": False,
        "local_plugins_path": "",
        "override_builtins": False,
    }


def test_plugins_booleans_are_coerced():
    adapter = ConfigAdapter(
        {
            "plugins": {
                "enable_local_plugins": "yes",
                "override_builtins": "no",
                "local_plugins_path": 42,
            }
        }
    )

    cfg = adapter.get_plugins_config()
    assert cfg["enable_local_plugins"] is True
    assert cfg["override_builtins"] is False
    assert cfg["local_plugins_path"] == ""


def test_book_ids_rejects_boolean_and_float():
    with pytest.raises(ValueError):
        ConfigAdapter({"sites": {"s": {"book_ids": True}}}).get_book_ids("s")
    with pytest.raises(ValueError):
        ConfigAdapter({"sites": {"s": {"book_ids": 1.5}}}).get_book_ids("s")


def test_book_ignore_ids_scalar_and_string_are_safe():
    adapter = ConfigAdapter(
        {
            "sites": {
                "s": {
                    "book_ids": [
                        {"book_id": "1", "ignore_ids": "42"},
                        {"book_id": "2", "ignore_ids": 7},
                        {"book_id": "3", "ignore_ids": None},
                    ]
                }
            }
        }
    )

    books = adapter.get_book_ids("s")

    assert books[0].ignore_ids == frozenset({"42"})
    assert books[1].ignore_ids == frozenset({"7"})
    assert books[2].ignore_ids == frozenset()


def test_book_start_and_end_ids_ignore_non_scalars():
    cfg = ConfigAdapter._dict_to_book_cfg(
        {"book_id": "1", "start_id": None, "end_id": {"x": 1}}
    )

    assert cfg.start_id is None
    assert cfg.end_id is None


def test_input_shape_invalid_becomes_none():
    cases = [
        [1, 2],  # wrong length
        [1, 2, "3"],  # non-int component
        [1, 2, 0],  # non-positive
        "3,32,320",  # wrong type
        None,
    ]
    for shape in cases:
        cfg = ConfigAdapter._dict_to_ocr_cfg({"input_shape": shape})
        assert cfg.input_shape is None, shape

    assert (
        ConfigAdapter._dict_to_ocr_cfg({"input_shape": [3, 32, True]}).input_shape
        is None
    )


def test_ocr_cpu_threads_and_flags_are_coerced():
    cfg = ConfigAdapter._dict_to_ocr_cfg(
        {"cpu_threads": "0", "enable_hpi": "false", "precision": 3}
    )

    assert cfg.cpu_threads == 1
    assert cfg.enable_hpi is False
    assert cfg.precision == "fp32"


def test_env_vars_are_expanded_in_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVEL_TEST_OUT", str(tmp_path / "expanded"))
    monkeypatch.setenv("NOVEL_TEST_CACHE", str(tmp_path / "cache-expanded"))

    adapter = ConfigAdapter(
        {
            "general": {
                "output_dir": "${NOVEL_TEST_OUT}",
                "raw_data_dir": "%NOVEL_TEST_CACHE%/raw",
                "cache_dir": "$NOVEL_TEST_CACHE",
            }
        }
    )

    assert adapter.get_output_dir() == (tmp_path / "expanded").resolve()
    assert adapter.get_raw_data_dir() == (tmp_path / "cache-expanded" / "raw").resolve()
    assert adapter.get_cache_dir() == (tmp_path / "cache-expanded").resolve()


def test_undefined_env_vars_are_left_literal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))

    adapter = ConfigAdapter({"general": {"output_dir": "${NOVEL_UNDEFINED_VAR}"}})

    assert adapter.get_output_dir() == (tmp_path / "${NOVEL_UNDEFINED_VAR}").resolve()


def test_recursive_and_self_referential_env_vars_terminate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVEL_LOOP_A", "${NOVEL_LOOP_B}")
    monkeypatch.setenv("NOVEL_LOOP_B", "${NOVEL_LOOP_A}")
    monkeypatch.setenv("NOVEL_SELF", "${NOVEL_SELF}")

    looped = ConfigAdapter({"general": {"output_dir": "${NOVEL_LOOP_A}"}})
    self_ref = ConfigAdapter({"general": {"raw_data_dir": "%NOVEL_SELF%"}})

    assert looped.get_output_dir().name in {"${NOVEL_LOOP_A}", "${NOVEL_LOOP_B}"}
    assert self_ref.get_raw_data_dir().name in {"${NOVEL_SELF}", "%NOVEL_SELF%"}


def test_export_formats_filter_non_strings():
    adapter = ConfigAdapter(
        {"general": {"output": {"formats": ["epub", 3, "", None, "txt"]}}}
    )

    assert adapter.get_export_fmt("s") == ["epub", "txt"]


def test_filename_template_non_string_falls_back():
    adapter = ConfigAdapter({"general": {"output": {"filename_template": 42}}})

    assert adapter.get_exporter_config("s").filename_template == "{title}_{author}"


def test_processor_overwrite_string_is_coerced():
    rows = [{"name": "cleaner", "overwrite": "false"}]

    procs = ConfigAdapter._to_processor_cfgs(rows)

    assert procs[0].overwrite is False
