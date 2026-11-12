#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.xshbook.searcher
-----------------------------------------------

"""

from novel_downloader.plugins.common.searcher.sososhu import SososhuSearcher
from novel_downloader.plugins.registry import registrar


@registrar.register_searcher()
class XshbookSearcher(SososhuSearcher):
    site_name = "xshbook"
    SOSOSHU_KEY = "xshbook"
    BASE_URL = "https://www.xshbook.com"

    @staticmethod
    def _url_to_id(url: str) -> str:
        # A result link may carry tracking parameters (``?from=...``); they are
        # not part of the id and would otherwise end up in the ``95139-95139418``
        # folder name used to rebuild the chapter URL.
        path = url.split("xshbook.com", 1)[-1].split("#", 1)[0].split("?", 1)[0]
        return path.strip("/").replace("/", "-")
