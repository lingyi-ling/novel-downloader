#!/usr/bin/env python3
"""
novel_downloader.apps.api.main
------------------------------

Command-line entry point for the local REST API (``novel-api``).

The token can be passed with ``--token`` or supplied through the
``NOVEL_API_TOKEN`` environment variable. Without a token the API performs no
authentication, which is only safe because it binds to localhost by default.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn

from .app import create_app


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

    token = args.token if args.token is not None else os.environ.get("NOVEL_API_TOKEN")
    if not token and args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "WARNING: binding to "
            f"{args.host} without an API token; anyone who can reach this "
            "port can control downloads."
        )

    application = create_app(token=token, cors=args.cors)
    uvicorn.run(application, host=args.host, port=args.port)


if __name__ in {"__main__", "__mp_main__"}:
    api_main()
