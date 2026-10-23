#!/usr/bin/env python3
"""
novel_downloader.apps.music
---------------------------

The music download engine, merged from the standalone NetEase downloader.

Submodules are imported lazily so that the optional ``music`` dependency
group (requests / pycryptodome / mutagen / Pillow / qrcode / websocket-client)
is only required when a music feature is actually used:

* :mod:`~novel_downloader.apps.music.weapi` – NetEase WEAPI client
* :mod:`~novel_downloader.apps.music.downloader` – download engine
* :mod:`~novel_downloader.apps.music.providers` – multi-platform providers
* :mod:`~novel_downloader.apps.music.browser_login` – browser-assisted login
* :mod:`~novel_downloader.apps.music.paths` – runtime data locations
* :mod:`~novel_downloader.apps.music.settings` – preferences
"""

__all__ = [
    "browser_login",
    "downloader",
    "paths",
    "providers",
    "settings",
    "weapi",
]
