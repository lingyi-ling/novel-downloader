#!/usr/bin/env python3
"""
Deploy the static website in ``website/`` to the production server.

The site is served by nginx from ``/var/www/noveldl`` (see
``/etc/nginx/sites-available/parfait.conf``).  Deployment is a plain file sync:
no build step, no package manager, no service restart.

Authentication, in order of preference:

* an SSH key / agent (``~/.ssh/id_ed25519`` and friends, tried by paramiko);
* the password in ``NOVELDL_DEPLOY_PASSWORD``.

The password is only ever read from the environment.  It is never written to
disk, echoed, or included in the command line sent to the server, so it does
not leak into shell history or process lists.

Usage::

    python scripts/deploy_site.py --dry-run
    python scripts/deploy_site.py

The remote step runs under ``sudo -n``: the target directory is root-owned and
the deploy user is expected to have passwordless sudo for it.  If that is not
the case the script fails loudly instead of leaving a half-updated site.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "website"

DEFAULT_HOST = "43.165.170.43"
DEFAULT_USER = "ubuntu"
DEFAULT_DOCROOT = "/var/www/noveldl"

#: Files that make up the site.  Kept explicit so a stray file in ``website/``
#: (an editor backup, a build artifact) never gets published by accident.
SITE_FILES: tuple[str, ...] = (
    "index.html",
    "main.js",
    "style.css",
    "favicon.svg",
    "og-image.png",
    "robots.txt",
    "sitemap.txt",
)

#: Files that must exist for the site to be considered deployable.
REQUIRED_FILES: tuple[str, ...] = ("index.html", "main.js", "style.css")


class DeployError(RuntimeError):
    """Raised when the deployment cannot proceed."""


def site_files(site_dir: Path = SITE_DIR) -> list[Path]:
    """Return the site files that exist, in a stable order."""
    return [site_dir / name for name in SITE_FILES if (site_dir / name).is_file()]


def missing_required(site_dir: Path = SITE_DIR) -> list[str]:
    """Return the required files that are absent (empty list means usable)."""
    return [name for name in REQUIRED_FILES if not (site_dir / name).is_file()]


def _remote_script(
    staging: str,
    docroot: str,
    names: Iterable[str],
) -> str:
    """
    Build the remote shell command that installs the staged files.

    ``set -eu`` plus an explicit existence check per file: a partial upload
    aborts before touching the live directory, so the site is never left in a
    mixed state.  Only the files listed in :data:`SITE_FILES` are replaced;
    anything else already in the docroot is left untouched.
    """
    quoted = " ".join(shlex.quote(name) for name in names)
    return " && ".join(
        [
            "set -eu",
            f"cd {shlex.quote(staging)}",
            f'for f in {quoted}; do test -f "$f"; done',
            f"sudo -n install -d -m 755 {shlex.quote(docroot)}",
            f"sudo -n cp -a {shlex.quote(staging)}/. {shlex.quote(docroot)}/",
        ]
    )


def _connect(host: str, user: str):  # type: ignore[no-untyped-def]
    """Open an SSH connection using a key when possible, else the password."""
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DeployError(
            "paramiko is required for deployment: pip install paramiko "
            "(or `pip install -e .[deploy]`)"
        ) from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    password = os.environ.get("NOVELDL_DEPLOY_PASSWORD")
    client.connect(
        host,
        username=user,
        password=password,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        allow_agent=True,
        look_for_keys=True,
    )
    return client


def deploy(
    *,
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
    docroot: str = DEFAULT_DOCROOT,
    dry_run: bool = False,
    site_dir: Path = SITE_DIR,
) -> list[str]:
    """
    Upload ``site_dir`` to ``docroot`` on ``host``.

    :return: The list of file names that were (or, with ``dry_run``, would be)
        published.
    :raises DeployError: when required files are missing or the remote step
        fails.
    """
    missing = missing_required(site_dir)
    if missing:
        raise DeployError("site is incomplete, missing: " + ", ".join(sorted(missing)))

    files = site_files(site_dir)
    names = [path.name for path in files]
    if not names:
        raise DeployError(f"no site files found in {site_dir}")

    if dry_run:
        return names

    staging = f"/tmp/noveldl-deploy-{int(time.time())}"
    client = _connect(host, user)
    try:
        sftp = client.open_sftp()
        try:
            sftp.mkdir(staging)
        except OSError as exc:
            raise DeployError(f"cannot create {staging}: {exc}") from exc
        try:
            for path in files:
                sftp.put(str(path), f"{staging}/{path.name}")
        finally:
            sftp.close()

        _, stdout, stderr = client.exec_command(
            _remote_script(staging, docroot, names), timeout=120
        )
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        code = stdout.channel.recv_exit_status()
        client.exec_command(f"rm -rf {shlex.quote(staging)}", timeout=30)
        if code != 0:
            raise DeployError(
                f"remote install failed (exit {code}): {err or out or 'no output'}"
            )
    finally:
        client.close()

    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy the static website to the production server."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--docroot", default=DEFAULT_DOCROOT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded without connecting.",
    )
    args = parser.parse_args(argv)

    try:
        names = deploy(
            host=args.host,
            user=args.user,
            docroot=args.docroot,
            dry_run=args.dry_run,
        )
    except DeployError as exc:
        print(f"deploy failed: {exc}", file=sys.stderr)
        return 1

    verb = "would publish" if args.dry_run else "published"
    print(f"{verb} {len(names)} file(s) to {args.user}@{args.host}:{args.docroot}")
    for name in names:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
