#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.__main__
--------------------------------------

Allow ``python -m novel_downloader.apps.desktop`` to open the desktop window.
"""

from .main import desktop_main

if __name__ == "__main__":
    desktop_main()
