#!/usr/bin/env python3
"""
novel_downloader.apps.utils
---------------------------
"""

import tomllib
from pathlib import Path
from typing import Any

from novel_downloader.apps.cli import ui
from novel_downloader.infra.config import copy_default_config, load_config
from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import DEFAULT_CONFIG_FILE


def _load_builtin_default_config() -> dict[str, Any] | None:
    """
    Parse the bundled sample configuration without touching the filesystem.

    The sample file is the same document ``copy_default_config`` writes, so a
    non-interactive run continues with exactly the values an interactive user
    would get after accepting the prompt -- minus the side effect of creating
    ``settings.toml`` (which would fail in a read-only working directory).

    :return: The parsed default configuration, or ``None`` when the bundled
        resource cannot be read/parsed (the reason is reported on stderr).
    """
    try:
        raw = DEFAULT_CONFIG_FILE.read_bytes()
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - a broken resource is reported
        ui.error(t("Failed to load configuration: {err}").format(err=str(exc)))
        return None

    if not isinstance(data, dict):  # pragma: no cover - trusted resource shape
        ui.error(t("The configuration root is not a table."))
        return None
    return data


def load_or_init_config(
    config_path: Path | None = None,
    *,
    non_interactive: bool = False,
) -> dict[str, Any] | None:
    """
    Load the configuration, offering to create it only when allowed to ask.

    Interactive callers (the default) keep the historical behaviour: a missing
    file shows the path and asks whether to copy the bundled default into
    place.  Non-interactive callers -- a pipe/CI run or any ``--json`` command
    -- must never block on that question and must never write a file, so they
    continue with the bundled defaults held in memory.

    :param config_path: Optional explicit configuration file.
    :param non_interactive: Whether prompting (and file creation) is forbidden.
    :return: Loaded configuration, the built-in defaults when non-interactive
        and no file exists, or ``None`` when the configuration is unusable.
    """
    try:
        return load_config(config_path)
    except FileNotFoundError:
        if non_interactive:
            target = (
                Path(config_path) if config_path is not None else Path("settings.toml")
            )
            ui.warn(t("No config found at {path}.").format(path=str(target.resolve())))
            return _load_builtin_default_config()

        if config_path is None:
            config_path = Path("settings.toml")

        ui.warn(t("No config found at {path}.").format(path=str(config_path.resolve())))

        if ui.confirm(t("Would you like to create a default config?"), default=True):
            copy_default_config(config_path)
            ui.success(
                t("Created default config at {path}.").format(
                    path=str(config_path.resolve())
                )
            )
        else:
            ui.error(t("Cannot continue without a config file."))
        return None

    except ValueError as e:
        ui.error(t("Failed to load configuration: {err}").format(err=str(e)))
        return None
