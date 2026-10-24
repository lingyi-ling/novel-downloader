import os
from types import SimpleNamespace

import pytest

from novel_downloader.libs.filesystem import sanitize as sanitize_module
from novel_downloader.libs.filesystem.sanitize import sanitize_filename

# ----------------------------------------------------------------------
# Common behavior (cross-platform)
# ----------------------------------------------------------------------


def test_empty_filename_becomes_untitled():
    assert sanitize_filename("") == "_untitled"


def test_strip_spaces_and_dots():
    assert sanitize_filename("  abc.txt  ") == "abc.txt"
    assert sanitize_filename(" abc. ") == "abc"


def test_posix_replaces_slash():
    # Always true on any OS
    assert sanitize_filename("a/b/c") == "a_b_c"


def test_max_length_cutting():
    name = "a" * 300 + ".txt"
    out = sanitize_filename(name, max_length=50)
    assert len(out) == 50
    assert out.endswith(".txt")


def test_max_length_no_extension():
    name = "x" * 300
    out = sanitize_filename(name, max_length=20)
    assert len(out) == 20


# ----------------------------------------------------------------------
# Windows-specific behavior
# ----------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows-only tests")
class TestWindowsBehavior:
    def test_windows_illegal_chars_replaced(self):
        assert sanitize_filename('a<b>c:"d"|e?f*g.txt') == "a_b_c__d__e_f_g.txt"

    def test_windows_reserved_names_prefixed(self):
        assert sanitize_filename("CON.txt") == "_CON.txt"
        assert sanitize_filename("aux.TXT") == "_aux.TXT"

    def test_windows_trailing_space_dot_removed(self):
        assert sanitize_filename("abc. ") == "abc"
        assert sanitize_filename("abc...") == "abc"


# ----------------------------------------------------------------------
# POSIX-specific behavior
# ----------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only tests")
class TestPosixBehavior:
    def test_posix_illegal_slash_replaced(self):
        assert sanitize_filename("a/b/c.txt") == "a_b_c.txt"

    def test_posix_no_reserved_word_handling(self):
        assert sanitize_filename("CON.txt") == "CON.txt"


# ----------------------------------------------------------------------
# Deterministic cross-platform rules (simulate os.name)
# ----------------------------------------------------------------------


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(sanitize_module, "os", SimpleNamespace(name="nt"))


@pytest.fixture
def as_posix(monkeypatch):
    monkeypatch.setattr(sanitize_module, "os", SimpleNamespace(name="posix"))


@pytest.mark.usefixtures("as_windows")
class TestWindowsRulesSimulated:
    @pytest.mark.parametrize(
        "name",
        [
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "COM9",
            "LPT1",
            "LPT9",
        ],
    )
    def test_all_reserved_names_are_prefixed(self, name):
        assert sanitize_filename(name) == f"_{name}"
        assert sanitize_filename(name.lower()) == f"_{name.lower()}"

    @pytest.mark.parametrize(
        "name",
        [
            "CON.txt",
            "con.md",
            "NUL.tar.gz",
            "COM1.log",
            "LPT9.json",
        ],
    )
    def test_reserved_names_with_extension(self, name):
        stem, dot, ext = name.rpartition(".")
        assert sanitize_filename(name) == f"_{stem}{dot}{ext}"

    def test_reserved_name_with_space_before_extension(self):
        # Windows treats "CON .txt" like "CON.txt".
        assert sanitize_filename("CON .txt") == "_CON .txt"
        assert sanitize_filename("NUL ..log") == "_NUL ..log"

    def test_non_reserved_lookalikes_untouched(self):
        for name in ("CONSOLE", "COM0", "COM10", "LPT0", "NULL", "AUXILIARY"):
            assert sanitize_filename(name) == name

    def test_all_illegal_windows_chars_replaced(self):
        assert sanitize_filename('<>:"/\\|?*') == "_________"
        assert sanitize_filename("a\x00b\x1fc") == "a_b_c"

    def test_backslash_is_a_separator(self):
        assert sanitize_filename(r"a\b\c.txt") == "a_b_c.txt"

    def test_trailing_dots_and_spaces_removed(self):
        assert sanitize_filename("abc. . . ") == "abc"
        assert sanitize_filename("abc..") == "abc"

    def test_leading_dot_is_dropped(self):
        # Existing behaviour: generated names never become hidden files.
        assert sanitize_filename(".hidden") == "hidden"
        assert sanitize_filename("..") == "_untitled"

    def test_control_char_truncation_keeps_valid_name(self):
        # The control char is replaced first, so this is no longer reserved.
        assert sanitize_filename("CON\x01.txt") == "CON_.txt"


@pytest.mark.usefixtures("as_posix")
class TestPosixRulesSimulated:
    def test_only_slash_and_nul_are_replaced(self):
        assert sanitize_filename("a/b\\c:d*e?.txt") == "a_b\\c:d*e?.txt"
        assert sanitize_filename("a\x00b") == "a_b"

    def test_reserved_names_are_allowed(self):
        for name in ("CON", "NUL", "COM1", "LPT1"):
            assert sanitize_filename(name) == name

    def test_windows_only_illegal_chars_are_preserved(self):
        assert sanitize_filename('a<b>c:"d"|e?f*g') == 'a<b>c:"d"|e?f*g'


class TestLengthAndUnicode:
    def test_extension_preserved_under_truncation(self):
        out = sanitize_filename("a" * 300 + ".txt", max_length=10)
        assert len(out) == 10
        assert out.endswith(".txt")

    def test_extension_longer_than_limit_never_overflows(self):
        # Regression: the previous implementation returned "a.very.verylongext"
        # (18 chars) for max_length=5.
        out = sanitize_filename("a.verylongext", max_length=5)
        assert len(out) <= 5
        assert out == "a.ver"

    def test_truncation_does_not_leave_trailing_spaces(self):
        # Regression: truncating "abc   def" to 5 used to yield "abc  ".
        out = sanitize_filename("abc   def", max_length=5)
        assert out == "abc"

    def test_truncation_does_not_leave_trailing_dots(self):
        # "abcd..ef" has stem "abcd." which can be exposed by truncation.
        out = sanitize_filename("abcd.ef", max_length=5)
        assert not out.endswith((" ", "."))

    def test_max_length_none_and_non_positive_keep_full_name(self):
        name = "a" * 300
        assert sanitize_filename(name, max_length=None) == name
        assert sanitize_filename("abcdef", max_length=0) == "abcdef"
        # Regression: a negative limit used to silently cut one character.
        assert sanitize_filename("abcdef", max_length=-1) == "abcdef"

    def test_unicode_is_preserved(self):
        assert sanitize_filename("书名：第一卷.txt") == "书名：第一卷.txt"
        assert sanitize_filename("café_naïve.txt") == "café_naïve.txt"
        assert sanitize_filename("表情😀.txt") == "表情😀.txt"

    def test_unicode_illegal_chars_still_replaced(self):
        assert sanitize_filename("书/名?.txt") == "书_名_.txt"

    def test_unicode_truncation_counts_code_points(self):
        out = sanitize_filename("😀" * 300, max_length=10)
        assert out == "😀" * 10

    def test_empty_and_whitespace_only(self):
        for name in ("", "   ", "...", " . . "):
            assert sanitize_filename(name) == "_untitled"

    def test_max_length_boundary_is_inclusive(self):
        name = "a" * 10 + ".txt"
        assert sanitize_filename(name, max_length=14) == name
        assert len(sanitize_filename(name, max_length=13)) == 13
