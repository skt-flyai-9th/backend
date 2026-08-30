"""SNS 플랫폼 OAuth 어댑터 (API명세서 16.1).

**플랫폼마다 다른 것을 이 파일에만 모은다** — 인증 URL 주소, 스코프 이름, 토큰 교환
방식, 계정 이름을 어디서 읽는지가 전부 다르다. 라우터·서비스는 `PlatformOAuth`
인터페이스만 보고 동작한다(외부 검색을 `store_search.py`로 뺀 것과 같은 구조).

**연동은 Instagram·YouTube 두 곳만 지원한다**(2026-08-24 확정). 게시(16.2)는 NAVER
Clip·TikTok도 되지만, 그 둘은 성과 지표를 가져올 API 경로가 없어 연동 대상이 아니다.
"""

import logging
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import quote, urlencode

import httpx

from app.core.config import settings
from app.core.exceptions import AppError, BadRequestError
from app.models.sns import SnsConnection

logger = logging.getLogger(__name__)

# 콜백 경로. 플랫폼 개발자 콘솔에 등록한 리디렉션 URI와 **정확히** 같아야 한다 —
# 한 글자만 달라도 플랫폼이 리다이렉트를 거부한다.
CALLBACK_PATH = "/sns-connections/callback"

# Instagram 장기 토큰 교환/갱신 엔드포인트. 최초 교환(token_url)이 돌려주는 건
# 1시간짜리 단기 토큰이라, 성과 수집(R17)이 쓰려면 여기서 60일짜리로 한 번 더
# 바꿔야 한다(2026-08-27, R17 성과 수집기 설계 중 발견).
_IG_EXCHANGE_URL = "https://graph.instagram.com/access_token"
_IG_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
# 마이페이지에 "연동됨" 대신 실제 계정명을 보여주기 위한 조회용(2026-08-30,
# FE 요청). 토큰 교환 응답 자체엔 사람이 읽을 이름이 안 실려 온다 —
# Instagram은 숫자 user_id뿐이고 YouTube는 아예 없다.
_IG_PROFILE_URL = "https://graph.instagram.com/me"
_YT_CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"


class UnsupportedPlatform(BadRequestError):
    error_code = "UNSUPPORTED_PLATFORM"
    message = "지원하지 않는 플랫폼입니다. INSTAGRAM 또는 YOUTUBE만 연동할 수 있습니다."


class SnsNotConfigured(AppError):
    """서버에 그 플랫폼 키가 없다. **사용자 잘못이 아니다.**

    연동을 *시작할 때* 막는다 — 키 없이 인증 URL을 만들면 사장님이 플랫폼 로그인
    화면까지 갔다가 거기서 실패한다. 그때는 무엇이 잘못됐는지 알 방법이 없다.
    """

    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    error_code = "SNS_NOT_CONFIGURED"
    message = "지금은 연동할 수 없습니다. 잠시 후 다시 시도해주세요."


class SnsAuthFailed(BadRequestError):
    error_code = "SNS_AUTH_FAILED"
    message = "SNS 연동에 실패했습니다. 다시 시도해주세요."


@dataclass(frozen=True)
class OAuthTokens:
    """플랫폼이 돌려준 토큰과 계정 정보."""

    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    account_name: str | None = None


@dataclass(frozen=True)
class PlatformOAuth:
    platform: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str

    def build_authorize_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
        }
        if self.platform == "YOUTUBE":
            # 구글은 이 둘이 있어야 refresh_token을 준다. 없으면 액세스 토큰이 만료된 뒤
            # 사장님에게 재로그인을 요구해야 해서, 주기적 지표 수집이 끊긴다.
            params["access_type"] = "offline"
            params["prompt"] = "consent"
        # urlencode 기본값(quote_plus)은 공백을 '+'로 인코딩하는데, Instagram의 OAuth
        # 서버는 쿼리스트링에서 '+'를 공백으로 풀어주지 않아 스코프 두 개가
        # "a+b"라는 하나의 잘못된 스코프로 합쳐져 "Invalid scope" 오류가 났다
        # (2026-08-26 실제로 겪음). RFC 3986대로 '%20'을 쓰는 quote로 바꾼다.
        return f"{self.authorize_url}?{urlencode(params, quote_via=quote)}"


_PLATFORMS = {
    "INSTAGRAM": {
        "authorize_url": "https://www.instagram.com/oauth/authorize",
        "token_url": "https://api.instagram.com/oauth/access_token",
        # 게시물 인사이트를 읽으려면 basic과 insights가 함께 필요하다.
        "scopes": ("instagram_business_basic", "instagram_business_manage_insights"),
    },
    "YOUTUBE": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # yt-analytics는 지표, youtube.readonly는 "어느 영상인지"를 찾는 데 쓴다.
        "scopes": (
            "https://www.googleapis.com/auth/yt-analytics.readonly",
            "https://www.googleapis.com/auth/youtube.readonly",
        ),
    },
}


def redirect_uri() -> str:
    """콜백 주소. 배포 도메인이 정해지면 `.env`의 값만 바꾸면 된다."""
    base = settings.SNS_REDIRECT_BASE_URL or settings.MEDIA_BASE_URL
    return f"{base.rstrip('/')}{CALLBACK_PATH}"


def get_platform(platform: str) -> PlatformOAuth:
    """플랫폼 설정을 돌려준다. 지원하지 않으면 400, 키가 없으면 503."""
    config = _PLATFORMS.get(platform)
    if config is None:
        raise UnsupportedPlatform

    client_id = getattr(settings, f"{platform}_CLIENT_ID", "")
    client_secret = getattr(settings, f"{platform}_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise SnsNotConfigured

    return PlatformOAuth(
        platform=platform,
        client_id=client_id,
        client_secret=client_secret,
        **config,  # type: ignore[arg-type]
    )


def exchange_code(platform: PlatformOAuth, code: str) -> OAuthTokens:
    """인증 코드를 액세스 토큰으로 바꾼다.

    **App Secret이 들어가는 유일한 호출이라 반드시 서버에서 한다.** 앱에 시크릿을
    넣으면 디컴파일로 꺼낼 수 있어, 2026-08-23에 이 구조로 바꿨다.
    """
    payload = {
        "client_id": platform.client_id,
        "client_secret": platform.client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
    }
    try:
        response = httpx.post(
            platform.token_url,
            data=payload,
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise SnsAuthFailed from error

    access_token = body.get("access_token")
    if not access_token:
        raise SnsAuthFailed

    expires_in = body.get("expires_in")
    if platform.platform == "INSTAGRAM":
        # 방금 받은 건 1시간짜리 단기 토큰이다. 성과 수집기가 매일 도는데 그때마다
        # 재로그인을 요구할 수 없으니, 여기서 바로 60일짜리로 바꿔서 저장한다.
        access_token, expires_in = _exchange_long_lived_instagram_token(
            platform.client_secret, access_token
        )
        account_name = _fetch_instagram_username(access_token)
    else:
        account_name = _fetch_youtube_channel_title(access_token)

    return OAuthTokens(
        access_token=access_token,
        refresh_token=body.get("refresh_token"),
        expires_in=expires_in,
        # 마이페이지가 "연동됨" 대신 실제 계정명("yeoljeong_coffee", "열정커피TV")을
        # 보여주는 데 쓴다(2026-08-30, FE 요청). 토큰 응답 자체엔 없어서 별도
        # 조회가 필요한데, 실패해도 연동 자체는 성공시킨다 — 이건 화면 표시용
        # 부가 정보라 없어도 동작에 지장이 없다(16.1에서 null로 내려가면
        # 프론트가 "연동됨"으로 대체 표시).
        account_name=account_name,
    )


def _fetch_instagram_username(access_token: str) -> str | None:
    try:
        response = httpx.get(
            _IG_PROFILE_URL,
            params={"fields": "username", "access_token": access_token},
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        username = response.json().get("username")
        return str(username) if username else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Instagram 계정명 조회 실패(무시): %s", type(exc).__name__)
        return None


def _fetch_youtube_channel_title(access_token: str) -> str | None:
    try:
        response = httpx.get(
            _YT_CHANNEL_URL,
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        items = response.json().get("items") or []
        if not items:
            return None
        title = items[0].get("snippet", {}).get("title")
        return str(title) if title else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("YouTube 채널명 조회 실패(무시): %s", type(exc).__name__)
        return None


def _exchange_long_lived_instagram_token(
    client_secret: str, short_lived_token: str
) -> tuple[str, int | None]:
    try:
        response = httpx.get(
            _IG_EXCHANGE_URL,
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": client_secret,
                "access_token": short_lived_token,
            },
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        access_token = body["access_token"]
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise SnsAuthFailed from error
    return access_token, body.get("expires_in")


def refresh_access_token(platform: PlatformOAuth, connection: SnsConnection) -> OAuthTokens:
    """만료가 가까운 토큰을 갱신한다 (R17 성과 수집기용, 2026-08-27).

    플랫폼마다 갱신 방식이 전혀 다르다.

    - **Instagram**: 별도 refresh_token이 없다. 장기 토큰 자신을 `ig_refresh_token`으로
      다시 발급받는 방식이고, 유효기간이 다시 60일로 늘어난다. 발급 후 24시간이
      지난 토큰만 갱신할 수 있다.
    - **YouTube**: 표준 OAuth `refresh_token` 그랜트. 구글은 갱신 응답에 새
      refresh_token을 다시 안 주므로 기존 값을 그대로 들고 있어야 한다.
    """
    if platform.platform == "INSTAGRAM":
        return _refresh_instagram_token(connection.access_token)
    return _refresh_youtube_token(platform, connection.refresh_token)


def _refresh_instagram_token(access_token: str | None) -> OAuthTokens:
    if not access_token:
        raise SnsAuthFailed
    try:
        response = httpx.get(
            _IG_REFRESH_URL,
            params={"grant_type": "ig_refresh_token", "access_token": access_token},
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        return OAuthTokens(access_token=body["access_token"], expires_in=body.get("expires_in"))
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise SnsAuthFailed from error


def _refresh_youtube_token(platform: PlatformOAuth, refresh_token: str | None) -> OAuthTokens:
    if not refresh_token:
        raise SnsAuthFailed
    try:
        response = httpx.post(
            platform.token_url,
            data={
                "client_id": platform.client_id,
                "client_secret": platform.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        return OAuthTokens(
            access_token=body["access_token"],
            # 구글은 갱신 응답에 refresh_token을 다시 실어주지 않는다 — 기존 값 유지.
            refresh_token=refresh_token,
            expires_in=body.get("expires_in"),
        )
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise SnsAuthFailed from error
