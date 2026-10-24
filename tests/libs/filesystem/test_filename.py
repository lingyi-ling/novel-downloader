import hashlib

from novel_downloader.libs.filesystem.filename import (
    DEFAULT_FONT_SUFFIX,
    DEFAULT_IMAGE_SUFFIX,
    SafeDict,
    font_filename,
    format_filename,
    image_filename,
    url_to_hashed_name,
)

# ----------------------------------------------------------------------
# SafeDict
# ----------------------------------------------------------------------


def test_safe_dict_missing():
    s = SafeDict()
    assert s["unknown"] == "{unknown}"


# ----------------------------------------------------------------------
# url_to_hashed_name
# ----------------------------------------------------------------------


def test_url_to_hashed_name_preserve_suffix():
    url = "http://example.com/img/photo.jpg"
    name = url_to_hashed_name(url, allowed_suffixes={".jpg"}, default_suffix=".png")
    assert name.endswith(".jpg")  # preserved


def test_url_to_hashed_name_disallowed_suffix():
    url = "http://example.com/img/photo.bmp"
    name = url_to_hashed_name(url, allowed_suffixes={".jpg"}, default_suffix=".png")
    assert name.endswith(".png")  # fallback to default suffix


def test_url_to_hashed_name_no_suffix_and_default():
    url = "http://example.com/file"
    name = url_to_hashed_name(url, default_suffix=".bin")
    assert name.endswith(".bin")


def test_url_to_hashed_name_custom_name():
    url = "http://example.com/a.png"
    assert (
        url_to_hashed_name(url, name="custom", allowed_suffixes={".png"})
        == "custom.png"
    )


def test_url_to_hashed_name_hash_name():
    url = "http://example.com/a.png"
    expected = hashlib.sha1(url.encode("utf-8")).hexdigest() + ".png"
    assert url_to_hashed_name(url, allowed_suffixes={".png"}) == expected


# ----------------------------------------------------------------------
# image_filename
# ----------------------------------------------------------------------


def test_image_filename_allows_common_ext():
    url = "https://cdn.xx.com/a/b/c.jpeg"
    assert image_filename(url).endswith(".jpeg")


def test_image_filename_fallback_default_suffix():
    url = "https://cdn.xx.com/a/b/c.xyz"
    assert image_filename(url).endswith(DEFAULT_IMAGE_SUFFIX)


def test_image_filename_custom_name():
    url = "https://cdn.xx.com/a/b/c.png"
    assert image_filename(url, name="pic") == "pic.png"


# ----------------------------------------------------------------------
# font_filename
# ----------------------------------------------------------------------


def test_font_filename_valid_ext():
    url = "https://fonts.xx.com/f.woff2"
    assert font_filename(url).endswith(".woff2")


def test_font_filename_fallback_default():
    url = "https://fonts.xx.com/f.abc"
    assert font_filename(url).endswith(DEFAULT_FONT_SUFFIX)


def test_font_filename_custom_name():
    url = "https://fonts.xx.com/font.ttf"
    assert font_filename(url, name="myfont") == "myfont.ttf"


# ----------------------------------------------------------------------
# format_filename
# ----------------------------------------------------------------------


def test_format_filename_basic_ext():
    name = format_filename("book_{id}", id="1", append_timestamp=False, ext="txt")
    assert name == "book_1.txt"


def test_format_filename_no_ext():
    name = format_filename("x_{a}", a="y", append_timestamp=False, ext="")
    assert name == "x_y"


def test_format_filename_timestamp_added():
    n = format_filename("file", append_timestamp=True, ext="log")

    assert n.startswith("file_")
    assert n.endswith(".log")
    ts = n[len("file_") : -4]
    assert len(ts) == len("20251111_123413")


def test_format_filename_missing_field_safe():
    name = format_filename("item_{missing}", append_timestamp=False, ext="txt")
    assert name == "item_{missing}.txt"


# ----------------------------------------------------------------------
# Additional URL edge cases
# ----------------------------------------------------------------------


def test_url_to_hashed_name_uses_full_url_for_hash():
    base = "https://cdn.example.com/a/photo.jpg"
    with_query = base + "?w=100#frag"
    # The suffix comes from the path, but the hash covers the whole URL.
    assert url_to_hashed_name(with_query, allowed_suffixes={".jpg"}).endswith(".jpg")
    assert url_to_hashed_name(with_query, allowed_suffixes={".jpg"}) == (
        hashlib.sha1(with_query.encode("utf-8")).hexdigest() + ".jpg"
    )
    assert url_to_hashed_name(base, allowed_suffixes={".jpg"}) != (
        url_to_hashed_name(with_query, allowed_suffixes={".jpg"})
    )


def test_url_to_hashed_name_percent_encoded_path():
    url = "https://cdn.example.com/a/%E5%9B%BE%E7%89%87.PNG"
    assert url_to_hashed_name(url, allowed_suffixes={".png"}).endswith(".png")


def test_url_to_hashed_name_no_allowed_list_preserves_suffix():
    assert url_to_hashed_name("http://x/a.BMP").endswith(".bmp")


def test_url_to_hashed_name_empty_allowed_set_preserves_suffix():
    # An empty allow-list is falsy, so the original suffix is kept.
    assert url_to_hashed_name("http://x/a.bmp", allowed_suffixes=set()).endswith(".bmp")


def test_url_to_hashed_name_custom_name_without_allowed_suffix():
    assert (
        url_to_hashed_name("http://x/a.png", name="custom", default_suffix=".bin")
        == "custom.png"
    )


def test_image_filename_dotless_url_uses_default():
    assert image_filename("https://cdn.example.com/image").endswith(
        DEFAULT_IMAGE_SUFFIX
    )


def test_font_filename_uppercase_ext_normalized():
    assert font_filename("https://cdn.example.com/F.WOFF2").endswith(".woff2")


# ----------------------------------------------------------------------
# format_filename edge cases
# ----------------------------------------------------------------------


def test_format_filename_strips_leading_dots_in_ext():
    assert format_filename("b", append_timestamp=False, ext=".txt") == "b.txt"


def test_format_filename_custom_timestamp_format(monkeypatch):
    class FixedDatetime:
        @staticmethod
        def now():
            class D:
                @staticmethod
                def strftime(fmt):
                    return "STAMP"

            return D()

    monkeypatch.setattr("datetime.datetime", FixedDatetime)
    assert format_filename("b", append_timestamp=True, timestamp_format="%Y") == (
        "b_STAMP"
    )


def test_format_filename_negative_field_value_is_stringified():
    assert format_filename("p_{n}", n="-1", append_timestamp=False) == "p_-1"
