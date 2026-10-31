#!/usr/bin/env python3
"""
novel_downloader.apps.desktop
-----------------------------

Native desktop client: a pywebview window hosting the single-file HTML front
end (:mod:`.web_assets`), driven through the :class:`.webview_app.ApiBridge`.
"""

__all__ = ["desktop_main"]

from .main import desktop_main
