#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.apps.music.weapi`.

The HTTP session is replaced with an in-memory double, so every code path
(WEAPI crypto, QR-login state machine, cookie persistence, batching) runs
without any network access.  The AES/RSA helpers are exercised for real via
pycryptodome, which is already a hard dependency of the module.

The target module is deliberately untyped (see the ``[tool.mypy.overrides]``
relaxation in ``pyproject.toml``), so calls into it skip that check here too.
"""

# mypy: disallow-untyped-calls=False

from __future__ import annotations

import base64
import json
import random
from pathlib import Path
from typing import Any

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from novel_downloader.apps.music import weapi
from novel_downloader.apps.music.weapi import (
    LEVELS,
    LoginExpired,
    NeteaseClient,
    NeteaseError,
    _guard,
    _rsa_encrypt,
    track_album,
    track_artists,
    track_cover,
    track_duration,
    track_name,
    track_no,
    weapi_encrypt,
)

# --------------------------------------------------------------------------- #
# crypto helpers
# --------------------------------------------------------------------------- #


def test_weapi_encrypt_is_deterministic_given_seed() -> None:
    random.seed(42)
    payload = {"uid": 1, "limit": 1000}
    result = weapi_encrypt(payload)

    assert set(result) == {"params", "encSecKey"}
    assert len(result["encSecKey"]) == 256
    base64.b64decode(result["params"])  # must be valid base64

    random.seed(42)
    again = weapi_encrypt(payload)
    assert again == result


def test_weapi_encrypt_first_layer_uses_fixed_nonce() -> None:
    # Reproduce the double AES by hand with a seeded secret and confirm the
    # published params decrypt to the same ciphertext as our own pipeline.
    random.seed(7)
    secret = "".join(random.choices("abcdefghij", k=16))
    real_choices = random.choices

    def _fixed(population: Any, k: int = 16) -> list[str]:
        return list(secret)

    random.choices = _fixed  # type: ignore[assignment]
    try:
        result = weapi_encrypt({"uid": 1})
    finally:
        random.choices = real_choices

    layer2 = AES.new(secret.encode(), AES.MODE_CBC, weapi._IV.encode()).decrypt(
        base64.b64decode(result["params"])
    )
    middle = layer2[: -layer2[-1]]  # strip PKCS7 padding
    layer1 = AES.new(weapi._NONCE.encode(), AES.MODE_CBC, weapi._IV.encode()).decrypt(
        base64.b64decode(middle)
    )
    assert json.loads(unpad(layer1, 16).decode("utf-8")) == {"uid": 1}


def test_rsa_encrypt_shape_is_stable() -> None:
    out1 = _rsa_encrypt("ABCDEFGHIJKLMNOP")
    out2 = _rsa_encrypt("ABCDEFGHIJKLMNOP")
    other = _rsa_encrypt("QRSTUVWXYZabcdef")
    assert out1 == out2
    assert out1 != other
    assert len(out1) == 256
    int(out1, 16)  # must be valid hex


def test_levels_table() -> None:
    assert set(LEVELS) == {
        "standard",
        "higher",
        "exhigh",
        "lossless",
        "hires",
    }


# --------------------------------------------------------------------------- #
# _guard
# --------------------------------------------------------------------------- #


def test_guard_raises_login_expired_on_301() -> None:
    with pytest.raises(LoginExpired):
        _guard({"code": 301})


def test_guard_passes_through_other_payloads() -> None:
    assert _guard({"code": 200}) == {"code": 200}
    assert _guard([1, 2]) == [1, 2]
    assert _guard("ok") == "ok"


# --------------------------------------------------------------------------- #
# fake session plumbing
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(
        self,
        body: Any,
        status_code: int = 200,
        cookies: list[Any] | None = None,
    ) -> None:
        self._body = body
        self.status_code = status_code
        self.cookies = cookies or []
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Records posts; returns queued responses in order."""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        import requests.cookies

        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, data: Any = None, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, "data": data, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected extra request: " + url)
        return self.responses.pop(0)


def _client(session: FakeSession, cookie_file: str | None = None) -> NeteaseClient:
    client = NeteaseClient(cookie_file=cookie_file)
    client.s = session  # type: ignore[assignment]
    return client


# --------------------------------------------------------------------------- #
# cookie persistence
# --------------------------------------------------------------------------- #


def test_cookie_roundtrip(tmp_path: Path) -> None:
    cookie_file = str(tmp_path / "cookies.json")
    client = NeteaseClient(cookie_file=cookie_file)
    client.s.cookies.set("MUSIC_U", "secret", domain=".music.163.com")
    client.s.cookies.set("__csrf", "token", domain=".music.163.com")

    client.save_cookies()
    stored = json.loads(Path(cookie_file).read_text(encoding="utf-8"))
    assert stored["MUSIC_U"] == "secret"

    client2 = NeteaseClient(cookie_file=cookie_file)
    assert client2.logged_in is True
    assert client2.cookie_names == ["MUSIC_U", "__csrf"]


def test_load_cookies_survives_corrupt_file(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text("{not json", encoding="utf-8")
    client = NeteaseClient(cookie_file=str(cookie_file))
    assert client.logged_in is False


def test_clear_cookies_removes_file(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.json"
    client = NeteaseClient(cookie_file=str(cookie_file))
    client.s.cookies.set("MUSIC_U", "x", domain=".music.163.com")
    client.save_cookies()
    assert cookie_file.exists()

    client.clear_cookies()
    assert client.logged_in is False
    assert not cookie_file.exists()


def test_save_cookies_restricts_file_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cookie file holds the MUSIC_U session credential (a login secret)."""
    chmod_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        weapi.os, "chmod", lambda path, mode: chmod_calls.append((str(path), mode))
    )
    cookie_file = str(tmp_path / "cookies.json")

    client = NeteaseClient(cookie_file=cookie_file)
    client.s.cookies.set("MUSIC_U", "secret", domain=".music.163.com")

    client.save_cookies()

    assert (cookie_file + ".tmp", 0o600) in chmod_calls
    assert Path(cookie_file).exists()
    assert client.logged_in is True


def test_save_cookies_without_file_is_noop() -> None:
    client = NeteaseClient()
    client.s.cookies.set("MUSIC_U", "x", domain=".music.163.com")
    client.save_cookies()  # no cookie_file configured


def test_save_cookies_cleans_tmp_residue_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cookie 文件存的是 MUSIC_U 登录凭证：原子替换失败时不能留半截明文。"""
    cookie_file = tmp_path / "cookies.json"
    client = NeteaseClient(cookie_file=str(cookie_file))
    client.s.cookies.set("MUSIC_U", "secret", domain=".music.163.com")
    client.save_cookies()
    before = cookie_file.read_text(encoding="utf-8")

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(weapi.os, "replace", failing_replace)
    client.s.cookies.set("MUSIC_U", "rotated", domain=".music.163.com")

    with pytest.raises(OSError, match="disk full"):
        client.save_cookies()

    assert not Path(str(cookie_file) + ".tmp").exists()
    assert cookie_file.read_text(encoding="utf-8") == before


def test_csrf_and_logged_in_helpers() -> None:
    session = FakeSession()
    client = _client(session)
    assert client.logged_in is False
    assert client._csrf() == ""
    session.cookies.set("__csrf", "tok", domain=".music.163.com")
    assert client._csrf() == "tok"


# --------------------------------------------------------------------------- #
# request plumbing
# --------------------------------------------------------------------------- #


def test_parse_rejects_non_200() -> None:
    with pytest.raises(NeteaseError, match="HTTP 503"):
        NeteaseClient._parse(FakeResponse({}, status_code=503))


def test_parse_rejects_non_json() -> None:
    with pytest.raises(NeteaseError, match="返回非 JSON"):
        NeteaseClient._parse(FakeResponse("<html>oops</html>"))


def test_post_weapi_injects_csrf_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def _spy(data: dict[str, Any]) -> dict[str, str]:
        captured.append(dict(data))
        return {"params": "p", "encSecKey": "k"}

    monkeypatch.setattr(weapi, "weapi_encrypt", _spy)
    session = FakeSession([FakeResponse({"code": 200})])
    client = _client(session)
    session.cookies.set("__csrf", "tok", domain=".music.163.com")

    client.post_weapi("user/playlist", {"uid": 1})

    call = session.calls[0]
    assert call["url"] == "https://music.163.com/weapi/user/playlist"
    assert captured == [{"uid": 1, "csrf_token": "tok"}]


def test_post_weapi_without_csrf_omits_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def _spy(data: dict[str, Any]) -> dict[str, str]:
        captured.append(dict(data))
        return {"params": "p", "encSecKey": "k"}

    monkeypatch.setattr(weapi, "weapi_encrypt", _spy)
    session = FakeSession([FakeResponse({"code": 200})])
    client = _client(session)

    client.post_weapi("x/y")
    assert captured == [{}]


def test_post_api_plain_channel() -> None:
    session = FakeSession([FakeResponse({"code": 200, "data": 1})])
    client = _client(session)
    result = client.post_api("login/qrcode/unikey", {"type": 1})
    assert result["data"] == 1
    assert session.calls[0]["url"] == "https://music.163.com/api/login/qrcode/unikey"


# --------------------------------------------------------------------------- #
# QR login state machine
# --------------------------------------------------------------------------- #


def test_qr_start_prefers_weapi_then_falls_back() -> None:
    # weapi fails -> api endpoint answers.
    session = FakeSession(
        [
            FakeResponse({}, status_code=500),
            FakeResponse({"unikey": "KEY-1"}),
        ]
    )
    client = _client(session)
    key, url = client.qr_start()
    assert key == "KEY-1"
    assert url == "https://music.163.com/login?codekey=KEY-1"
    assert [c["url"].split("/weapi/")[-1] for c in session.calls[:1]] == [
        "login/qrcode/unikey"
    ]
    assert "api/login/qrcode/unikey" in session.calls[1]["url"]


def test_qr_start_raises_when_both_channels_fail() -> None:
    session = FakeSession([FakeResponse({}), FakeResponse({})])
    client = _client(session)
    with pytest.raises(NeteaseError, match="Failed to get the QR-code key"):
        client.qr_start()


def test_qr_check_states_without_login() -> None:
    for code, expected in (
        (800, "二维码已过期"),
        (801, "等待扫码"),
        (802, "已扫码，请在手机上确认"),
    ):
        session = FakeSession([FakeResponse({"code": code})])
        client = _client(session)
        got, msg = client.qr_check("KEY")
        assert got == code
        assert msg == expected
        assert session.calls[0]["url"].endswith("/api/login/qrcode/client/login")


def test_qr_check_803_absorbs_music_u_and_saves(tmp_path: Path) -> None:
    cookie_file = str(tmp_path / "c.json")
    music_cookie = _cookie("MUSIC_U", "u-1")
    session = FakeSession([FakeResponse({"code": 803}, cookies=[music_cookie])])
    client = _client(session, cookie_file)

    code, msg = client.qr_check("KEY")
    assert code == 803
    assert msg == "授权成功"
    assert client.logged_in is True
    assert Path(cookie_file).exists()


def test_qr_check_803_without_music_u_raises(tmp_path: Path) -> None:
    session = FakeSession([FakeResponse({"code": 803})])
    client = _client(session, str(tmp_path / "c.json"))
    with pytest.raises(NeteaseError, match="MUSIC_U"):
        client.qr_check("KEY")


def test_qr_check_falls_back_to_weapi_then_raises() -> None:
    session = FakeSession(
        [FakeResponse({}, status_code=500), FakeResponse({"nocode": 1})]
    )
    client = _client(session)
    with pytest.raises(NeteaseError, match="QR-code status query failed"):
        client.qr_check("KEY")


def test_qr_check_unknown_code_uses_message_field() -> None:
    session = FakeSession([FakeResponse({"code": 999, "message": "weird"})])
    client = _client(session)
    code, msg = client.qr_check("KEY")
    assert (code, msg) == (999, "weird")


class _CookieObj:
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


def _cookie(name: str, value: str) -> _CookieObj:
    return _CookieObj(name, value)


# --------------------------------------------------------------------------- #
# account / cookie login
# --------------------------------------------------------------------------- #


def test_account_prefers_api_then_weapi() -> None:
    session = FakeSession(
        [
            FakeResponse({"code": 200, "account": {"id": 7}, "profile": {"p": 1}}),
        ]
    )
    client = _client(session)
    account, profile = client.account()
    assert account == {"id": 7}
    assert profile == {"p": 1}


def test_account_returns_none_when_both_channels_answer_without_account() -> None:
    session = FakeSession(
        [
            FakeResponse({"code": 200, "account": None}),
            FakeResponse({"code": 200, "account": None}),
        ]
    )
    client = _client(session)
    account, profile = client.account()
    assert account is None and profile is None


def test_account_reraises_last_error_when_weapi_also_fails() -> None:
    # api fails, weapi answers but without an account: the saved transport
    # error is still raised instead of being masked as "not logged in".
    session = FakeSession(
        [
            FakeResponse({}, status_code=500),
            FakeResponse({"code": 200, "account": None}),
        ]
    )
    client = _client(session)
    with pytest.raises(NeteaseError, match="HTTP 500"):
        client.account()


def test_account_reraises_when_both_channels_fail() -> None:
    session = FakeSession(
        [FakeResponse({}, status_code=500), FakeResponse({}, status_code=500)]
    )
    client = _client(session)
    with pytest.raises(NeteaseError, match="HTTP 500"):
        client.account()


def test_login_by_cookie_accepts_full_cookie_string() -> None:
    session = FakeSession(
        [FakeResponse({"code": 200, "account": {"id": 1}, "profile": {}})]
    )
    client = _client(session)
    account, profile, n = client.login_by_cookie("MUSIC_U=abc; __csrf=tok; broken; =; ")
    assert account == {"id": 1}
    assert n == 2
    assert client.logged_in is True


def test_login_by_cookie_accepts_bare_music_u_value() -> None:
    session = FakeSession(
        [FakeResponse({"code": 200, "account": {"id": 1}, "profile": {}})]
    )
    client = _client(session)
    _, _, n = client.login_by_cookie("  rawvalue123  ")
    assert n == 1


def test_login_by_cookie_rejects_empty_input() -> None:
    client = NeteaseClient()
    client.s = FakeSession()  # type: ignore[assignment]
    with pytest.raises(NeteaseError, match="Cookie is empty"):
        client.login_by_cookie("   ")


def test_login_by_cookie_rejects_missing_music_u() -> None:
    client = NeteaseClient()
    client.s = FakeSession()  # type: ignore[assignment]
    # "MUSIC_U" without a '=' separator cannot be parsed into the jar.
    with pytest.raises(NeteaseError, match="Could not parse MUSIC_U"):
        client.login_by_cookie("MUSIC_U")


def test_login_by_cookie_clears_state_when_validation_fails() -> None:
    session = FakeSession(
        [FakeResponse({}, status_code=500), FakeResponse({}, status_code=500)]
    )
    client = _client(session)
    with pytest.raises(NeteaseError, match="校验登录态失败"):
        client.login_by_cookie("MUSIC_U=abc")
    assert client.logged_in is False


def test_login_by_cookie_clears_state_when_account_is_none() -> None:
    session = FakeSession(
        [
            FakeResponse({"code": 200, "account": None}),
            FakeResponse({"code": 200, "account": None}),
        ]
    )
    client = _client(session)
    with pytest.raises(NeteaseError, match="Cookie is invalid or expired"):
        client.login_by_cookie("MUSIC_U=abc")
    assert client.logged_in is False


# --------------------------------------------------------------------------- #
# catalogue endpoints
# --------------------------------------------------------------------------- #


def test_user_playlists_defaults_to_empty() -> None:
    session = FakeSession([FakeResponse({"code": 200})])
    client = _client(session)
    assert client.user_playlists(1) == []


def test_playlist_tracks_uses_song_detail_when_ids_exceed_tracks() -> None:
    pl = {
        "playlist": {
            "tracks": [{"id": 1, "name": "one"}],
            "trackIds": [{"id": 1}, {"id": 2}, {"id": 3}],
        }
    }
    detail = {"songs": [{"id": 1, "name": "one"}, {"id": 2}, {"id": 3}]}
    session = FakeSession([FakeResponse(pl), FakeResponse(detail)])
    client = _client(session)
    tracks = client.playlist_tracks(42)
    assert [t["id"] for t in tracks] == [1, 2, 3]
    assert session.calls[1]["url"].endswith("/weapi/v3/song/detail")


def test_playlist_tracks_short_circuits_when_complete() -> None:
    pl = {
        "playlist": {
            "tracks": [{"id": 1}, {"id": 2}],
            "trackIds": [{"id": 1}, {"id": 2}],
        }
    }
    session = FakeSession([FakeResponse(pl)])
    client = _client(session)
    tracks = client.playlist_tracks(42)
    assert len(tracks) == 2
    assert len(session.calls) == 1


def test_playlist_tracks_tolerates_bare_and_missing_track_ids() -> None:
    """个别 trackIds 条目是裸 id 或缺 id（接口抖动/历史版本）。

    旧实现对每个条目取 ``t["id"]``，一个坏条目就 KeyError 炸掉整个歌单。
    """
    pl = {
        "playlist": {
            "tracks": [{"id": 1}, {"id": 2}],
            # 裸 id / 缺 id 的字典混在一起
            "trackIds": [{"id": 1}, 2, {}, {"no_id": True}, None],
        }
    }
    session = FakeSession([FakeResponse(pl)])
    client = _client(session)
    tracks = client.playlist_tracks(42)

    assert [t["id"] for t in tracks] == [1, 2]
    # 跳过后 id 数与 tracks 数一致：不需要 song_detail 补齐
    assert len(session.calls) == 1


def test_playlist_tracks_bare_ids_still_trigger_song_detail_backfill() -> None:
    pl = {
        "playlist": {
            "tracks": [{"id": 1}],
            "trackIds": [{"id": 1}, 2, 3],
        }
    }
    detail = {"songs": [{"id": 1, "name": "one"}, {"id": 2}, {"id": 3}]}
    session = FakeSession([FakeResponse(pl), FakeResponse(detail)])
    client = _client(session)
    tracks = client.playlist_tracks(42)
    assert [t["id"] for t in tracks] == [1, 2, 3]
    assert session.calls[1]["url"].endswith("/weapi/v3/song/detail")


def test_playlist_tracks_raises_login_expired_when_song_detail_returns_301() -> None:
    """An expired session must surface as LoginExpired, not an empty playlist.

    ``song_detail`` is the only catalogue call without the 301 guard; when the
    session dies between the two requests the user used to silently get
    ``[]`` (looks like an empty playlist) instead of a re-login prompt.
    """
    pl = {
        "playlist": {
            "tracks": [{"id": 1, "name": "one"}],
            "trackIds": [{"id": 1}, {"id": 2}, {"id": 3}],
        }
    }
    session = FakeSession([FakeResponse(pl), FakeResponse({"code": 301})])
    client = _client(session)

    with pytest.raises(LoginExpired):
        client.playlist_tracks(42)


def test_song_detail_batches_by_500() -> None:
    ids = list(range(1200))
    responses = [
        FakeResponse({"songs": [{"id": i} for i in ids[0:500]]}),
        FakeResponse({"songs": [{"id": i} for i in ids[500:1000]]}),
        FakeResponse({"songs": [{"id": i} for i in ids[1000:1200]]}),
    ]
    session = FakeSession(responses)
    client = _client(session)
    songs = client.song_detail(ids)
    assert len(songs) == 1200
    assert len(session.calls) == 3


def test_search_prefers_cloudsearch_and_returns_total() -> None:
    session = FakeSession(
        [FakeResponse({"result": {"songs": [{"id": 1}], "songCount": 55}})]
    )
    client = _client(session)
    songs, total = client.search("hello", limit=30)
    assert total == 55
    assert len(songs) == 1
    assert session.calls[0]["url"].endswith("/weapi/cloudsearch/get/web")


def test_search_falls_back_to_legacy_endpoint() -> None:
    session = FakeSession(
        [
            FakeResponse({"result": {}}),  # cloudsearch: empty
            FakeResponse({"result": {"songs": [{"id": 9}], "songCount": 1}}),
        ]
    )
    client = _client(session)
    songs, total = client.search("hello")
    assert total == 1
    assert session.calls[1]["url"].endswith("/weapi/search/get")


def test_search_returns_empty_when_both_fail() -> None:
    session = FakeSession(
        [FakeResponse({}, status_code=500), FakeResponse({}, status_code=500)]
    )
    client = _client(session)
    assert client.search("x") == ([], 0)


def test_song_urls_batches_by_100_and_picks_encode_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def _spy(data: dict[str, Any]) -> dict[str, str]:
        captured.append(dict(data))
        return {"params": "p", "encSecKey": "k"}

    monkeypatch.setattr(weapi, "weapi_encrypt", _spy)
    ids = list(range(150))
    session = FakeSession(
        [
            FakeResponse({"data": [{"id": i} for i in ids[0:100]]}),
            FakeResponse({"data": [{"id": i} for i in ids[100:150]]}),
        ]
    )
    client = _client(session)
    out = client.song_urls(ids, level="lossless")
    assert len(out) == 150
    assert len(captured) == 2
    assert json.loads(captured[0]["ids"]) == ids[0:100]
    assert captured[0]["encodeType"] == "flac"  # lossless/hires use flac
    assert captured[1]["encodeType"] == "flac"


def test_song_urls_uses_mp3_for_standard_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def _spy(data: dict[str, Any]) -> dict[str, str]:
        captured.append(dict(data))
        return {"params": "p", "encSecKey": "k"}

    monkeypatch.setattr(weapi, "weapi_encrypt", _spy)
    session = FakeSession([FakeResponse({"data": []})])
    client = _client(session)
    client.song_urls([1], level="exhigh")
    assert captured[0]["encodeType"] == "mp3"


def test_lyric_returns_original_and_translation() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "lrc": {"lyric": "[00:00]hi"},
                    "tlyric": {"lyric": "[00:00]你好"},
                }
            )
        ]
    )
    client = _client(session)
    assert client.lyric(5) == ("[00:00]hi", "[00:00]你好")


def test_lyric_defaults_to_empty_strings() -> None:
    session = FakeSession([FakeResponse({})])
    client = _client(session)
    assert client.lyric(5) == ("", "")


# --------------------------------------------------------------------------- #
# track field accessors
# --------------------------------------------------------------------------- #


def test_track_accessors() -> None:
    web = {
        "name": "Song",
        "ar": [{"name": "A"}, {"name": "B"}],
        "al": {"name": "Album", "picUrl": "http://cover"},
        "dt": 123456,
        "no": 3,
    }
    legacy = {
        "artists": [{"name": "X"}],
        "album": {"name": "Old", "picUrl": "http://old"},
        "duration": 2000,
    }
    assert track_name(web) == "Song"
    assert track_name({}) == "未知歌曲"
    assert track_artists(web) == "A / B"
    assert track_artists(legacy) == "X"
    assert track_artists({}) == "未知歌手"
    assert track_album(web) == "Album"
    assert track_album(legacy) == "Old"
    assert track_album({}) == "未知专辑"
    assert track_cover(web) == "http://cover"
    assert track_cover({}) == ""
    assert track_duration(web) == 123
    assert track_duration(legacy) == 2
    assert track_no(web) == 3
    assert track_no({}) == 0
    assert track_no({"no": "7"}) == 7
