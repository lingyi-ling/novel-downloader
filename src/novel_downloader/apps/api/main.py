#!/usr/bin/env python3
"""
novel_downloader.apps.api.main
------------------------------

Command-line entry point for the local REST API (``novel-api``).

The token can be passed with ``--token`` or supplied through the
``NOVEL_API_TOKEN`` environment variable. Without a token the API performs no
authentication, which is only safe because it binds to localhost by default:
binding to a non-local address (or enabling ``--cors`` there) prints a
prominent warning to stderr before the server starts.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import uvicorn

from .app import create_app

#: Bind addresses that count as "loopback only".
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: Environment variable read when ``--token`` is not passed.
TOKEN_ENV_VAR = "NOVEL_API_TOKEN"


def is_local_host(host: str) -> bool:
    """Whether ``host`` restricts the server to the local machine."""
    return host.strip().lower() in LOCAL_HOSTS


def warn_if_insecure_bind(host: str, token: str | None, cors: bool) -> list[str]:
    """
    Print prominent warnings for an unsafe launch configuration.

    :param host: The ``--host`` value the server will bind to.
    :param token: The configured token, or a falsy value when auth is disabled.
    :param cors: Whether permissive CORS was requested.
    :return: The warnings that were printed (empty when the setup is safe), so
        callers and tests can inspect them.
    """
    warnings: list[str] = []
    if not is_local_host(host):
        if not token:
            warnings.append(
                f"WARNING: no API token configured while binding to {host!r}. "
                "Anyone who can reach this port can control downloads and read "
                "the exported files under /downloads. Set --token or "
                f"{TOKEN_ENV_VAR}."
            )
        if cors:
            warnings.append(
                f"WARNING: --cors enables Access-Control-Allow-Origin: * on the "
                f"non-local bind {host!r}; any website the user visits could "
                "then call this API. Use --cors only on 127.0.0.1."
            )
    for message in warnings:
        print(message, file=sys.stderr, flush=True)
    return warnings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Novel Downloader local REST API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Address to bind to. Keep 127.0.0.1 unless you configure a token.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8081,
        help="TCP port to serve the API on.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "Require this token in 'Authorization: Bearer <token>' or "
            "'X-API-Key'. Defaults to NOVEL_API_TOKEN; empty disables auth."
        ),
    )
    parser.add_argument(
        "--cors",
        action="store_true",
        help=(
            "Enable permissive CORS (Access-Control-Allow-Origin: *). "
            "Only for local use; do not combine with a public bind."
        ),
    )
    return parser


def api_main(argv: Sequence[str] | None = None) -> None:
    """Parse arguments and run the API with uvicorn."""
    args = _build_parser().parse_args(argv)

    token = args.token if args.token is not None else os.environ.get(TOKEN_ENV_VAR)
    warn_if_insecure_bind(args.host, token, args.cors)

    application = create_app(token=token, cors=args.cors)
    uvicorn.run(application, host=args.host, port=args.port)


if __name__ in {"__main__", "__mp_main__"}:
    api_main()
