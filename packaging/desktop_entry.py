#!/usr/bin/env python3
"""Frozen entry point for the ``NovelDownloader`` desktop executable.

PyInstaller analyses this module as the program entry; it only imports the
public launcher, which itself defers creating the pywebview window (and the
Edge WebView2 runtime it needs) until start-up.
"""

from __future__ import annotations

from novel_downloader.apps.desktop.main import desktop_main

if __name__ == "__main__":
    desktop_main()
