#!/usr/bin/env python3
"""Contract tests for the configuration dataclasses in ``schemas.config``."""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import FrozenInstanceError, asdict, fields
from pathlib import Path

import pytest

from novel_downloader.infra.config import load_config, validate_config
from novel_downloader.infra.config.adapter import ConfigAdapter
from novel_downloader.infra.config.validation import DEFAULT_DOWNLOAD_RETRY_TIMES
from novel_downloader.infra.paths import DEFAULT_CONFIG_FILE, app_home
from novel_downloader.schemas import (
    BookConfig,
    ClientConfig,
    ExporterConfig,
    FetcherConfig,
    OCRConfig,
    ParserConfig,
    ProcessorConfig,
)


class TestFetcherConfig:
    def test_defaults(self) -> None:
        cfg = FetcherConfig()

        assert cfg.request_interval == 0.5
        assert cfg.retry_times == 3
        assert cfg.backoff_factor == 2.0
        assert cfg.timeout == 10.0
        assert cfg.max_connections == 10
        assert cfg.max_rps == 1000.0
        assert cfg.user_agent is None
        assert cfg.headers is None
        assert cfg.impersonate is None
        assert cfg.verify_ssl is True
        assert cfg.http2 is True
        assert cfg.proxy is None
        assert cfg.proxy_user is None
        assert cfg.proxy_pass is None
        assert cfg.trust_env is False
        assert cfg.cache_dir == str(app_home() / "cache")
        assert cfg.backend == "aiohttp"
        assert cfg.locale_style == "simplified"

    def test_mutable_field_defaults_are_not_shared(self) -> None:
        first = FetcherConfig()
        second = FetcherConfig()

        # ``headers`` defaults to None rather than a shared dict.
        assert first.headers is None
        assert second.headers is None

        first.headers = {"X-Test": "1"}
        assert second.headers is None

    def test_explicit_values(self) -> None:
        cfg = FetcherConfig(
            request_interval=1.25,
            retry_times=7,
            headers={"User-Agent": "agent"},
            verify_ssl=False,
            backend="httpx",
            locale_style="traditional",
        )

        assert cfg.request_interval == 1.25
        assert cfg.retry_times == 7
        assert cfg.headers == {"User-Agent": "agent"}
        assert cfg.verify_ssl is False
        assert cfg.backend == "httpx"
        assert cfg.locale_style == "traditional"

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            FetcherConfig(unknown_option=True)  # type: ignore[call-arg]


class TestOCRConfig:
    def test_defaults(self) -> None:
        cfg = OCRConfig()

        assert cfg.model_name is None
        assert cfg.model_dir is None
        assert cfg.input_shape is None
        assert cfg.device is None
        assert cfg.precision == "fp32"
        assert cfg.cpu_threads == 10
        assert cfg.enable_hpi is False

    def test_input_shape_is_stored_verbatim(self) -> None:
        cfg = OCRConfig(input_shape=(3, 32, 320))

        assert cfg.input_shape == (3, 32, 320)
        assert isinstance(cfg.input_shape, tuple)

    def test_no_runtime_validation_of_input_shape(self) -> None:
        # The annotation is static only; the dataclass stores any value.
        cfg = OCRConfig(input_shape=[3, 32, 320])  # type: ignore[arg-type]

        assert cfg.input_shape == [3, 32, 320]


class TestParserConfig:
    def test_defaults(self) -> None:
        cfg = ParserConfig()

        assert cfg.cache_dir == str(app_home() / "cache")
        assert cfg.use_truncation is True
        assert cfg.enable_ocr is False
        assert cfg.batch_size == 32
        assert cfg.remove_watermark is False
        assert cfg.cut_mode == "none"
        assert cfg.ocr_cfg == OCRConfig()

    def test_nested_default_is_not_shared(self) -> None:
        first = ParserConfig()
        second = ParserConfig()

        assert first.ocr_cfg is not second.ocr_cfg
        first.ocr_cfg.enable_hpi = True
        assert second.ocr_cfg.enable_hpi is False

    def test_explicit_nested_config(self) -> None:
        ocr = OCRConfig(model_name="custom", cpu_threads=2)
        cfg = ParserConfig(enable_ocr=True, ocr_cfg=ocr)

        assert cfg.ocr_cfg is ocr
        assert cfg.ocr_cfg.model_name == "custom"

    def test_asdict_nested_round_trip(self) -> None:
        cfg = ParserConfig(enable_ocr=True, ocr_cfg=OCRConfig(cpu_threads=2))

        data = asdict(cfg)
        payload = dict(data)
        payload["ocr_cfg"] = OCRConfig(**payload["ocr_cfg"])

        assert ParserConfig(**payload) == cfg


class TestExporterConfig:
    def test_defaults(self) -> None:
        cfg = ExporterConfig()

        assert cfg.render_missing_chapter is True
        assert cfg.append_timestamp is True
        assert cfg.filename_template == "{title}_{author}"
        assert cfg.include_picture is True
        assert cfg.split_mode == "book"

    def test_explicit_values(self) -> None:
        cfg = ExporterConfig(
            render_missing_chapter=False,
            append_timestamp=False,
            filename_template="{title}",
            include_picture=False,
            split_mode="volume",
        )

        assert cfg.render_missing_chapter is False
        assert cfg.append_timestamp is False
        assert cfg.filename_template == "{title}"
        assert cfg.include_picture is False
        assert cfg.split_mode == "volume"


class TestClientConfig:
    def test_defaults(self) -> None:
        cfg = ClientConfig()

        assert cfg.request_interval == 0.5
        assert cfg.retry_times == 3
        assert cfg.backoff_factor == 2.0
        assert cfg.cache_dir == str(app_home() / "cache")
        assert cfg.raw_data_dir == "./raw_data"
        assert cfg.output_dir == "./downloads"
        assert cfg.workers == 4
        assert cfg.cache_book_info is True
        assert cfg.cache_chapter is True
        assert cfg.fetch_inaccessible is False
        assert cfg.save_html is False
        assert cfg.storage_batch_size == 1
        assert cfg.fetcher_cfg == FetcherConfig()
        assert cfg.parser_cfg == ParserConfig()

    def test_nested_defaults_are_not_shared(self) -> None:
        first = ClientConfig()
        second = ClientConfig()

        assert first.fetcher_cfg is not second.fetcher_cfg
        assert first.parser_cfg is not second.parser_cfg

        first.fetcher_cfg.headers = {"X-Test": "1"}
        first.parser_cfg.ocr_cfg.model_name = "custom"
        assert second.fetcher_cfg.headers is None
        assert second.parser_cfg.ocr_cfg.model_name is None

    def test_asdict_nested_round_trip(self) -> None:
        cfg = ClientConfig(
            request_interval=1.5,
            workers=8,
            fetcher_cfg=FetcherConfig(backend="httpx", retry_times=5),
            parser_cfg=ParserConfig(enable_ocr=True, ocr_cfg=OCRConfig(cpu_threads=2)),
        )

        data = asdict(cfg)

        assert data["fetcher_cfg"]["backend"] == "httpx"
        assert data["parser_cfg"]["ocr_cfg"]["cpu_threads"] == 2

        parser_payload = dict(data["parser_cfg"])
        parser_payload["ocr_cfg"] = OCRConfig(**parser_payload["ocr_cfg"])
        rebuilt = ClientConfig(
            **{
                **data,
                "fetcher_cfg": FetcherConfig(**data["fetcher_cfg"]),
                "parser_cfg": ParserConfig(**parser_payload),
            }
        )

        assert rebuilt == cfg


class TestProcessorConfig:
    def test_required_name(self) -> None:
        with pytest.raises(TypeError):
            ProcessorConfig()  # type: ignore[call-arg]

        cfg = ProcessorConfig(name="cleaner")
        assert cfg.name == "cleaner"
        assert cfg.overwrite is False
        assert cfg.options == {}

    def test_options_default_is_not_shared(self) -> None:
        first = ProcessorConfig(name="cleaner")
        second = ProcessorConfig(name="cleaner")

        first.options["threshold"] = 1
        assert second.options == {}
        assert first.options is not second.options

    def test_explicit_options_are_kept(self) -> None:
        cfg = ProcessorConfig(name="corrector", overwrite=True, options={"x": 1})

        assert cfg.overwrite is True
        assert cfg.options == {"x": 1}

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            ProcessorConfig(name="cleaner", unknown=True)  # type: ignore[call-arg]


class TestBookConfig:
    def test_defaults(self) -> None:
        cfg = BookConfig(book_id="123")

        assert cfg.book_id == "123"
        assert cfg.start_id is None
        assert cfg.end_id is None
        assert cfg.ignore_ids == frozenset()

    def test_required_book_id(self) -> None:
        with pytest.raises(TypeError):
            BookConfig()  # type: ignore[call-arg]

    def test_is_frozen(self) -> None:
        cfg = BookConfig(book_id="123")

        with pytest.raises(FrozenInstanceError):
            cfg.book_id = "456"  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        cfg = BookConfig(book_id="123")

        assert not hasattr(cfg, "__dict__")
        assert set(cfg.__slots__) == {"book_id", "start_id", "end_id", "ignore_ids"}

    def test_hashable_and_usable_in_sets(self) -> None:
        same = {BookConfig(book_id="1"), BookConfig(book_id="1")}
        different = {BookConfig(book_id="1"), BookConfig(book_id="2")}

        assert len(same) == 1
        assert len(different) == 2
        assert hash(BookConfig(book_id="1")) == hash(BookConfig(book_id="1"))

    def test_ignore_ids_participates_in_equality_and_hash(self) -> None:
        plain = BookConfig(book_id="1")
        ignored = BookConfig(book_id="1", ignore_ids=frozenset({"2"}))

        assert plain != ignored
        assert plain == BookConfig(book_id="1", ignore_ids=frozenset())
        assert len({plain, ignored}) == 2

    def test_asdict_round_trip(self) -> None:
        cfg = BookConfig(
            book_id="1",
            start_id="10",
            end_id="20",
            ignore_ids=frozenset({"5", "6"}),
        )

        data = asdict(cfg)

        assert data == {
            "book_id": "1",
            "start_id": "10",
            "end_id": "20",
            "ignore_ids": frozenset({"5", "6"}),
        }
        assert BookConfig(**data) == cfg

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            BookConfig(book_id="1", unknown=True)  # type: ignore[call-arg]


@pytest.mark.parametrize("cls", [FetcherConfig, OCRConfig, ExporterConfig])
def test_flat_config_asdict_round_trip(cls: type) -> None:
    """Flat configs survive ``asdict`` + reconstruction without losing fields."""

    cfg = cls()
    data = asdict(cfg)

    assert set(data) == {field.name for field in fields(cls)}
    assert cls(**data) == cfg


# ---------------------------------------------------------------------------
# Cross-file consistency: schema defaults, bundled sample, and reference docs
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "guide"
REFERENCE_DOCS = ["settings-reference.md", "settings-reference.en.md"]
SAMPLE_TEXT = DEFAULT_CONFIG_FILE.read_text(encoding="utf-8")
SAMPLE = tomllib.loads(SAMPLE_TEXT)

#: Sample values that intentionally differ from the schema defaults.  Both are
#: explicit user-facing choices of the shipped configuration, not accidental
#: drift: ``config init`` exports two formats, and PaddleOCR's server model is
#: unusably slow on CPU.
SAMPLE_VALUE_OVERRIDES = {
    ("general.output", "formats"),
    ("general.parser", "model_name"),
}

GENERAL_ACTIVE_DEFAULTS = {
    "raw_data_dir": ClientConfig().raw_data_dir,
    "output_dir": ClientConfig().output_dir,
    "request_interval": FetcherConfig().request_interval,
    "workers": ClientConfig().workers,
    "max_connections": FetcherConfig().max_connections,
    "max_rps": FetcherConfig().max_rps,
    "retry_times": FetcherConfig().retry_times,
    "download_retry_times": DEFAULT_DOWNLOAD_RETRY_TIMES,
    "backoff_factor": FetcherConfig().backoff_factor,
    "timeout": FetcherConfig().timeout,
    "storage_batch_size": ClientConfig().storage_batch_size,
    "cache_book_info": ClientConfig().cache_book_info,
    "cache_chapter": ClientConfig().cache_chapter,
    "fetch_inaccessible": ClientConfig().fetch_inaccessible,
    "backend": FetcherConfig().backend,
    "locale_style": FetcherConfig().locale_style,
    "update_check": "auto",
    "login_required": False,
}

OUTPUT_ACTIVE_DEFAULTS = {
    "append_timestamp": ExporterConfig().append_timestamp,
    "render_missing_chapter": ExporterConfig().render_missing_chapter,
    "filename_template": ExporterConfig().filename_template,
    "include_picture": ExporterConfig().include_picture,
    "split_mode": ExporterConfig().split_mode,
}

PARSER_ACTIVE_DEFAULTS = {
    "enable_ocr": ParserConfig().enable_ocr,
    "batch_size": ParserConfig().batch_size,
    "use_truncation": ParserConfig().use_truncation,
    "cut_mode": ParserConfig().cut_mode,
    "remove_watermark": ParserConfig().remove_watermark,
}

DEBUG_ACTIVE_DEFAULTS = {
    "save_html": ClientConfig().save_html,
    "log_dir": "./logs",
    "log_level": "INFO",
}

PLUGINS_ACTIVE_DEFAULTS = {
    "enable_local_plugins": False,
    "override_builtins": False,
    "local_plugins_path": "./novel_plugins",
}

#: Every documented/schema-backed key the bundled example is expected to show,
#: either as an active assignment or as a commented-out line.
SAMPLE_REQUIRED_KEYS = {
    # general
    "raw_data_dir",
    "output_dir",
    "cache_dir",
    "request_interval",
    "workers",
    "max_connections",
    "max_rps",
    "retry_times",
    "download_retry_times",
    "backoff_factor",
    "timeout",
    "storage_batch_size",
    "cache_book_info",
    "cache_chapter",
    "fetch_inaccessible",
    "login_required",
    "strict_config",
    "backend",
    "locale_style",
    "update_check",
    "browser_assisted_sites",
    "http2",
    "impersonate",
    "user_agent",
    "headers",
    "proxy",
    "proxy_user",
    "proxy_pass",
    "trust_env",
    "verify_ssl",
    # output
    "formats",
    "render_missing_chapter",
    "append_timestamp",
    "filename_template",
    "include_picture",
    "split_mode",
    # parser / OCR
    "enable_ocr",
    "batch_size",
    "remove_watermark",
    "use_truncation",
    "cut_mode",
    "model_name",
    "model_dir",
    "input_shape",
    "device",
    "precision",
    "cpu_threads",
    "enable_hpi",
    # debug
    "save_html",
    "log_level",
    "log_dir",
    # plugins
    "enable_local_plugins",
    "override_builtins",
    "local_plugins_path",
}

DOCUMENTED_DEFAULTS: dict[str, object] = {
    "raw_data_dir": ClientConfig().raw_data_dir,
    "output_dir": ClientConfig().output_dir,
    "request_interval": FetcherConfig().request_interval,
    "workers": ClientConfig().workers,
    "max_connections": FetcherConfig().max_connections,
    "max_rps": FetcherConfig().max_rps,
    "retry_times": FetcherConfig().retry_times,
    "backoff_factor": FetcherConfig().backoff_factor,
    "timeout": FetcherConfig().timeout,
    "storage_batch_size": ClientConfig().storage_batch_size,
    "cache_book_info": ClientConfig().cache_book_info,
    "cache_chapter": ClientConfig().cache_chapter,
    "fetch_inaccessible": ClientConfig().fetch_inaccessible,
    "login_required": False,
    "strict_config": False,
    "backend": FetcherConfig().backend,
    "http2": FetcherConfig().http2,
    "impersonate": FetcherConfig().impersonate,
    "browser_assisted_sites": ["fanqienovel"],
    "download_retry_times": DEFAULT_DOWNLOAD_RETRY_TIMES,
    "proxy": FetcherConfig().proxy,
    "proxy_user": FetcherConfig().proxy_user,
    "proxy_pass": FetcherConfig().proxy_pass,
    "trust_env": FetcherConfig().trust_env,
    "verify_ssl": FetcherConfig().verify_ssl,
    "locale_style": FetcherConfig().locale_style,
    "update_check": "auto",
    "user_agent": FetcherConfig().user_agent,
    "headers": FetcherConfig().headers,
    "enable_ocr": ParserConfig().enable_ocr,
    "batch_size": ParserConfig().batch_size,
    "remove_watermark": ParserConfig().remove_watermark,
    "model_name": OCRConfig().model_name,
    "model_dir": OCRConfig().model_dir,
    "input_shape": OCRConfig().input_shape,
    "device": OCRConfig().device,
    "precision": OCRConfig().precision,
    "cpu_threads": OCRConfig().cpu_threads,
    "enable_hpi": OCRConfig().enable_hpi,
    "use_truncation": ParserConfig().use_truncation,
    "cut_mode": ParserConfig().cut_mode,
    "formats": [],
    "render_missing_chapter": ExporterConfig().render_missing_chapter,
    "append_timestamp": ExporterConfig().append_timestamp,
    "filename_template": ExporterConfig().filename_template,
    "include_picture": ExporterConfig().include_picture,
    "split_mode": ExporterConfig().split_mode,
    "debug.save_html": ClientConfig().save_html,
    "debug.log_level": "INFO",
    "debug.log_dir": "./logs",
    "enable_local_plugins": False,
    "override_builtins": False,
    "local_plugins_path": "./novel_plugins",
}


def _doc_value(value: object) -> str:
    """Render a default the way the reference tables spell it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "None"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(f'"{item}"' for item in value) + "]"
    return str(value)


def _doc_table_defaults(path: Path) -> dict[str, str]:
    """Extract ``key -> rendered default`` from markdown reference tables."""
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [cell.strip() for cell in line[1:-1].split("|")]
        if len(cells) < 4:
            continue
        key = cells[0].strip("`").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", key):
            continue
        rows.setdefault(key, cells[2].strip("`").strip())
    return rows


def _sample_mentions(key: str) -> bool:
    return (
        re.search(
            rf"^[ \t]*#?[ \t]*{re.escape(key)}[ \t]*=",
            SAMPLE_TEXT,
            flags=re.MULTILINE,
        )
        is not None
    )


def _reference_example(path: Path) -> dict:
    """Parse the TOML example under the ``Example configuration`` heading."""
    text = path.read_text(encoding="utf-8")
    heading = re.search(r"^#### (?:示例配置|Example configuration)$", text, re.M)
    assert heading is not None, f"{path.name}: example heading not found"
    fence = text.index("```toml", heading.end())
    body_start = text.index("\n", fence) + 1
    body_end = text.index("```", body_start)
    return tomllib.loads(text[body_start:body_end])


@pytest.mark.parametrize(
    ("getter", "expected"),
    [
        ("get_fetcher_config", FetcherConfig),
        ("get_parser_config", ParserConfig),
        ("get_client_config", ClientConfig),
        ("get_exporter_config", ExporterConfig),
    ],
)
def test_adapter_defaults_match_dataclass_defaults(getter: str, expected: type) -> None:
    """An empty config must resolve exactly to the dataclass defaults."""

    resolved = getattr(ConfigAdapter({}), getter)("missing")
    assert resolved == expected()


def test_sample_config_loads_without_warnings(tmp_path: Path, caplog) -> None:
    """The bundled example must be loadable by the real loader with no warning."""

    target = tmp_path / "settings.toml"
    target.write_text(SAMPLE_TEXT, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        loaded = load_config(str(target))

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert warnings == []
    assert loaded == SAMPLE
    assert [p for p in validate_config(loaded) if p.level != "info"] == []


@pytest.mark.parametrize("key", sorted(SAMPLE_REQUIRED_KEYS))
def test_sample_mentions_supported_key(key: str) -> None:
    """Every documented key appears in the example (active or commented)."""

    assert _sample_mentions(key), f"{key!r} is missing from settings.sample.toml"


@pytest.mark.parametrize(
    ("section", "defaults"),
    [
        ("general", GENERAL_ACTIVE_DEFAULTS),
        ("general.output", OUTPUT_ACTIVE_DEFAULTS),
        ("general.parser", PARSER_ACTIVE_DEFAULTS),
        ("general.debug", DEBUG_ACTIVE_DEFAULTS),
        ("plugins", PLUGINS_ACTIVE_DEFAULTS),
    ],
)
def test_sample_active_values_match_defaults(
    section: str, defaults: dict[str, object]
) -> None:
    """Active sample values must equal the schema/adapter defaults."""

    node: object = SAMPLE
    for part in section.split("."):
        assert isinstance(node, dict) and part in node
        node = node[part]
    assert isinstance(node, dict)

    mismatches: dict[str, tuple[object, object]] = {}
    for key, expected in defaults.items():
        if (section, key) in SAMPLE_VALUE_OVERRIDES:
            continue
        if key not in node:
            mismatches[key] = ("missing", expected)
        elif node[key] != expected:
            mismatches[key] = (node[key], expected)
    assert not mismatches, f"{section} drifted from defaults: {mismatches}"


def test_sample_has_no_deprecated_site_keys() -> None:
    for site_name, site in SAMPLE["sites"].items():
        assert "cookie" not in site, f"sites.{site_name}.cookie is deprecated"
        assert "use_truncation" not in site, (
            f"sites.{site_name}.use_truncation belongs in the parser table"
        )


@pytest.mark.parametrize("doc_name", REFERENCE_DOCS)
def test_reference_docs_example_uses_defaults(doc_name: str) -> None:
    """The copy-pasteable example must not silently deviate from defaults."""

    example = _reference_example(DOCS / doc_name)
    sections = [
        (("general",), GENERAL_ACTIVE_DEFAULTS),
        (("general", "output"), OUTPUT_ACTIVE_DEFAULTS),
        (("general", "parser"), PARSER_ACTIVE_DEFAULTS),
    ]
    for section, defaults in sections:
        node: object = example
        for part in section:
            node = node[part]
        assert isinstance(node, dict)
        for key, expected in defaults.items():
            if (".".join(section), key) in SAMPLE_VALUE_OVERRIDES:
                continue
            if key in node:
                assert node[key] == expected, f"{doc_name}: {section}.{key}"


@pytest.mark.parametrize("doc_name", REFERENCE_DOCS)
def test_reference_docs_defaults_match_schema(doc_name: str) -> None:
    """Each documented default agrees with the schema/adapter default."""

    table = _doc_table_defaults(DOCS / doc_name)
    mismatches = {
        key: (table.get(key), _doc_value(value))
        for key, value in DOCUMENTED_DEFAULTS.items()
        if table.get(key) != _doc_value(value)
    }
    assert not mismatches, f"{doc_name} drifted: {mismatches}"

    # The cache directory is platform-dependent and documented as a
    # placeholder rather than an absolute path.
    cache_default = table.get("cache_dir", "")
    assert "cache" in cache_default


@pytest.mark.parametrize("doc_name", REFERENCE_DOCS)
def test_reference_docs_cache_dir_is_described_as_user_data(doc_name: str) -> None:
    table = _doc_table_defaults(DOCS / doc_name)
    cache_default = table.get("cache_dir", "")
    assert "用户数据目录" in cache_default or "user data dir" in cache_default
