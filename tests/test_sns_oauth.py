"""SNS OAuth 어댑터 단위 테스트 — 장기 토큰 교환·갱신 (R17 성과 수집기, 2026-08-27)."""

from typing import Any

import httpx
import pytest

from app.models.sns import SnsConnection
from app.services import sns_oauth


def _platform(platform: str, **overrides: Any) -> sns_oauth.PlatformOAuth:
    defaults = sns_oauth._PLATFORMS[platform]
    return sns_oauth.PlatformOAuth(
        platform=platform,
        client_id="client-id",
        client_secret="client-secret",
        **{**defaults, **overrides},
    )


def _response(json_body: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=json_body, request=httpx.Request("GET", "https://x"))


# ---------------------------------------------------------------- exchange_code


def test_exchange_code_instagram_upgrades_to_long_lived_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """최초 교환은 1시간짜리 단기 토큰이라, Instagram은 반드시 장기 토큰으로 바꿔야 한다."""

    def fake_post(url: str, data: dict[str, Any], timeout: float) -> httpx.Response:
        return _response({"access_token": "short-lived", "user_id": "12345"})

    def fake_get(url: str, params: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == sns_oauth._IG_EXCHANGE_URL
        assert params["grant_type"] == "ig_exchange_token"
        assert params["access_token"] == "short-lived"
        return _response({"access_token": "long-lived", "expires_in": 5184000})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    # 계정명 조회(_fetch_instagram_username)는 별도 함수로 검증한다 — 여기서는
    # 장기 토큰 교환 로직만 본다.
    monkeypatch.setattr(sns_oauth, "_fetch_instagram_username", lambda token: None)

    tokens = sns_oauth.exchange_code(_platform("INSTAGRAM"), "auth-code")

    assert tokens.access_token == "long-lived"
    assert tokens.expires_in == 5184000


def test_exchange_code_youtube_skips_long_lived_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """YouTube는 표준 OAuth라 별도 장기 토큰 교환이 없다."""

    def fake_post(url: str, data: dict[str, Any], timeout: float) -> httpx.Response:
        return _response(
            {"access_token": "yt-token", "refresh_token": "yt-refresh", "expires_in": 3600}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    # 채널명 조회(_fetch_youtube_channel_title)는 별도 함수로 검증한다.
    monkeypatch.setattr(sns_oauth, "_fetch_youtube_channel_title", lambda token: None)

    tokens = sns_oauth.exchange_code(_platform("YOUTUBE"), "auth-code")

    assert tokens.access_token == "yt-token"
    assert tokens.refresh_token == "yt-refresh"


# ---------------------------------------------------------------- 계정명 조회 (2026-08-30)


def test_exchange_code_instagram_uses_fetched_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """마이페이지에 숫자 user_id가 아니라 실제 닉네임이 뜨도록 한다(FE 요청)."""

    def fake_post(url: str, data: dict[str, Any], timeout: float) -> httpx.Response:
        return _response({"access_token": "short-lived", "user_id": "12345"})

    def fake_get(url: str, params: dict[str, Any], timeout: float) -> httpx.Response:
        if url == sns_oauth._IG_EXCHANGE_URL:
            return _response({"access_token": "long-lived", "expires_in": 5184000})
        assert url == sns_oauth._IG_PROFILE_URL
        assert params == {"fields": "username", "access_token": "long-lived"}
        return _response({"username": "yeoljeong_coffee"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    tokens = sns_oauth.exchange_code(_platform("INSTAGRAM"), "auth-code")

    assert tokens.account_name == "yeoljeong_coffee"


def test_fetch_instagram_username_returns_none_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """계정명 조회 실패는 연동 자체를 막으면 안 된다 — 화면 표시용 부가 정보다."""

    def fake_get(*args: Any, **kwargs: Any) -> httpx.Response:
        return _response({}, status=500)

    monkeypatch.setattr(httpx, "get", fake_get)

    assert sns_oauth._fetch_instagram_username("token") is None


def test_exchange_code_youtube_uses_fetched_channel_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """마이페이지에 아무 값도 없던 대신 실제 채널명이 뜨도록 한다(FE 요청)."""

    def fake_post(url: str, data: dict[str, Any], timeout: float) -> httpx.Response:
        return _response(
            {"access_token": "yt-token", "refresh_token": "yt-refresh", "expires_in": 3600}
        )

    def fake_get(
        url: str, params: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        assert url == sns_oauth._YT_CHANNEL_URL
        assert params == {"part": "snippet", "mine": "true"}
        assert headers == {"Authorization": "Bearer yt-token"}
        return _response({"items": [{"snippet": {"title": "열정커피TV"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    tokens = sns_oauth.exchange_code(_platform("YOUTUBE"), "auth-code")

    assert tokens.account_name == "열정커피TV"


def test_fetch_youtube_channel_title_returns_none_when_no_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """채널이 없는 계정(개인 유튜브 미개설 등)도 연동 자체는 성공해야 한다."""

    def fake_get(*args: Any, **kwargs: Any) -> httpx.Response:
        return _response({"items": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    assert sns_oauth._fetch_youtube_channel_title("token") is None


def test_exchange_code_instagram_raises_when_long_lived_exchange_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, data: dict[str, Any], timeout: float) -> httpx.Response:
        return _response({"access_token": "short-lived"})

    def fake_get(url: str, params: dict[str, Any], timeout: float) -> httpx.Response:
        return _response({"error": "boom"}, status=400)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(sns_oauth.SnsAuthFailed):
        sns_oauth.exchange_code(_platform("INSTAGRAM"), "auth-code")


# ---------------------------------------------------------------- refresh_access_token


def test_refresh_instagram_calls_refresh_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, params: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == sns_oauth._IG_REFRESH_URL
        assert params["grant_type"] == "ig_refresh_token"
        assert params["access_token"] == "old-long-lived"
        return _response({"access_token": "refreshed", "expires_in": 5184000})

    monkeypatch.setattr(httpx, "get", fake_get)
    connection = SnsConnection(user_id=1, sns_platform="INSTAGRAM", access_token="old-long-lived")

    tokens = sns_oauth.refresh_access_token(_platform("INSTAGRAM"), connection)

    assert tokens.access_token == "refreshed"
    assert tokens.expires_in == 5184000


def test_refresh_instagram_without_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = SnsConnection(user_id=1, sns_platform="INSTAGRAM", access_token=None)

    with pytest.raises(sns_oauth.SnsAuthFailed):
        sns_oauth.refresh_access_token(_platform("INSTAGRAM"), connection)


def test_refresh_youtube_uses_refresh_token_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, data: dict[str, Any], timeout: float) -> httpx.Response:
        captured["data"] = data
        return _response({"access_token": "new-access"})

    monkeypatch.setattr(httpx, "post", fake_post)
    connection = SnsConnection(
        user_id=1, sns_platform="YOUTUBE", access_token="stale", refresh_token="yt-refresh"
    )

    tokens = sns_oauth.refresh_access_token(_platform("YOUTUBE"), connection)

    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == "yt-refresh"
    assert tokens.access_token == "new-access"
    # 구글은 갱신 응답에 refresh_token을 다시 안 준다 — 기존 값을 그대로 들고 있어야 한다.
    assert tokens.refresh_token == "yt-refresh"


def test_refresh_youtube_without_refresh_token_raises() -> None:
    connection = SnsConnection(
        user_id=1, sns_platform="YOUTUBE", access_token="stale", refresh_token=None
    )

    with pytest.raises(sns_oauth.SnsAuthFailed):
        sns_oauth.refresh_access_token(_platform("YOUTUBE"), connection)
