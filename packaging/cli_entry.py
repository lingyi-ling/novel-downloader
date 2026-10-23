#!/usr/bin/env python3
"""Frozen entry point for the ``novel-cli`` console executable."""

from __future__ import annotations

from novel_downloader.apps.cli.main import cli_main

if __name__ == "__main__":
    cli_main()
