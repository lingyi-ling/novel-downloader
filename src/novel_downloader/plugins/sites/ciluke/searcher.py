#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.ciluke.searcher
----------------------------------------------

"""

from urllib.parse import urlsplit

from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.sites.mangg_net.searcher import ManggNetSearcher


@registrar.register_searcher()
class CilukeSearcher(ManggNetSearcher):
    site_name = "ciluke"
    priority = 30
    BASE_URL = "https://www.ciluke.com/"
    SEARCH_URL = "https://www.ciluke.com/search.php"

    @staticmethod
    def _url_to_id(url: str) -> str:
        # "/54/54978/" -> "54-54978"; absolute and protocol-relative links
        # with a query string or fragment yield the same id.
        path = urlsplit(url).path.strip("/")
        if not path:
            return ""
        path = path.split(".", 1)[0].rstrip("/")
        return path.replace("/", "-")
