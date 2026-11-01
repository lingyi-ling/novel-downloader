#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.update
-----------------------------------------

``novel-cli update``: check the release feed and optionally fetch this
platform's asset.

The command is deliberately split into "check" and "download": the check is a
single short ``GET`` against the GitHub ``releases/latest`` endpoint, the
download is an explicit second step.  Three flags control which half runs:

``--check``
    Check only. The exit code reports whether the *check* worked, not whether
    an update exists: a newer release, no release and an unreachable feed that
    still answered are all distinguishable on stdout, while ``1`` means the
    check itself failed.

``--json``
    One machine-readable document on stdout; nothing is printed to stdout
    that is not part of it. The document is never interactive: without
    ``--yes`` it behaves like ``--check`` and leaves ``download_path`` null.

``--yes``
    Non-interactive download. It downloads, verifies the SHA-256 digest and
    prints the resulting path -- nothing is executed, unpacked or installed.
    An asset without a digest is refused: a file that cannot be verified must
    be fetched by hand from the release page.

The download always lands in ``app_home()/updates/`` (never the current
directory) and every path decision is delegated to
:mod:`novel_downloader.apps.common.updater`, which sanitises the file name,
refuses non-HTTP(S) URLs and rejects an escaping path.

Exit codes: ``0`` when the check succeeded (with or without an update), when
the user declined the download, or after a successful download; ``1`` when the
check failed, no asset matches the platform (with ``--yes``), the digest is
missing (with ``--yes``) or the download failed.
"""

from __future__ import annotations

import json
import os
from argparse import ArgumentParser, Namespace
from pathlib import Path

from novel_downloader import __version__
from novel_downloader.apps.cli import ui
from novel_downloader.apps.common.updater import (
    DEFAULT_TIMEOUT,
    ReleaseAsset,
    UpdateInfo,
    check_for_update,
    detect_platform,
    download_asset,
)
from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import app_home

from .base import EXIT_FAILURE, Command
from .storage import format_size

__all__ = ["MIRROR_ENV_VAR", "UPDATES_DIRNAME", "UpdateCmd", "updates_dir"]

#: Directory (below :func:`app_home`) the asset is written to.
UPDATES_DIRNAME = "updates"

#: Comma-separated mirror prefixes tried before the release URL, e.g.
#: ``NOVEL_UPDATE_MIRROR="https://proxy.example/"``. An unset variable means
#: "no mirror", so the default is a direct download; ``--no-mirror`` ignores
#: the variable for one run.
MIRROR_ENV_VAR = "NOVEL_UPDATE_MIRROR"

#: Timeout for the (small) release document, in seconds.
CHECK_TIMEOUT = DEFAULT_TIMEOUT

#: Timeout for the asset transfer itself, in seconds.
DOWNLOAD_TIMEOUT = 60.0


def updates_dir() -> Path:
    """Return the fixed download directory (``app_home()/updates``).

    Resolved on every call (instead of at import time) so ``NOVEL_HOME`` and
    tests that patch the application home are honoured.
    """
    return app_home() / UPDATES_DIRNAME


def _mirror_prefixes() -> list[str]:
    """Parse :data:`MIRROR_ENV_VAR` into ordered, non-empty prefixes."""
    raw = os.environ.get(MIRROR_ENV_VAR, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _payload(
    info: UpdateInfo | None,
    *,
    current: str,
    error: str | None = None,
    download_path: Path | None = None,
) -> dict[str, object]:
    """Build the ``--json`` document; every documented key is always present."""
    asset = info.asset if info is not None else None
    return {
        "current": current,
        "latest": info.latest if info is not None else "",
        "available": bool(info is not None and info.available),
        "asset_name": asset.name if asset is not None else None,
        "asset_size": asset.size if asset is not None else None,
        "sha256": asset.sha256 if asset is not None else None,
        "download_path": str(download_path) if download_path is not None else None,
        "error": error,
    }


def _emit_json(payload: dict[str, object]) -> None:
    """Write one JSON document to stdout (the only stdout writer here)."""
    ui.emit(json.dumps(payload, ensure_ascii=False, indent=2))


def _check(current: str) -> tuple[UpdateInfo | None, str | None]:
    """Run the release check, returning ``(info, error)``.

    The shared helper already collapses every failure into an ``UpdateCheck``
    with ``info is None``; the ``error`` string is only used for the message.
    """
    result = check_for_update(
        current,
        platform_tag=detect_platform(),
        timeout=CHECK_TIMEOUT,
    )
    if result.info is None:
        return None, result.error or "unreachable"
    return result.info, None


def _print_summary(info: UpdateInfo, *, current: str) -> None:
    """Print the version/notes/asset summary on stderr."""
    ui.info(t("Current version: {version}").format(version=current))
    ui.info(t("Latest version: {version}").format(version=info.latest))
    if info.notes_url:
        ui.info(t("Release notes: {url}").format(url=info.notes_url))
    asset = info.asset
    if asset is None:
        ui.warn(
            t("No release asset matches this platform ({platform}).").format(
                platform=detect_platform()
            )
        )
        return
    size = format_size(asset.size) if asset.size is not None else t("unknown size")
    ui.info(t("Download asset: {name} ({size})").format(name=asset.name, size=size))


def _download(asset: ReleaseAsset, *, use_mirror: bool) -> Path:
    """Download and verify ``asset`` into :func:`updates_dir`."""
    root = updates_dir()
    mirror = _mirror_prefixes() if use_mirror else None
    return download_asset(
        asset.url,
        root / asset.name,
        asset.sha256,
        mirror,
        root=root,
        timeout=DOWNLOAD_TIMEOUT,
    )


def _install_hint(path: Path) -> str:
    """Return the platform-specific "install it yourself" sentence."""
    if path.suffix.lower() == ".exe":
        return t(
            "Nothing was run or installed. Double-click the installer at the "
            "path above to install it."
        )
    return t(
        "Nothing was run or installed. Extract the archive above and replace "
        "the installed files manually."
    )


class UpdateCmd(Command):
    name = "update"
    help = t("Check for a newer release and optionally download it.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help=t("Only check for an update; never ask or download."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print one machine-readable JSON document on stdout and exit."),
        )
        parser.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help=t(
                "Download and verify without asking (the file is not executed "
                "or installed)."
            ),
        )
        parser.add_argument(
            "--no-mirror",
            dest="no_mirror",
            action="store_true",
            help=t(
                "Ignore the {env} prefixes and download from the release URL directly."
            ).format(env=MIRROR_ENV_VAR),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        current = __version__
        as_json = bool(getattr(args, "as_json", False))
        check_only = bool(getattr(args, "check", False))
        assume_yes = bool(getattr(args, "yes", False))
        no_mirror = bool(getattr(args, "no_mirror", False))

        info, error = _check(current)

        if info is None:
            if as_json:
                _emit_json(_payload(None, current=current, error=error))
            else:
                ui.error(t("Update check failed: {error}").format(error=error))
            raise SystemExit(EXIT_FAILURE)

        if not as_json:
            _print_summary(info, current=current)

        if not info.available:
            if as_json:
                _emit_json(_payload(info, current=current))
            else:
                ui.success(
                    t("Already up to date (v{version}).").format(version=current)
                )
            return

        if check_only or (as_json and not assume_yes):
            if as_json:
                _emit_json(_payload(info, current=current))
            else:
                ui.info(t("A newer version is available."))
            return

        asset = info.asset
        if asset is None:
            if as_json:
                _emit_json(_payload(info, current=current, error="no-asset"))
                raise SystemExit(EXIT_FAILURE)
            # ``_print_summary`` already said no asset matches this platform.
            if not assume_yes:
                return
            ui.error(
                t("No release asset matches this platform ({platform}).").format(
                    platform=detect_platform()
                )
            )
            raise SystemExit(EXIT_FAILURE)

        if assume_yes and not asset.sha256:
            # Without a digest the download cannot be verified, so it is not
            # offered silently: the user has to fetch it from the release
            # page, where the browser/OS can at least show where it came from.
            reason = t(
                "The release provides no SHA-256 digest for {name}, so the "
                "download was not verified. Download it manually from {url}."
            ).format(name=asset.name, url=info.notes_url or asset.url)
            if as_json:
                _emit_json(_payload(info, current=current, error="missing-digest"))
                raise SystemExit(EXIT_FAILURE)
            ui.error(reason)
            raise SystemExit(EXIT_FAILURE)

        # ``--json`` without ``--yes`` already returned above, so this is the
        # interactive path only: Enter means No, matching the rest of the CLI.
        if not assume_yes and not ui.confirm(
            t("Download {name} now?").format(name=asset.name), default=False
        ):
            ui.warn(t("Cancelled."))
            return

        try:
            path = _download(asset, use_mirror=not no_mirror)
        except Exception as exc:  # noqa: BLE001 - report any transfer failure
            reason = t("Download failed: {error}").format(error=exc)
            if as_json:
                _emit_json(_payload(info, current=current, error=str(exc)))
                raise SystemExit(EXIT_FAILURE) from exc
            ui.error(reason)
            raise SystemExit(EXIT_FAILURE) from exc

        if as_json:
            _emit_json(_payload(info, current=current, download_path=path))
            return

        ui.success(t("Downloaded and verified: {path}").format(path=path))
        ui.info(_install_hint(path))
