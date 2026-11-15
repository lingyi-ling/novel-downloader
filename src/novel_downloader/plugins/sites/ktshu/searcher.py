#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.ktshu.searcher
---------------------------------------------

"""

from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.sites.mangg_net.searcher import ManggNetSearcher


@registrar.register_searcher()
class KtshuSearcher(ManggNetSearcher):
    site_name = "ktshu"
    priority = 30
    BASE_URL = "https://www.ktshu.cc"
    SEARCH_URL = "https://www.ktshu.cc/search.php"

    @staticmethod
    def _url_to_id(url: str) -> str:
        # "/book/62773/" -> "62773"; absolute links, queries and the
        # ".html" suffix must not change the id.
        path = ManggNetSearcher._url_path(url).strip("/")
        if not path:
            return ""
        return path.rsplit("/", 1)[-1].split(".", 1)[0]
