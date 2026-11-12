"""Unit tests for the backend-neutral proxy resolver."""

from __future__ import annotations

import pytest

from novel_downloader.infra.sessions.proxy import (
    NoProxyEntry,
    ProxyConfig,
    no_proxy_patterns,
    parse_no_proxy,
    parse_proxy,
    proxy_from_env,
    redact_secrets,
    should_bypass_proxy,
    split_proxy_settings,
)


# --------------------------------------------------------------------------- #
# parse_proxy
# --------------------------------------------------------------------------- #
def test_blank_proxy_is_none() -> None:
    assert parse_proxy(None) is None
    assert parse_proxy("") is None
    # Whitespace-only used to reach the backends: aiohttp ignored it, httpx
    # raised "Unknown scheme for proxy URL URL('%20%20%20')" and curl failed
    # with "Unsupported proxy syntax in '   '".
    assert parse_proxy("   ") is None
    assert parse_proxy("\t\n") is None


def test_scheme_is_added_for_host_port() -> None:
    proxy = parse_proxy("127.0.0.1:8080")
    assert proxy is not None
    assert proxy.url == "http://127.0.0.1:8080"
    assert proxy.scheme == "http"
    assert proxy.port == 8080


def test_default_ports_are_filled_in() -> None:
    http_proxy = parse_proxy("http://proxy.example")
    https_proxy = parse_proxy("https://proxy.example")
    assert http_proxy is not None and http_proxy.port == 80
    assert https_proxy is not None and https_proxy.port == 443


def test_url_credentials_are_stripped_and_decoded() -> None:
    proxy = parse_proxy("http://us%40er:p%40ss@proxy.example:3128")
    assert proxy is not None
    assert proxy.url == "http://proxy.example:3128"
    assert proxy.auth == ("us@er", "p@ss")
    # The password must not be reachable from the URL handed to a library.
    assert "p@ss" not in proxy.url
    assert "p%40ss" not in proxy.url


def test_explicit_credentials_override_the_url() -> None:
    proxy = parse_proxy("http://urluser:urlpass@proxy.example:3128", "cfg", "secret")
    assert proxy is not None
    assert proxy.auth == ("cfg", "secret")
    assert proxy.url == "http://proxy.example:3128"


def test_user_without_password_is_kept() -> None:
    """A proxy may legitimately use an empty password.

    Both the old aiohttp and curl code required ``proxy_user and
    proxy_pass``, so a user-only configuration silently dropped the
    credentials and the proxy answered 407.
    """
    proxy = parse_proxy("http://proxy.example:3128", "user", "")
    assert proxy is not None
    assert proxy.auth == ("user", "")

    from_url = parse_proxy("http://user@proxy.example:3128")
    assert from_url is not None
    assert from_url.auth == ("user", "")


def test_password_without_user_is_ignored() -> None:
    proxy = parse_proxy("http://proxy.example:3128", None, "secret")
    assert proxy is not None
    assert proxy.auth is None


@pytest.mark.parametrize(
    "raw",
    ["http://", "://proxy:1", "http://proxy:notaport"],
)
def test_unusable_proxy_urls_raise_value_error(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_proxy(raw)


def test_invalid_proxy_message_is_redacted() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_proxy("http://user:topsecret@:8080")

    assert "topsecret" not in str(excinfo.value)


def test_repr_hides_the_password() -> None:
    proxy = parse_proxy("http://user:topsecret@proxy.example:3128")
    assert proxy is not None
    assert "topsecret" not in repr(proxy)
    assert "authenticated=True" in repr(proxy)


def test_secret_values_cover_password_and_userinfo() -> None:
    proxy = parse_proxy("http://user:topsecret@proxy.example:3128")
    assert proxy is not None
    secrets = proxy.secret_values()
    assert "topsecret" in secrets
    assert "user:topsecret" in secrets

    unauthenticated = parse_proxy("http://proxy.example:3128")
    assert unauthenticated is not None
    assert unauthenticated.secret_values() == ()


# --------------------------------------------------------------------------- #
# redact_secrets
# --------------------------------------------------------------------------- #
def test_redact_masks_userinfo() -> None:
    message = "Failed to connect to http://user:topsecret@proxy.example:3128"
    redacted = redact_secrets(message)
    assert "topsecret" not in redacted
    assert "http://***@proxy.example:3128" in redacted


def test_redact_masks_known_secret_values() -> None:
    message = "proxy rejected credentials for user:topsecret (topsecret)"
    redacted = redact_secrets(message, ("topsecret", "user:topsecret"))
    assert "topsecret" not in redacted


def test_redact_keeps_unrelated_text() -> None:
    assert redact_secrets("connection refused") == "connection refused"


# --------------------------------------------------------------------------- #
# NO_PROXY
# --------------------------------------------------------------------------- #
def test_no_proxy_wildcard() -> None:
    entries = parse_no_proxy("*")
    assert should_bypass_proxy(entries, "anything.example", 443)


def test_no_proxy_exact_and_subdomains() -> None:
    entries = parse_no_proxy("example.com")
    assert should_bypass_proxy(entries, "example.com")
    assert should_bypass_proxy(entries, "api.example.com")
    assert not should_bypass_proxy(entries, "notexample.com")


def test_no_proxy_leading_dot_excludes_the_apex() -> None:
    entries = parse_no_proxy(".example.com")
    assert not should_bypass_proxy(entries, "example.com")
    assert should_bypass_proxy(entries, "api.example.com")


def test_no_proxy_ports() -> None:
    entries = parse_no_proxy("example.com:8080")
    assert should_bypass_proxy(entries, "example.com", 8080)
    assert not should_bypass_proxy(entries, "example.com", 80)


def test_no_proxy_ip_literals_match_exactly() -> None:
    entries = parse_no_proxy("127.0.0.1,::1")
    assert should_bypass_proxy(entries, "127.0.0.1")
    assert should_bypass_proxy(entries, "::1")
    assert not should_bypass_proxy(entries, "127.0.0.10")


def test_no_proxy_ignores_garbage_entries() -> None:
    entries = parse_no_proxy(" , ,\t, ::")
    assert all(entry.host for entry in entries)


def test_httpx_patterns_match_the_same_hosts() -> None:
    """Patterns handed to httpx must reproduce the matcher's behaviour."""
    httpx = pytest.importorskip("httpx")
    from httpx._utils import URLPattern

    cases: list[tuple[str, str, bool]] = [
        ("example.com", "http://example.com/x", True),
        ("example.com", "http://api.example.com/x", True),
        ("example.com", "http://notexample.com/x", False),
        (".example.com", "http://example.com/x", False),
        (".example.com", "http://api.example.com/x", True),
        ("127.0.0.1", "http://127.0.0.1:9000/x", True),
        ("*", "http://anything/x", True),
    ]
    for raw, url, expected in cases:
        entries = parse_no_proxy(raw)
        assert should_bypass_proxy(entries, httpx.URL(url).host) is expected

        patterns = no_proxy_patterns(entries)
        if patterns == ("*",):
            assert expected is True
            continue
        matches = any(
            URLPattern(f"all://{p}").matches(httpx.URL(url)) for p in patterns
        )
        assert matches is expected, (raw, url)


# --------------------------------------------------------------------------- #
# Environment resolution
# --------------------------------------------------------------------------- #
def _proxy_settings(env: dict[str, str], **cfg: object):
    return split_proxy_settings(
        cfg.get("proxy"),  # type: ignore[arg-type]
        cfg.get("proxy_user"),  # type: ignore[arg-type]
        cfg.get("proxy_pass"),  # type: ignore[arg-type]
        bool(cfg.get("trust_env")),
    )


def test_explicit_proxy_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://env.example:3128")
    http_proxy, https_proxy, entries = split_proxy_settings(
        "http://cfg.example:8080", None, None, True
    )
    assert http_proxy is not None and http_proxy.host == "cfg.example"
    assert https_proxy is not None and https_proxy.host == "cfg.example"
    # An explicitly configured proxy is not bypassed by another process's
    # NO_PROXY.
    assert entries == ()


def test_environment_is_ignored_without_trust_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://env.example:3128")
    assert split_proxy_settings(None, None, None, False) == (None, None, ())


def test_all_proxy_is_the_fallback_for_both_schemes() -> None:
    http_proxy, https_proxy, _ = proxy_from_env({"ALL_PROXY": "http://all:3128"})
    assert http_proxy is not None and http_proxy.host == "all"
    assert https_proxy is not None and https_proxy.host == "all"


def test_scheme_specific_values_win_over_all_proxy() -> None:
    env = {
        "ALL_PROXY": "http://all:1",
        "http_proxy": "http://plain:2",
        "HTTPS_PROXY": "http://tls:3",
    }
    http_proxy, https_proxy, _ = proxy_from_env(env)
    assert http_proxy is not None and http_proxy.host == "plain"
    assert https_proxy is not None and https_proxy.host == "tls"


def test_lowercase_key_wins_over_uppercase() -> None:
    """curl's precedence; ``getproxies()`` resolves both to one arbitrary key."""
    env = {"http_proxy": "http://lower:1", "HTTP_PROXY": "http://upper:2"}
    http_proxy, _, _ = proxy_from_env(env)
    assert http_proxy is not None and http_proxy.host == "lower"


def test_blank_environment_values_are_ignored() -> None:
    http_proxy, https_proxy, entries = proxy_from_env(
        {"HTTP_PROXY": "   ", "NO_PROXY": ""}
    )
    assert http_proxy is None
    assert https_proxy is None
    assert entries == ()


def test_no_proxy_is_read_from_the_environment() -> None:
    _, _, entries = proxy_from_env({"http_proxy": "http://p:1", "NO_PROXY": "a,b"})
    assert should_bypass_proxy(entries, "b")


def test_environment_credentials_are_split_out() -> None:
    http_proxy, _, _ = proxy_from_env({"http_proxy": "http://u:topsecret@h:1"})
    assert http_proxy is not None
    assert http_proxy.url == "http://h:1"
    assert http_proxy.auth == ("u", "topsecret")


def test_no_proxy_entry_dataclass_is_public() -> None:
    # Guards the dataclass against accidental field removal: the httpx mount
    # builder and the matcher both consume it.
    entry = NoProxyEntry(host="example.com")
    assert entry.wildcard is False
    assert isinstance(parse_proxy("http://h:1"), ProxyConfig)
