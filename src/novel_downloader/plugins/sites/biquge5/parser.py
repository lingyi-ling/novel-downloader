#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.biquge5.parser
---------------------------------------------

"""

from novel_downloader.plugins.common.parser.biquge1 import Biquge1Parser
from novel_downloader.plugins.registry import registrar


@registrar.register_parser()
class Biquge5Parser(Biquge1Parser):
    """
    Parser for 笔趣阁 book pages.
    """

    site_name: str = "biquge5"
    #: Fallback base for root-relative / protocol-relative ``og:image`` values
    #: on pages that carry no ``og:url``/canonical link (the fetcher already
    #: builds every request URL from the same origin).
    BASE_URL: str = "https://www.biquge5.com/"
