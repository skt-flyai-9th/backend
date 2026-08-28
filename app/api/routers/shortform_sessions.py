"""숏폼 Agent 세션 API (R06 재설계, 2026-08-26).

세션 생성(`POST /stores/{storeId}/shortform-sessions`)만 가게 하위 경로이고,
이후 조작은 전부 세션 ID만으로 접근한다 — `/tasks/{taskId}`와 같은 방식이다.
"""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession
from app.models.video_format import VideoFormat
from app.schemas.common import MessageResponse
from app.schemas.shortform_session import (
    NextRecommendationResponse,
    RecommendationResponse,
    SessionAcceptRequest,
    SessionAcceptResponse,
    SessionOptionResponse,
    TurnRequest,
    TurnResponse,
)
from app.services import shortform_session as session_service

router = APIRouter(prefix="/shortform-sessions", tags=["shortform-sessions"])


def _recommendation_response(db: Session, r: Any) -> RecommendationResponse:
    format_id = session_service.find_video_format_id(
        db, r.editing_template_id, r.editing_template_version
    )
    video_format = db.get(VideoFormat, format_id) if format_id is not None else None
    if video_format is None:
        raise session_service.RecommendationMediaUnavailable
    return RecommendationResponse(
        recommendation_id=r.recommendation_id,
        project_title=r.project_title,
        title=r.title,
        concept=r.concept,
        editing_template_id=r.editing_template_id,
        editing_template_version=r.editing_template_version,
        video_format_id=format_id,
        reference_url=video_format.reference_url,
        guide_video_url=video_format.guide_video_url or video_format.reference_url,
        source_platform=video_format.source_platform or "YOUTUBE",
    )


@router.post("/{session_id}/turns", response_model=TurnResponse)
def submit_turn(
    session_id: int, payload: TurnRequest, user: CurrentUser, db: DbSession
) -> TurnResponse:
    """대화 turn 하나를 보낸다(텍스트/선택지/확인)."""
    session = session_service.get_owned_session(db, user, session_id)
    result, recommendations = session_service.submit_turn(db, session, payload.input)
    return TurnResponse(
        id=session.id,
        action=result.action,
        assistant_message=result.assistant_message,
        project_state=result.project_state,
        options=[SessionOptionResponse(id=o.id, label=o.label) for o in result.options],
        recommendations=[_recommendation_response(db, r) for r in recommendations],
        has_more_recommendations=session_service.has_more_recommendations(db, session),
    )


@router.post("/{session_id}/recommendations/next", response_model=NextRecommendationResponse)
def get_next_recommendation(
    session_id: int, user: CurrentUser, db: DbSession
) -> NextRecommendationResponse:
    """같은 세션에서 이전 템플릿들을 제외한 다음 추천 묶음을 받는다."""
    session = session_service.get_owned_session(db, user, session_id)
    recommendations, shown_ids = session_service.get_next_recommendation(db, session)
    return NextRecommendationResponse(
        id=session.id,
        recommendations=[_recommendation_response(db, r) for r in recommendations],
        shown_template_ids=shown_ids,
        has_more_recommendations=session_service.has_more_recommendations(db, session),
    )


@router.post(
    "/{session_id}/accept", response_model=SessionAcceptResponse, status_code=HTTPStatus.CREATED
)
def accept_recommendation(
    session_id: int, payload: SessionAcceptRequest, user: CurrentUser, db: DbSession
) -> SessionAcceptResponse:
    """추천 카드 중 하나를 수락해 숏폼 프로젝트를 만든다. AI 호출 없이 BE 로직만으로 처리된다."""
    session = session_service.get_owned_session(db, user, session_id)
    project = session_service.accept_recommendation(db, session, payload.recommendation_id)
    return SessionAcceptResponse.model_validate(project)


@router.delete("/{session_id}", response_model=MessageResponse)
def discard_session(session_id: int, user: CurrentUser, db: DbSession) -> MessageResponse:
    """세션을 종료한다(새로고침/포기). 이미 종료된 세션도 200(멱등)."""
    session = session_service.get_owned_session(db, user, session_id)
    session_service.discard_session(db, session)
    return MessageResponse(message="세션이 종료되었습니다.")
