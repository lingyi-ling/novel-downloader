#!/usr/bin/env python3
"""Contract tests for the configuration dataclasses in ``schemas.config``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, fields

import pytest

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
        assert cfg.cache_dir == "./novel_cache"
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

        assert cfg.cache_dir == "./novel_cache"
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
        assert cfg.cache_dir == "./novel_cache"
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
