#!/usr/bin/env python3
"""Frozen entry point for the ``novel-api`` console executable."""

from __future__ import annotations

from novel_downloader.apps.api.main import api_main

if __name__ == "__main__":
    api_main()
