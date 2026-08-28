"""숏폼 Agent 세션 스키마 (R06 재설계, 2026-08-26).

`project_state`·`recommendation`은 AI 응답을 그대로 캐시하는 자리라 `promotion_detail`
과 같은 이유로 고정 스키마 대신 `dict[str, Any]`로 둔다 — AI 스펙(`docs/AI_연동_입출력.md`)
이 v0.1이라 필드가 더 늘 수 있고, 우리가 구조를 검증할 필요도 없다(그대로 캐시했다가
그대로 돌려줄 뿐).
"""

from enum import StrEnum
from typing import Any

from app.models.shortform_session import SessionStatus
from app.schemas.common import BaseSchema, UtcDatetime


class TurnInputType(StrEnum):
    TEXT = "TEXT"
    OPTION = "OPTION"
    CONFIRM = "CONFIRM"


class TurnInput(BaseSchema):
    """대화 turn 입력. `type`에 따라 나머지 필드 중 하나만 채운다."""

    type: TurnInputType
    text: str | None = None
    option_id: str | None = None
    value: bool | None = None


class TurnRequest(BaseSchema):
    input: TurnInput


class SessionOptionResponse(BaseSchema):
    id: str
    label: str


class RecommendationResponse(BaseSchema):
    recommendation_id: str
    project_title: str
    title: str
    concept: str
    editing_template_id: str
    editing_template_version: int
    # 2026-08-26 추가. editing_template_id/version으로 video_formats를 조회해
    # 찾은 값 — 프론트가 5.2로 썸네일·촬영시간·난이도·얼굴노출을 채울 수 있게
    # 한다. 아직 한 번도 채택된 적 없는 템플릿이면 매칭되는 행이 없어 null이다
    # (지어내지 않는다 — 채택 시점에만 생기는 값이라 순수 조회로 그친다).
    video_format_id: int | None
    reference_url: str
    guide_video_url: str
    source_platform: str


class SessionCreateResponse(BaseSchema):
    id: int
    status: SessionStatus
    assistant_message: str | None
    options: list[SessionOptionResponse]
    project_state: dict[str, Any]


class TurnResponse(BaseSchema):
    id: int
    action: str
    assistant_message: str | None
    project_state: dict[str, Any]
    options: list[SessionOptionResponse]
    # 추천이 나온 turn이면 화면에 한 번에 보여줄 개수(기본 3장)만큼 채워서 온다.
    # 추천이 없는 turn이면 빈 배열이다 — null 대신 빈 배열을 써서 프론트가 항상
    # 같은 타입으로 다룰 수 있게 한다.
    recommendations: list[RecommendationResponse]
    has_more_recommendations: bool


class NextRecommendationResponse(BaseSchema):
    id: int
    recommendations: list[RecommendationResponse]
    shown_template_ids: list[str]
    has_more_recommendations: bool


class SessionAcceptRequest(BaseSchema):
    """여러 장 중 어느 카드를 골랐는지(2026-08-26, 추천 3장 동시 노출로 변경)."""

    recommendation_id: str


class SessionAcceptResponse(BaseSchema):
    """추천 수락 → 프로젝트 생성 (4.1 `ProjectCreateResponse`의 상위집합).

    `project_title`·`video_format_id`가 이미 채워진 채로 만들어지는 것이
    기존 4.1(직접 진입)과의 차이다.
    """

    id: int
    store_id: int
    project_title: str | None
    video_format_id: int
    promotion_purpose: str
    menu_id: int | None
    shorts_status: str
    created_at: UtcDatetime
