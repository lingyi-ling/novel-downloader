import json
from pathlib import Path

import pytest

from novel_downloader.infra.persistence.state import StateManager


def test_load_nonexistent_file(tmp_path):
    """When file does not exist, StateManager should load empty state."""
    f = tmp_path / "state.json"
    mgr = StateManager(f)

    assert mgr.get_language() == "zh_CN"  # default
    assert f.exists() is False  # no auto-create on load


def test_set_and_get_language(tmp_path):
    """Setting language should persist to file."""
    f = tmp_path / "state.json"
    mgr = StateManager(f)

    mgr.set_language("en_US")
    assert mgr.get_language() == "en_US"

    # Ensure file was actually written
    assert f.exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["lang"] == "en_US"


def test_load_existing_state(tmp_path):
    """Loading a pre-existing JSON file should populate state."""
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"lang": "ja_JP"}), encoding="utf-8")

    mgr = StateManager(f)
    assert mgr.get_language() == "ja_JP"


def test_load_invalid_json(tmp_path):
    """Invalid JSON should be treated as empty state."""
    f = tmp_path / "state.json"
    f.write_text("{not a json", encoding="utf-8")

    mgr = StateManager(f)
    assert mgr.get_language() == "zh_CN"  # fallback to default
    # Writing should overwrite invalid file
    mgr.set_language("ko_KR")
    assert json.loads(f.read_text(encoding="utf-8"))["lang"] == "ko_KR"


def test_save_creates_parent_dirs(tmp_path):
    """Saving should auto-create parent directories."""
    nested_dir = tmp_path / "a/b/c"
    f = nested_dir / "state.json"

    mgr = StateManager(f)
    mgr.set_language("fr_FR")

    assert f.exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["lang"] == "fr_FR"


# ---------------------------------------------------------------------
# Corrupt / malformed state files
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        "[1, 2]",
        "123",
        '"hello"',
        "true",
        "null",
        '["lang", "en_US"]',
    ],
)
def test_load_non_dict_json_does_not_crash(tmp_path, payload):
    """Valid JSON that is not a mapping must degrade to empty state."""
    f = tmp_path / "state.json"
    f.write_text(payload, encoding="utf-8")

    mgr = StateManager(f)
    assert mgr.get_language() == "zh_CN"

    # Reads and writes must keep working after the bad payload.
    mgr.set_language("en_US")
    assert json.loads(f.read_text(encoding="utf-8"))["lang"] == "en_US"


def test_unparsable_file_is_quarantined(tmp_path):
    """A broken state file should be kept aside instead of being lost."""
    f = tmp_path / "state.json"
    f.write_text("{not a json", encoding="utf-8")

    mgr = StateManager(f)
    assert mgr.get_language() == "zh_CN"

    quarantined = tmp_path / "state.json.corrupt"
    assert quarantined.exists()
    assert quarantined.read_text(encoding="utf-8") == "{not a json"


def test_non_dict_json_is_quarantined(tmp_path):
    """A non-mapping payload is equally unusable and must be preserved."""
    f = tmp_path / "state.json"
    f.write_text("[1, 2]", encoding="utf-8")

    mgr = StateManager(f)
    assert mgr.get_language() == "zh_CN"

    quarantined = tmp_path / "state.json.corrupt"
    assert quarantined.read_text(encoding="utf-8") == "[1, 2]"


def test_missing_file_is_not_quarantined(tmp_path):
    """A missing state file is normal and must not create artifacts."""
    f = tmp_path / "state.json"
    StateManager(f)
    assert not (tmp_path / "state.json.corrupt").exists()


# ---------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------
def test_save_is_atomic_when_write_fails(tmp_path, monkeypatch):
    """A crash mid-save must leave the previously saved state intact."""
    f = tmp_path / "state.json"
    mgr = StateManager(f)
    mgr.set_language("en_US")
    original = f.read_text(encoding="utf-8")

    def crashing_write_text(self, data, encoding=None, errors=None, newline=None):
        # Simulate a crash mid-write: half the payload reaches the disk.
        with open(self, "w", encoding=encoding, newline=newline) as fh:
            fh.write(data[: max(len(data) // 2, 1)])
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "write_text", crashing_write_text)
    with pytest.raises(OSError, match="simulated disk failure"):
        mgr.set_language("zh_CN")

    # The previously persisted state must survive the failed save untouched.
    assert f.read_text(encoding="utf-8") == original
    assert json.loads(f.read_text(encoding="utf-8"))["lang"] == "en_US"


def test_save_leaves_no_temp_files_behind(tmp_path):
    """A successful save must not litter the state directory with temp files."""
    f = tmp_path / "state.json"
    mgr = StateManager(f)

    mgr.set_language("en_US")

    assert f.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
