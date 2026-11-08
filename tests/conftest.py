import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session", autouse=True)
def _compiled_catalogs() -> None:
    """
    Compile the ``.po`` catalogs so localization tests use real translations.

    ``.mo`` files are build artifacts (git-ignored) and are not created by an
    editable install, so compile them once per test session. Skipped silently
    when Babel is not installed.
    """
    try:
        import babel  # noqa: F401
    except ImportError:
        return
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "compile_mo.py")],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )


@pytest.fixture(scope="session", autouse=True)
def _isolated_music_home(tmp_path_factory: pytest.TempPathFactory) -> object:
    """
    Keep music runtime data out of the developer's real profile.

    The aggregate search persists per-platform health to
    ``<music data dir>/search_stats.json``; without this it would write to
    ``%LOCALAPPDATA%`` and leak state between runs (and machines).
    """
    import os

    key = "NOVEL_MUSIC_HOME"
    previous = os.environ.get(key)
    os.environ[key] = str(tmp_path_factory.mktemp("music-home"))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@pytest.fixture(autouse=True)
def _force_english_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Pin the interface language to English for every test.

    Without this, a developer machine that persisted ``zh_CN`` in the state
    file would make assertions on English user-facing messages fail randomly.
    """
    monkeypatch.setenv("NOVEL_LANG", "en_US")
