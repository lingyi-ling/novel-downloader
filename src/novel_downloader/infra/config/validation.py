#!/usr/bin/env python3
"""
novel_downloader.infra.config.validation
----------------------------------------

Offline validation and migration helpers for ``settings.toml`` style
configuration mappings.

The validator is deliberately *non-fatal*: unknown keys, deprecated keys and
out-of-range values are reported as :class:`Problem` objects instead of raising
exceptions.  ``load_config`` consumes these problems and only logs them, which
keeps existing configuration files working after upgrades.

The rules below are derived from the documented contract in
``docs/guide/settings-reference.md`` and from the keys actually read by
``ConfigAdapter`` (``infra/config/adapter.py``).
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

__all__ = [
    "DEFAULT_DOWNLOAD_RETRY_TIMES",
    "Problem",
    "migrate_config",
    "validate_config",
]

ProblemLevel = Literal["error", "warning", "info"]

#: Mirrors ``novel_downloader.apps.core.task_manager.DEFAULT_MAX_DOWNLOAD_RETRIES``.
#: Kept here (instead of importing the application layer) so ``infra`` stays
#: independent from ``apps``; the value is asserted by the config tests.
DEFAULT_DOWNLOAD_RETRY_TIMES = 2

_ROOT_KEYS = frozenset({"general", "sites", "plugins"})

_GENERAL_KEYS = frozenset(
    {
        "raw_data_dir",
        "output_dir",
        "cache_dir",
        "request_interval",
        "workers",
        "max_connections",
        "max_rps",
        "retry_times",
        "backoff_factor",
        "timeout",
        "storage_batch_size",
        "cache_book_info",
        "cache_chapter",
        "fetch_inaccessible",
        "backend",
        "locale_style",
        "login_required",
        "http2",
        "impersonate",
        "verify_ssl",
        "user_agent",
        "headers",
        "proxy",
        "proxy_user",
        "proxy_pass",
        "trust_env",
        # Sites whose search needs a real browser (read by
        # apps/browser/settings.py).
        "browser_assisted_sites",
        "parser",
        "output",
        "debug",
        "processors",
        # Read by apps/core/task_manager.py (undocumented, but supported).
        "download_retry_times",
    }
)

_PARSER_KEYS = frozenset(
    {
        "enable_ocr",
        "batch_size",
        "remove_watermark",
        "model_name",
        "model_dir",
        "input_shape",
        "device",
        "precision",
        "cpu_threads",
        "enable_hpi",
        "use_truncation",
        "cut_mode",
    }
)

_OUTPUT_KEYS = frozenset(
    {
        "formats",
        "render_missing_chapter",
        "append_timestamp",
        "filename_template",
        "include_picture",
        "split_mode",
    }
)

_DEBUG_KEYS = frozenset({"save_html", "log_level", "log_dir"})

_PLUGINS_KEYS = frozenset(
    {"enable_local_plugins", "override_builtins", "local_plugins_path"}
)

_SITE_KEYS = frozenset(
    {
        "book_ids",
        "login_required",
        "username",
        "password",
        "cookies",
        "request_interval",
        "retry_times",
        "backoff_factor",
        "timeout",
        "max_connections",
        "max_rps",
        "workers",
        "storage_batch_size",
        "cache_book_info",
        "cache_chapter",
        "fetch_inaccessible",
        "backend",
        "locale_style",
        "http2",
        "impersonate",
        "verify_ssl",
        "user_agent",
        "headers",
        "proxy",
        "proxy_user",
        "proxy_pass",
        "trust_env",
        "parser",
        "output",
        "processors",
    }
)

_BACKENDS = ("aiohttp", "httpx", "curl_cffi")
_LOCALE_STYLES = ("simplified", "traditional")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_FORMATS = ("epub", "html", "txt")
_SPLIT_MODES = ("book", "volume", "chapter")
_BOOK_KEYS = frozenset({"book_id", "start_id", "end_id", "ignore_ids"})


@dataclass(frozen=True)
class Problem:
    """A single validation finding.

    Attributes:
        level: ``"error"`` for values that cannot work at runtime,
            ``"warning"`` for ignored/unknown/deprecated values and
            ``"info"`` for purely informational notes.
        path: Dotted location inside the configuration mapping, e.g.
            ``"general.timeout"`` or ``"sites.qidian.book_ids[0].book_id"``.
        message: Human-readable description of the problem.
        fix_hint: Short actionable suggestion, empty when there is none.
    """

    level: ProblemLevel
    path: str
    message: str
    fix_hint: str = ""

    def __str__(self) -> str:
        hint = f" Hint: {self.fix_hint}" if self.fix_hint else ""
        return f"[{self.level}] {self.path}: {self.message}{hint}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _join(prefix: str, key: str | int) -> str:
    """Join a parent path with a key (or list index)."""
    if isinstance(key, int):
        return f"{prefix}[{key}]"
    return f"{prefix}.{key}" if prefix else key


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _type_problem(
    problems: list[Problem],
    path: str,
    expected: str,
    value: Any,
) -> None:
    key = path.rsplit(".", 1)[-1]
    problems.append(
        Problem(
            "error",
            path,
            f"expected {expected}, got {type(value).__name__}.",
            f"Set '{key}' to a {expected} value.",
        )
    )


def _unknown_key(
    problems: list[Problem],
    path: str,
    key: str,
    allowed: frozenset[str] | set[str] | tuple[str, ...],
) -> None:
    matches = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
    hint = (
        f"Did you mean '{matches[0]}'?"
        if matches
        else "Remove the key or check docs/guide/settings-reference.md."
    )
    problems.append(Problem("warning", path, "unknown configuration key.", hint))


def _validate_unknown_keys(
    mapping: Mapping[str, Any],
    allowed: frozenset[str] | set[str] | tuple[str, ...],
    prefix: str,
    problems: list[Problem],
    *,
    extra_allowed: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
) -> None:
    for key in mapping:
        if not isinstance(key, str):
            problems.append(
                Problem(
                    "error",
                    prefix or "<root>",
                    "configuration keys must be strings.",
                    "Use quoted keys in the configuration file.",
                )
            )
            continue
        if key not in allowed and key not in extra_allowed:
            _unknown_key(problems, _join(prefix, key), key, allowed)


def _check_bool(
    mapping: Mapping[str, Any], key: str, prefix: str, problems: list[Problem]
) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    if not isinstance(value, bool):
        _type_problem(problems, _join(prefix, key), "boolean (true/false)", value)


def _check_int(
    mapping: Mapping[str, Any],
    key: str,
    prefix: str,
    problems: list[Problem],
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    path = _join(prefix, key)
    if not _is_int(value):
        _type_problem(problems, path, "integer", value)
        return
    if minimum is not None and value < minimum:
        problems.append(
            Problem(
                "error",
                path,
                f"must be >= {minimum}, got {value}.",
                f"Use an integer >= {minimum}.",
            )
        )
    if maximum is not None and value > maximum:
        problems.append(
            Problem(
                "error",
                path,
                f"must be <= {maximum}, got {value}.",
                f"Use an integer <= {maximum}.",
            )
        )


def _check_number(
    mapping: Mapping[str, Any],
    key: str,
    prefix: str,
    problems: list[Problem],
    *,
    minimum: float | None = None,
    exclusive_minimum: bool = False,
) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    path = _join(prefix, key)
    if not _is_number(value):
        _type_problem(problems, path, "number", value)
        return
    number = float(value)
    if minimum is not None:
        too_small = number <= minimum if exclusive_minimum else number < minimum
        if too_small:
            operator = ">" if exclusive_minimum else ">="
            problems.append(
                Problem(
                    "error",
                    path,
                    f"must be {operator} {minimum:g}, got {number:g}.",
                    f"Use a value {operator} {minimum:g}.",
                )
            )


def _check_str(
    mapping: Mapping[str, Any],
    key: str,
    prefix: str,
    problems: list[Problem],
    *,
    optional: bool = False,
    allow_empty: bool = False,
) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    if value is None and optional:
        return
    path = _join(prefix, key)
    if not isinstance(value, str):
        expected = "string or null" if optional else "string"
        _type_problem(problems, path, expected, value)
        return
    if not allow_empty and not value.strip():
        problems.append(
            Problem(
                "error",
                path,
                "must not be empty.",
                "Provide a non-empty string or remove the key.",
            )
        )


def _check_enum(
    mapping: Mapping[str, Any],
    key: str,
    prefix: str,
    problems: list[Problem],
    allowed: tuple[str, ...],
    *,
    level: ProblemLevel = "error",
) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    path = _join(prefix, key)
    if not isinstance(value, str):
        _type_problem(problems, path, "string", value)
        return
    if value not in allowed:
        problems.append(
            Problem(
                level,
                path,
                f"unsupported value '{value}'.",
                f"Use one of: {', '.join(allowed)}.",
            )
        )


def _check_headers(
    mapping: Mapping[str, Any], key: str, prefix: str, problems: list[Problem]
) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    path = _join(prefix, key)
    if value is None:
        return
    if not isinstance(value, Mapping):
        _type_problem(problems, path, "table of strings", value)
        return
    for header, header_value in value.items():
        if not isinstance(header, str) or not isinstance(header_value, str):
            problems.append(
                Problem(
                    "error",
                    path,
                    "HTTP headers must map strings to strings.",
                    'Use e.g. headers = { "User-Agent" = "..." }.',
                )
            )
            return


def _check_directory(
    mapping: Mapping[str, Any], key: str, prefix: str, problems: list[Problem]
) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    path = _join(prefix, key)
    if not isinstance(value, str):
        _type_problem(problems, path, "string path", value)
        return
    raw = value.strip()
    if not raw:
        problems.append(
            Problem(
                "error",
                path,
                "must not be empty.",
                "Provide a writable directory path.",
            )
        )
        return

    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    # Keep the unresolved path on failure; the checks below still report a
    # useful problem instead of raising.
    with contextlib.suppress(OSError):
        target = target.resolve()

    if target.exists():
        if not target.is_dir():
            problems.append(
                Problem(
                    "error",
                    path,
                    f"'{target}' exists but is not a directory.",
                    "Point this setting at a directory, not a file.",
                )
            )
        elif not os.access(target, os.W_OK | os.X_OK):
            problems.append(
                Problem(
                    "error",
                    path,
                    f"directory '{target}' is not writable.",
                    "Choose a writable directory or fix its permissions.",
                )
            )
        return

    ancestor = target
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.exists() or not ancestor.is_dir():
        problems.append(
            Problem(
                "error",
                path,
                f"cannot create '{target}': parent directory does not exist.",
                "Create the parent directory first or use an existing path.",
            )
        )
    elif not os.access(ancestor, os.W_OK | os.X_OK):
        problems.append(
            Problem(
                "error",
                path,
                f"cannot create '{target}': '{ancestor}' is not writable.",
                "Choose a writable directory or fix its permissions.",
            )
        )


# ---------------------------------------------------------------------------
# Section validators
# ---------------------------------------------------------------------------


def _validate_parser(
    parser: Mapping[str, Any], prefix: str, problems: list[Problem]
) -> None:
    _validate_unknown_keys(parser, _PARSER_KEYS, prefix, problems)

    _check_bool(parser, "enable_ocr", prefix, problems)
    _check_int(parser, "batch_size", prefix, problems, minimum=1)
    _check_bool(parser, "remove_watermark", prefix, problems)
    _check_str(parser, "model_name", prefix, problems, optional=True)
    _check_str(parser, "model_dir", prefix, problems, optional=True)
    _check_str(parser, "device", prefix, problems, optional=True)
    _check_str(parser, "precision", prefix, problems)
    _check_int(parser, "cpu_threads", prefix, problems, minimum=1)
    _check_bool(parser, "enable_hpi", prefix, problems)
    _check_bool(parser, "use_truncation", prefix, problems)
    _check_str(parser, "cut_mode", prefix, problems)

    if "input_shape" in parser:
        shape = parser["input_shape"]
        shape_path = _join(prefix, "input_shape")
        if shape is not None:
            if not isinstance(shape, (list, tuple)) or len(shape) != 3:
                problems.append(
                    Problem(
                        "error",
                        shape_path,
                        "must contain exactly three integers (C, H, W).",
                        "Use e.g. input_shape = [3, 32, 320].",
                    )
                )
            else:
                for index, dimension in enumerate(shape):
                    if not _is_int(dimension) or dimension <= 0:
                        problems.append(
                            Problem(
                                "error",
                                _join(shape_path, index),
                                f"expected a positive integer, got {dimension!r}.",
                                "Use positive integers for C, H and W.",
                            )
                        )


def _validate_output(
    output: Mapping[str, Any], prefix: str, problems: list[Problem]
) -> None:
    _validate_unknown_keys(output, _OUTPUT_KEYS, prefix, problems)

    _check_bool(output, "render_missing_chapter", prefix, problems)
    _check_bool(output, "append_timestamp", prefix, problems)
    _check_str(output, "filename_template", prefix, problems)
    _check_bool(output, "include_picture", prefix, problems)
    _check_enum(output, "split_mode", prefix, problems, _SPLIT_MODES, level="warning")

    if "formats" in output:
        formats = output["formats"]
        formats_path = _join(prefix, "formats")
        if not isinstance(formats, (list, tuple)):
            _type_problem(problems, formats_path, "list of strings", formats)
            return
        for index, fmt in enumerate(formats):
            fmt_path = _join(formats_path, index)
            if not isinstance(fmt, str) or not fmt.strip():
                problems.append(
                    Problem(
                        "error",
                        fmt_path,
                        "expected a non-empty format name.",
                        'Use e.g. formats = ["epub", "txt"].',
                    )
                )
            elif fmt not in _FORMATS:
                problems.append(
                    Problem(
                        "warning",
                        fmt_path,
                        f"unsupported export format '{fmt}'.",
                        f"Use one of: {', '.join(_FORMATS)} (or install a "
                        "plugin exporter that provides it).",
                    )
                )


def _validate_debug(
    debug: Mapping[str, Any], prefix: str, problems: list[Problem]
) -> None:
    _validate_unknown_keys(debug, _DEBUG_KEYS, prefix, problems)
    _check_bool(debug, "save_html", prefix, problems)
    _check_str(debug, "log_level", prefix, problems)
    _check_enum(debug, "log_level", prefix, problems, _LOG_LEVELS, level="warning")
    _check_directory(debug, "log_dir", prefix, problems)


def _validate_processors(value: Any, path: str, problems: list[Problem]) -> None:
    if not isinstance(value, list):
        _type_problem(problems, path, "list of processor tables", value)
        return
    for index, row in enumerate(value):
        row_path = _join(path, index)
        if not isinstance(row, Mapping):
            _type_problem(problems, row_path, "processor table", row)
            continue
        name = row.get("name")
        if "name" not in row:
            problems.append(
                Problem(
                    "error",
                    row_path,
                    "missing required field 'name'.",
                    'Add e.g. name = "cleaner".',
                )
            )
        elif not isinstance(name, str) or not name.strip():
            problems.append(
                Problem(
                    "error",
                    _join(row_path, "name"),
                    "must be a non-empty string.",
                    'Use e.g. name = "cleaner".',
                )
            )
        if "overwrite" in row and not isinstance(row["overwrite"], bool):
            _type_problem(
                problems,
                _join(row_path, "overwrite"),
                "boolean (true/false)",
                row["overwrite"],
            )


def _validate_book_entry(
    entry: Mapping[str, Any], path: str, problems: list[Problem]
) -> None:
    _validate_unknown_keys(entry, _BOOK_KEYS, path, problems)

    if "book_id" not in entry:
        problems.append(
            Problem(
                "error",
                path,
                "missing required field 'book_id'.",
                'Add e.g. book_id = "123456".',
            )
        )
    else:
        book_id = entry["book_id"]
        if not isinstance(book_id, (str, int)) or isinstance(book_id, bool):
            _type_problem(
                problems, _join(path, "book_id"), "string or integer", book_id
            )
        elif isinstance(book_id, str) and not book_id.strip():
            problems.append(
                Problem(
                    "error",
                    _join(path, "book_id"),
                    "must not be empty.",
                    "Provide the book identifier.",
                )
            )

    for field in ("start_id", "end_id"):
        if field not in entry:
            continue
        value = entry[field]
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            _type_problem(problems, _join(path, field), "string or integer", value)

    if "ignore_ids" in entry:
        ignore_ids = entry["ignore_ids"]
        ignore_path = _join(path, "ignore_ids")
        if not isinstance(ignore_ids, (list, tuple)):
            _type_problem(problems, ignore_path, "list of strings/integers", ignore_ids)
        else:
            for index, item in enumerate(ignore_ids):
                if not isinstance(item, (str, int)) or isinstance(item, bool):
                    _type_problem(
                        problems,
                        _join(ignore_path, index),
                        "string or integer",
                        item,
                    )


def _validate_book_ids(value: Any, path: str, problems: list[Problem]) -> None:
    if isinstance(value, (bool, float)):
        _type_problem(
            problems,
            path,
            "string, integer, table or list",
            value,
        )
        return
    if isinstance(value, (str, int)):
        if isinstance(value, str) and not value.strip():
            problems.append(
                Problem(
                    "error",
                    path,
                    "book id must not be empty.",
                    "Provide the book identifier.",
                )
            )
        return
    if isinstance(value, Mapping):
        _validate_book_entry(cast(Mapping[str, Any], value), path, problems)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            item_path = _join(path, index)
            if isinstance(item, (str, int)) and not isinstance(item, bool):
                if isinstance(item, str) and not item.strip():
                    problems.append(
                        Problem(
                            "error",
                            item_path,
                            "book id must not be empty.",
                            "Provide the book identifier.",
                        )
                    )
            elif isinstance(item, Mapping):
                _validate_book_entry(cast(Mapping[str, Any], item), item_path, problems)
            else:
                _type_problem(
                    problems,
                    item_path,
                    "string, integer or table",
                    item,
                )
        return
    _type_problem(
        problems,
        path,
        "string, integer, table or list",
        value,
    )


def _validate_site(
    site: Mapping[str, Any], prefix: str, problems: list[Problem]
) -> None:
    _validate_unknown_keys(
        site,
        _SITE_KEYS,
        prefix,
        problems,
        extra_allowed=frozenset({"cookie", "use_truncation"}),
    )

    # Deprecated / misplaced keys (they are ignored by ConfigAdapter).
    if "cookie" in site:
        problems.append(
            Problem(
                "warning",
                _join(prefix, "cookie"),
                "'cookie' is not read by the loader (supported key is 'cookies').",
                "Rename it to 'cookies'; migrate_config() does this automatically.",
            )
        )
        _check_str(site, "cookie", prefix, problems)
    if "use_truncation" in site:
        problems.append(
            Problem(
                "warning",
                _join(prefix, "use_truncation"),
                "site-level 'use_truncation' is ignored; it belongs to "
                "the parser table.",
                f"Move it to {prefix}.parser.use_truncation.",
            )
        )
        _check_bool(site, "use_truncation", prefix, problems)

    _check_number(site, "request_interval", prefix, problems, minimum=0.0)
    _check_int(site, "retry_times", prefix, problems, minimum=0)
    _check_number(site, "backoff_factor", prefix, problems, minimum=0.0)
    _check_number(
        site, "timeout", prefix, problems, minimum=0.0, exclusive_minimum=True
    )
    _check_int(site, "max_connections", prefix, problems, minimum=1)
    _check_number(site, "max_rps", prefix, problems, minimum=0.0)
    _check_int(site, "workers", prefix, problems, minimum=1)
    _check_int(site, "storage_batch_size", prefix, problems, minimum=1)
    _check_bool(site, "cache_book_info", prefix, problems)
    _check_bool(site, "cache_chapter", prefix, problems)
    _check_bool(site, "fetch_inaccessible", prefix, problems)
    _check_bool(site, "login_required", prefix, problems)
    _check_bool(site, "http2", prefix, problems)
    _check_bool(site, "verify_ssl", prefix, problems)
    _check_bool(site, "trust_env", prefix, problems)
    _check_str(site, "impersonate", prefix, problems, optional=True)
    _check_str(site, "user_agent", prefix, problems, optional=True)
    _check_str(site, "proxy", prefix, problems, optional=True)
    _check_str(site, "proxy_user", prefix, problems, optional=True)
    _check_str(site, "proxy_pass", prefix, problems, optional=True)
    _check_str(site, "username", prefix, problems, optional=True, allow_empty=True)
    _check_str(site, "password", prefix, problems, optional=True, allow_empty=True)
    _check_str(site, "cookies", prefix, problems, optional=True, allow_empty=True)
    _check_headers(site, "headers", prefix, problems)
    _check_enum(site, "backend", prefix, problems, _BACKENDS)
    _check_enum(site, "locale_style", prefix, problems, _LOCALE_STYLES)

    parser = site.get("parser")
    if "parser" in site:
        if isinstance(parser, Mapping):
            _validate_parser(
                cast(Mapping[str, Any], parser), _join(prefix, "parser"), problems
            )
        else:
            _type_problem(problems, _join(prefix, "parser"), "table", parser)

    output = site.get("output")
    if "output" in site:
        if isinstance(output, Mapping):
            _validate_output(
                cast(Mapping[str, Any], output), _join(prefix, "output"), problems
            )
        else:
            _type_problem(problems, _join(prefix, "output"), "table", output)

    if "processors" in site:
        _validate_processors(site["processors"], _join(prefix, "processors"), problems)

    if "book_ids" in site:
        _validate_book_ids(site["book_ids"], _join(prefix, "book_ids"), problems)


def _validate_general(general: Mapping[str, Any], problems: list[Problem]) -> None:
    prefix = "general"
    _validate_unknown_keys(general, _GENERAL_KEYS, prefix, problems)

    _check_directory(general, "raw_data_dir", prefix, problems)
    _check_directory(general, "output_dir", prefix, problems)
    _check_directory(general, "cache_dir", prefix, problems)

    _check_number(general, "request_interval", prefix, problems, minimum=0.0)
    _check_int(general, "workers", prefix, problems, minimum=1)
    _check_int(general, "max_connections", prefix, problems, minimum=1)
    _check_number(general, "max_rps", prefix, problems, minimum=0.0)
    _check_int(general, "retry_times", prefix, problems, minimum=0)
    _check_number(general, "backoff_factor", prefix, problems, minimum=0.0)
    _check_number(
        general, "timeout", prefix, problems, minimum=0.0, exclusive_minimum=True
    )
    _check_int(general, "storage_batch_size", prefix, problems, minimum=1)
    _check_int(general, "download_retry_times", prefix, problems, minimum=0)

    _check_bool(general, "cache_book_info", prefix, problems)
    _check_bool(general, "cache_chapter", prefix, problems)
    _check_bool(general, "fetch_inaccessible", prefix, problems)
    _check_bool(general, "login_required", prefix, problems)
    _check_bool(general, "http2", prefix, problems)
    _check_bool(general, "verify_ssl", prefix, problems)
    _check_bool(general, "trust_env", prefix, problems)

    _check_str(general, "impersonate", prefix, problems, optional=True)
    _check_str(general, "user_agent", prefix, problems, optional=True)
    _check_str(general, "proxy", prefix, problems, optional=True)
    _check_str(general, "proxy_user", prefix, problems, optional=True)
    _check_str(general, "proxy_pass", prefix, problems, optional=True)
    _check_headers(general, "headers", prefix, problems)
    _check_enum(general, "backend", prefix, problems, _BACKENDS)
    _check_enum(general, "locale_style", prefix, problems, _LOCALE_STYLES)

    parser = general.get("parser")
    if "parser" in general:
        if isinstance(parser, Mapping):
            _validate_parser(
                cast(Mapping[str, Any], parser), _join(prefix, "parser"), problems
            )
        else:
            _type_problem(problems, _join(prefix, "parser"), "table", parser)

    output = general.get("output")
    if "output" in general:
        if isinstance(output, Mapping):
            _validate_output(
                cast(Mapping[str, Any], output), _join(prefix, "output"), problems
            )
        else:
            _type_problem(problems, _join(prefix, "output"), "table", output)

    debug = general.get("debug")
    if "debug" in general:
        if isinstance(debug, Mapping):
            _validate_debug(
                cast(Mapping[str, Any], debug), _join(prefix, "debug"), problems
            )
        else:
            _type_problem(problems, _join(prefix, "debug"), "table", debug)

    if "processors" in general:
        _validate_processors(
            general["processors"], _join(prefix, "processors"), problems
        )


def _validate_plugins(plugins: Mapping[str, Any], problems: list[Problem]) -> None:
    prefix = "plugins"
    _validate_unknown_keys(plugins, _PLUGINS_KEYS, prefix, problems)
    _check_bool(plugins, "enable_local_plugins", prefix, problems)
    _check_bool(plugins, "override_builtins", prefix, problems)
    _check_str(plugins, "local_plugins_path", prefix, problems, allow_empty=True)
    enabled = plugins.get("enable_local_plugins")
    if enabled is True:
        _check_directory(plugins, "local_plugins_path", prefix, problems)


def _validate_root(data: Mapping[str, Any], problems: list[Problem]) -> None:
    _validate_unknown_keys(data, _ROOT_KEYS, "", problems)

    general = data.get("general")
    if "general" in data:
        if isinstance(general, Mapping):
            _validate_general(cast(Mapping[str, Any], general), problems)
        else:
            _type_problem(problems, "general", "table", general)

    sites = data.get("sites")
    if "sites" in data:
        if isinstance(sites, Mapping):
            for site_name, site_cfg in sites.items():
                if not isinstance(site_name, str) or not site_name.strip():
                    problems.append(
                        Problem(
                            "error",
                            "sites",
                            "site names must be non-empty strings.",
                            "Use e.g. [sites.qidian].",
                        )
                    )
                    continue
                site_path = _join("sites", site_name)
                if isinstance(site_cfg, Mapping):
                    _validate_site(
                        cast(Mapping[str, Any], site_cfg), site_path, problems
                    )
                else:
                    _type_problem(problems, site_path, "table", site_cfg)
        else:
            _type_problem(problems, "sites", "table", sites)

    plugins = data.get("plugins")
    if "plugins" in data:
        if isinstance(plugins, Mapping):
            _validate_plugins(cast(Mapping[str, Any], plugins), problems)
        else:
            _type_problem(problems, "plugins", "table", plugins)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_config(
    path_or_dict: str | Path | Mapping[str, Any],
) -> list[Problem]:
    """Validate a configuration mapping or a config file path.

    The function never raises for malformed configuration: parse errors and
    missing files are returned as ``error`` problems.  It performs no network
    access, so it is safe to use in offline tests.

    Args:
        path_or_dict: A mapping already loaded from TOML/JSON, or a path to a
            ``.toml``/``.json`` file.

    Returns:
        A list of :class:`Problem` objects.  An empty list means the
        configuration is valid.
    """
    if isinstance(path_or_dict, (str, Path)):
        path = Path(path_or_dict).expanduser()
        try:
            if not path.is_file():
                return [
                    Problem(
                        "error",
                        "<file>",
                        f"configuration file not found: {path}",
                        "Check the path passed to --config.",
                    )
                ]
            # Imported lazily to avoid a circular import with file_io.
            from .file_io import _load_by_extension

            data: Any = _load_by_extension(path)
        except (OSError, ValueError) as exc:
            return [
                Problem(
                    "error",
                    "<file>",
                    str(exc),
                    "Fix the file syntax or use a .toml/.json file.",
                )
            ]
    else:
        data = path_or_dict

    if not isinstance(data, Mapping):
        return [
            Problem(
                "error",
                "<root>",
                "configuration root must be a table/mapping.",
                "Make sure the file contains key/value pairs.",
            )
        ]

    problems: list[Problem] = []
    _validate_root(cast(Mapping[str, Any], data), problems)
    return problems


def migrate_config(data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Migrate a configuration mapping to the current key layout.

    Only transformations with evidence in the repository are performed:

    * ``sites.<site>.cookie`` -> ``sites.<site>.cookies``
      (the documentation mentions ``cookie``, while ``ConfigAdapter`` reads
      ``cookies``).
    * ``sites.<site>.use_truncation`` -> ``sites.<site>.parser.use_truncation``
      (the documented site-level example is ignored by ``ConfigAdapter``).
    * ``general.download_retry_times`` is filled with
      :data:`DEFAULT_DOWNLOAD_RETRY_TIMES` when a ``[general]`` table exists.

    The function is idempotent: running it twice yields the same data and an
    empty change list on the second call.  The input mapping is not mutated.

    Args:
        data: Raw configuration mapping.

    Returns:
        ``(migrated, changes)`` where ``migrated`` is a deep copy of ``data``
        with migrations applied and ``changes`` is a list of human-readable
        descriptions (empty when nothing was changed).
    """
    if not isinstance(data, Mapping):
        return {}, ["input is not a mapping; nothing to migrate."]

    migrated: dict[str, Any] = copy.deepcopy(dict(data))
    changes: list[str] = []

    sites = migrated.get("sites")
    if isinstance(sites, dict):
        for site_name in sorted(sites, key=str):
            site = sites.get(site_name)
            if isinstance(site, dict):
                _migrate_site(cast(dict[str, Any], site), f"sites.{site_name}", changes)

    general = migrated.get("general")
    if isinstance(general, dict) and "download_retry_times" not in general:
        general["download_retry_times"] = DEFAULT_DOWNLOAD_RETRY_TIMES
        changes.append(
            "general.download_retry_times not set; filled default "
            f"{DEFAULT_DOWNLOAD_RETRY_TIMES}"
        )

    return migrated, changes


def _migrate_site(site: dict[str, Any], prefix: str, changes: list[str]) -> None:
    if "cookie" in site and "cookies" not in site:
        site["cookies"] = site.pop("cookie")
        changes.append(f"{prefix}.cookie -> {prefix}.cookies")

    if "use_truncation" not in site:
        return

    parser = site.get("parser")
    if parser is None and "parser" not in site:
        parser = {}
        site["parser"] = parser
    if isinstance(parser, dict) and "use_truncation" not in parser:
        parser["use_truncation"] = site.pop("use_truncation")
        changes.append(f"{prefix}.use_truncation -> {prefix}.parser.use_truncation")
