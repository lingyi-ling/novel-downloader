#!/usr/bin/env python3
"""
novel_downloader.apps.embedded
------------------------------

Embedding support for the Android client: the full Python service layer runs
on the phone behind a loopback-only HTTP server that mirrors the REST API
contract (see :mod:`novel_downloader.apps.api.routes`).  The Compose client
keeps speaking the same protocol — it just points at ``127.0.0.1``.
"""

__all__ = ["start_server"]
