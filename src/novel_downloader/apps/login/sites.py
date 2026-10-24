#!/usr/bin/env python3
"""
novel_downloader.apps.login.sites
---------------------------------

Declarative configuration for the interactive (pywebview) login flow.

Every site that can be logged in through a real browser window is described
by a :class:`SiteLoginConfig`.  The configuration contains all site-specific
knowledge needed to decide "is the user logged in yet?":

* the page that is opened first (:attr:`SiteLoginConfig.start_url`),
* URL keywords that identify an authentication page or an authenticated area,
* cookie names that must be present,
* an optional probe endpoint and how to interpret its response.

Keeping the rules in data rather than in the polling loop makes them easy to
unit-test offline and makes adding a site a one-entry change.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "LOGIN_SITE_CONFIGS",
    "ProbeKind",
    "SiteLoginConfig",
    "evaluate_probe_payload",
    "get_site_config",
    "login_sites",
    "normalize_site",
    "supported_sites",
]

#: How a probe response is interpreted.
#:
#: * ``cookies``      -- no HTTP probe; cookie rules alone decide.
#: * ``json_code``    -- parse JSON and compare a numeric field to a value.
#: * ``text_markers`` -- inspect a page body for markers.
ProbeKind = Literal["cookies", "json_code", "text_markers"]


@dataclass(frozen=True)
class SiteLoginConfig:
    """
    Login rules for a single site.

    :param site: Canonical site key (lower case).
    :param start_url: Page opened in the login window.
    :param required_cookies: Cookie names that must be present before a probe
        is attempted (and, for cookie-only sites, before login is accepted).
    :param cookie_names: When non-empty, only these cookies are considered and
        persisted; an empty tuple means "all cookies".
    :param probe_url: Optional endpoint used to confirm an authenticated
        session.  ``None`` means the cookie rules are authoritative.
    :param probe_kind: Interpretation of the probe response.
    :param json_code_field: Field inspected by the ``json_code`` mode.
    :param json_success_code: Value of ``json_code_field`` that means success.
    :param anonymous_markers: Substrings whose presence in a text probe proves
        the session is anonymous.  Checked before ``success_markers``.
    :param success_markers: Substrings required in a text probe (when set).
    :param login_url_keywords: URL substrings that identify an authentication
        page.  Used by cookie-only sites to avoid accepting partial cookies.
    :param success_url_keywords: URL substrings that identify an authenticated
        area.  Used by cookie-only sites as a positive signal.
    """

    site: str
    start_url: str
    required_cookies: tuple[str, ...] = ()
    cookie_names: tuple[str, ...] = ()
    probe_url: str | None = None
    probe_kind: ProbeKind = "cookies"
    json_code_field: str = "code"
    json_success_code: int = 0
    anonymous_markers: tuple[str, ...] = ()
    success_markers: tuple[str, ...] = ()
    login_url_keywords: tuple[str, ...] = ()
    success_url_keywords: tuple[str, ...] = ()

    @property
    def needs_probe(self) -> bool:
        """Return True when login has to be confirmed by an HTTP probe."""
        return self.probe_url is not None and self.probe_kind != "cookies"

    def has_required_cookies(self, cookies: Mapping[str, str]) -> bool:
        """
        Return True when every configured required cookie is present.

        :param cookies: Cookie name/value pairs.
        :return: True when no required cookie is missing.
        """
        return all(name in cookies for name in self.required_cookies)

    def filter_cookies(self, cookies: Mapping[str, str]) -> dict[str, str]:
        """
        Keep only the cookies this site wants persisted.

        :param cookies: Cookie name/value pairs.
        :return: A new dictionary; unchanged when no allow-list is configured.
        """
        if not self.cookie_names:
            return dict(cookies)
        allowed = set(self.cookie_names)
        return {name: value for name, value in cookies.items() if name in allowed}

    def is_login_url(self, url: str) -> bool:
        """
        Return True when *url* points at an authentication page.

        :param url: URL currently shown in the login window.
        :return: True when any ``login_url_keywords`` entry matches.
        """
        lowered = url.lower()
        return any(
            keyword and keyword.lower() in lowered
            for keyword in self.login_url_keywords
        )

    def is_success_url(self, url: str) -> bool:
        """
        Return True when *url* points at an authenticated area.

        :param url: URL currently shown in the login window.
        :return: True when any ``success_url_keywords`` entry matches.
        """
        lowered = url.lower()
        return any(
            keyword and keyword.lower() in lowered
            for keyword in self.success_url_keywords
        )

    def evaluate_probe(self, payload: str | bytes | None) -> bool:
        """
        Interpret a probe response using this site's rules.

        :param payload: Raw response body, or ``None`` when the request failed.
        :return: True when the payload represents an authenticated session.
        """
        return evaluate_probe_payload(self, payload)


def _decode(payload: str | bytes | None) -> str:
    """Decode a probe payload, returning an empty string for ``None``."""
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace")
    return payload


def evaluate_probe_payload(
    config: SiteLoginConfig, payload: str | bytes | None
) -> bool:
    """
    Evaluate a probe payload against a site's rules.

    This is the pure part of the login check: no network access, no window.

    :param config: Site login configuration.
    :param payload: Raw response body, or ``None`` when the request failed.
    :return: True when the payload represents an authenticated session.
    """
    text = _decode(payload)
    if config.probe_kind == "cookies":
        return True

    if config.probe_kind == "text_markers":
        if not text.strip():
            return False
        lowered = text.lower()
        if any(
            marker and marker.lower() in lowered for marker in config.anonymous_markers
        ):
            return False
        if config.success_markers:
            return any(
                marker and marker.lower() in lowered
                for marker in config.success_markers
            )
        return True

    # ``json_code`` mode.
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, Mapping):
        return False
    value = data.get(config.json_code_field)
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value == config.json_success_code
    if isinstance(value, str):
        try:
            return int(value.strip()) == config.json_success_code
        except ValueError:
            return False
    return False


def normalize_site(site: str) -> str:
    """
    Normalize a user-supplied site key.

    :param site: Site key, possibly with surrounding whitespace or different
        casing.
    :return: The canonical lower-case key (empty for empty input).
    """
    return str(site or "").strip().lower()


#: Sites with a working interactive login window.
#:
#: Only sites whose session cookies are actually consumed by the downloader
#: and whose login state can be verified are listed here.  ``fanqienovel`` is
#: the only site with a complete browser-login workflow today; its public
#: user-info endpoint reports ``code == 0`` for an authenticated session.
LOGIN_SITE_CONFIGS: dict[str, SiteLoginConfig] = {
    "fanqienovel": SiteLoginConfig(
        site="fanqienovel",
        start_url="https://fanqienovel.com/",
        probe_url="https://fanqienovel.com/api/user/info/v2",
        probe_kind="json_code",
        json_code_field="code",
        json_success_code=0,
        # A login page stays on the site's own host; the keywords are kept for
        # diagnostics and are exercised by the pure tests.
        login_url_keywords=("/login", "passport."),
    ),
}


def get_site_config(site: str) -> SiteLoginConfig | None:
    """
    Look up the login configuration of a site.

    :param site: Site key.
    :return: The matching configuration, or ``None`` when unsupported.
    """
    return LOGIN_SITE_CONFIGS.get(normalize_site(site))


def login_sites() -> dict[str, str]:
    """
    Return the supported login sites mapped to their entry URL.

    :return: A fresh ``site -> start_url`` dictionary.
    """
    return {site: config.start_url for site, config in LOGIN_SITE_CONFIGS.items()}


def supported_sites() -> tuple[str, ...]:
    """
    Return the supported login site keys in deterministic order.

    :return: Sorted tuple of site keys.
    """
    return tuple(sorted(LOGIN_SITE_CONFIGS))
