from pathlib import Path

import pytest

from novel_downloader.libs.filesystem import sanitize as sanitize_module
from novel_downloader.libs.filesystem.file import write_file


def test_write_file_text(tmp_path):
    """Write text content normally."""
    p = tmp_path / "test.txt"
    out = write_file("hello", p)
    assert out == p
    assert p.read_text("utf-8") == "hello"


def test_write_file_bytes(tmp_path):
    """Write binary content normally."""
    p = tmp_path / "bin.dat"
    out = write_file(b"\x01\x02", p)
    assert out == p
    assert p.read_bytes() == b"\x01\x02"


def test_write_file_skip_if_exists(tmp_path):
    """If file exists and on_exist=skip, do not overwrite."""
    p = tmp_path / "skip.txt"
    p.write_text("old")

    out = write_file("new", p, on_exist="skip")
    assert out == p
    assert p.read_text("utf-8") == "old"  # must not change


def test_write_file_overwrite(tmp_path):
    """overwrite path forces replacement."""
    p = tmp_path / "ow.txt"
    p.write_text("old")

    out = write_file("new", p, on_exist="overwrite")
    assert out == p
    assert p.read_text("utf-8") == "new"


def test_write_file_sanitize_filename(tmp_path, monkeypatch):
    """Filename must be sanitized before writing."""
    # Fake sanitize_filename to observe behavior
    monkeypatch.setattr(
        "novel_downloader.libs.filesystem.file.sanitize_filename",
        lambda name: name.replace("?", "_safe_"),
    )

    p = tmp_path / "bad?.txt"
    out = write_file("x", p)

    # The output path must use sanitized name
    assert out.name == "bad_safe_.txt"
    assert out.exists()
    assert out.read_text("utf-8") == "x"


def test_write_file_atomic(tmp_path):
    """
    Ensure write_file uses a temp file then replaces.
    We detect this by checking:
        - dest file contains new content
        - no leftover temporary files remain
    """
    p = tmp_path / "atomic.txt"
    out = write_file("atomic", p)

    assert out.exists()
    assert out.read_text("utf-8") == "atomic"

    # No stray temp files
    leftovers = [x for x in tmp_path.iterdir() if x.name != "atomic.txt"]
    assert leftovers == []


def test_write_file_cleanup_on_error(tmp_path, monkeypatch):
    """If writing fails, temporary file must be deleted."""
    tmp_created = []

    class FakeTmp:
        def __init__(self, *a, **k):
            real = tmp_path / "fake_temp.tmp"
            real.write_text("temp")
            tmp_created.append(real)
            self.name = str(real)

        def write(self, *_):
            raise OSError("write failed")  # FAIL on first write

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "novel_downloader.libs.filesystem.file.tempfile.NamedTemporaryFile",
        FakeTmp,
    )

    p = tmp_path / "err.txt"

    with pytest.raises(IOError):
        write_file("xxx", p)

    # ensure temporary file is cleaned up
    assert not tmp_created[0].exists()
    assert not p.exists()


# ----------------------------------------------------------------------
# Directory creation / path joining
# ----------------------------------------------------------------------


def test_write_file_creates_missing_parent_directories(tmp_path):
    p = tmp_path / "a" / "b" / "c" / "out.txt"
    out = write_file("x", p)

    assert out == p
    assert p.read_text("utf-8") == "x"


def test_write_file_accepts_relative_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = write_file("relative", Path("sub") / "file.txt")
    assert out == Path("sub/file.txt")
    assert (tmp_path / "sub" / "file.txt").read_text("utf-8") == "relative"


def test_write_file_unicode_path(tmp_path):
    p = tmp_path / "中文目录" / "书名：第一卷.txt"
    out = write_file("内容", p)
    assert out.exists()
    assert out.read_text("utf-8") == "内容"


def test_write_file_reserved_name_is_sanitized(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(sanitize_module, "os", SimpleNamespace(name="nt"))

    out = write_file("x", tmp_path / "CON.txt")
    assert out.name == "_CON.txt"
    assert (tmp_path / "_CON.txt").exists()


# ----------------------------------------------------------------------
# Atomicity details
# ----------------------------------------------------------------------


def test_write_file_preserves_original_when_replace_fails(tmp_path, monkeypatch):
    """A failure during the final replace must not corrupt the old file."""
    p = tmp_path / "keep.txt"
    p.write_text("original", encoding="utf-8")

    def boom(self, target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(OSError, match="replace failed"):
        write_file("replacement", p)

    assert p.read_text("utf-8") == "original"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["keep.txt"]


def test_write_file_cleans_temp_on_base_exception(tmp_path, monkeypatch):
    """KeyboardInterrupt is a BaseException but still must not leak temp files."""
    created = []

    class FakeTmp:
        def __init__(self, *a, **k):
            real = tmp_path / "interrupted.tmp"
            real.write_text("temp")
            created.append(real)
            self.name = str(real)

        def write(self, *_):
            raise KeyboardInterrupt

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "novel_downloader.libs.filesystem.file.tempfile.NamedTemporaryFile",
        FakeTmp,
    )

    with pytest.raises(KeyboardInterrupt):
        write_file("x", tmp_path / "dest.txt")

    assert created and not created[0].exists()
    assert not (tmp_path / "dest.txt").exists()


def test_write_file_skip_does_not_touch_existing_file(tmp_path):
    p = tmp_path / "skip.txt"
    p.write_text("old", encoding="utf-8")
    before = p.stat().st_mtime_ns

    out = write_file(b"new", p, on_exist="skip")

    assert out == p
    assert p.read_bytes() == b"old"
    assert p.stat().st_mtime_ns == before
    assert sorted(x.name for x in tmp_path.iterdir()) == ["skip.txt"]


# ----------------------------------------------------------------------
# Content / encoding behaviour
# ----------------------------------------------------------------------


def test_write_file_text_uses_lf_newlines(tmp_path):
    p = tmp_path / "lf.txt"
    write_file("a\nb\n", p)
    assert p.read_bytes() == b"a\nb\n"


def test_write_file_custom_encoding_roundtrip(tmp_path):
    p = tmp_path / "gbk.txt"
    write_file("中文", p, encoding="gbk")
    assert p.read_bytes() == "中文".encode("gbk")


def test_write_file_empty_content(tmp_path):
    p = tmp_path / "empty.txt"
    write_file("", p)
    assert p.exists()
    assert p.read_bytes() == b""

    b = tmp_path / "empty.bin"
    write_file(b"", b)
    assert b.read_bytes() == b""


def test_write_file_returns_sanitized_path(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(sanitize_module, "os", SimpleNamespace(name="nt"))

    out = write_file("x", tmp_path / "a<b>c?.txt")
    assert out.name == "a_b_c_.txt"
    assert out.exists()


def test_write_file_existing_directory_target_raises(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()

    with pytest.raises(OSError):
        write_file("x", d)

    assert d.is_dir()
