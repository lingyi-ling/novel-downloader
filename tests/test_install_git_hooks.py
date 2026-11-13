"""Tests for scripts/install_git_hooks.py.

Each test builds a throwaway Git repository; nothing is committed, and the
hook's contents are inspected rather than executed.  The script is loaded by
path, matching the other ``scripts/`` tests in this repository.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_git_hooks.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("install_hooks_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


installer = _load_script()


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    subprocess.run(
        ["git", "-C", str(root), "init", "-q"],
        check=True,
        capture_output=True,
    )
    return root


def _hook(root: Path) -> Path:
    return installer.hooks_dir(root) / "pre-commit"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_install_writes_a_managed_hook(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    change = installer.install(root)

    hook = _hook(root)
    assert change.changed is True
    content = hook.read_text(encoding="utf-8")
    assert installer.MARKER in content
    assert "scan_secrets.py" in content
    assert "--staged" in content
    assert content.startswith("#!/bin/sh\n")
    # Git Bash handles LF; a CRLF would end up in the shell tokens.
    assert b"\r\n" not in hook.read_bytes()


def test_hook_prefers_the_project_venv_and_falls_back_to_python() -> None:
    content = installer.render_hook()

    assert ".venv/Scripts/python.exe" in content
    assert ".venv/bin/python" in content
    assert "command -v python3" in content
    assert "command -v python" in content


def test_install_is_idempotent(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    first = installer.install(root)
    before = _hook(root).read_bytes()

    second = installer.install(root)

    assert first.changed is True
    assert second.changed is False
    assert _hook(root).read_bytes() == before


def test_install_refuses_to_overwrite_an_unmanaged_hook(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    hook = _hook(root)
    original = "#!/bin/sh\necho from-another-tool\n"
    _write(hook, original)

    with pytest.raises(installer.HookError, match="--force"):
        installer.install(root)

    assert hook.read_text(encoding="utf-8") == original
    assert not hook.with_name("pre-commit.bak").exists()


def test_force_backs_up_an_unmanaged_hook(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    hook = _hook(root)
    original = "#!/bin/sh\necho from-another-tool\n"
    _write(hook, original)

    change = installer.install(root, force=True)

    assert change.changed is True
    backup = hook.with_name("pre-commit.bak")
    assert backup.read_text(encoding="utf-8") == original
    assert installer.MARKER in hook.read_text(encoding="utf-8")


def test_force_keeps_an_existing_backup(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    hook = _hook(root)
    _write(hook, "#!/bin/sh\necho newer\n")
    backup = hook.with_name("pre-commit.bak")
    _write(backup, "older backup\n")

    installer.install(root, force=True)

    assert backup.read_text(encoding="utf-8") == "older backup\n"
    assert hook.with_name("pre-commit.bak.1").read_text(encoding="utf-8") == (
        "#!/bin/sh\necho newer\n"
    )


def test_uninstall_removes_only_the_managed_hook(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    installer.install(root)
    other = installer.hooks_dir(root) / "pre-push"
    _write(other, "#!/bin/sh\necho unrelated\n")

    change = installer.uninstall(root)

    assert change.changed is True
    assert not _hook(root).exists()
    assert other.read_text(encoding="utf-8") == "#!/bin/sh\necho unrelated\n"


def test_uninstall_leaves_an_unmanaged_hook_untouched(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    hook = _hook(root)
    original = "#!/bin/sh\necho from-another-tool\n"
    _write(hook, original)

    with pytest.raises(installer.HookError, match="not installed by this tool"):
        installer.uninstall(root)

    assert hook.read_text(encoding="utf-8") == original


def test_uninstall_without_a_hook_is_a_no_op(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    change = installer.uninstall(root)

    assert change.changed is False
    assert not _hook(root).exists()


def test_repository_paths_with_spaces_are_quoted(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, "repo with spaces")

    installer.install(root)

    hook = _hook(root)
    assert hook.is_file()
    content = hook.read_text(encoding="utf-8")
    assert '"$root/scripts/scan_secrets.py"' in content
    assert '"$root/.venv/Scripts/python.exe"' in content
    assert 'exec "$python_bin" "$scan" --staged' in content


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
def test_hook_is_executable(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    installer.install(root)

    mode = _hook(root).stat().st_mode
    assert mode & stat.S_IXUSR


def test_main_installs_and_uninstalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_repo(tmp_path)
    monkeypatch.setattr(installer, "DEFAULT_ROOT", root)

    assert installer.main([]) == 0
    assert _hook(root).exists()
    assert installer.main(["--uninstall"]) == 0
    assert not _hook(root).exists()


def test_main_refuses_an_unmanaged_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_repo(tmp_path)
    _write(_hook(root), "#!/bin/sh\necho from-another-tool\n")
    monkeypatch.setattr(installer, "DEFAULT_ROOT", root)

    assert installer.main([]) == 1


def test_main_rejects_force_with_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_repo(tmp_path)
    monkeypatch.setattr(installer, "DEFAULT_ROOT", root)

    assert installer.main(["--force", "--uninstall"]) == 1
