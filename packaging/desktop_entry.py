#!/usr/bin/env python3
"""Frozen entry point for the ``NovelDownloader`` desktop executable.

PyInstaller analyses this module as the program entry; it only imports the
public launcher, which itself defers ``tk.Tk()`` until the window is created.
"""

from __future__ import annotations

from novel_downloader.apps.desktop.main import desktop_main

if __name__ == "__main__":
    desktop_main()
