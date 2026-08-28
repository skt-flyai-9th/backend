"""숏폼 Agent 세션 로직 (R06 재설계, `docs/AI_연동_입출력.md` 5~12번 기준).

폐기된 예전 R06(질문형 6.1~6.3)을 대체한다. 대화(turn)를 몇 번 주고받으면 AI가
ACTIVE 영상편집템플릿 중 1개를 추천하고(`RECOMMEND`), 사장님이 수락하면 그 결과로
`shorts_projects`를 만든다. 4.1의 "AI 숏폼 추천(질문형, R06)" 경로가 정확히 이 흐름을
가리킨다 — 그 표에 이미 "대화 중 홍보 목적을 받는다"고 적혀 있었다.
"""

from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.models.shortform_session import SessionStatus, ShortformSession
from app.models.shorts_project import PromotionPurpose, ShortsProject, ShortsStatus
from app.models.store import Store
from app.models.store_insight import StoreInsight
from app.models.store_menu import StoreMenu
from app.models.user import User
from app.models.video_format import VideoFormat
from app.schemas.shortform_session import TurnInput
from app.services import ai_client
from app.services import plan as plan_service
from app.services.store import get_owned_store


class SessionNotFound(NotFoundError):
    error_code = "SESSION_NOT_FOUND"
    message = "숏폼 Agent 세션을 찾을 수 없습니다."


class RecommendationNotReady(ConflictError):
    error_code = "RECOMMENDATION_NOT_READY"
    message = "아직 수락할 수 있는 추천이 없습니다. 먼저 대화를 진행해주세요."


class SessionNotActive(ConflictError):
    error_code = "SESSION_NOT_ACTIVE"
    message = "이미 종료된 세션입니다."


class RecommendationNotFound(NotFoundError):
    error_code = "RECOMMENDATION_NOT_FOUND"
    message = "선택한 추천을 찾을 수 없습니다."


class RecommendationsExhausted(ConflictError):
    error_code = "NO_MORE_SHORTFORM_RECOMMENDATIONS"
    message = "현재 조건에 맞는 추천을 모두 확인했습니다."


class RecommendationMediaUnavailable(ConflictError):
    error_code = "RECOMMENDATION_MEDIA_UNAVAILABLE"
    message = "원본·가이드 영상이 확인된 숏폼만 추천할 수 있습니다. 다시 추천해주세요."


# 화면에 한 번에 보여줄 추천 개수. AI는 호출 한 번에 1개만 준다(`docs/AI_연동_입출력.md`
# 9번 "추천 원칙: 항상 1개만 반환") — 여러 개를 보여주는 건 백엔드가 "다시 추천 받기"를
# 필요한 만큼 이어서 호출해 묶어주는 것이다.
_RECOMMENDATION_BATCH_SIZE = 3


def _representative_menus(db: Session, store_id: int) -> list[StoreMenu]:
    """가게의 대표메뉴 전체(등록순).

    AI의 `store_context.representative_menus`(`docs/AI_연동_입출력.md` 6번)를 채우는
    데 쓴다. placeholder는 이 중 첫 번째만 "홍보 대상"으로 쓰지만, 실제 연동 시에는
    Agent가 전체 목록을 보고 대화 중에 하나를 골라야 하므로 목록 전체를 넘겨야 한다.
    """
    return list(
        db.scalars(select(StoreMenu).where(StoreMenu.store_id == store_id).order_by(StoreMenu.id))
    )


def _first_menu(db: Session, store_id: int) -> StoreMenu | None:
    """가게의 대표메뉴 하나(등록순 첫 번째). placeholder turn이 "홍보 대상"으로 쓴다."""
    menus = _representative_menus(db, store_id)
    return menus[0] if menus else None


def _trade_area_summary(db: Session, store_id: int) -> str | None:
    """가장 최근 상권분석 인사이트의 본문(자유 텍스트).

    AI가 원하는 `trade_area.characteristics`/`target_age_ranges`(구조화된 값)로
    바꾸는 방법이 아직 없다 — `store_insights.insight_content`가 사람이 읽는 문장이라
    구조를 분해할 수 없다. 지금은 원문을 그대로 넘기고, 필요하면 연동 시점에 다시
    설계한다.
    """
    insight = db.scalar(
        select(StoreInsight)
        .where(StoreInsight.store_id == store_id, StoreInsight.insight_type == "상권분석")
        .order_by(StoreInsight.generated_at.desc())
        .limit(1)
    )
    return insight.insight_content if insight else None


def create_session(db: Session, owner: User, store_id: int) -> tuple[ShortformSession, Any]:
    """세션을 시작한다."""
    store = get_owned_store(db, owner, store_id)
    menus = _representative_menus(db, store.id)
    trade_area = _trade_area_summary(db, store.id)
    greeting = ai_client.start_shortform_session(store, menus, trade_area)

    session = ShortformSession(
        store_id=store.id,
        session_token=greeting.session_token,
        status=SessionStatus.ACTIVE,
        project_state=greeting.project_state,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, greeting


def get_owned_session(db: Session, owner: User, session_id: int) -> ShortformSession:
    """본인 가게의 세션만 가져온다. 남의 것은 404(존재 자체를 숨긴다)."""
    session = db.get(ShortformSession, session_id)
    if session is None:
        raise SessionNotFound
    store = db.get(Store, session.store_id)
    if store is None or store.user_id != owner.id:
        raise SessionNotFound
    return session


def _serialize_recommendation(recommendation: Any) -> dict[str, Any]:
    return {
        "recommendation_id": recommendation.recommendation_id,
        "project_title": recommendation.project_title,
        "title": recommendation.title,
        "concept": recommendation.concept,
        "editing_template_id": recommendation.editing_template_id,
        "editing_template_version": recommendation.editing_template_version,
        "reference_url": recommendation.reference_url,
        "guide_video_url": recommendation.guide_video_url,
        "source_platform": recommendation.source_platform,
    }


def has_more_recommendations(db: Session, session: ShortformSession) -> bool:
    """동기화된 ACTIVE 포맷 중 아직 표시하지 않은 템플릿이 있는지 확인한다."""
    if session.session_token.startswith("sf_placeholder_"):
        return True
    linked_ids = set(
        db.scalars(
            select(VideoFormat.editing_template_id).where(
                VideoFormat.is_active.is_(True),
                VideoFormat.editing_template_id.is_not(None),
            )
        )
    )
    if not linked_ids:
        return True
    return bool(linked_ids - set(session.shown_template_ids or []))


def submit_turn(
    db: Session, session: ShortformSession, payload: TurnInput
) -> tuple[Any, list[Any]]:
    """대화 turn 하나를 처리한다.

    `payload`(사용자가 실제로 입력한 내용)를 그대로 어댑터에 넘긴다. placeholder는
    지금 이 값을 해석하지 않지만(대화 로직 자체가 없어서), **버리지는 않는다** — AI
    연동 시 이 값을 AI 서버 요청에 그대로 실어 보내야 하므로, 여기서 흘려버리면
    연동 시점에 호출부(라우터→서비스)까지 다시 손봐야 한다.

    `action`이 추천을 냈으면 AI가 한 번의 응답으로 내려준 추천 3개를 그대로 돌려준다.
    """
    if session.status is not SessionStatus.ACTIVE:
        raise SessionNotActive

    store = db.get(Store, session.store_id)
    assert store is not None  # 세션이 있으면 가게도 있다(FK)
    menu = _first_menu(db, store.id)

    result = ai_client.submit_shortform_turn(
        store, session.session_token, session.project_state or {}, payload.model_dump(), menu
    )

    session.project_state = result.project_state
    recommendations: list[Any] = []
    if result.recommendations:
        recommendations = result.recommendations[:_RECOMMENDATION_BATCH_SIZE]
        for recommendation in recommendations:
            _resolve_video_format(db, _serialize_recommendation(recommendation))
        shown = list(session.shown_template_ids or [])
        for recommendation in recommendations:
            if recommendation.editing_template_id not in shown:
                shown.append(recommendation.editing_template_id)
        session.last_recommendation = [_serialize_recommendation(r) for r in recommendations]
        session.shown_template_ids = shown
    db.commit()
    db.refresh(session)
    return result, recommendations


def get_next_recommendation(db: Session, session: ShortformSession) -> tuple[list[Any], list[str]]:
    """다시 추천 받기. 이미 종료된 세션이면 막는다.

    이번에도 한 번에 `_RECOMMENDATION_BATCH_SIZE`개를 채워서 돌려준다 — 화면
    구성(카드 3장)이 처음 추천 때와 같아야 하므로.
    """
    if session.status is not SessionStatus.ACTIVE:
        raise SessionNotActive

    store = db.get(Store, session.store_id)
    assert store is not None
    menu = _first_menu(db, store.id)
    shown_before = session.shown_template_ids or []

    try:
        recommendations = ai_client.get_next_shortform_recommendations(
            store, session.session_token, menu, shown_before
        )
    except ai_client.AINoMoreRecommendations as exc:
        raise RecommendationsExhausted from exc
    recommendations = recommendations[:_RECOMMENDATION_BATCH_SIZE]
    for recommendation in recommendations:
        _resolve_video_format(db, _serialize_recommendation(recommendation))
    shown = list(shown_before)
    for recommendation in recommendations:
        if recommendation.editing_template_id not in shown:
            shown.append(recommendation.editing_template_id)

    session.last_recommendation = [_serialize_recommendation(r) for r in recommendations]
    session.shown_template_ids = shown
    db.commit()
    db.refresh(session)
    return recommendations, shown


def find_video_format_id(
    db: Session, editing_template_id: str, editing_template_version: int
) -> int | None:
    """추천 카드에 보여줄 `video_format_id`를 조회한다 (2026-08-26 추가).

    **읽기 전용이다 — `_resolve_video_format`과 달리 없으면 만들지 않는다.**
    이 함수는 추천을 "보여줄 때"(수락 전) 호출되는데, 여기서 없다고 새로
    만들면 사장님이 고르지도 않은 템플릿이 `is_active=True`인 채로 5.1 피드에
    가짜 영상(`internal://...`) 카드로 노출된다. 아직 한 번도 채택된 적 없는
    템플릿이면 매칭되는 행이 없는 게 정상이라 `None`을 돌려준다.
    """
    return db.scalar(
        select(VideoFormat.id).where(
            VideoFormat.editing_template_id == editing_template_id,
            VideoFormat.editing_template_version == editing_template_version,
        )
    )


def _resolve_video_format(db: Session, recommendation: dict[str, Any]) -> VideoFormat:
    """추천받은 영상편집템플릿을 우리 `video_formats`에 연결한다.

    없으면 새로 적재한다 — 5.1이 AI가 발굴한 포맷을 `reference_url` 기준으로
    적재하는 것과 같은 자리이며, 여기서는 `editing_template_id`+`version`이 그 키다.
    """
    template_id = recommendation["editing_template_id"]
    version = recommendation["editing_template_version"]
    existing = db.scalar(
        select(VideoFormat).where(
            VideoFormat.editing_template_id == template_id,
            VideoFormat.editing_template_version == version,
        )
    )
    if existing is None:
        existing = db.scalar(
            select(VideoFormat)
            .where(VideoFormat.editing_template_id == template_id)
            .order_by(VideoFormat.is_active.desc(), VideoFormat.id.desc())
        )

    reference_url = str(recommendation.get("reference_url") or "").strip()
    guide_video_url = str(recommendation.get("guide_video_url") or "").strip()
    if existing is not None:
        if not reference_url and _is_playable_youtube_url(existing.reference_url):
            reference_url = existing.reference_url
        if not guide_video_url and _is_playable_youtube_url(existing.guide_video_url):
            guide_video_url = str(existing.guide_video_url)
    guide_video_url = guide_video_url or reference_url
    if not _is_playable_youtube_url(reference_url) or not _is_playable_youtube_url(guide_video_url):
        raise RecommendationMediaUnavailable

    # **`reference_url`이 다른 행과 같아도 충돌로 보지 않는다**(2026-08-28 정정).
    # 서로 다른 챌린지가 같은 대표 영상을 의도적으로 공유할 수 있다고 AI팀이
    # 확인했다(`app/services/trend_format.py`와 같은 결정, `video_formats.
    # reference_url` UNIQUE 제약도 이미 제거함). 예전엔 여기서 URL이 같은 다른
    # 행을 찾아 "충돌"로 보고 거부하거나 은퇴시켰는데, 그 전제가 틀렸다는 게
    # 밝혀졌다 — 정상적인 추천(예: "동그리오" 매장 홍보 버전)이 이미 카탈로그에
    # 있는 다른 템플릿(챌린지 버전)과 영상을 공유한다는 이유만으로 거부됐다.
    # 챌린지 정체성은 오직 `editing_template_id`+`version`으로만 판단한다.

    fallback_title = recommendation.get("title") or recommendation.get("project_title")
    video_format = existing or VideoFormat()
    video_format.format_title = fallback_title or "추천 포맷"
    video_format.reference_url = reference_url
    video_format.guide_video_url = guide_video_url
    video_format.source_platform = "YOUTUBE"
    video_format.editing_template_id = template_id
    video_format.editing_template_version = version
    video_format.is_active = True
    if existing is None:
        db.add(video_format)
    db.flush()
    return video_format


def _is_playable_youtube_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (
        host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
        or host.endswith(".youtube.com")
    )


def _derive_promotion(subject: dict[str, Any] | None) -> tuple[PromotionPurpose, int | None]:
    """`promotion_subject`에서 홍보 목적·메뉴 ID를 정한다.

    ⚠️ AI 문서(`docs/AI_연동_입출력.md`)는 `type: "MENU"` 예시만 보여주고
    이벤트/가게소개/고객늘리기에 대응하는 타입을 정의하지 않았다. MENU가 아니면
    일단 "가게소개"로 두고 `menu_id`는 비운다 — `docs/PM_DECISIONS.md`에 확인
    대기 항목으로 등록했다.
    """
    if subject and subject.get("type") == "MENU" and subject.get("menu_id") is not None:
        return PromotionPurpose.MENU, subject["menu_id"]
    return PromotionPurpose.STORE, None


def accept_recommendation(
    db: Session, session: ShortformSession, recommendation_id: str
) -> ShortsProject:
    """추천 카드 중 하나를 수락해 프로젝트를 만든다.

    추천 수락 자체는 AI 호출이 필요 없다(`docs/AI_연동_입출력.md` 11번). 다만
    프로젝트가 만들어지는 순간 바로 촬영 준비가 끝나 있도록, **7.1과 같은 로직
    (`plan_service.generate_plan`)을 이어서 호출해 콘티·태스크까지 함께 채운다**
    (2026-08-26 결정 — 두 단계로 쪼개면 프론트가 6.4 다음에 7.1을 또 불러야 하고,
    깜빡하면 "포맷은 있는데 콘티가 없는" 어중간한 프로젝트가 남는다).

    화면에 여러 장을 동시에 보여주므로(`_RECOMMENDATION_BATCH_SIZE`), 어느 카드를
    골랐는지 `recommendation_id`로 받아 캐시된 목록에서 찾는다.
    """
    if session.status is not SessionStatus.ACTIVE:
        raise SessionNotActive
    if not session.last_recommendation:
        raise RecommendationNotReady

    recommendation = next(
        (r for r in session.last_recommendation if r.get("recommendation_id") == recommendation_id),
        None,
    )
    if recommendation is None:
        raise RecommendationNotFound
    subject = (session.project_state or {}).get("promotion_subject")
    promotion_purpose, menu_id = _derive_promotion(subject)
    video_format = _resolve_video_format(db, recommendation)

    project = ShortsProject(
        store_id=session.store_id,
        video_format_id=video_format.id,
        project_title=recommendation.get("project_title"),
        recommendation_id=recommendation.get("recommendation_id"),
        promotion_purpose=promotion_purpose,
        menu_id=menu_id,
        shorts_status=ShortsStatus.DRAFT,
    )
    db.add(project)
    session.status = SessionStatus.ACCEPTED
    db.commit()
    db.refresh(project)

    # generate_plan이 project_title을 건드리지 않으므로 방금 저장한 값이 유지된다.
    plan_service.generate_plan(db, project, video_format.id)
    db.refresh(project)
    return project


def discard_session(db: Session, session: ShortformSession) -> None:
    """세션을 종료한다(새로고침/포기). 멱등이다 — 이미 종료된 세션도 200."""
    if session.status is SessionStatus.ACTIVE:
        session.status = SessionStatus.DISCARDED
        db.commit()
