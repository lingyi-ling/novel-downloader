#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.doctor
-----------------------------------------

Environment self-check (``novel-cli doctor``).

Every check is offline. The output-directory probe may create the configured
output directory and writes/removes a temporary file inside it; nothing else is
modified. Each row is printed as ``✓`` or ``✗``; failing rows carry a short,
actionable fix. Optional capabilities (the music extra) are reported but do not
make the command fail.

Exit codes: ``0`` when every required check passes, ``1`` otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import tempfile
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import Any

from novel_downloader.apps.cli import ui
from novel_downloader.apps.constants import DOWNLOAD_SUPPORT_SITES
from novel_downloader.infra.config import ConfigAdapter, load_config
from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import app_home
from novel_downloader.plugins import registrar

from .base import EXIT_FAILURE, Command

#: Minimum Python version supported by the project (``requires-python``).
MIN_PYTHON = (3, 11)

#: Default port used by ``novel-api``.
API_PORT = 8081

#: Optional dependencies required by ``novel-cli music``.
MUSIC_DEPENDENCIES = ("mutagen", "qrcode", "websocket")

#: Candidate configuration filenames, in loader priority order.
_SETTINGS_FILENAMES = ("settings.toml", "settings.json")


@dataclass
class Check:
    """One doctor check result."""

    name: str
    ok: bool
    detail: str = ""
    suggestion: str = ""
    #: Optional failures are reported but never fail the command.
    fatal: bool = True


def _settings_path() -> Path | None:
    """Return the first configuration file the loader would use, if any."""
    for name in _SETTINGS_FILENAMES:
        local = Path.cwd() / name
        if local.is_file():
            return local
    home = app_home()
    for name in _SETTINGS_FILENAMES:
        candidate = home / name
        if candidate.is_file():
            return candidate
    return None


def _load_config_for_checks() -> dict[str, Any] | None:
    """Load the configuration, returning ``None`` when it cannot be read."""
    path = _settings_path()
    try:
        return load_config(path) if path is not None else load_config()
    except Exception:
        return None


def _check_python() -> Check:
    """Verify the interpreter is new enough."""
    current = sys.version_info[:3]
    version_text = ".".join(str(part) for part in current)
    if tuple(current[:2]) >= MIN_PYTHON:
        return Check(t("Python version"), True, version_text)
    minimum = ".".join(str(part) for part in MIN_PYTHON)
    return Check(
        t("Python version"),
        False,
        version_text,
        t("Install Python {minimum} or newer.").format(minimum=minimum),
    )


def _check_package() -> Check:
    """Report the installed package version."""
    try:
        version = dist_version("novel-downloader")
    except PackageNotFoundError:
        from novel_downloader import __version__

        version = __version__
    except Exception as exc:
        return Check(t("Package version"), False, str(exc))
    return Check(t("Package version"), True, version)


def _check_settings_file() -> Check:
    """Verify that a settings file exists in the loader search path."""
    path = _settings_path()
    if path is None:
        return Check(
            t("settings.toml"),
            False,
            t("No configuration file found."),
            t("Run 'novel-cli config init' to create settings.toml."),
        )
    return Check(t("settings.toml"), True, str(path))


def _check_config() -> Check:
    """Verify the configuration file parses and is readable."""
    path = _settings_path()
    if path is None:
        return Check(
            t("Configuration"),
            False,
            t("No configuration file found."),
            t("Run 'novel-cli config init' to create settings.toml."),
        )
    try:
        config = load_config(path)
    except Exception as exc:
        return Check(
            t("Configuration"),
            False,
            str(exc),
            t("Fix the file or recreate it with 'novel-cli config init'."),
        )
    if not isinstance(config, dict):
        return Check(
            t("Configuration"),
            False,
            t("The configuration root is not a table."),
            t("Fix the file or recreate it with 'novel-cli config init'."),
        )
    return Check(t("Configuration"), True, str(path))


def _output_dir() -> Path:
    """Resolve the configured output directory, falling back to ``./downloads``."""
    config = _load_config_for_checks()
    if config is None:
        return Path("./downloads").resolve()
    return ConfigAdapter(config).get_output_dir()


def _dir_writable(path: Path) -> tuple[bool, str]:
    """Create ``path`` when needed and probe it with a temporary file."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".doctor-", dir=str(path))
        os.close(fd)
        os.unlink(tmp_name)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def _check_output_dir() -> Check:
    """Verify the output directory can be created and written to."""
    path = _output_dir()
    writable, error = _dir_writable(path)
    if writable:
        return Check(t("Output directory"), True, str(path))
    return Check(
        t("Output directory"),
        False,
        f"{path}: {error}",
        t("Check the permissions of the output directory or change output_dir."),
    )


def _check_sites() -> Check:
    """Report how many download sites are registered."""
    count = len(DOWNLOAD_SUPPORT_SITES)
    return Check(t("Sites"), True, t("{count} site(s)").format(count=count))


def _check_searchers() -> Check:
    """Report how many search providers can be loaded."""
    try:
        classes = registrar.get_searcher_classes(load_all_if_none=True)
    except Exception as exc:
        return Check(
            t("Searchers"),
            False,
            str(exc),
            t("Reinstall the package or check the plugin directory."),
        )
    return Check(
        t("Searchers"),
        True,
        t("{count} searcher(s)").format(count=len(classes)),
    )


def _check_music() -> Check:
    """Report whether the optional music dependencies are importable."""
    missing = [
        name for name in MUSIC_DEPENDENCIES if importlib.util.find_spec(name) is None
    ]
    if not missing:
        return Check(t("Music dependencies"), True, t("installed"), fatal=False)
    return Check(
        t("Music dependencies"),
        False,
        t("missing: {names}").format(names=", ".join(missing)),
        t('Install the optional music extra: pip install "novel-downloader[music]"'),
        fatal=False,
    )


def _port_is_free(port: int) -> bool:
    """Return whether ``127.0.0.1:port`` can be bound right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _check_api_port(port: int = API_PORT) -> Check:
    """Verify that the local API port is available."""
    if _port_is_free(port):
        return Check(t("API port {port}").format(port=port), True, t("free"))
    return Check(
        t("API port {port}").format(port=port),
        False,
        t("already in use"),
        t(
            "Stop the process using the port, or start the API with "
            "'novel-api --port PORT'."
        ),
    )


def collect_checks(port: int = API_PORT) -> list[Check]:
    """Run every check and return the results in display order."""
    return [
        _check_python(),
        _check_package(),
        _check_settings_file(),
        _check_config(),
        _check_output_dir(),
        _check_sites(),
        _check_searchers(),
        _check_music(),
        _check_api_port(port),
    ]


class DoctorCmd(Command):
    name = "doctor"
    help = t("Check the environment and diagnose common problems.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--port",
            type=int,
            default=API_PORT,
            metavar="N",
            help=t("API port to check for availability (default: 8081)."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        checks = collect_checks(int(args.port))
        fatal_failures = 0

        for check in checks:
            if check.ok:
                line = f"✓ {check.name}"
                if check.detail:
                    line += f": {check.detail}"
                ui.success(line)
                continue

            line = f"✗ {check.name}"
            if check.detail:
                line += f": {check.detail}"
            if check.fatal:
                fatal_failures += 1
                ui.error(line)
            else:
                ui.warn(f"{line} ({t('optional')})")
            if check.suggestion:
                ui.info(f"  -> {check.suggestion}")

        if fatal_failures:
            ui.error(t("{count} check(s) failed.").format(count=fatal_failures))
            raise SystemExit(EXIT_FAILURE)

        ui.success(t("All checks passed."))
