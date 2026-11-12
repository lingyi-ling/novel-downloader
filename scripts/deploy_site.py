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

A deployment never writes into the live tree: the new files are assembled in
a sibling directory (with the previous contents copied in, so unrelated files
survive) and the directory is swapped with two renames.  If the swap fails,
the old directory is restored.  Each run uses a unique work id, so two
concurrent deployments cannot share a staging directory; the last completed
swap wins and is always a complete site.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import posixpath
import secrets
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "website"

DEFAULT_HOST = "43.165.170.43"
DEFAULT_USER = "ubuntu"
DEFAULT_DOCROOT = "/var/www/noveldl"

CONNECT_TIMEOUT = 20.0
REMOTE_TIMEOUT = 120.0
CLEANUP_TIMEOUT = 30.0

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


def _new_work_id() -> str:
    """A per-run id: pid (diagnostics) plus randomness (no collisions)."""
    return f"{os.getpid()}-{secrets.token_hex(6)}"


def _install_paths(docroot: str, work_id: str) -> tuple[str, str]:
    """
    The sibling directories used to swap the site into place.

    Both live next to *docroot* so the final ``mv`` is a same-filesystem
    rename (atomic on POSIX), and both carry the unique work id so concurrent
    deployments never touch each other's tree.
    """
    root = posixpath.normpath(docroot)
    return f"{root}.new-{work_id}", f"{root}.old-{work_id}"


def _remote_script(
    staging: str,
    docroot: str,
    names: Iterable[str],
    *,
    work_id: str = "manual",
) -> str:
    """
    Build the remote shell command that installs the staged files.

    ``set -eu`` plus an explicit existence check per file: a partial upload
    aborts before touching the live directory.  The new tree starts as a copy
    of the current docroot (so files not in :data:`SITE_FILES` survive), gets
    the staged files on top, and is swapped in with ``mv``.  If the swap
    fails, the previous tree is moved back before the script exits non-zero.
    """
    new, old = _install_paths(docroot, work_id)
    quoted = " ".join(shlex.quote(name) for name in names)
    return " && ".join(
        [
            "set -eu",
            f"cd {shlex.quote(staging)}",
            f'for f in {quoted}; do test -f "$f"; done',
            f"sudo -n install -d -m 755 {shlex.quote(new)}",
            f"if [ -d {shlex.quote(docroot)} ]; then "
            f"sudo -n cp -a {shlex.quote(docroot)}/. {shlex.quote(new)}/; fi",
            f"sudo -n cp -a {shlex.quote(staging)}/. {shlex.quote(new)}/",
            f"if [ -e {shlex.quote(docroot)} ]; then "
            f"sudo -n mv {shlex.quote(docroot)} {shlex.quote(old)}; fi",
            f"if sudo -n mv {shlex.quote(new)} {shlex.quote(docroot)}; then "
            f"sudo -n rm -rf {shlex.quote(old)} || true; "
            f"else if [ -e {shlex.quote(old)} ]; then "
            f"sudo -n mv {shlex.quote(old)} {shlex.quote(docroot)}; fi; "
            "exit 1; fi",
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

    # An empty variable is the same as unset: passing "" would try an empty
    # password and shadow the key/agent path.
    password = os.environ.get("NOVELDL_DEPLOY_PASSWORD") or None
    try:
        client.connect(
            host,
            username=user,
            password=password,
            timeout=CONNECT_TIMEOUT,
            banner_timeout=CONNECT_TIMEOUT,
            auth_timeout=CONNECT_TIMEOUT,
            allow_agent=True,
            look_for_keys=True,
        )
    except Exception as exc:  # paramiko/socket errors are not DeployError
        with contextlib.suppress(Exception):
            client.close()  # never mask the connect failure
        # paramiko's messages name the host and failure kind, never the secret.
        raise DeployError(f"SSH connection to {user}@{host} failed: {exc}") from exc
    return client


def _cleanup_remote(client: object, paths: Iterable[str]) -> None:
    """
    Best-effort removal of staging/assembly paths after success or failure.

    Never raises and never touches the ``*.old-*`` path: after a failed swap
    that directory may be the only complete copy of the site.
    """
    command = "sudo -n rm -rf " + " ".join(shlex.quote(path) for path in paths)
    try:
        _, stdout, _ = client.exec_command(  # type: ignore[attr-defined]
            command, timeout=CLEANUP_TIMEOUT
        )
        # Drain the (empty) output first.  ``recv_exit_status`` has no timeout
        # of its own and blocks forever on a dead connection or when the
        # remote has written more than the channel window; ``read`` is bounded
        # by the channel timeout set above.  Cleanup is best-effort, so a
        # timeout here only produces the warning below.
        stdout.read()
        code = stdout.channel.recv_exit_status()
        if code != 0:
            print(
                f"warning: remote cleanup exited with {code}: {command}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the error
        print(f"warning: could not clean up {', '.join(paths)}: {exc}", file=sys.stderr)


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
    if not posixpath.isabs(docroot):
        # The remote script cds into the staging directory first, so a
        # relative docroot would silently resolve inside it.
        raise DeployError(f"--docroot must be an absolute path: {docroot!r}")

    if dry_run:
        return names

    work_id = _new_work_id()
    staging = f"/tmp/noveldl-deploy-{work_id}"
    new, old = _install_paths(docroot, work_id)

    client = _connect(host, user)
    try:
        sftp = None
        try:
            sftp = client.open_sftp()
            try:
                sftp.mkdir(staging)
            except OSError as exc:
                raise DeployError(f"cannot create {staging}: {exc}") from exc
            for path in files:
                sftp.put(str(path), f"{staging}/{path.name}")
        except DeployError:
            raise
        except Exception as exc:  # socket timeouts, channel failures, ...
            raise DeployError(
                f"upload to {user}@{host}:{staging} failed: {exc}"
            ) from exc
        finally:
            if sftp is not None:
                with contextlib.suppress(Exception):
                    sftp.close()  # closing must not mask the upload error

        try:
            _, stdout, stderr = client.exec_command(
                _remote_script(staging, docroot, names, work_id=work_id),
                timeout=REMOTE_TIMEOUT,
            )
            out = stdout.read().decode("utf-8", "replace").strip()
            err = stderr.read().decode("utf-8", "replace").strip()
            code = stdout.channel.recv_exit_status()
        except DeployError:
            raise
        except Exception as exc:
            raise DeployError(
                f"remote install on {user}@{host} was interrupted: {exc}"
            ) from exc
        if code != 0:
            raise DeployError(
                f"remote install failed (exit {code}): {err or out or 'no output'}"
                f"; if the live directory is missing, the previous copy is at {old}"
            )
    finally:
        try:
            _cleanup_remote(client, (staging, new))
        finally:
            with contextlib.suppress(Exception):
                client.close()  # the deployment result is already decided

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
