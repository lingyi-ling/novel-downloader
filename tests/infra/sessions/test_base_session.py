import ssl
from types import SimpleNamespace

import pytest

from novel_downloader.infra.sessions import create_session
from novel_downloader.infra.sessions.base import (
    NonRetryableConnectionError,
    as_non_retryable_connection_error,
    is_certificate_verification_error,
    is_proxy_authentication_error,
    iter_exception_chain,
)
from novel_downloader.schemas import FetcherConfig


@pytest.fixture
def cfg(tmp_path):
    return FetcherConfig(
        timeout=5,
        headers={"User-Agent": "pytest"},
        proxy=None,
        proxy_user=None,
        proxy_pass=None,
        trust_env=False,
        verify_ssl=False,
        max_connections=5,
    )


@pytest.mark.parametrize("backend", ["aiohttp", "httpx", "curl_cffi"])
@pytest.mark.asyncio
async def test_headers_returns_copy(backend, cfg):
    """BaseSession.headers should always return a copy."""
    session = create_session(backend, cfg, cookies=None)

    h1 = session.headers
    h2 = session.headers

    assert h1 == h2
    assert h1 is not h2

    # modifying the copy should not affect original headers
    h1["User-Agent"] = "modified"
    assert session.headers["User-Agent"] == "pytest"


@pytest.mark.parametrize("backend", ["aiohttp", "httpx", "curl_cffi"])
@pytest.mark.asyncio
async def test_async_context_manager_calls_init_and_close(backend, cfg):
    """`async with session:` should call init() and close()."""

    session = create_session(backend, cfg, cookies=None)

    # Should not have an active session yet
    assert session._session is None

    async with session as s2:
        # During context manager, session must be initialized
        assert s2._session is not None

    # After exiting context manager, the session must be closed
    assert session._session is None


# --------------------------------------------------------------------------- #
# Deterministic transport failure classification
# --------------------------------------------------------------------------- #
def test_certificate_verification_error_detected_directly() -> None:
    exc = ssl.SSLCertVerificationError(1, "certificate verify failed")

    assert is_certificate_verification_error(exc) is True


def test_certificate_verification_error_detected_through_cause_chain() -> None:
    """httpx/httpcore wrap the TLS error two levels deep."""
    inner = ssl.SSLCertVerificationError(1, "certificate has expired")
    middle = ConnectionError("connect failed")
    middle.__cause__ = inner
    outer = ConnectionError("transport failed")
    outer.__cause__ = middle

    assert is_certificate_verification_error(outer) is True


def test_plain_connection_error_is_not_a_certificate_failure() -> None:
    assert is_certificate_verification_error(ConnectionError("reset")) is False


def test_exception_chain_tolerates_a_cycle() -> None:
    first = ConnectionError("first")
    second = TimeoutError("second")
    first.__cause__ = second
    second.__cause__ = first

    assert list(iter_exception_chain(first)) == [first, second]


@pytest.mark.parametrize(
    "message",
    [
        "407 Proxy Authentication Required",
        "CONNECT tunnel failed, response 407",
        "Invalid username/password",
    ],
)
def test_proxy_authentication_messages_are_recognised(message: str) -> None:
    assert is_proxy_authentication_error(ConnectionError(message)) is True


def test_proxy_status_attribute_is_recognised() -> None:
    assert is_proxy_authentication_error(SimpleNamespace(status=407)) is True
    assert is_proxy_authentication_error(SimpleNamespace(status=502)) is False


def test_proxy_connection_failure_is_not_authentication() -> None:
    """A port that merely starts with 407 must not look like an HTTP 407."""
    error = ConnectionError("Failed to connect to proxy at 10.0.0.1:4070")

    assert is_proxy_authentication_error(error) is False


def test_non_retryable_error_stays_a_connection_error() -> None:
    """Existing ``except ConnectionError`` callers must keep working."""
    error = as_non_retryable_connection_error(
        ssl.SSLCertVerificationError(1, "bad certificate")
    )

    assert isinstance(error, ConnectionError)
    assert isinstance(error, NonRetryableConnectionError)
    assert error.retryable is False
    assert "bad certificate" in str(error)
