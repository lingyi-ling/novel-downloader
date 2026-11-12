#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.doctor
-----------------------------------------

Environment self-check (``novel-cli doctor``).

By default every check is offline. The output-directory and log-directory
probes may create the configured directories and write/remove a temporary
file inside them; nothing else is modified. Each row is printed as ``✓`` or
``✗``; failing rows carry a short, actionable fix. Optional capabilities (the
music extra, the network probe, the disk-space warning) are reported but do
not make the command fail.

``--online`` adds an egress probe: a HEAD request with a short timeout
against a couple of well-known endpoints. It stays advisory — an offline
machine is not a broken installation.

``--json`` prints one machine-readable document on stdout instead of the
table (diagnostics keep going to stderr, so the document can be piped). Each
entry carries a stable ``key`` plus optional structured ``data``.

Exit codes: ``0`` when every required check passes, ``1`` otherwise.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import platform
import re
import socket
import sys
import tempfile
import time
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import Any

from novel_downloader.apps.cli import ui
from novel_downloader.apps.constants import DOWNLOAD_SUPPORT_SITES
from novel_downloader.infra.config import ConfigAdapter, load_config, validate_config
from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import app_home
from novel_downloader.plugins import registrar

from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail
from .storage import disk_free, format_size, scan_tree

#: Minimum Python version supported by the project (``requires-python``).
MIN_PYTHON = (3, 11)

#: Default port used by ``novel-api``.
API_PORT = 8081

#: Optional dependencies required by ``novel-cli music``.
MUSIC_DEPENDENCIES = ("mutagen", "qrcode", "websocket")

#: Candidate configuration filenames, in loader priority order.
_SETTINGS_FILENAMES = ("settings.toml", "settings.json")

#: Core runtime dependencies (import name, distribution name), mirroring
#: ``[project.dependencies]`` in ``pyproject.toml``. The doctor compatibility
#: matrix reports each one's presence and installed version.
CORE_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("rich", "rich"),
    ("requests", "requests"),
    ("Crypto", "pycryptodome"),
    ("aiohttp", "aiohttp"),
    ("lxml", "lxml"),
    ("cssselect", "cssselect"),
    ("platformdirs", "platformdirs"),
    ("webview", "pywebview"),
)

#: Optional dependencies of the ``api`` extra, reported but never fatal.
API_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
)

#: Root package every built-in site plugin lives under.
SITE_PACKAGE_ROOT = "novel_downloader.plugins.sites"

#: Plugin kinds imported per sampled site.
SITE_PLUGIN_KINDS: tuple[str, ...] = ("fetcher", "parser")

#: How many sites the plugin-health sample covers (deterministic spread).
SITE_PLUGIN_SAMPLE_SIZE = 8

#: Endpoints probed by ``--online`` (any HTTP answer counts as egress).
PROBE_ENDPOINTS: tuple[str, ...] = ("https://www.baidu.com", "https://github.com")

#: Per-endpoint timeout of the egress probe, in seconds.
PROBE_TIMEOUT = 2.5

#: Problem/plugin names listed in a human detail line before truncation.
MAX_DETAIL_ITEMS = 3

#: Volume free space (on the output directory's volume) below which the
#: disk-space check warns, in bytes (512 MiB).
LOW_DISK_FREE_BYTES = 512 * 1024 * 1024

#: Maximum length of one import failure detail.
_MAX_FAILURE_TEXT = 120

#: ``key=value`` pairs whose value is a credential.  Doctor details are mostly
#: exception text, and a third-party library may echo a token or password
#: without asking.  The key itself is kept for context; only the value goes.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|api[_-]?key|secret|password|passwd|pwd|authorization|cookie)"
    r"(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)

#: Windows absolute paths (``C:\...``), including spaces.
_WINDOWS_ABS_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\"'<>|;,)\r\n]*")

#: POSIX absolute paths under the directories user data normally lives in.
_POSIX_ABS_PATH = re.compile(
    r"(?<![\w:/])/(?:home|Users|root|tmp|var|opt|mnt|srv|private)"
    r"(?:/[^\s\"'<>|;,)]*)?"
)


def _home_dir() -> str:
    """Return the user's home directory without a trailing separator."""
    try:
        home = os.path.expanduser("~")
    except (KeyError, RuntimeError):  # pragma: no cover - exotic environments
        return ""
    if not home or home == "~":
        return ""
    return home.rstrip("\\/")


def _mask_path_text(text: str) -> str:
    """Shorten an absolute path so diagnostics do not leak the user's layout.

    Paths under the current directory become relative (``./...``), paths under
    the home directory use ``~``, and any other absolute path keeps only its
    tail behind an ``<abs>`` marker.  Relative paths and plain values are
    returned unchanged.
    """
    value = str(text)
    candidates = ((os.getcwd(), "."), (_home_dir(), "~"))
    for base, label in candidates:
        if not base:
            continue
        if os.path.normcase(value) == os.path.normcase(base):
            return label
        prefix = os.path.normcase(base) + os.sep
        if os.path.normcase(value).startswith(prefix):
            return label + value[len(base) :]
    if os.path.isabs(value):
        parts = Path(value).parts
        tail = os.sep.join(parts[1:])
        return f"<abs>{os.sep}{tail}" if tail else "<abs>"
    return value


def _redact_text(text: str) -> str:
    """Remove credentials, tokens and absolute-path prefixes from *text*."""
    if not text:
        return text

    # Reuse the session layer's proxy scrubber: it handles
    # ``scheme://user:password@host`` exactly like the network backends do.
    try:
        from novel_downloader.infra.sessions.proxy import redact_secrets

        text = redact_secrets(text)
    except Exception:  # pragma: no cover - defensive; keep the other passes
        pass

    text = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}***", text
    )
    text = _WINDOWS_ABS_PATH.sub(lambda match: _mask_path_text(match.group(0)), text)
    text = _POSIX_ABS_PATH.sub(lambda match: _mask_path_text(match.group(0)), text)
    return text


def _redact_data(value: Any) -> Any:
    """Recursively redact strings inside a JSON-friendly structure."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            _redact_text(key) if isinstance(key, str) else key: _redact_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_data(item) for item in value]
    return value


@dataclass
class Check:
    """One doctor check result."""

    name: str
    ok: bool
    detail: str = ""
    suggestion: str = ""
    #: Optional failures are reported but never fail the command.
    fatal: bool = True
    #: Stable identifier used as the ``--json`` key (defaults to ``name``).
    key: str = ""
    #: Structured extras for ``--json`` (already JSON-friendly values).
    data: dict[str, Any] | None = None
    #: Wall-clock duration of the check, in seconds (filled by the runner).
    elapsed: float | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON view of this check used by ``--json``."""
        payload: dict[str, object] = {
            "key": self.key or self.name,
            "name": self.name,
            "ok": self.ok,
            "fatal": self.fatal,
        }
        # Real runs time every check, so the field is present in the shipped
        # document. Hand-built checks (tests, embedders) keep the historical
        # shape instead of gaining a ``null`` field.
        if self.elapsed is not None:
            payload["elapsed"] = round(self.elapsed, 3)
        if self.detail:
            payload["detail"] = self.detail
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        if self.data is not None:
            payload["data"] = self.data
        return payload


def _sanitize_check(check: Check) -> Check:
    """Return ``check`` with secrets and absolute-path prefixes removed."""
    return Check(
        name=check.name,
        ok=check.ok,
        detail=_redact_text(check.detail),
        suggestion=_redact_text(check.suggestion),
        fatal=check.fatal,
        key=check.key,
        data=_redact_data(check.data) if check.data is not None else None,
        elapsed=check.elapsed,
    )


def _run_check(key: str, name: str, func: Callable[[], Check]) -> Check:
    """Run one check, converting an unexpected exception into a failed row.

    A diagnostic command must finish its report even when one probe is broken
    (an unreadable directory, a third-party import that raises during
    ``find_spec``, ...).  The exception type and message are kept so the row is
    actionable, but a traceback never escapes.
    """
    started = time.perf_counter()
    try:
        check = func()
        if not isinstance(check, Check):
            raise TypeError(f"check returned {type(check).__name__}, expected Check")
        check.elapsed = round(time.perf_counter() - started, 3)
    except Exception as exc:
        check = Check(
            name,
            False,
            f"{type(exc).__name__}: {exc}",
            key=key,
            elapsed=round(time.perf_counter() - started, 3),
        )
    return check


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
        return Check(t("Python version"), True, version_text, key="python")
    minimum = ".".join(str(part) for part in MIN_PYTHON)
    return Check(
        t("Python version"),
        False,
        version_text,
        t("Install Python {minimum} or newer.").format(minimum=minimum),
        key="python",
    )


def _check_package() -> Check:
    """Report the package version.

    ``novel_downloader.__version__`` is the source of truth: it is what
    ``novel-cli --version`` prints, and it always reflects the running code.
    The installed distribution metadata is only a secondary signal -- an
    editable install can carry stale metadata (e.g. 1.0.0 while the source
    tree is at 1.2.0), which used to make ``doctor`` disagree with
    ``--version``. When the two disagree, both numbers are reported so the
    drift is visible instead of silently masked.
    """
    from novel_downloader import __version__

    try:
        installed = dist_version("novel-downloader")
    except PackageNotFoundError:
        return Check(t("Package version"), True, __version__, key="package")
    except Exception as exc:
        return Check(t("Package version"), False, str(exc), key="package")
    if installed == __version__:
        return Check(t("Package version"), True, __version__, key="package")
    return Check(
        t("Package version"),
        True,
        t("{version} (installed metadata: {installed})").format(
            version=__version__, installed=installed
        ),
        key="package",
        data={"version": __version__, "installed_metadata": installed},
    )


def _check_settings_file() -> Check:
    """Verify that a settings file exists in the loader search path."""
    path = _settings_path()
    if path is None:
        return Check(
            t("settings.toml"),
            False,
            t("No configuration file found."),
            t("Run 'novel-cli config init' to create settings.toml."),
            key="settings",
        )
    return Check(t("settings.toml"), True, str(path), key="settings")


def _check_config() -> Check:
    """Verify the configuration parses and passes validation.

    Error-level validation findings fail the check; warnings are reported in
    the detail line while the check still passes. The full problem list (up
    to :data:`MAX_DETAIL_ITEMS` entries) is attached as structured ``data``.
    """
    path = _settings_path()
    if path is None:
        return Check(
            t("Configuration"),
            False,
            t("No configuration file found."),
            t("Run 'novel-cli config init' to create settings.toml."),
            key="config",
        )
    try:
        config = load_config(path)
    except Exception as exc:
        return Check(
            t("Configuration"),
            False,
            str(exc),
            t("Fix the file or recreate it with 'novel-cli config init'."),
            key="config",
        )
    if not isinstance(config, dict):
        return Check(
            t("Configuration"),
            False,
            t("The configuration root is not a table."),
            t("Fix the file or recreate it with 'novel-cli config init'."),
            key="config",
        )

    problems = validate_config(config)
    data = {
        "path": str(path),
        "problems": [
            {
                "level": problem.level,
                "path": problem.path,
                "message": problem.message,
                "fix_hint": problem.fix_hint,
            }
            for problem in problems[:MAX_DETAIL_ITEMS]
        ],
    }
    errors = [problem for problem in problems if problem.level == "error"]
    warnings = [problem for problem in problems if problem.level == "warning"]
    if errors:
        preview = "; ".join(str(problem) for problem in errors[:MAX_DETAIL_ITEMS])
        return Check(
            t("Configuration"),
            False,
            t("{count} error(s): {preview}").format(count=len(errors), preview=preview),
            t("Fix the listed keys or recreate it with 'novel-cli config init'."),
            key="config",
            data=data,
        )
    if warnings:
        preview = "; ".join(str(problem) for problem in warnings[:MAX_DETAIL_ITEMS])
        return Check(
            t("Configuration"),
            True,
            t("{count} warning(s): {preview}").format(
                count=len(warnings), preview=preview
            ),
            key="config",
            data=data,
        )
    return Check(t("Configuration"), True, str(path), key="config", data=data)


def _dependency_row(
    module_name: str,
    dist: str,
    *,
    optional: bool,
) -> dict[str, Any]:
    """Return one dependency-matrix row for ``--json``."""
    installed = importlib.util.find_spec(module_name) is not None
    version: str | None
    try:
        version = dist_version(dist)
    except Exception:
        version = None
    return {
        "module": module_name,
        "dist": dist,
        "optional": optional,
        "installed": installed,
        "version": version,
    }


def _check_dependencies(
    core: Sequence[tuple[str, str]] = CORE_DEPENDENCIES,
    optional: Sequence[tuple[str, str]] = API_DEPENDENCIES,
) -> Check:
    """Verify the runtime dependency matrix (imports and versions).

    A missing core dependency is fatal: the package cannot run without it.
    Missing optional extras (the ``api`` extra here, the music extra in its
    own check) are reported without failing the command.
    """
    rows = [
        _dependency_row(module_name, dist, optional=False) for module_name, dist in core
    ]
    rows.extend(
        _dependency_row(module_name, dist, optional=True)
        for module_name, dist in optional
    )
    missing_core = [
        row["dist"] for row in rows if not row["optional"] and not row["installed"]
    ]
    missing_optional = [
        row["dist"] for row in rows if row["optional"] and not row["installed"]
    ]
    data: dict[str, Any] = {
        "dependencies": rows,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "minimum": ".".join(str(part) for part in MIN_PYTHON),
        },
    }
    if missing_core:
        return Check(
            t("Dependencies"),
            False,
            t("missing: {names}").format(names=", ".join(missing_core)),
            t('Install missing dependencies: pip install "novel-downloader"'),
            key="dependencies",
            data=data,
        )
    return Check(
        t("Dependencies"),
        True,
        t(
            "{core}/{core_total} core and {optional}/{optional_total} optional "
            "dependencies installed"
        ).format(
            core=len(core) - len(missing_core),
            core_total=len(core),
            optional=len(optional) - len(missing_optional),
            optional_total=len(optional),
        ),
        key="dependencies",
        data=data,
    )


def _output_dir() -> Path:
    """Resolve the configured output directory, falling back to ``./downloads``."""
    config = _load_config_for_checks()
    if config is None:
        return Path("./downloads").resolve()
    return ConfigAdapter(config).get_output_dir()


def _cache_dir() -> Path:
    """Resolve the configured cache directory (app-home ``cache`` default)."""
    config = _load_config_for_checks()
    data = config if config is not None else {}
    return ConfigAdapter(data).get_cache_dir()


def _log_dir() -> Path:
    """Resolve the configured log directory (app-home ``logs`` default)."""
    config = _load_config_for_checks()
    data = config if config is not None else {}
    return ConfigAdapter(data).get_log_dir()


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
        return Check(t("Output directory"), True, str(path), key="output_dir")
    return Check(
        t("Output directory"),
        False,
        f"{path}: {error}",
        t("Check the permissions of the output directory or change output_dir."),
        key="output_dir",
    )


def _check_log_dir() -> Check:
    """Verify the log directory can be created and written to.

    Every run appends to a log file under this directory, so an unwritable
    log directory otherwise fails later with a much more confusing error.
    """
    path = _log_dir()
    writable, error = _dir_writable(path)
    if writable:
        return Check(t("Log directory"), True, str(path), key="log_dir")
    return Check(
        t("Log directory"),
        False,
        f"{path}: {error}",
        t("Check the permissions of the log directory or change log_dir."),
        key="log_dir",
    )


def _check_disk_space(
    minimum: int = LOW_DISK_FREE_BYTES,
    path: Path | None = None,
) -> Check:
    """Report the free space of the volume holding the output directory.

    Advisory only: a nearly full disk does not make the installation broken,
    but it is the most common cause of truncated downloads and exports. The
    output directory's volume is probed because that is where the bulk of the
    data lands; the ``storage`` command reports per-bucket free space.
    """
    target = _output_dir() if path is None else path
    free = disk_free(target)
    data: dict[str, Any] = {
        "path": str(target),
        "free_bytes": free,
        "minimum_bytes": minimum,
    }
    if free is None:
        return Check(
            t("Disk space"),
            True,
            t("unknown"),
            fatal=False,
            key="disk_space",
            data=data,
        )
    if free >= minimum:
        detail = t("Free space on {path}: {free}").format(
            path=target, free=format_size(free)
        )
        return Check(
            t("Disk space"), True, detail, fatal=False, key="disk_space", data=data
        )
    return Check(
        t("Disk space"),
        False,
        t("only {free} free, minimum {minimum}").format(
            free=format_size(free), minimum=format_size(minimum)
        ),
        t("Free up disk space or move the data directories to another volume."),
        fatal=False,
        key="disk_space",
        data=data,
    )


def _check_cache_dir() -> Check:
    """Report the cache directory size and the reclaimable amount.

    Everything under the cache directory is regenerable (it holds site
    cookies and downloaded chapter caches), so its full size is exactly what
    ``novel-cli clean cache`` would free. Purely informational.
    """
    path = _cache_dir()
    files, total = scan_tree(path)
    detail = t("{count} file(s), {size}").format(count=files, size=format_size(total))
    suggestion = (
        t("Run 'novel-cli clean cache' to reclaim {size}.").format(
            size=format_size(total)
        )
        if total
        else ""
    )
    return Check(
        t("Cache directory"),
        True,
        detail,
        suggestion,
        fatal=False,
        key="cache",
        data={"path": str(path), "files": files, "bytes": total},
    )


def _check_sites() -> Check:
    """Report how many download sites are registered."""
    count = len(DOWNLOAD_SUPPORT_SITES)
    return Check(
        t("Sites"), True, t("{count} site(s)").format(count=count), key="sites"
    )


def _import_site_plugin(module_name: str) -> None:
    """Import one site plugin module.

    Kept as a module-level hook so tests can simulate a broken plugin
    without touching the real import machinery.
    """
    importlib.import_module(module_name)


def _site_sample(sites: Sequence[str], sample_size: int) -> list[str]:
    """Return a deterministic, evenly-spread sample of ``sites``."""
    if sample_size <= 0:
        return []
    if sample_size >= len(sites):
        return list(sites)
    step = len(sites) / sample_size
    indexes = sorted({round(index * step) for index in range(sample_size)})
    return [sites[index] for index in indexes]


def _check_site_plugins(
    sample_size: int = SITE_PLUGIN_SAMPLE_SIZE,
    package_root: str = SITE_PACKAGE_ROOT,
) -> Check:
    """Import a deterministic sample of site plugins (registry loadable).

    Importing ``<package_root>.<site>.fetcher``/``parser`` runs the registry
    decorators, so a broken site plugin surfaces as an import error. Failures
    are advisory (one broken site does not break the install); the check only
    becomes fatal when every sampled import failed.
    """
    sites = sorted(DOWNLOAD_SUPPORT_SITES)
    sample = _site_sample(sites, sample_size)
    if not sample:
        return Check(
            t("Site plugins"),
            True,
            t("no sites to sample"),
            fatal=False,
            key="site_plugins",
        )

    failed: list[str] = []
    for site_key in sample:
        for kind in SITE_PLUGIN_KINDS:
            module_name = f"{package_root}.{site_key}.{kind}"
            try:
                _import_site_plugin(module_name)
            except Exception as exc:
                text = str(exc)[:_MAX_FAILURE_TEXT]
                failed.append(f"{site_key}/{kind}: {text}")

    ok = not failed
    fatal = len(failed) == len(sample) * len(SITE_PLUGIN_KINDS)
    detail = t("{count}/{total} sampled site plugin(s) loadable").format(
        count=len(sample), total=len(sample)
    )
    if failed:
        detail = t("{count} import failure(s): {names}").format(
            count=len(failed),
            names=", ".join(failed[:MAX_DETAIL_ITEMS]),
        )
    return Check(
        t("Site plugins"),
        ok,
        detail,
        t("Reinstall the package or check the plugin directory.") if failed else "",
        fatal=fatal,
        key="site_plugins",
        data={"sampled": sample, "failed": failed},
    )


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
            key="searchers",
        )
    return Check(
        t("Searchers"),
        True,
        t("{count} searcher(s)").format(count=len(classes)),
        key="searchers",
    )


def _check_music() -> Check:
    """Report whether the optional music dependencies are importable."""
    missing = [
        name for name in MUSIC_DEPENDENCIES if importlib.util.find_spec(name) is None
    ]
    if not missing:
        return Check(
            t("Music dependencies"), True, t("installed"), fatal=False, key="music"
        )
    return Check(
        t("Music dependencies"),
        False,
        t("missing: {names}").format(names=", ".join(missing)),
        t('Install the optional music extra: pip install "novel-downloader[music]"'),
        fatal=False,
        key="music",
    )


def _probe_endpoint(url: str, timeout: float) -> tuple[bool, str]:
    """HEAD ``url`` with a short timeout; any answer means egress works.

    ``requests`` is imported lazily so the offline doctor never touches the
    network stack at import time. A 4xx/5xx still counts as reachable: the
    origin answered, so the machine has an egress route.
    """
    import requests

    try:
        response = requests.head(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return False, type(exc).__name__
    return True, f"HTTP {response.status_code}"


def _check_network(
    endpoints: Sequence[str] = PROBE_ENDPOINTS,
    timeout: float = PROBE_TIMEOUT,
) -> Check:
    """Probe network egress against ``endpoints`` (advisory, never fatal)."""
    results = {url: _probe_endpoint(url, timeout) for url in endpoints}
    reachable = sum(1 for ok, _detail in results.values() if ok)
    ok = reachable > 0
    return Check(
        t("Network egress"),
        ok,
        t("{count}/{total} endpoint(s) reachable").format(
            count=reachable, total=len(results)
        ),
        "" if ok else t("Check the network, proxy or firewall settings."),
        fatal=False,
        key="network",
        data={
            "endpoints": {
                url: {"ok": ok, "status": status}
                for url, (ok, status) in results.items()
            },
            "timeout": timeout,
        },
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
        return Check(
            t("API port {port}").format(port=port), True, t("free"), key="api_port"
        )
    return Check(
        t("API port {port}").format(port=port),
        False,
        t("already in use"),
        t(
            "Stop the process using the port, or start the API with "
            "'novel-api --port PORT'."
        ),
        key="api_port",
    )


def collect_checks(port: int = API_PORT, *, online: bool = False) -> list[Check]:
    """Run every check and return the results in display order.

    ``online`` appends the network-egress probe; by default the doctor is a
    purely offline self-check.  Each check is isolated: an exception raised by
    one probe becomes a failed row instead of aborting the report.
    """
    specs: list[tuple[str, str, Callable[[], Check]]] = [
        ("python", t("Python version"), _check_python),
        ("package", t("Package version"), _check_package),
        ("settings", t("settings.toml"), _check_settings_file),
        ("config", t("Configuration"), _check_config),
        ("dependencies", t("Dependencies"), _check_dependencies),
        ("output_dir", t("Output directory"), _check_output_dir),
        ("log_dir", t("Log directory"), _check_log_dir),
        ("disk_space", t("Disk space"), _check_disk_space),
        ("cache", t("Cache directory"), _check_cache_dir),
        ("sites", t("Sites"), _check_sites),
        ("site_plugins", t("Site plugins"), _check_site_plugins),
        ("searchers", t("Searchers"), _check_searchers),
        ("music", t("Music dependencies"), _check_music),
    ]
    if online:
        specs.append(("network", t("Network egress"), _check_network))
    specs.append(
        (
            "api_port",
            t("API port {port}").format(port=port),
            lambda: _check_api_port(port),
        )
    )
    return [_sanitize_check(_run_check(key, name, func)) for key, name, func in specs]


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
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )
        parser.add_argument(
            "--online",
            action="store_true",
            help=t(
                "Also probe network egress with short-timeout requests "
                "(never fails the command)."
            ),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        port = int(args.port)
        # ``socket.bind`` raises ``OverflowError`` (not ``OSError``) outside
        # 0..65535, which used to escape as a traceback after half the checks
        # had run -- and, with ``--json``, without any document on stdout. An
        # unbindable port is a bad argument, so refuse it before anything runs.
        if not 1 <= port <= 65535:
            fail(t("Invalid input."), EXIT_USAGE)
        online = bool(getattr(args, "online", False))
        as_json = bool(getattr(args, "as_json", False))
        checks = collect_checks(port, online=True) if online else collect_checks(port)
        # ``collect_checks`` already sanitizes; doing it again here keeps the
        # contract for callers that replace it (tests, embedders) without
        # exposing credentials or the user's directory layout.
        checks = [_sanitize_check(check) for check in checks]
        fatal_failures = sum(1 for check in checks if not check.ok and check.fatal)
        warnings = sum(1 for check in checks if not check.ok and not check.fatal)
        status = "failed" if fatal_failures else ("warnings" if warnings else "ok")

        if as_json:
            payload = {
                # ``status`` distinguishes "all good", "warnings only" and
                # "hard failure" without changing the documented summary
                # fields (an exact-match consumer keeps working).
                "status": status,
                "warnings": warnings,
                "summary": {
                    "total": len(checks),
                    "failures": fatal_failures,
                    "optional_failures": warnings,
                    "ok": fatal_failures == 0,
                },
                "checks": [check.to_dict() for check in checks],
            }
            # One line: the document is meant to be piped into ``jq`` and
            # consumed by scripts; pretty-printing only risks truncation in
            # line-oriented tooling.
            ui.emit(json.dumps(payload, ensure_ascii=False))
            if fatal_failures:
                raise SystemExit(EXIT_FAILURE)
            return

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
                ui.error(line)
            else:
                optional_label = t("optional")
                ui.warn(f"{line} ({optional_label})")
            if check.suggestion:
                ui.info(f"  -> {check.suggestion}")

        if fatal_failures:
            ui.error(t("{count} check(s) failed.").format(count=fatal_failures))
            raise SystemExit(EXIT_FAILURE)

        if warnings:
            preview = ", ".join(
                check.name for check in checks if not check.ok and not check.fatal
            )
            ui.warn(
                t("{count} warning(s): {preview}").format(
                    count=warnings, preview=preview
                )
            )
            return

        ui.success(t("All checks passed."))
