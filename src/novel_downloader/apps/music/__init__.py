#!/usr/bin/env python3
"""
novel_downloader.apps.music
---------------------------

The music download engine, merged from the standalone NetEase downloader.

Submodules are imported lazily so that the optional ``music`` dependency
group (requests / pycryptodome / mutagen / Pillow / qrcode / websocket-client)
is only required when a music feature is actually used. The group also
carries the modules the vendored platform adapters import at load time
(m3u8 / emoji / bleach / filetype / puremagic / fake-useragent /
json-repair / tinytag / tabulate / prettytable / wcwidth /
prompt_toolkit); without them, providers such as 5sing or migu fail
before issuing a request:

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
