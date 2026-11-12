"""Unit coverage for :mod:`novel_downloader.apps.cli.interactive`.

The interactive wizard is the guided entry point of ``novel-cli`` (shown when
the app runs with no subcommand).  Its branching -- cancelled prompts,
unresolvable URLs, failed logins and download errors -- had no tests at all
before this file; every branch below is driven with the ``ui`` / ``prompts``
layers replaced by recorders so no console input is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.apps.cli import interactive
from novel_downloader.schemas import BookConfig

# The ``rec`` fixture patches these module attributes so higher-level flows
# hit the fakes; tests that exercise the real functions call these references.
_REAL_DO_DOWNLOAD = interactive._do_download
_REAL_EXPORT = interactive._interactive_export

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeAdapter:
    """Stand-in for ConfigAdapter covering every method the wizard calls."""

    def __init__(
        self,
        *,
        login_required: bool = False,
        processors: list[dict[str, Any]] | None = None,
        formats: list[str] | None = None,
        exporter_cfg: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        plugins_config: dict[str, Any] | None = None,
    ) -> None:
        self._login_required = login_required
        self._processors = processors if processors is not None else []
        self._formats = formats if formats is not None else ["txt"]
        self._exporter_cfg = exporter_cfg if exporter_cfg is not None else {}
        self._config = config if config is not None else {}
        self._plugins_config = plugins_config if plugins_config is not None else {}

    def get_log_level(self) -> str:
        return "INFO"

    def get_plugins_config(self) -> dict[str, Any]:
        return self._plugins_config

    def get_client_config(self, site: str) -> dict[str, Any]:
        return {}

    def get_login_required(self, site: str) -> bool:
        return self._login_required

    def get_login_config(self, site: str) -> dict[str, Any]:
        return {}

    def get_processor_configs(self, site: str) -> list[dict[str, Any]]:
        return self._processors

    def get_export_fmt(self, site: str) -> list[str]:
        return self._formats

    def get_exporter_config(self, site: str) -> dict[str, Any]:
        return self._exporter_cfg

    def get_config(self) -> dict[str, Any]:
        return self._config


class FakeClient:
    """Site client with recording hooks and optional failures."""

    def __init__(self, *, login_ok: bool = True, fail_with: Exception | None = None):
        self.login_calls: list[dict[str, Any]] = []
        self.download_calls: list[BookConfig] = []
        self.process_calls: list[tuple[BookConfig, list[Any]]] = []
        self.export_calls: list[BookConfig] = []
        self._login_ok = login_ok
        self._fail_with = fail_with

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def login(self, *, ui: Any, login_cfg: Any) -> bool:
        self.login_calls.append({"ui": ui, "login_cfg": login_cfg})
        return self._login_ok

    async def download_book(self, book: BookConfig, *, ui: Any) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.download_calls.append(book)

    def process_book(self, book: BookConfig, *, processors: Any, ui: Any) -> None:
        self.process_calls.append((book, processors))

    def export_book(self, book: BookConfig, *, cfg: Any, formats: Any, ui: Any) -> None:
        self.export_calls.append(book)


@pytest.fixture()
def rec(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the ui/prompt layers with recorders and collect call logs."""
    calls: dict[str, list[Any]] = {
        "warn": [],
        "error": [],
        "success": [],
        "confirm": [],
        "prompt": [],
        "prompt_choice": [],
        "download": [],
        "export": [],
        "search": [],
        "asyncio_run": [],
    }

    monkeypatch.setattr(interactive.ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(
        interactive.ui, "success", lambda msg, **k: calls["success"].append(msg)
    )
    monkeypatch.setattr(
        interactive.ui, "warn", lambda msg, **k: calls["warn"].append(msg)
    )
    monkeypatch.setattr(
        interactive.ui, "error", lambda msg, **k: calls["error"].append(msg)
    )
    monkeypatch.setattr(interactive.ui, "setup_logging", lambda *a, **k: None)

    state: dict[str, Any] = {
        "confirm_result": True,
        "choice": "u",
        "prompts": [],
        "search_results": [],
        "selected_result": None,
        "sites": [],
        "books": [],
        "actions": [],
    }

    def fake_confirm(message: str, *, default: bool = False) -> bool:
        calls["confirm"].append(message)
        return state["confirm_result"]

    def fake_prompt(message: str = "", *, default: str | None = None) -> str:
        calls["prompt"].append(message)
        if not state["prompts"]:
            return ""
        return state["prompts"].pop(0)

    def fake_prompt_choice(prompt_text: str, choices: Any) -> str:
        calls["prompt_choice"].append(prompt_text)
        return state["choice"]

    monkeypatch.setattr(interactive.ui, "confirm", fake_confirm)
    monkeypatch.setattr(interactive.ui, "prompt", fake_prompt)
    monkeypatch.setattr(interactive.ui, "prompt_choice", fake_prompt_choice)

    class _Status:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(interactive.ui, "status", lambda message: _Status())

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls["search"].append(keyword)
        if isinstance(state["search_results"], Exception):
            raise state["search_results"]
        return state["search_results"]

    monkeypatch.setattr(interactive, "search", fake_search)

    monkeypatch.setattr(
        interactive.prompts,
        "select_main_action",
        lambda: state["actions"].pop(0) if state["actions"] else "",
    )
    monkeypatch.setattr(
        interactive.prompts,
        "select_search_result",
        lambda results: state["selected_result"],
    )
    monkeypatch.setattr(
        interactive.prompts,
        "select_site",
        lambda raw_dir: state["sites"][0] if state["sites"] else None,
    )
    monkeypatch.setattr(
        interactive.prompts, "select_books", lambda raw_dir, site: state["books"]
    )

    async def fake_do_download(adapter: Any, site: str, book: BookConfig) -> None:
        calls["download"].append((site, book))

    monkeypatch.setattr(interactive, "_do_download", fake_do_download)

    def fake_export(adapter: Any, site: str | None = None, book: Any = None) -> None:
        calls["export"].append((site, book))

    monkeypatch.setattr(interactive, "_interactive_export", fake_export)

    return {"calls": calls, "state": state, "ui": interactive.ui}


# --------------------------------------------------------------------------- #
# start_interactive
# --------------------------------------------------------------------------- #
def test_start_interactive_exits_nonzero_without_config(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """A missing/declined config is a failure, not a successful no-op."""
    monkeypatch.setattr(interactive, "load_or_init_config", lambda: None)
    prompted: list[bool] = []
    monkeypatch.setattr(
        interactive.prompts, "select_main_action", lambda: prompted.append(True) or ""
    )

    with pytest.raises(SystemExit) as excinfo:
        interactive.start_interactive()

    assert excinfo.value.code == 1
    assert prompted == [], "no menu prompt when the config cannot be loaded"


def test_start_interactive_dispatches_menu_actions(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    adapter = FakeAdapter()
    monkeypatch.setattr(interactive, "load_or_init_config", lambda: {"general": {}})
    monkeypatch.setattr(interactive, "ConfigAdapter", lambda config: adapter)

    ran: list[Any] = []

    def fake_run(coro: Any) -> None:
        ran.append(coro)
        coro.close()

    monkeypatch.setattr(interactive.asyncio, "run", fake_run)

    state = rec["state"]
    state["actions"] = ["1", "2", "3", "9", ""]
    interactive.start_interactive()

    # two coroutines were dispatched (search + download), export ran inline
    assert len(ran) == 2
    assert len(rec["calls"]["export"]) == 1
    assert rec["calls"]["warn"], "invalid choice should warn"


def test_start_interactive_enables_local_plugins_when_configured(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    enabled: list[tuple[Any, bool]] = []
    monkeypatch.setattr(
        interactive.registrar,
        "enable_local_plugins",
        lambda path, override: enabled.append((path, override)),
    )
    monkeypatch.setattr(interactive, "load_or_init_config", lambda: {})
    monkeypatch.setattr(
        interactive,
        "ConfigAdapter",
        lambda config: FakeAdapter(
            plugins_config={
                "enable_local_plugins": True,
                "local_plugins_path": "D:/plugins",
                "override_builtins": True,
            }
        ),
    )

    interactive.start_interactive()

    assert enabled == [("D:/plugins", True)]


# --------------------------------------------------------------------------- #
# _interactive_search
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_search_requires_keyword(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    state = rec["state"]
    state["prompts"] = []  # empty keyword

    await interactive._interactive_search(FakeAdapter())

    assert rec["calls"]["search"] == []


@pytest.mark.asyncio
async def test_search_reports_failure(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    state = rec["state"]
    state["prompts"] = ["斗破苍穹"]
    state["search_results"] = RuntimeError("boom")

    await interactive._interactive_search(FakeAdapter())

    assert rec["calls"]["error"], "a failing search must be reported"


@pytest.mark.asyncio
async def test_search_cancelled_at_selection(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    state = rec["state"]
    state["prompts"] = ["keyword"]
    state["search_results"] = []
    state["selected_result"] = None

    await interactive._interactive_search(FakeAdapter())

    assert rec["calls"]["download"] == []


@pytest.mark.asyncio
async def test_search_result_confirmed_downloads(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    state = rec["state"]
    state["prompts"] = ["keyword"]
    state["search_results"] = [{"title": "x"}]
    state["selected_result"] = {"site": "qidian", "book_id": "42"}

    await interactive._interactive_search(FakeAdapter())

    assert rec["calls"]["download"] == [("qidian", BookConfig(book_id="42"))]


@pytest.mark.asyncio
async def test_search_result_declined_skips_download(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    state = rec["state"]
    state["prompts"] = ["keyword"]
    state["search_results"] = []
    state["selected_result"] = {"site": "qidian", "book_id": "42"}
    state["confirm_result"] = False

    await interactive._interactive_search(FakeAdapter())

    assert rec["calls"]["download"] == []


@pytest.mark.asyncio
async def test_search_without_results_does_not_claim_cancellation(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """An empty result list is not a user cancellation.

    The real ``select_search_result`` already warned "No results found."; the
    wizard must not then also report "Cancelled.", which reads like the user
    backed out of results that never existed.
    """
    state = rec["state"]
    state["prompts"] = ["keyword"]
    state["search_results"] = []
    state["selected_result"] = None

    await interactive._interactive_search(FakeAdapter())

    assert rec["calls"]["warn"] == []


@pytest.mark.parametrize(
    "selected",
    [
        {"site": "qidian"},  # no book_id
        {"book_id": "42"},  # no site
        {"site": "", "book_id": "42"},  # empty site
        {"site": "qidian", "book_id": ""},  # empty book_id
    ],
)
@pytest.mark.asyncio
async def test_search_result_missing_identifiers_is_reported(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any], selected: dict[str, str]
) -> None:
    """A malformed selected row must be an error, not a ``KeyError``."""
    state = rec["state"]
    state["prompts"] = ["keyword"]
    state["search_results"] = [{"title": "x"}]
    state["selected_result"] = selected

    await interactive._interactive_search(FakeAdapter())

    assert rec["calls"]["error"]
    assert rec["calls"]["download"] == []


# --------------------------------------------------------------------------- #
# _interactive_download
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_download_cancelled_before_mode_choice(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["choice"] = ""

    await interactive._interactive_download(FakeAdapter())

    assert rec["calls"]["warn"]


@pytest.mark.asyncio
async def test_download_by_url_requires_url(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["choice"] = "u"
    rec["state"]["prompts"] = []  # empty URL

    await interactive._interactive_download(FakeAdapter())

    assert rec["calls"]["warn"]
    assert rec["calls"]["download"] == []


@pytest.mark.asyncio
async def test_download_by_url_reports_unrecognized_url(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["choice"] = "u"
    rec["state"]["prompts"] = ["https://unknown.example/book/1"]

    await interactive._interactive_download(FakeAdapter())

    assert rec["calls"]["error"]
    assert rec["calls"]["download"] == []


@pytest.mark.asyncio
async def test_download_by_url_reports_resolver_failure(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """A crashing extractor is reported, never a raw traceback."""
    rec["state"]["choice"] = "u"
    rec["state"]["prompts"] = ["https://broken.example/book/1"]

    def _boom(url: str) -> None:
        raise RuntimeError("bad url")

    monkeypatch.setattr(interactive, "resolve_book_url", _boom)

    await interactive._interactive_download(FakeAdapter())

    assert rec["calls"]["error"]
    assert rec["calls"]["download"] == []


@pytest.mark.asyncio
async def test_download_by_url_requires_book_id(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["choice"] = "u"
    rec["state"]["prompts"] = ["https://www.uaa.com/novel/chapter?id=1"]
    monkeypatch.setattr(
        interactive,
        "resolve_book_url",
        lambda url: {"site_key": "uaa", "book_id": None, "chapter_id": "1"},
    )

    await interactive._interactive_download(FakeAdapter())

    assert rec["calls"]["error"]
    assert rec["calls"]["download"] == []


@pytest.mark.asyncio
async def test_download_by_url_resolves_site_and_book(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["choice"] = "u"
    rec["state"]["prompts"] = ["https://www.qidian.com/book/101/"]
    monkeypatch.setattr(
        interactive,
        "resolve_book_url",
        lambda url: {"site_key": "qidian", "book_id": "101", "chapter_id": None},
    )

    await interactive._interactive_download(FakeAdapter())

    assert rec["calls"]["download"] == [("qidian", BookConfig(book_id="101"))]


@pytest.mark.asyncio
async def test_download_by_site_and_id_requires_both(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["choice"] = "i"
    rec["state"]["prompts"] = ["qidian", ""]  # missing book id

    await interactive._interactive_download(FakeAdapter())

    assert rec["calls"]["warn"]
    assert rec["calls"]["download"] == []


@pytest.mark.asyncio
async def test_download_by_site_and_id_dispatches(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["choice"] = "i"
    rec["state"]["prompts"] = ["qidian", "101"]

    await interactive._interactive_download(FakeAdapter())

    assert rec["calls"]["download"] == [("qidian", BookConfig(book_id="101"))]


# --------------------------------------------------------------------------- #
# _do_download
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_do_download_runs_without_login_when_not_required(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    client = FakeClient()
    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: client
    )

    await _REAL_DO_DOWNLOAD(FakeAdapter(), "qidian", BookConfig(book_id="1"))

    assert client.login_calls == []
    assert len(client.download_calls) == 1
    assert len(client.process_calls) == 1
    assert rec["calls"]["export"], "successful download offers export"


@pytest.mark.asyncio
async def test_do_download_declined_export(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    client = FakeClient()
    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: client
    )
    rec["state"]["confirm_result"] = False

    await _REAL_DO_DOWNLOAD(FakeAdapter(), "qidian", BookConfig(book_id="1"))

    assert rec["calls"]["export"] == []


@pytest.mark.asyncio
async def test_do_download_login_required_success(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    client = FakeClient(login_ok=True)
    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: client
    )

    await _REAL_DO_DOWNLOAD(
        FakeAdapter(login_required=True), "qidian", BookConfig(book_id="1")
    )

    assert len(client.login_calls) == 1
    assert len(client.download_calls) == 1


@pytest.mark.asyncio
async def test_do_download_login_required_failure(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    client = FakeClient(login_ok=False)
    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: client
    )

    await _REAL_DO_DOWNLOAD(
        FakeAdapter(login_required=True), "qidian", BookConfig(book_id="1")
    )

    assert len(client.login_calls) == 1
    assert client.download_calls == []
    assert client.process_calls == []
    assert rec["calls"]["warn"], "failed login must warn"
    assert rec["calls"]["export"] == []


@pytest.mark.asyncio
async def test_do_download_unsupported_site_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    client = FakeClient(fail_with=ValueError("Unsupported site: nope"))
    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: client
    )

    await _REAL_DO_DOWNLOAD(FakeAdapter(), "nope", BookConfig(book_id="1"))

    assert rec["calls"]["warn"]
    assert rec["calls"]["error"] == []


@pytest.mark.asyncio
async def test_do_download_generic_error_is_reported(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    client = FakeClient(fail_with=RuntimeError("network down"))
    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: client
    )

    await _REAL_DO_DOWNLOAD(FakeAdapter(), "qidian", BookConfig(book_id="1"))

    assert rec["calls"]["error"]
    assert rec["calls"]["export"] == []


@pytest.mark.asyncio
async def test_do_download_registry_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """``get_client`` runs before the download; it must be guarded too."""

    def _boom(site: str, config: Any = None) -> None:
        raise ValueError("Unsupported site: nope")

    monkeypatch.setattr(interactive.registrar, "get_client", _boom)

    await _REAL_DO_DOWNLOAD(FakeAdapter(), "nope", BookConfig(book_id="1"))

    assert rec["calls"]["warn"]
    assert rec["calls"]["error"] == []


@pytest.mark.asyncio
async def test_do_download_processing_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """A crashing processor must not escape the wizard as a traceback."""

    class FailingProcessClient(FakeClient):
        def process_book(self, book: BookConfig, *, processors: Any, ui: Any) -> None:
            raise RuntimeError("bad processor")

    monkeypatch.setattr(
        interactive.registrar,
        "get_client",
        lambda site, config=None: FailingProcessClient(),
    )

    await _REAL_DO_DOWNLOAD(FakeAdapter(), "qidian", BookConfig(book_id="1"))

    assert rec["calls"]["error"]
    assert rec["calls"]["export"] == []


@pytest.mark.asyncio
async def test_do_download_does_not_swallow_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """The broad handlers must not turn Ctrl+C into a download error."""

    class InterruptClient(FakeClient):
        async def download_book(self, book: BookConfig, *, ui: Any) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: InterruptClient()
    )

    with pytest.raises(KeyboardInterrupt):
        await _REAL_DO_DOWNLOAD(FakeAdapter(), "qidian", BookConfig(book_id="1"))


# --------------------------------------------------------------------------- #
# _interactive_export
# --------------------------------------------------------------------------- #
def test_export_without_selected_site_warns(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["sites"] = []

    _REAL_EXPORT(FakeAdapter(config={"general": {}}))

    assert rec["calls"]["warn"]


def test_export_without_selected_books_warns(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    rec["state"]["sites"] = ["qidian"]
    rec["state"]["books"] = []

    _REAL_EXPORT(FakeAdapter(config={"general": {}}))

    assert rec["calls"]["warn"]


def test_export_uses_the_first_selected_book(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    client = FakeClient()
    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: client
    )
    rec["state"]["sites"] = ["qidian"]
    rec["state"]["books"] = ["11", "12"]

    _REAL_EXPORT(FakeAdapter(config={"general": {}}))

    assert client.export_calls == [BookConfig(book_id="11")]


def test_export_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    class FailingClient(FakeClient):
        def export_book(
            self, book: BookConfig, *, cfg: Any, formats: Any, ui: Any
        ) -> None:
            raise RuntimeError("disk full")

    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: FailingClient()
    )

    _REAL_EXPORT(FakeAdapter(), site="qidian", book=BookConfig(book_id="1"))

    assert rec["calls"]["error"]


def test_export_explicit_site_and_book_skips_prompts(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    client = FakeClient()
    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: client
    )

    _REAL_EXPORT(FakeAdapter(), site="qidian", book=BookConfig(book_id="7"))

    assert client.export_calls == [BookConfig(book_id="7")]


def test_export_without_any_output_is_reported(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """``export_book`` reports a failed format as an empty list, not an error."""

    class EmptyClient(FakeClient):
        def export_book(
            self, book: BookConfig, *, cfg: Any, formats: Any, ui: Any
        ) -> dict[str, list[Any]]:
            self.export_calls.append(book)
            return {"txt": []}

    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: EmptyClient()
    )

    _REAL_EXPORT(FakeAdapter(), site="qidian", book=BookConfig(book_id="1"))

    assert rec["calls"]["error"]


def test_export_reports_client_construction_failure(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """``get_client`` is part of export; its failure must be reported."""

    def _boom(site: str, config: Any = None) -> None:
        raise ValueError("Unsupported site: nope")

    monkeypatch.setattr(interactive.registrar, "get_client", _boom)

    _REAL_EXPORT(FakeAdapter(), site="nope", book=BookConfig(book_id="1"))

    assert rec["calls"]["error"]
    assert rec["calls"]["success"] == []


def test_export_non_dict_result_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """A third-party client returning ``None`` must not be called success."""

    class NoneClient(FakeClient):
        def export_book(
            self, book: BookConfig, *, cfg: Any, formats: Any, ui: Any
        ) -> Any:
            self.export_calls.append(book)
            return None

    monkeypatch.setattr(
        interactive.registrar, "get_client", lambda site, config=None: NoneClient()
    )

    _REAL_EXPORT(FakeAdapter(), site="qidian", book=BookConfig(book_id="1"))

    assert rec["calls"]["error"]
    assert rec["calls"]["success"] == []


def test_export_passes_the_configured_client_to_get_client(
    monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]
) -> None:
    """Exports must use the configured output/raw-data directories."""
    seen: dict[str, Any] = {}
    adapter = FakeAdapter()
    monkeypatch.setattr(
        adapter, "get_client_config", lambda _site: {"output_dir": "configured"}
    )

    def _get_client(site: str, config: Any = None) -> FakeClient:
        seen["config"] = config
        return FakeClient()

    monkeypatch.setattr(interactive.registrar, "get_client", _get_client)

    _REAL_EXPORT(adapter, site="qidian", book=BookConfig(book_id="1"))

    assert seen["config"] == {"output_dir": "configured"}
