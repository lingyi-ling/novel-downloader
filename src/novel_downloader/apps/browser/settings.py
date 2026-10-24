#!/usr/bin/env python3
"""
novel_downloader.apps.browser.settings
--------------------------------------

Configuration for browser-assisted search.

Some sites can only be searched by their own JavaScript, so a real browser
window has to issue the request.  Which sites need that treatment used to be
knowledge baked into the search orchestration; it is now read from
``settings.toml``::

    [general]
    browser_assisted_sites = ["fanqienovel"]

The default is :data:`DEFAULT_BROWSER_ASSISTED_SITES`, which keeps the
historical ``fanqienovel`` behaviour.  An explicit empty list disables
browser-assisted search entirely (those sites then fall back to plain HTTP).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from novel_downloader.infra.config import load_config

__all__ = [
    "DEFAULT_BROWSER_ASSISTED_SITES",
    "browser_assisted_sites",
    "is_browser_assisted_site",
    "parse_browser_assisted_sites",
]

logger = logging.getLogger(__name__)

#: Sites that need a real browser unless configured otherwise.
DEFAULT_BROWSER_ASSISTED_SITES: tuple[str, ...] = ("fanqienovel",)

#: Location of the setting inside ``settings.toml``.
CONFIG_SECTION = "general"
CONFIG_KEY = "browser_assisted_sites"


def _normalize(site: str) -> str:
    return site.strip().lower()


def parse_browser_assisted_sites(value: Any) -> tuple[str, ...] | None:
    """
    Parse the configured browser-assisted site list.

    Accepts a TOML array of strings or a comma-separated string.  Entries are
    stripped, lower-cased and de-duplicated while preserving their order.

    :param value: Raw configuration value.
    :return: The parsed site keys.  An empty tuple means browser-assisted
        search is disabled.  ``None`` means the value could not be understood
        and the caller should fall back to the default.
    """
    if isinstance(value, str):
        raw_items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        return None

    sites: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            logger.warning("browser_assisted_sites: ignoring non-string entry %r", item)
            continue
        site = _normalize(item)
        if site and site not in sites:
            sites.append(site)
    return tuple(sites)


def browser_assisted_sites(
    config_path: str | None = None,
) -> tuple[str, ...]:
    """
    Return the site keys that require browser-assisted search.

    A missing configuration file or a missing key yields
    :data:`DEFAULT_BROWSER_ASSISTED_SITES`.  An unreadable or malformed value
    also falls back to the default instead of breaking search.

    :param config_path: Optional explicit ``settings.toml`` path.
    :return: Lower-cased site keys.
    """
    try:
        data = load_config(config_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        logger.debug("browser_assisted_sites: no usable config: %s", exc)
        return DEFAULT_BROWSER_ASSISTED_SITES

    section = data.get(CONFIG_SECTION)
    if not isinstance(section, Mapping):
        return DEFAULT_BROWSER_ASSISTED_SITES

    value = section.get(CONFIG_KEY)
    if value is None:
        return DEFAULT_BROWSER_ASSISTED_SITES

    parsed = parse_browser_assisted_sites(value)
    if parsed is None:
        logger.warning(
            "browser_assisted_sites: expected a list of site keys, got %s; "
            "using the default %s",
            type(value).__name__,
            list(DEFAULT_BROWSER_ASSISTED_SITES),
        )
        return DEFAULT_BROWSER_ASSISTED_SITES
    return parsed


def is_browser_assisted_site(site: str, config_path: str | None = None) -> bool:
    """
    Whether ``site`` is configured to use browser-assisted search.

    :param site: Site key.
    :param config_path: Optional explicit ``settings.toml`` path.
    :return: True when the site is listed in ``browser_assisted_sites``.
    """
    return _normalize(site) in browser_assisted_sites(config_path)
