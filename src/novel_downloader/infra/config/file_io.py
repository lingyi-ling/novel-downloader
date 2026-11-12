#!/usr/bin/env python3
"""
novel_downloader.infra.config.file_io
-------------------------------------

Utilities to load, validate, and save configuration files.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import DEFAULT_CONFIG_FILE, app_home

from .validation import ConfigError, _display_path, validate_config

logger = logging.getLogger(__name__)

#: Refuse to parse absurdly large configuration files instead of reading an
#: arbitrary file into memory.  Real settings files are a few kilobytes; an
#: 8 MiB cap still leaves room for large hand-written book/rule sections.
MAX_CONFIG_FILE_BYTES = 8 * 1024 * 1024


def _resolve_file_path(
    user_path: str | Path | None,
    local_filename: list[str],
) -> Path | None:
    """
    Resolve the file path to use based on a prioritized lookup order.

    Priority:
      1. A user-specified path (if provided and exists)
      2. A file in the current working directory with the given name
      3. A file in the application home directory (see ``app_home()``)

    :return: Resolved Path or None if not found.
    """
    if user_path:
        path = Path(user_path).expanduser().resolve()
        if path.is_file():
            return path
        logger.warning("Specified file not found: %s", _display_path(path))

    for name in local_filename:
        local_path = (Path.cwd() / name).resolve()
        if local_path.is_file():
            logger.debug("Using local file: %s", _display_path(local_path))
            return local_path

    home = app_home()
    for name in local_filename:
        home_path = (home / name).resolve()
        if home_path.is_file():
            logger.debug("Using home file: %s", _display_path(home_path))
            return home_path

    return None


def _validate_dict(data: Any, path: Path, format: str) -> dict[str, Any]:
    """
    Validate that the parsed config is a dictionary.

    A non-table root is an error rather than an empty configuration: silently
    returning ``{}`` would make the application start with every setting reset
    to its default.

    :param data: The loaded content to validate.
    :param path: Path to the original config file (used for diagnostics).
    :param format: Format name ('json', 'toml', etc.) for context.
    :return: The original data if valid.
    :raises ValueError: If the root is not a mapping.
    """
    if not isinstance(data, dict):
        logger.warning(
            "%s content is not a dictionary: %s",
            format.upper(),
            _display_path(path),
        )
        raise ValueError(
            t("The configuration root is not a table.")
            + f" ({format.upper()}: {_display_path(path)})"
        )
    return data


def _read_config_text(path: Path) -> str:
    """Read a configuration file as UTF-8, with a size cap and BOM support.

    :raises ValueError: When the file is too large or not valid UTF-8.
    :raises OSError: When the file cannot be read.
    """
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            t("Configuration file is too large: {path} ({size} bytes).").format(
                path=_display_path(path), size=size
            )
        )

    with path.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_FILE_BYTES + 1)
    if len(raw) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            t("Configuration file is too large: {path} ({size} bytes).").format(
                path=_display_path(path), size=len(raw)
            )
        )
    try:
        # ``utf-8-sig`` tolerates a BOM (common after a Windows editor round
        # trip) and decodes CRLF unchanged.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        byte = raw[exc.start] if exc.start < len(raw) else 0
        raise ValueError(
            t(
                "Configuration file is not valid UTF-8: {path} "
                "(byte 0x{byte:02x} at position {position})."
            ).format(
                path=_display_path(path),
                byte=byte,
                position=exc.start,
            )
        ) from exc


def _read_text(path: Path) -> str:
    """Read a config file, converting OS errors into a path-safe ValueError."""
    try:
        return _read_config_text(path)
    except OSError as exc:
        raise ValueError(
            t("Could not read {path}: {err}").format(
                path=_display_path(path),
                err=exc.strerror or type(exc).__name__,
            )
        ) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(
                t("Duplicate key '{key}' in JSON configuration.").format(key=key)
            )
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> Any:
    raise ValueError(t("Invalid JSON number: {value}.").format(value=value))


def _load_by_extension(path: Path) -> dict[str, Any]:
    """
    Load a configuration file by its file extension.

    Supports: .toml, .json

    :param path: Path to the configuration file.
    :return: Parsed configuration as a dictionary.
    :raises ValueError: If the file extension is unsupported or parsing fails.
    """
    ext = path.suffix.lower()

    if ext == ".json":
        text = _read_text(path)
        try:
            data = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite_constant,
            )
        except Exception as e:
            raise ValueError(
                t("Invalid JSON in {path}: {err}").format(
                    path=_display_path(path), err=e
                )
            ) from e
        return _validate_dict(data, path, "json")

    elif ext == ".toml":
        text = _read_text(path)
        try:
            import tomllib

            data = tomllib.loads(text)
        except Exception as e:
            raise ValueError(
                t("Invalid TOML in {path}: {err}").format(
                    path=_display_path(path), err=e
                )
            ) from e
        return _validate_dict(data, path, "toml")

    raise ValueError(t("Unsupported config file extension: {ext}").format(ext=ext))


def load_config(
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Load configuration data from a Toml file.

    :param config_path: Optional path to the Toml configuration file.
    :return: Parsed configuration as a dict.
    :raises FileNotFoundError: If no viable config is found.
    :raises ValueError: If parsing fails.
    """
    path = _resolve_file_path(
        user_path=config_path,
        local_filename=["settings.toml", "settings.json"],
    )

    if not path or not path.is_file():
        raise FileNotFoundError("No valid config file found.")

    logger.debug("Loading configuration from: %s", _display_path(path))
    data = _load_by_extension(path)
    _log_validation_problems(data)
    return data


def _log_validation_problems(data: dict[str, Any]) -> None:
    """
    Validate ``data`` and log findings without aborting the load.

    Validation is advisory for backward compatibility: old configuration
    files must keep loading even when they contain unknown or deprecated
    keys.  Any unexpected validator error is therefore swallowed and only
    reported at debug level.

    :class:`~.validation.ConfigError` is the one exception.  It is raised only
    when the user opted into ``general.strict_config``; swallowing it here
    would silently disable the mode they just asked for.
    """
    try:
        problems = validate_config(data)
    except ConfigError:
        raise
    except Exception:  # pragma: no cover - defensive: never break loading
        logger.debug("Configuration validation failed", exc_info=True)
        return

    for problem in problems:
        if problem.level == "info":
            # Informational notes (e.g. the legacy cache directory hint) must
            # not look like configuration errors in the log.
            logger.info("Configuration note: %s", problem)
        else:
            logger.warning("Configuration problem: %s", problem)


def get_config_value(keys: list[str], default: Any) -> Any:
    """
    Safely retrieve a nested config value from the current config.
    """
    cur = load_config()
    for i, k in enumerate(keys):
        if not isinstance(cur, dict):
            return default
        if i == len(keys) - 1:
            val = cur.get(k, default)
            return val if isinstance(val, type(default)) else default
        cur = cur.get(k, {})
    return default


def _write_bytes_atomic(target: Path, data: bytes) -> None:
    """Replace ``target`` atomically, keeping the original on any failure.

    The temporary file lives in the target directory so ``os.replace`` is
    atomic on every supported filesystem.  A failed write (read-only file,
    full disk, permissions) leaves the previous file byte-for-byte intact and
    removes the temporary file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Keep an existing read-only/ACL mode rather than silently granting
        # broader permissions to the replacement file.  Best effort: some
        # filesystems do not support chmod, and the data write already
        # succeeded, so a failure here must not destroy it.
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(os.stat(target).st_mode)
            os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


def copy_default_config(target: Path) -> None:
    """
    Copy the bundled default config to the given target path.

    The replacement is atomic: an existing configuration is never truncated
    and concurrent init calls cannot observe a half-written file.
    """
    target = Path(target)
    data = DEFAULT_CONFIG_FILE.read_bytes()
    _write_bytes_atomic(target, data)
