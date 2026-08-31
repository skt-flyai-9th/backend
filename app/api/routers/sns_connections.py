"""SNS 연동 API (API명세서 16.1 시작 / 해제 / 목록 조회)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.api.deps import CurrentUser, DbSession
from app.core.security import create_oauth_state, decode_oauth_state
from app.schemas.common import MessageResponse
from app.schemas.sns import (
    AuthorizeResponse,
    ConnectionItem,
    ConnectionListResponse,
    SnsPlatform,
)
from app.services import sns as sns_service
from app.services import sns_oauth

router = APIRouter(prefix="/sns-connections", tags=["sns-connections"])

logger = logging.getLogger(__name__)


@router.get("", response_model=ConnectionListResponse)
def list_connections(user: CurrentUser, db: DbSession) -> ConnectionListResponse:
    """연동된 계정 목록.

    앱을 껐다 켜도 연동 상태를 복원할 수 있고, **성과 화면의 플랫폼 탭도 이 응답으로
    구성**한다(하드코딩하면 플랫폼이 늘 때 화면을 고쳐야 한다).
    """
    connections = sns_service.list_connections(db, user)
    return ConnectionListResponse(
        connections=[ConnectionItem.model_validate(c) for c in connections]
    )


@router.get("/authorize", response_model=AuthorizeResponse)
def authorize(
    user: CurrentUser,
    platform: Annotated[SnsPlatform, Query(description="연동할 플랫폼")],
) -> AuthorizeResponse:
    """연동을 시작할 인증 URL을 만든다.

    **앱은 이 URL을 브라우저로 열기만 하면 된다.** App ID·스코프·위조 방지용 `state`는
    서버가 만들며, 앱에 App Secret이 들어가지 않는다.

    서버에 그 플랫폼 키가 없으면 **여기서** 503으로 막는다 — 키 없이 URL을 만들면
    사장님이 플랫폼 로그인 화면까지 갔다가 거기서 실패하고, 무엇이 잘못됐는지
    알 수 없게 된다.
    """
    config = sns_oauth.get_platform(platform.value)
    state = create_oauth_state(user.id, platform.value)
    return AuthorizeResponse(
        authorize_url=config.build_authorize_url(state, sns_oauth.redirect_uri())
    )


@router.get("/callback", response_class=HTMLResponse, include_in_schema=False)
def callback(
    db: DbSession,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """플랫폼이 사용자를 되돌려 보내는 곳. **앱이 아니라 브라우저가 연다.**

    명세서에는 없는 서버 내부 경로다 — 프론트가 호출하지 않는다. 대신 사장님이
    **직접 보는 화면**이라 HTML을 돌려준다.

    동의 거부·실패에도 안내 페이지를 띄운다(FE 합의, 2026-08-23). 빈 화면이나
    JSON 오류만 보이면 사장님은 무엇을 해야 할지 알 수 없다.

    인증이 필요 없는 경로다 — 플랫폼이 리다이렉트하는 요청이라 우리 액세스 토큰이
    실려 오지 않는다. 대신 `state`에 담긴 서명으로 누구의 연동인지 확인한다.
    """
    if error or not code or not state:
        logger.warning("SNS 연동 콜백 실패(파라미터 부족): error=%s", error)
        return _page("연동에 실패했습니다.", "앱으로 돌아가 다시 시도해주세요.", ok=False)

    try:
        parsed = decode_oauth_state(state)
        config = sns_oauth.get_platform(parsed.platform)
        tokens = sns_oauth.exchange_code(config, code)
    except Exception:
        # 어떤 이유든 사장님에게는 같은 안내를 보여준다. 원인은 서버 로그에 남는다.
        # (2026-08-31 이전에는 실제로 로그를 남기는 코드가 없어 이 주석과 달리
        # 원인이 그대로 사라졌다 — Q2-7 유튜브 연동 미스터리의 원인.)
        logger.exception("SNS 연동 콜백 실패")
        return _page("연동에 실패했습니다.", "앱으로 돌아가 다시 시도해주세요.", ok=False)

    sns_service.save_connection(db, parsed.user_id, parsed.platform, tokens)
    return _page("연결됐습니다.", "앱으로 돌아가 주세요.", ok=True)


def _page(title: str, message: str, *, ok: bool) -> HTMLResponse:
    """사장님이 보는 안내 페이지. 앱으로 돌아가라는 것 외에 할 일이 없어 단순하게 둔다."""
    color = "#111827" if ok else "#B91C1C"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="margin:0;display:flex;align-items:center;justify-content:center;
min-height:100vh;font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;
background:#F9FAFB;text-align:center;padding:24px">
<div><h1 style="font-size:20px;color:{color};margin:0 0 12px">{title}</h1>
<p style="font-size:15px;color:#6B7280;margin:0">{message}</p></div>
</body></html>""",
        status_code=200 if ok else 400,
    )


@router.delete("/{connection_id}", response_model=MessageResponse)
def disconnect(connection_id: int, user: CurrentUser, db: DbSession) -> MessageResponse:
    """연동을 해제한다.

    **게시 이력은 남는다.** 연동을 끊었다고 "이 영상을 올렸다"는 사실이 사라지는
    건 아니라서, 성과 화면의 과거 기록도 유지된다.
    """
    connection = sns_service.get_owned_connection(db, user, connection_id)
    sns_service.disconnect(db, connection)
    return MessageResponse(message="연동이 해제되었습니다.")
