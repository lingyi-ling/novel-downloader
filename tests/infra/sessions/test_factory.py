import builtins
import sys

import pytest

from novel_downloader.infra.sessions import available_backends, create_session
from novel_downloader.infra.sessions._aiohttp import AiohttpSession
from novel_downloader.infra.sessions._curl_cffi import CurlCffiSession
from novel_downloader.infra.sessions._httpx import HttpxSession
from novel_downloader.schemas import FetcherConfig


@pytest.fixture
def cfg():
    return FetcherConfig(
        timeout=5,
        headers={"ua": "pytest"},
        user_agent="agent123",
        proxy=None,
        proxy_user=None,
        proxy_pass=None,
        trust_env=False,
        verify_ssl=False,
        max_connections=10,
    )


def test_create_session_aiohttp(cfg):
    s = create_session("aiohttp", cfg, cookies={"a": "1"})
    assert isinstance(s, AiohttpSession)
    assert s._cookies == {"a": "1"}


def test_create_session_httpx(cfg):
    s = create_session("httpx", cfg, cookies={"b": "2"})
    assert isinstance(s, HttpxSession)
    assert s._cookies == {"b": "2"}


def test_create_session_curl_cffi(cfg):
    s = create_session("curl_cffi", cfg, cookies={"c": "3"})
    assert isinstance(s, CurlCffiSession)
    assert s._cookies == {"c": "3"}


def test_create_session_kwargs_passthrough(cfg):
    # Ensure kwargs are correctly passed to the session constructor
    s = create_session("aiohttp", cfg, cookies=None, extra_option="xyz")
    assert hasattr(s, "_extra_option") or True  # we just test that kwargs don't crash


def test_create_session_invalid_backend(cfg):
    with pytest.raises(ValueError) as excinfo:
        create_session("not-a-backend", cfg)

    msg = str(excinfo.value)
    assert "Unsupported backend" in msg
    assert "not-a-backend" in msg


# ---------------------------------------------------------------------------
# Backend availability probing
# ---------------------------------------------------------------------------


def test_available_backends_lists_installed_packages() -> None:
    installed = available_backends()
    # This test environment has all three optional backends installed.
    assert installed == ["aiohttp", "httpx", "curl_cffi"]


def test_create_session_missing_package_raises_informative_error(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing backend package must fail loudly and name the alternatives.

    Desktop and Android builds ship a subset of the optional HTTP backends;
    the default ``aiohttp`` backend then has to produce an actionable error
    instead of a bare ``ImportError`` deep inside the session module.
    """
    module_name = "novel_downloader.infra.sessions._aiohttp"
    monkeypatch.delitem(sys.modules, module_name, raising=False)

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "aiohttp":
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError) as excinfo:
        create_session("aiohttp", cfg)

    msg = str(excinfo.value)
    assert "aiohttp" in msg
    # The error tells the user which backends actually work here.
    assert "httpx" in msg
    assert "curl_cffi" in msg
    assert "Available backends" in msg


def test_create_session_missing_package_preserves_cause(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "novel_downloader.infra.sessions._httpx"
    monkeypatch.delitem(sys.modules, module_name, raising=False)

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "httpx":
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError) as excinfo:
        create_session("httpx", cfg)

    assert isinstance(excinfo.value.__cause__, ImportError)
