#!/usr/bin/env python3
"""
novel_downloader.infra.config.adapter
-------------------------------------
"""

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from novel_downloader.infra.i18n import t
from novel_downloader.schemas import (
    BookConfig,
    ClientConfig,
    ExporterConfig,
    FetcherConfig,
    OCRConfig,
    ParserConfig,
    ProcessorConfig,
)
from novel_downloader.schemas.config import default_cache_dir

from .validation import expand_env_path

_BACKENDS = ("aiohttp", "httpx", "curl_cffi")
_LOCALE_STYLES = ("simplified", "traditional")
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_SPLIT_MODES = ("book", "volume", "chapter")
#: Generous safety caps: a hand-edited value of ``999999999`` must not spawn
#: an unbounded number of tasks/connections.  They are far above any sane
#: configuration, so normal files are never altered.
_MAX_CONCURRENCY = 4096
_MAX_BATCH_SIZE = 65536
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off", "n", "f"})


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` when it is a mapping, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def _int_value(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Coerce an integer-ish config value, clamping to the given bounds.

    Accepts integers, integral floats and decimal strings (``"8"`` /
    ``"8.0"``).  Booleans are rejected (``True`` is not the number 1) and
    anything else falls back to ``default``.
    """
    if isinstance(value, bool):
        return default
    number: int | None = None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = int(text, 10)
        except ValueError:
            try:
                parsed = float(text)
            except ValueError:
                return default
            if not math.isfinite(parsed) or not parsed.is_integer():
                return default
            number = int(parsed)
    if number is None:
        return default
    if minimum is not None and number < minimum:
        number = minimum
    if maximum is not None and number > maximum:
        number = maximum
    return number


def _float_value(
    value: Any,
    default: float,
    *,
    minimum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    """Coerce a finite float-ish value and clamp it to a lower bound."""
    if isinstance(value, bool):
        return default
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return default
    if number is None or not math.isfinite(number):
        return default
    if minimum is not None:
        if exclusive_minimum:
            if number <= minimum:
                return default
        elif number < minimum:
            number = minimum
    return number


def _bool_value(value: Any, default: bool) -> bool:
    """Parse booleans the way users write them in TOML.

    Plain ``bool(value)`` turns ``"false"`` into ``True``; this helper accepts
    common truthy/falsy spellings and otherwise keeps the default.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return default


def _str_value(value: Any, default: str) -> str:
    """Return a non-empty string or ``default``."""
    if isinstance(value, str) and value.strip():
        return value
    return default


def _optional_str(value: Any) -> str | None:
    """Return a non-empty string or ``None`` (never a coerced number)."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _enum_value(value: Any, allowed: tuple[str, ...], default: str) -> str:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in allowed:
            return text
    return default


def _path_value(value: Any, default: str) -> str:
    """Expand environment variables in a path, falling back to ``default``."""
    if not isinstance(value, str):
        return default
    expanded = expand_env_path(value)
    return expanded or default


def _headers_value(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return None
    if not all(
        isinstance(key, str) and isinstance(item, str) and key.strip()
        for key, item in value.items()
    ):
        return None
    return dict(value)


class ConfigAdapter:
    """Adapter that provides convenient access to general and site-specific settings."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the adapter.

        Args:
            config: A fully loaded configuration mapping.
        """
        self._config: dict[str, Any] = (
            dict(config) if isinstance(config, Mapping) else {}
        )

    def get_config(self) -> dict[str, Any]:
        """Return the entire configuration mapping.

        Returns:
            The raw configuration dictionary stored in this adapter.
        """
        return self._config

    def get_fetcher_config(self, site: str) -> FetcherConfig:
        """Build a FetcherConfig by resolving fields from site and general settings.

        Site-level settings override general ones; defaults are applied last.

        Args:
            site: The site key.

        Returns:
            A fully populated configuration for the network fetcher.
        """
        site_cfg, general_cfg = self._site_cfg(site), self._gen_cfg()
        cfg = {**general_cfg, **site_cfg}

        return FetcherConfig(
            cache_dir=_path_value(general_cfg.get("cache_dir"), default_cache_dir()),
            request_interval=_float_value(
                cfg.get("request_interval"), 0.5, minimum=0.0
            ),
            retry_times=_int_value(cfg.get("retry_times"), 3, minimum=0),
            backoff_factor=_float_value(cfg.get("backoff_factor"), 2.0, minimum=0.0),
            timeout=_float_value(
                cfg.get("timeout"), 10.0, minimum=0.0, exclusive_minimum=True
            ),
            max_connections=_int_value(
                cfg.get("max_connections"),
                10,
                minimum=1,
                maximum=_MAX_CONCURRENCY,
            ),
            max_rps=_float_value(cfg.get("max_rps"), 1000.0, minimum=0.0),
            user_agent=_optional_str(cfg.get("user_agent")),
            headers=_headers_value(cfg.get("headers")),
            impersonate=_optional_str(cfg.get("impersonate")),
            verify_ssl=_bool_value(cfg.get("verify_ssl"), True),
            http2=_bool_value(cfg.get("http2"), True),
            proxy=_optional_str(cfg.get("proxy")),
            proxy_user=_optional_str(cfg.get("proxy_user")),
            proxy_pass=_optional_str(cfg.get("proxy_pass")),
            trust_env=_bool_value(cfg.get("trust_env"), False),
            backend=_enum_value(cfg.get("backend"), _BACKENDS, "aiohttp"),
            locale_style=_enum_value(
                cfg.get("locale_style"), _LOCALE_STYLES, "simplified"
            ),
        )

    def get_parser_config(self, site: str) -> ParserConfig:
        """Build a ParserConfig from general, OCR-related, and site-specific settings.

        Args:
            site: The site key.

        Returns:
            A fully populated configuration for the parser stage.
        """
        site_cfg, general_cfg = self._site_cfg(site), self._gen_cfg()
        parser_cfg: dict[str, Any] = {
            **_mapping(general_cfg.get("parser")),
            **_mapping(site_cfg.get("parser")),
        }

        return ParserConfig(
            cache_dir=_path_value(general_cfg.get("cache_dir"), default_cache_dir()),
            use_truncation=_bool_value(parser_cfg.get("use_truncation"), True),
            enable_ocr=_bool_value(parser_cfg.get("enable_ocr"), False),
            batch_size=_int_value(
                parser_cfg.get("batch_size"),
                32,
                minimum=1,
                maximum=_MAX_BATCH_SIZE,
            ),
            remove_watermark=_bool_value(parser_cfg.get("remove_watermark"), False),
            cut_mode=_str_value(parser_cfg.get("cut_mode"), "none"),
            ocr_cfg=self._dict_to_ocr_cfg(parser_cfg),
        )

    def get_client_config(self, site: str) -> ClientConfig:
        """Build a ClientConfig using both general and site-specific settings.

        Site-level settings override general ones; defaults are applied last.

        Args:
            site: The site key.

        Returns:
            A fully populated configuration for the high-level client.
        """
        site_cfg, general_cfg = self._site_cfg(site), self._gen_cfg()
        cfg = {**general_cfg, **site_cfg}
        debug_cfg = _mapping(general_cfg.get("debug"))

        return ClientConfig(
            raw_data_dir=_path_value(general_cfg.get("raw_data_dir"), "./raw_data"),
            cache_dir=_path_value(general_cfg.get("cache_dir"), default_cache_dir()),
            output_dir=_path_value(general_cfg.get("output_dir"), "./downloads"),
            workers=_int_value(
                cfg.get("workers"),
                4,
                minimum=1,
                maximum=_MAX_CONCURRENCY,
            ),
            request_interval=_float_value(
                cfg.get("request_interval"), 0.5, minimum=0.0
            ),
            retry_times=_int_value(cfg.get("retry_times"), 3, minimum=0),
            backoff_factor=_float_value(cfg.get("backoff_factor"), 2.0, minimum=0.0),
            storage_batch_size=_int_value(
                cfg.get("storage_batch_size"),
                1,
                minimum=1,
                maximum=_MAX_BATCH_SIZE,
            ),
            cache_book_info=_bool_value(cfg.get("cache_book_info"), True),
            cache_chapter=_bool_value(cfg.get("cache_chapter"), True),
            fetch_inaccessible=_bool_value(cfg.get("fetch_inaccessible"), False),
            save_html=_bool_value(debug_cfg.get("save_html"), False),
            fetcher_cfg=self.get_fetcher_config(site),
            parser_cfg=self.get_parser_config(site),
        )

    def get_exporter_config(self, site: str) -> ExporterConfig:
        """Build an ExporterConfig from general.output with site-specific overrides.

        Args:
            site: The site key.

        Returns:
            The resolved exporter configuration.
        """
        site_cfg, general_cfg = self._site_cfg(site), self._gen_cfg()
        out = {
            **_mapping(general_cfg.get("output")),
            **_mapping(site_cfg.get("output")),
        }

        return ExporterConfig(
            render_missing_chapter=_bool_value(out.get("render_missing_chapter"), True),
            append_timestamp=_bool_value(out.get("append_timestamp"), True),
            filename_template=_str_value(
                out.get("filename_template"), "{title}_{author}"
            ),
            include_picture=_bool_value(out.get("include_picture"), True),
            split_mode=_enum_value(out.get("split_mode"), _SPLIT_MODES, "book"),
        )

    def get_login_config(self, site: str) -> dict[str, str]:
        """Extract login-related fields from the current site configuration.

        Only non-empty string values are returned; values are stripped.

        Args:
            site: The site key.

        Returns:
            A subset of {"username", "password", "cookies"} that are non-empty.
        """
        site_cfg = self._site_cfg(site)
        out: dict[str, str] = {}
        for key in ("username", "password", "cookies"):
            val = site_cfg.get(key, "")
            if isinstance(val, str):
                s = val.strip()
                if s:
                    out[key] = s
        return out

    def get_login_required(self, site: str) -> bool:
        """Determine whether the given site requires login.

        Args:
            site: The site key.

        Returns:
            True if the site or general settings define ``login_required``.
            Defaults to False if unspecified.
        """
        site_cfg, general_cfg = self._site_cfg(site), self._gen_cfg()
        cfg = {**general_cfg, **site_cfg}
        return _bool_value(cfg.get("login_required"), False)

    def get_export_fmt(self, site: str) -> list[str]:
        """Return the list of export formats for a given site.

        Args:
            site: The site key.

        Returns:
            A list of enabled export format names. Empty if not configured.
        """
        site_cfg, general_cfg = self._site_cfg(site), self._gen_cfg()
        out = {
            **_mapping(general_cfg.get("output")),
            **_mapping(site_cfg.get("output")),
        }
        fmt = out.get("formats")
        if not isinstance(fmt, list):
            return []
        return [item.strip() for item in fmt if isinstance(item, str) and item.strip()]

    def get_plugins_config(self) -> dict[str, Any]:
        """Return the plugin-related configuration section.

        Returns:
            A mapping with normalized plugin-related configuration.
        """
        plugins_cfg = _mapping(self._config.get("plugins"))
        return {
            "enable_local_plugins": _bool_value(
                plugins_cfg.get("enable_local_plugins"), False
            ),
            "local_plugins_path": _path_value(
                plugins_cfg.get("local_plugins_path"), ""
            ),
            "override_builtins": _bool_value(
                plugins_cfg.get("override_builtins"), False
            ),
        }

    def get_processor_configs(self, site: str) -> list[ProcessorConfig]:
        """Build the list of ProcessorConfig objects for the given site.

        Site-specific processors take precedence over general ones. If the
        site defines any processors, only those are returned. Otherwise, the
        general processor list is used.

        Args:
            site: The site key.

        Returns:
            A list of ProcessorConfig objects in the order they should be run.
        """
        site_cfg, general_cfg = self._site_cfg(site), self._gen_cfg()

        site_rows = site_cfg.get("processors") or []
        site_procs = self._to_processor_cfgs(site_rows)
        general_rows = general_cfg.get("processors") or []
        general_procs = self._to_processor_cfgs(general_rows)

        return site_procs if site_procs else general_procs

    def get_book_ids(self, site: str) -> list[BookConfig]:
        """Extract and normalize the list of target books for the current site.

        Accepted shapes for ``site.book_ids``:
          * a single str or int (book id)
          * a dict with fields: ``book_id`` and optional ``start_id``, ``end_id``,
            ``ignore_ids``
          * a list containing any mix of the above

        Args:
            site: The site key.

        Returns:
            A normalized list of BookConfig objects.

        Raises:
            ValueError: If ``book_ids`` is neither a scalar ``str|int``, dict, list.
        """
        site_cfg = self._site_cfg(site)
        raw = site_cfg.get("book_ids", [])

        if isinstance(raw, (str, int)) and not isinstance(raw, bool):
            return [self._dict_to_book_cfg({"book_id": raw})]

        if isinstance(raw, Mapping):
            return [self._dict_to_book_cfg(dict(raw))]

        if not isinstance(raw, list):
            raise ValueError(
                t("book_ids must be a list or string, got {type}").format(
                    type=type(raw).__name__
                )
            )

        result: list[BookConfig] = []
        for item in raw:
            if isinstance(item, (str, int)) and not isinstance(item, bool):
                result.append(self._dict_to_book_cfg({"book_id": item}))
            elif isinstance(item, Mapping):
                result.append(self._dict_to_book_cfg(dict(item)))
            else:
                raise ValueError(
                    t(
                        "Invalid book_id entry: expected str|int|dict, got {type}"
                    ).format(type=type(item).__name__)
                )
        return result

    def get_log_level(self) -> str:
        """Return the configured logging level.

        The value is taken from ``general.debug.log_level``. If missing,
        ``"INFO"`` is returned.

        Returns:
            The logging level string.
        """
        debug_cfg = _mapping(self._gen_cfg().get("debug"))
        value = _str_value(debug_cfg.get("log_level"), "INFO").strip().upper()
        return value if value in _LOG_LEVELS else "INFO"

    def get_log_dir(self) -> Path:
        """Return the directory used to store log files.

        This reads ``general.debug.log_dir``. Defaults to ``"./logs"``.

        Returns:
            An absolute Path to the log directory.
        """
        debug_cfg = _mapping(self._gen_cfg().get("debug"))
        log_dir = _path_value(debug_cfg.get("log_dir"), "./logs")
        return Path(log_dir).expanduser().resolve()

    def get_cache_dir(self) -> Path:
        """Return the directory used for local resource caching.

        This corresponds to ``general.cache_dir``. When the key is absent the
        default is ``<app_home>/cache`` (the platform user-data directory plus
        ``cache``), so cookies are no longer written into the working
        directory. An explicitly configured relative path is still resolved
        against the current working directory.

        Returns:
            An absolute Path to the cache directory.
        """
        cache_dir = _path_value(self._gen_cfg().get("cache_dir"), default_cache_dir())
        return Path(cache_dir).expanduser().resolve()

    def get_raw_data_dir(self) -> Path:
        """Return the directory used to store raw scraped data.

        This corresponds to ``general.raw_data_dir``. Defaults to
        ``"./raw_data"``.

        Returns:
            An absolute Path for storing raw scraped data.
        """
        raw_data_dir = _path_value(self._gen_cfg().get("raw_data_dir"), "./raw_data")
        return Path(raw_data_dir).expanduser().resolve()

    def get_output_dir(self) -> Path:
        """Return the directory used for final output files.

        This corresponds to ``general.output_dir``. Defaults to
        ``"./downloads"``.

        Returns:
            An absolute Path to the output directory.
        """
        output_dir = _path_value(self._gen_cfg().get("output_dir"), "./downloads")
        return Path(output_dir).expanduser().resolve()

    def _gen_cfg(self) -> dict[str, Any]:
        """Return the global ``general`` configuration mapping.

        Returns:
            The ``general`` dictionary or an empty dict if missing or invalid.
        """
        general = self._config.get("general")
        return dict(general) if isinstance(general, Mapping) else {}

    def _site_cfg(self, site: str) -> dict[str, Any]:
        """Return the configuration block for the given site.

        Args:
            site: The site name.

        Returns:
            A dictionary representing the site's configuration, or an empty
            dict if missing or invalid.
        """
        sites_cfg = self._config.get("sites")
        if not isinstance(sites_cfg, Mapping):
            return {}
        value = sites_cfg.get(site)
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _dict_to_book_cfg(data: dict[str, Any]) -> BookConfig:
        """Convert a raw dict into a BookConfig with normalized types.

        All IDs are coerced to strings.

        Args:
            data: A mapping that must contain at least ``"book_id"``.

        Returns:
            A normalized BookConfig instance.

        Raises:
            ValueError: If ``"book_id"`` is missing.
        """
        if not isinstance(data, Mapping):
            raise ValueError(
                t("book_id entry must be a table, got {type}").format(
                    type=type(data).__name__
                )
            )
        if "book_id" not in data:
            raise ValueError(t("Missing required field 'book_id'"))

        raw_book_id = data["book_id"]
        if isinstance(raw_book_id, bool) or not isinstance(raw_book_id, (str, int)):
            raise ValueError(
                t("book_id must be a string or integer, got {type}").format(
                    type=type(raw_book_id).__name__
                )
            )
        book_id = str(raw_book_id).strip()
        if not book_id:
            raise ValueError(t("Missing required field 'book_id'"))

        start_id = ConfigAdapter._optional_identifier(data.get("start_id"))
        end_id = ConfigAdapter._optional_identifier(data.get("end_id"))

        return BookConfig(
            book_id=book_id,
            start_id=start_id,
            end_id=end_id,
            ignore_ids=ConfigAdapter._ignore_ids(data.get("ignore_ids")),
        )

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        """Convert a scalar id to ``str``; ignore tables/lists/bools/None."""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (str, int)):
            text = str(value).strip()
            return text or None
        return None

    @staticmethod
    def _ignore_ids(value: Any) -> frozenset[str]:
        """Normalize ``ignore_ids`` without iterating arbitrary values.

        A bare string is a single id (``ignore_ids = "42"``), not a sequence
        of characters.  Non-scalar entries are skipped; the validator reports
        them separately.
        """
        if value is None:
            return frozenset()
        if isinstance(value, bool):
            return frozenset()
        if isinstance(value, (str, int)):
            return frozenset({str(value)})
        if isinstance(value, (list, tuple, set, frozenset)):
            return frozenset(
                str(item)
                for item in value
                if isinstance(item, (str, int))
                and not isinstance(item, bool)
                and str(item).strip()
            )
        return frozenset()

    @staticmethod
    def _dict_to_ocr_cfg(data: dict[str, Any]) -> OCRConfig:
        """Convert a raw config dict into an OCRConfig.

        Args:
            data: The raw OCR configuration mapping.

        Returns:
            An OCRConfig instance with normalized values.
        """
        if not isinstance(data, Mapping):
            return OCRConfig()

        return OCRConfig(
            model_name=_optional_str(data.get("model_name")),
            model_dir=_optional_str(data.get("model_dir")),
            input_shape=ConfigAdapter._input_shape(data.get("input_shape")),
            device=_optional_str(data.get("device")),
            precision=_str_value(data.get("precision"), "fp32"),
            cpu_threads=_int_value(data.get("cpu_threads"), 10, minimum=1),
            enable_hpi=_bool_value(data.get("enable_hpi"), False),
        )

    @staticmethod
    def _input_shape(value: Any) -> tuple[int, int, int] | None:
        """Return ``(C, H, W)`` only for three positive integer dimensions."""
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        if not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in value
        ):
            return None
        return (value[0], value[1], value[2])

    @staticmethod
    def _to_processor_cfgs(data: list[dict[str, Any]]) -> list[ProcessorConfig]:
        """Convert a list of raw processor table dicts into ProcessorConfig objects.

        Args:
            data: A list of raw processor configuration mappings.

        Returns:
            A list of ProcessorConfig objects.
        """
        if not isinstance(data, list):
            return []

        result: list[ProcessorConfig] = []
        for row in data:
            if not isinstance(row, Mapping):
                continue

            raw_name = row.get("name")
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip().lower()
            if not name:
                continue

            overwrite = _bool_value(row.get("overwrite"), False)
            # Pass everything else as options.
            opts = {k: v for k, v in row.items() if k not in ("name", "overwrite")}
            result.append(ProcessorConfig(name=name, overwrite=overwrite, options=opts))

        return result
