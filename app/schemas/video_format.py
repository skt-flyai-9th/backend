"""숏폼 포맷 스키마 (API명세서 5.1~5.2)."""

from enum import StrEnum

from pydantic import Field

from app.schemas.common import BaseSchema, UtcDatetime


class FormatSort(StrEnum):
    """정렬 기준 (기능명세서 S05.2.1).

    ⚠️ `trending`·`views`는 **아직 최신순으로 동작한다.** 랭킹은 AI 서버가
    포맷 목록과 함께 내려줄 예정이라 우리 DB에 조회수·시계열 데이터가 없다
    (`docs/IMPLEMENTATION.md` 2026-08-23 항목). 계약을 유지해두면 AI 연동 시
    응답 순서만 바뀌고 프론트는 그대로다.
    """

    TRENDING = "trending"
    VIEWS = "views"
    LATEST = "latest"


class VideoFormatSummary(BaseSchema):
    """5.1 목록 항목.

    `reference_url`·`source_platform`은 5.2와 동일한 필드를 목록에도 노출한 것이다
    (2026-08-21 추가). 프론트가 상세를 열지 않고도 YouTube 썸네일을 구성할 수 있다.
    """

    id: int
    format_title: str
    format_type: str | None
    # 2026-08-30 개명(🔴 Breaking, 구 expected_duration_sec) — 완성 영상 길이다.
    # `shooting_summary.expected_duration_sec`(예상 촬영 소요시간, 7.1 응답)과
    # 이름이 같아 FE가 혼동해서 홈 카드에 엉뚱한 값을 "#촬영"으로 잘못 표시했다.
    # DB 컬럼명은 그대로 두고(다른 곳에서 "완성 영상 길이"로 이미 널리 쓰임)
    # 이 응답 필드에서만 별칭을 준다.
    reference_duration_sec: int | None = Field(validation_alias="expected_duration_sec")
    # 2026-08-30 추가 — 예상 촬영 소요시간(초). 템플릿에 고정된 값이라(실측 확인,
    # 가게·메뉴를 바꿔도 응답이 동일했다) 프로젝트를 만들지 않고도 카탈로그 동기화
    # 시점에 캐싱해서 바로 내려준다(`app/services/trend_format.py`). 트렌드 동기화
    # 전이거나 AI가 값을 안 준 포맷은 null이다 — 지어내지 않는다.
    estimated_shooting_sec: int | None
    shooting_difficulty: str | None
    requires_face: bool | None
    reference_url: str
    # 촬영 준비 화면에서 트는 가이드 영상. 홈 피드가 쓰는 대표 영상(`reference_url`)과
    # 다를 수 있다. 트렌드 클러스터에서 온 포맷에만 값이 있다.
    guide_video_url: str | None = None
    source_platform: str | None
    # 로그인 사용자가 이 포맷을 찜했는지. 피드에서 하트 채움 여부를 그리는 데 쓴다.
    is_favorite: bool = False
    # AI 추천 이유. 연동 전이라 항상 빈 배열이다(기능명세서 S05.1.2는 최소 2개 요구).
    recommend_reasons: list[str] = Field(default_factory=list)


class VideoFormatListResponse(BaseSchema):
    formats: list[VideoFormatSummary]


class VideoFormatDetailResponse(BaseSchema):
    id: int
    format_title: str
    format_type: str | None
    reference_url: str
    guide_video_url: str | None = None
    source_platform: str | None
    # 2026-08-30 개명(🔴 Breaking) — VideoFormatSummary와 같은 이유.
    reference_duration_sec: int | None = Field(validation_alias="expected_duration_sec")
    # 2026-08-30 추가 — VideoFormatSummary와 같은 값·같은 이유.
    estimated_shooting_sec: int | None
    shooting_difficulty: str | None
    requires_face: bool | None
    is_favorite: bool = False


class FavoriteResponse(BaseSchema):
    """찜하기 응답 (5.3 POST)."""

    video_format_id: int
    is_favorite: bool
    created_at: UtcDatetime
