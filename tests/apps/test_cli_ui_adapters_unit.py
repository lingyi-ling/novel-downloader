"""Unit coverage for :mod:`novel_downloader.apps.cli.ui_adapters`.

The four adapter classes translate client/fetcher callbacks into console
output.  They are the glue every interactive download goes through, yet only
the export side had any coverage.  ``ui`` output functions are silenced here;
progress bars are replaced with a recording fake.
"""

from __future__ import annotations

import pytest

from novel_downloader.apps.cli import ui, ui_adapters
from novel_downloader.schemas import BookConfig, LoginField


class FakeProgress:
    """Records ProgressUI usage instead of rendering a rich bar."""

    instances: list[FakeProgress] = []

    def __init__(self, prefix: str = "", unit: str = "") -> None:
        self.prefix = prefix
        self.unit = unit
        self.started = 0
        self.stopped = 0
        self.updates: list[tuple[int, int]] = []
        FakeProgress.instances.append(self)

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    async def update(self, done: int, total: int) -> None:
        self.updates.append((done, total))

    def update_sync(self, done: int, total: int) -> None:
        self.updates.append((done, total))


@pytest.fixture(autouse=True)
def _silence_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    monkeypatch.setattr(ui_adapters.ui, "ProgressUI", FakeProgress)
    FakeProgress.instances.clear()


# --------------------------------------------------------------------------- #
# CLIDownloadUI
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_download_ui_starts_and_updates_progress() -> None:
    download_ui = ui_adapters.CLIDownloadUI()
    book = BookConfig(book_id="b1")

    await download_ui.on_start(book)
    await download_ui.on_progress(3, 10)

    progress = FakeProgress.instances[-1]
    assert progress.started == 1
    assert progress.updates == [(3, 10)]
    assert download_ui.completed_books == set()


@pytest.mark.asyncio
async def test_download_ui_progress_without_start_is_ignored() -> None:
    download_ui = ui_adapters.CLIDownloadUI()

    await download_ui.on_progress(1, 2)  # no bar created yet

    assert FakeProgress.instances == []


@pytest.mark.asyncio
async def test_download_ui_completion_stops_and_records_book() -> None:
    download_ui = ui_adapters.CLIDownloadUI()
    book = BookConfig(book_id="b1")

    await download_ui.on_start(book)
    progress = FakeProgress.instances[-1]
    await download_ui.on_complete(book)

    assert download_ui.completed_books == {book}
    assert progress.stopped == 1
    assert download_ui._progress is None


@pytest.mark.asyncio
async def test_download_ui_completion_without_start() -> None:
    download_ui = ui_adapters.CLIDownloadUI()
    book = BookConfig(book_id="b1")

    await download_ui.on_complete(book)

    assert download_ui.completed_books == {book}
    assert FakeProgress.instances == []


# --------------------------------------------------------------------------- #
# CLIExportUI
# --------------------------------------------------------------------------- #
def test_export_ui_records_success_per_format() -> None:
    export_ui = ui_adapters.CLIExportUI()
    book = BookConfig(book_id="b1")

    export_ui.on_start(book, fmt="txt")
    export_ui.on_success(book, "txt", __import__("pathlib").Path("out/b1.txt"))
    export_ui.on_success(book, "epub", __import__("pathlib").Path("out/b1.epub"))

    assert export_ui.completed_books == {
        "b1": {
            "txt": __import__("pathlib").Path("out/b1.txt"),
            "epub": __import__("pathlib").Path("out/b1.epub"),
        }
    }


def test_export_ui_error_reports_without_recording() -> None:
    export_ui = ui_adapters.CLIExportUI()
    book = BookConfig(book_id="b1")

    export_ui.on_error(book, None, RuntimeError("boom"))

    assert export_ui.completed_books == {}


def test_export_ui_unsupported_format_reports() -> None:
    export_ui = ui_adapters.CLIExportUI()
    book = BookConfig(book_id="b1")

    export_ui.on_unsupported(book, "pdf")

    assert export_ui.completed_books == {}


# --------------------------------------------------------------------------- #
# CLILoginUI
# --------------------------------------------------------------------------- #
def _field(
    name: str, *, type: str = "text", required: bool = False, default: str = ""
) -> LoginField:
    return LoginField(
        name=name, label=f"label-{name}", type=type, required=required, default=default
    )


@pytest.mark.asyncio
async def test_login_ui_prompt_uses_prefilled_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(
        ui, "prompt", lambda message, **k: prompts.append(message) or ""
    )

    login_ui = ui_adapters.CLILoginUI()
    result = await login_ui.prompt(
        [_field("username"), _field("token")],
        prefill={"username": "alice", "token": "  tok  "},
    )

    assert result == {"username": "alice", "token": "tok"}
    assert prompts == [], "prefilled fields must not prompt"


@pytest.mark.asyncio
async def test_login_ui_prompt_asks_for_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ui, "prompt", lambda message, **k: "answer")
    monkeypatch.setattr(ui, "prompt_password", lambda message: "secret")

    login_ui = ui_adapters.CLILoginUI()
    result = await login_ui.prompt(
        [_field("username"), _field("password", type="password")]
    )

    assert result == {"username": "answer", "password": "secret"}


@pytest.mark.asyncio
async def test_login_ui_prompt_cookie_field_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ui, "prompt", lambda message, **k: "sessionid=abc; token=xyz")

    login_ui = ui_adapters.CLILoginUI()
    result = await login_ui.prompt([_field("cookies", type="cookie")])

    assert result["cookies"] == {"sessionid": "abc", "token": "xyz"}


@pytest.mark.asyncio
async def test_login_ui_prompt_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ui, "prompt", lambda message, **k: "")
    monkeypatch.setattr(ui, "prompt_password", lambda message: "fallback-pw")

    login_ui = ui_adapters.CLILoginUI()
    result = await login_ui.prompt(
        [_field("password", type="password", default="fallback-pw")]
    )

    assert result == {"password": "fallback-pw"}


@pytest.mark.asyncio
async def test_login_ui_prompt_retries_required_empty_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = ["", "final"]
    monkeypatch.setattr(
        ui, "prompt", lambda message, **k: answers.pop(0) if answers else "final"
    )
    monkeypatch.setattr(ui, "prompt_password", lambda message: "")

    login_ui = ui_adapters.CLILoginUI()
    result = await login_ui.prompt([_field("username", required=True)])

    assert result == {"username": "final"}


def test_login_ui_outcome_callbacks_do_not_raise() -> None:
    login_ui = ui_adapters.CLILoginUI()

    login_ui.on_login_failed()
    login_ui.on_login_success()


# --------------------------------------------------------------------------- #
# CLIProcessUI
# --------------------------------------------------------------------------- #
def test_process_ui_stage_lifecycle_replaces_the_progress_bar() -> None:
    process_ui = ui_adapters.CLIProcessUI()
    book = BookConfig(book_id="b1")

    process_ui.on_stage_start(book, "clean")
    first = FakeProgress.instances[-1]
    process_ui.on_stage_progress(book, "clean", 2, 5)
    process_ui.on_stage_start(book, "translate")
    second = FakeProgress.instances[-1]

    assert first.stopped == 1, "starting a new stage stops the previous bar"
    assert second is not first
    assert second.prefix != first.prefix
    assert first.updates == [(2, 5)]

    process_ui.on_stage_complete(book, "translate")

    assert second.stopped == 1
    assert process_ui._progress is None


def test_process_ui_progress_without_stage_is_ignored() -> None:
    process_ui = ui_adapters.CLIProcessUI()

    process_ui.on_stage_progress(BookConfig(book_id="b"), "clean", 1, 2)

    assert FakeProgress.instances == []


def test_process_ui_missing_data_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(ui, "warn", lambda msg: warnings.append(msg))

    process_ui = ui_adapters.CLIProcessUI()
    process_ui.on_missing(
        BookConfig(book_id="b1"), "chapters", __import__("pathlib").Path("raw")
    )

    assert warnings and "b1" in warnings[0]


def test_process_ui_stage_complete_without_stage() -> None:
    process_ui = ui_adapters.CLIProcessUI()

    process_ui.on_stage_complete(BookConfig(book_id="b"), "clean")

    assert FakeProgress.instances == []
