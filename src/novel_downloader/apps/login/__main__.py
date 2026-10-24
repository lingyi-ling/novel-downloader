#!/usr/bin/env python3
"""
novel_downloader.apps.login.__main__
------------------------------------

Allow ``python -m novel_downloader.apps.login`` to run the login window,
mirroring the ``__main__`` modules of the other apps.
"""

from __future__ import annotations

from .main import login_main

if __name__ == "__main__":
    raise SystemExit(login_main())
