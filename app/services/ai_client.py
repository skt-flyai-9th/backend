"""AI 서버 호출 어댑터.

**AI 팀에서 만드는 별도 서버를 부르는 자리다.** 아직 스펙이 나오지 않아
임시 구현(`_placeholder_plan`)이 들어 있고, 스펙이 확정되면 이 파일만 채우면 된다.
호출부(`app/services/plan.py`)와 라우터는 바뀌지 않는다.

외부 검색을 `store_search.py`로 분리한 것과 같은 이유다.

- **테스트 목킹**: CI에 AI 서버가 없으므로 경계가 없으면 테스트를 못 돌린다.
- **실패 격리**: AI가 죽어도 앱이 통째로 죽지 않게 한다.
- **교체 용이**: 동기/비동기 전환도 여기만 바꾸면 된다.

AI는 **콘티(`scenes`)와 촬영 태스크(`tasks`)를 함께** 내려준다(2026-08-23 확정).
태스크를 만드는 별도 API는 없고 7.1이 둘 다 생성한다.

⚠️ **임시 결과는 진짜 기획이 아니다.** 포맷·가게에 상관없이 같은 뼈대를 돌려주며,
`is_placeholder=True`로 표시된다. 화면에서 "AI 준비 중"을 안내하는 데 쓸 수 있다.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import AppError, ConflictError, UnprocessableEntityError
from app.models.shorts_project import ShortsProject
from app.models.store import Store
from app.models.store_menu import StoreMenu
from app.models.video_format import VideoFormat

logger = logging.getLogger(__name__)


class AIServiceUnavailable(AppError):
    status_code = 503
    error_code = "AI_SERVICE_UNAVAILABLE"
    message = "AI 서버에 일시적으로 연결할 수 없습니다. 잠시 후 다시 시도해주세요."


class AIServiceConfigurationError(AppError):
    status_code = 503
    error_code = "AI_SERVICE_CONFIGURATION_ERROR"
    message = "AI 서버 연동 설정을 확인해주세요."


class AINoMoreRecommendations(ConflictError):
    error_code = "NO_MORE_SHORTFORM_RECOMMENDATIONS"
    message = "현재 조건에 맞는 추천을 모두 확인했습니다."


class AITemplateNotLinked(UnprocessableEntityError):
    error_code = "AI_TEMPLATE_NOT_LINKED"
    message = "선택한 영상 포맷이 AI 편집 템플릿과 연결되어 있지 않습니다."


def _request_json(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """AI 내부 API를 호출하고 안전한 도메인 오류로 변환한다.

    인증 키와 AI 응답 본문은 로그에 남기지 않는다. AI 서버의 상세 오류는 내부 구현과
    외부 API 제공자 정보를 포함할 수 있어 메인 API 응답으로 그대로 전달하지 않는다.
    """
    url = f"{settings.AI_SERVER_URL.rstrip('/')}{path}"
    headers = {"X-Internal-API-Key": settings.AI_SERVER_API_KEY}
    try:
        response = httpx.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=query_params,
            timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        logger.warning("AI 서버 연결 실패: %s %s (%s)", method, path, type(exc).__name__)
        raise AIServiceUnavailable from exc

    if response.status_code == 401:
        logger.error("AI 서버 내부 인증 실패: %s %s", method, path)
        raise AIServiceConfigurationError
    if response.status_code == 409:
        try:
            detail = response.json().get("detail") or {}
        except ValueError:
            detail = {}
        if isinstance(detail, dict) and detail.get("code") == "NO_MORE_SHORTFORM_RECOMMENDATIONS":
            raise AINoMoreRecommendations
    if response.status_code in {409, 422, 429, 503}:
        logger.warning(
            "AI 서버가 요청을 처리할 준비가 되지 않음: %s %s -> %s",
            method,
            path,
            response.status_code,
        )
        raise AIServiceUnavailable
    if response.is_error:
        logger.warning("AI 서버 호출 실패: %s %s -> %s", method, path, response.status_code)
        raise AIServiceUnavailable

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("AI 서버가 JSON이 아닌 응답을 반환함: %s %s", method, path)
        raise AIServiceUnavailable from exc
    if not isinstance(payload, dict):
        raise AIServiceUnavailable
    return payload


_FACE_EXPOSURE_TOKENS = {
    "노출있음": "allowed",
    "노출없음": "not_allowed",
}


def _map_face_exposure(value: str | None) -> str:
    """한국어 얼굴노출모드를 AI 쪽 영문 토큰으로 바꾼다.

    우리 쪽은 노출있음/노출없음 2가지만 구분하기로 확정됐다(2026-08-25). AI 문서가
    실제로 확인해준 값은 "not_allowed" 하나뿐이라 "allowed"는 이름 대칭으로 추정한
    값이다 — AI팀 확인 전까지는 잠정치. 모르는 값(구버전 4모드 잔재 등)이 오면 동의
    안 된 얼굴을 노출시키는 쪽보다 안전하게 "not_allowed"로 떨어뜨린다.
    """
    return _FACE_EXPOSURE_TOKENS.get(value or "", "not_allowed")


def _promotion_subject_name(
    project: ShortsProject,
    store: Store,
    menu_name: str | None,
) -> str:
    if menu_name:
        return menu_name
    detail = project.promotion_detail or {}
    for key in ("name", "title", "menu_name", "event_name", "description", "subject"):
        value = str(detail.get(key) or "").strip()
        if value:
            return value
    return project.project_title or store.name


def _option(item: dict[str, Any]) -> "SessionOption":
    return SessionOption(id=str(item["id"]), label=str(item["label"]))


def _recommendation(item: dict[str, Any] | None) -> "Recommendation | None":
    if item is None:
        return None
    return Recommendation(
        recommendation_id=str(item["recommendation_id"]),
        project_title=str(item["project_title"]),
        title=str(item["title"]),
        concept=str(item["concept"]),
        editing_template_id=str(item["editing_template_id"]),
        editing_template_version=int(item["editing_template_version"]),
        reference_url=str(item.get("reference_url") or "") or None,
        guide_video_url=str(item.get("guide_video_url") or "") or None,
        source_platform=str(item.get("source_platform") or "") or None,
    )


def _recommendations(items: list[dict[str, Any]] | None) -> list["Recommendation"]:
    recommendations = [
        recommendation
        for item in items or []
        if (recommendation := _recommendation(item)) is not None
    ]
    if not recommendations:
        return []
    template_ids = [item.editing_template_id for item in recommendations]
    if len(recommendations) != 3 or len(set(template_ids)) != 3:
        raise AIServiceUnavailable
    return recommendations


@dataclass(frozen=True)
class PlannedScene:
    """AI가 만든 장면 하나."""

    scene_order: int
    scene_description: str
    scene_dialogue: str | None = None
    scene_subtitle: str | None = None
    shot_type: str | None = None
    target_duration_sec: int | None = None


@dataclass(frozen=True)
class PlannedTask:
    """AI가 쪼갠 촬영 태스크 하나 (기능명세서 S08.1.1).

    `scene_index`는 `ShootingPlan.scenes`의 몇 번째 장면에서 나온 태스크인지를
    가리킨다. 장면이 아직 DB에 저장되기 전이라 실제 `scene_id`를 알 수 없어,
    저장 시점에 호출부가 매핑한다.
    """

    display_order: int
    task_title: str
    task_type: str | None = None
    scene_index: int | None = None
    # 촬영 안내 (9.1). guide_type / instructions / broll_shot 를 담는다.
    guide: dict[str, Any] | None = None


@dataclass(frozen=True)
class ShootingGuide:
    """영상편집템플릿에 고정된 촬영 가이드 (`docs/AI_연동_입출력.md` 13번).

    7.1(`POST /shorts-projects/{projectId}/plan`)과 6.4(추천 수락)가 둘 다
    `app/services/plan.py::generate_plan()`을 통해 이 값을 쓴다(2026-08-26 결정 —
    "기존 방식 사용 안 함"이라는 AI팀 지침에 따라 7.1의 AI 호출 자체를 이걸로
    교체했다). **매번 새로 만드는 게 아니라 템플릿에 저장된 값을 그대로 반환**하는
    조회이므로, 구 `ShootingPlan`과 달리 `project_title`이 없다 — 제목은 R06의
    추천(`accept` 시점)에서만 나온다(`docs/AI_연동_입출력.md` 23번).

    `required_people`·`props`는 **영상편집템플릿의 고정 메타데이터**다(2026-08-26
    AI팀 확인) — 사용자 입력값이 아니고, 프로젝트 생성 단계에서 따로 받지 않는다.
    """

    estimated_shooting_sec: int | None = None
    required_people: int | None = None
    props: list[str] = field(default_factory=list)
    difficulty: str | None = None
    scenes: list[PlannedScene] = field(default_factory=list)
    tasks: list[PlannedTask] = field(default_factory=list)
    # 실제 AI가 만든 결과가 아니라 임시 뼈대라는 표시
    is_placeholder: bool = False


# ------------------------------------------------------- 트렌드 클러스터 (5.1 피드)
#
# AI 레포의 `challenges` 테이블이 원본이다. 유행 챌린지마다 **대표 영상**과
# **가이드 영상** YouTube URL을 갖고 있고, 사람이 덮어쓴 값(`override_*`)이 있으면
# 그쪽이 우선한다 — AI 서버가 `representative_youtube_url`/`guide_youtube_url`로
# 이미 합쳐서 내려주므로 우리는 그대로 받는다.


@dataclass(frozen=True)
class TrendChallenge:
    """트렌드 클러스터 항목 하나 (AI `GET /api/v1/challenges`)."""

    id: str
    name: str
    rank: int | None = None
    representative_youtube_url: str | None = None
    guide_youtube_url: str | None = None
    format_type: str | None = None
    expected_duration_sec: int | None = None
    shooting_difficulty: str | None = None
    requires_face: bool | None = None
    # 이 챌린지의 촬영가이드 템플릿이 승인 완료됐을 때만 채워진다(2026-08-26 AI팀
    # 확인). null이면 아직 승인 전이라 촬영가이드를 조회할 수 없다는 뜻이다.
    editing_template_id: str | None = None
    editing_template_version: int | None = None
    # AI가 트렌드에서 내린(철 지난) 챌린지인지. 기본 목록 조회는 활성만 주므로
    # `include_inactive=true`로 비활성까지 받아서 우리 쪽 `is_active`에 그대로
    # 반영한다 — 안 그러면 AI가 내린 챌린지가 우리 피드엔 계속 남는다.
    active: bool = True


def list_trend_challenges() -> list[TrendChallenge]:
    """유행 챌린지 목록을 가져온다.

    5.1 피드에 실제로 재생 가능한 영상을 채우는 유일한 경로다. R06 추천이 만드는
    행(`internal://editing-template/...`)은 AI 서버 내부 자산이라 앱에서 썸네일도
    재생도 되지 않는다 — 그건 편집 템플릿이고, 이건 사장님이 보고 따라 만들 원본이다.

    **AI 서버가 없으면 빈 목록이다.** 여기서 가짜 URL을 만들면 재생되지 않는 영상이
    피드에 그대로 노출된다(`start_editing_run`이 placeholder를 COMPLETED로 만들지
    않는 것과 같은 이유 — 영상은 지어낼 수 없는 종류의 값이다).
    """
    if not is_enabled():
        return []

    data = _request_json("GET", "/api/v1/challenges?include_inactive=true")
    challenges: list[TrendChallenge] = []
    for item in data.get("results") or []:
        challenge_id = item.get("id")
        name = item.get("name")
        if not challenge_id or not name:
            continue
        rank = item.get("rank")
        challenges.append(
            TrendChallenge(
                id=str(challenge_id),
                name=str(name),
                rank=int(rank) if rank is not None else None,
                representative_youtube_url=item.get("representative_youtube_url"),
                guide_youtube_url=item.get("guide_youtube_url"),
                format_type=item.get("format_type"),
                expected_duration_sec=(
                    int(item["expected_duration_sec"])
                    if item.get("expected_duration_sec") is not None
                    else None
                ),
                shooting_difficulty=item.get("shooting_difficulty"),
                requires_face=item.get("requires_face"),
                editing_template_id=item.get("editing_template_id"),
                active=bool(item.get("active", True)),
                editing_template_version=(
                    int(item["editing_template_version"])
                    if item.get("editing_template_version") is not None
                    else None
                ),
            )
        )
    return challenges


def is_enabled() -> bool:
    """AI 서버가 설정돼 있는지."""
    return bool(settings.AI_SERVER_URL)


def _with_default_guide_type(guide: dict[str, Any] | None) -> dict[str, Any] | None:
    """AI가 `guide_type`을 안 주면 `OVERLAY`로 채운다.

    지금 AI가 만드는 태스크가 전부 영상촬영형(오버레이 안내)이라 `guide_type`을
    직접 못 준다고 확인했다(2026-08-26, `docs/PM_DECISIONS.md`). 값이 오면 그대로
    쓰고, 없을 때만 기본값을 채운다 — 나중에 AI가 DANCE/BROLL을 구분해서 주기
    시작해도 이 함수만 자연히 통과시키면 된다.
    """
    if guide is None:
        return None
    return {**guide, "guide_type": guide.get("guide_type") or "OVERLAY"}


def get_shooting_guide(
    video_format: VideoFormat,
    store: Store,
    project: ShortsProject,
    *,
    menu_name: str | None = None,
) -> ShootingGuide:
    """포맷(영상편집템플릿)에 고정된 촬영 가이드를 가져온다.

    2026-08-26 AI팀 확인: **세션 없이 `template_id`+`version`만으로 호출 가능**하되,
    "가게/프로젝트에 필요한 컨텍스트"도 함께 넘기는 구조로 설계하라고 했다 — 그래서
    `store`·`project`를 받는다. 다만 가이드 **내용 자체는 템플릿에 고정**돼 있어
    (`docs/AI_연동_입출력.md` 13번 "LLM이 매 요청마다 생성하지 않는다") 이 컨텍스트가
    응답을 바꾸지는 않을 것으로 보인다 — 로깅/권한 확인 등에 쓰일 가능성이 높지만
    확정은 아니다.

    AI 서버가 설정돼 있지 않으면 임시 뼈대를 돌려준다 — 연동 전에도 화면 흐름을
    끝까지 확인할 수 있어야 하고, CI에서도 테스트가 돌아야 한다.
    """
    if not is_enabled():
        return _placeholder_shooting_guide(video_format)

    if video_format.editing_template_id is None or video_format.editing_template_version is None:
        # 5.1과 R06은 같은 템플릿 카탈로그를 쓰기로 확인됐다(2026-08-26). 그런데도
        # 이 값이 없다면 5.1이 옛(레거시) 방식으로 적재된 행이라는 뜻이다.
        raise NotImplementedError(
            "이 포맷은 영상편집템플릿과 연결되어 있지 않습니다(editing_template_id 없음)."
        )

    data = _request_json(
        "GET",
        (
            f"/api/v1/editing-templates/{video_format.editing_template_id}"
            f"/versions/{video_format.editing_template_version}/shooting-guide"
        ),
        query_params={
            "store_name": store.name,
            "business_type": store.sub_category or store.category,
            "promotion_subject": _promotion_subject_name(project, store, menu_name),
            "promotion_objective": str(project.promotion_purpose or ""),
            "menu_name": menu_name,
            "face_exposure": _map_face_exposure(project.face_exposure_mode),
        },
    )
    scenes = []
    for index, item in enumerate(data.get("scenes") or [], start=1):
        dialogue = item.get("scene_dialogue")
        if dialogue is not None and len(str(dialogue)) > 9:
            logger.error("AI 촬영 가이드 계약 위반: scene_dialogue 9자 초과")
            raise AIServiceUnavailable
        scenes.append(
            PlannedScene(
                scene_order=int(item.get("scene_order") or index),
                scene_description=str(item.get("scene_description") or "촬영 장면"),
                scene_dialogue=str(dialogue) if dialogue is not None else None,
                scene_subtitle=item.get("scene_subtitle"),
                shot_type=item.get("shot_type"),
                target_duration_sec=item.get("target_duration_sec"),
            )
        )
    tasks: list[PlannedTask] = []
    source_tasks = data.get("tasks") or []
    for index, item in enumerate(source_tasks, start=1):
        scene_index = item.get("scene_index")
        if scene_index is None and item.get("shooting_scene_order") is not None:
            # 1-인덱스 계약이다. 0 이하가 오면(AI 쪽 오류) -1 같은 음수가 나와
            # 파이썬 음수 인덱싱으로 엉뚱한(마지막) 장면에 조용히 연결되므로
            # 차라리 "모른다"로 둔다(2026-08-28, 코드리뷰로 발견).
            raw_order = int(item["shooting_scene_order"])
            scene_index = raw_order - 1 if raw_order >= 1 else None
        tasks.append(
            PlannedTask(
                display_order=int(item.get("display_order") or index),
                task_title=str(item.get("task_title") or item.get("title") or "촬영 태스크"),
                task_type=item.get("task_type") or "영상촬영",
                scene_index=int(scene_index) if scene_index is not None else None,
                guide=_with_default_guide_type(item.get("guide")),
            )
        )
    if not tasks:
        tasks = [
            PlannedTask(
                display_order=scene.scene_order,
                task_title=f"{scene.scene_description} 촬영",
                task_type="영상촬영",
                scene_index=index,
                guide={"guide_type": "OVERLAY", "instructions": []},
            )
            for index, scene in enumerate(scenes)
        ]

    return ShootingGuide(
        estimated_shooting_sec=data.get("estimated_shooting_sec"),
        required_people=data.get("required_people"),
        props=list(data.get("props") or []),
        difficulty=data.get("difficulty"),
        scenes=scenes,
        tasks=tasks,
    )


def get_template_shooting_sec(
    editing_template_id: str, editing_template_version: int
) -> int | None:
    """영상편집템플릿의 예상 촬영 소요시간(초)을 가게 무관하게 조회한다.

    `get_shooting_guide`가 받는 `store`/`project` 컨텍스트는 응답을 바꾸지 않는다
    (2026-08-30 실측 확인 — 가게명·메뉴명을 완전히 다르게 넣어도 `estimated_
    shooting_sec`이 동일했다). 그래서 실제 가게·프로젝트 없이 플레이스홀더 값만
    넣어 호출해도 안전하고, 트렌드 동기화 시점(`app/services/trend_format.py`)에
    카탈로그에 캐싱해둘 수 있다 — 5.1 목록 화면을 위해 프로젝트를 미리 만들
    필요가 없다.

    카탈로그의 부가 정보일 뿐이라 실패해도 동기화 전체를 막지 않는다 — 조용히
    `None`을 돌려준다.
    """
    if not is_enabled():
        return None
    try:
        data = _request_json(
            "GET",
            f"/api/v1/editing-templates/{editing_template_id}"
            f"/versions/{editing_template_version}/shooting-guide",
            query_params={
                "store_name": "포맷카탈로그",
                "business_type": "기타",
                "promotion_subject": "미리보기",
                "promotion_objective": "",
                "face_exposure": "not_allowed",
            },
        )
    except AppError:
        # 부가 정보일 뿐이다 — 401(키 설정 오류)이든 404(존재하지 않는 템플릿)든
        # 동기화 전체를 막을 이유가 없다.
        logger.info(
            "촬영 소요시간 캐싱 실패(무시): template_id=%s version=%s",
            editing_template_id,
            editing_template_version,
        )
        return None
    value = data.get("estimated_shooting_sec")
    return int(value) if value is not None else None


def _placeholder_shooting_guide(video_format: VideoFormat) -> ShootingGuide:
    """AI 연동 전 임시 촬영 가이드.

    포맷의 촬영 컷 구성을 흉내 낸 뼈대만 만든다. **가게별 맞춤이 아니다** — 실제
    응답도 템플릿 고정값이라 가게별로 다르지 않으므로, 이 부분은 실제 동작과
    형태가 같다(내용만 가짜).
    """
    duration = video_format.expected_duration_sec or 30
    per_scene = max(duration // 4, 3)

    scenes = [
        PlannedScene(
            scene_order=1,
            scene_description="간판 클로즈업",
            scene_subtitle="이 가게만의 특별한 순간",
            shot_type="클로즈업",
            target_duration_sec=per_scene,
        ),
        PlannedScene(
            scene_order=2,
            scene_description="대상 준비 장면",
            shot_type="미디엄샷",
            target_duration_sec=per_scene,
        ),
        PlannedScene(
            scene_order=3,
            scene_description="핵심 장면",
            shot_type="클로즈업",
            target_duration_sec=per_scene,
        ),
        PlannedScene(
            scene_order=4,
            scene_description="마무리 리액션",
            scene_subtitle="지금 보러 오세요!",
            shot_type="풀샷",
            target_duration_sec=duration - per_scene * 3,
        ),
    ]
    # 임시 태스크는 장면 하나당 하나로 만든다. 실제로는 AI가 "영상촬영 / 대사 /
    # B-roll / 텍스트 확인" 같은 유형으로 쪼갠다(기능명세서 S08.1.1).
    tasks = [
        PlannedTask(
            display_order=scene.scene_order,
            task_title=f"{scene.scene_description} 촬영",
            task_type="영상촬영",
            scene_index=index,
            guide={
                "guide_type": "OVERLAY",
                # ⚠️ 비워둔다. AI 없이 지어내면 가짜 안내가 진짜처럼 보인다.
                "instructions": [],
                "broll_shot": {"distance": None, "angle": None},
            },
        )
        for index, scene in enumerate(scenes)
    ]

    return ShootingGuide(
        # 촬영은 완성 길이보다 오래 걸린다는 가정. 실제 값은 AI가 판단한다.
        estimated_shooting_sec=duration * 10,
        required_people=1,
        props=["삼각대"],
        difficulty=video_format.shooting_difficulty,
        scenes=scenes,
        tasks=tasks,
        is_placeholder=True,
    )


@dataclass(frozen=True)
class PublishKit:
    """게시자료 (API명세서 15.1).

    사장님이 SNS에 올릴 때 그대로 붙여넣을 캡션·해시태그와, 음원 선택 같은
    플랫폼 안내 문구를 담는다. `EditingRunResult.publishing`도 같은 모양을
    쓰므로 여기(먼저 나오는 자리)에 정의한다 — placeholder 전용이 된 경위는
    `generate_publish_kit()` 독스트링 참고.
    """

    title: str
    caption: str
    hashtags: list[str]
    post_note: str | None = None
    # 음원 가이드. 저작권 때문에 배경음악을 직접 입히지 않고, 사장님이 플랫폼에서
    # 붙이도록 "무슨 곡을 어디부터" 알려준다(2026-08-24 결정).
    # 값의 출처는 미정이다 — 포맷에 고정해둘지 AI가 영상을 보고 채울지 확인 중이라
    # 지금은 항상 None이며, 정해지면 이 자리에 담는다.
    track: dict[str, Any] | None = None
    is_placeholder: bool = False


@dataclass(frozen=True)
class FootageInput:
    """편집 Agent에 보낼 촬영본 하나 (`docs/AI_연동_입출력.md` 16번 `videos[]`)."""

    video_id: str
    footage_url: str
    shooting_scene_order: int | None = None


@dataclass(frozen=True)
class EditingRun:
    """편집 실행(run) 식별자와 상태 (16·17·20번).

    `status`는 AI가 쓰는 문자열(`QUEUED`/`RUNNING`/`COMPLETED`/`FAILED`/
    `SOURCE_GAP`)을 그대로 담는다 — `app/services/video_edit.py`가 우리
    `RenderStatus`로 옮긴다.

    `stage`/`progress`/`error_message`는 2026-08-27 추가. AI 응답에 원래
    실려 있었는데(`docs/AI_연동_입출력.md` 17번) 지금까지 파싱하지 않고 버리고
    있었다 — 실서버 편집 실패를 조사하다가 발견(FE 리포트, project 56/50).
    """

    run_id: str
    status: str
    stage: str | None = None
    progress: int | None = None
    error_message: str | None = None
    queue_position: int | None = None
    estimated_wait_sec: int | None = None
    stage_elapsed_sec: int | None = None


@dataclass(frozen=True)
class EditingRunResult:
    """완료된 편집 결과 (18번 `GET /editing-runs/{run_id}/result`).

    `recipe`는 자유 형식 JSON이라 그대로 보관한다(기능명세서 S14.x가 요구하는
    컷 순서·전환·자막·오디오 큐가 여기 들어있다). `SOURCE_GAP`이면 `recipe`·
    `video_url` 등은 비고 `missing_scene_roles`·`available_options`만 채워진다.

    `publishing`은 22번("게시자료는 편집 결과에 포함된다")에 대응한다 — 15.1
    전용 AI 호출이 따로 없다. `PublishKit`과 같은 필드를 쓰지만 이름을 분리했다
    (`PublishKit`은 placeholder 전용으로 남았다).
    """

    recipe: dict[str, Any] | None = None
    video_url: str | None = None
    resolution: str | None = None
    cover_image_url: str | None = None
    publishing: PublishKit | None = None
    missing_scene_roles: list[str] | None = None
    available_options: list[str] | None = None
    warnings: list[str] | None = None
    is_placeholder: bool = False


def _editing_run_from_json(data: dict[str, Any]) -> EditingRun:
    """AI 응답에서 run 정보를 뽑는다. `start`/`polling`/`revision` 세 곳이 공유한다."""
    return EditingRun(
        run_id=str(data.get("run_id") or data["id"]),
        status=str(data["status"]),
        stage=data.get("stage"),
        progress=data.get("progress"),
        error_message=data.get("error_message"),
        queue_position=(
            int(data["queue_position"]) if data.get("queue_position") is not None else None
        ),
        estimated_wait_sec=(
            int(data["estimated_wait_sec"]) if data.get("estimated_wait_sec") is not None else None
        ),
        stage_elapsed_sec=(
            int(data["stage_elapsed_sec"]) if data.get("stage_elapsed_sec") is not None else None
        ),
    )


def start_editing_run(
    store: Store,
    project: ShortsProject,
    video_format: VideoFormat,
    footages: list[FootageInput],
) -> EditingRun:
    """편집을 시작한다 (`docs/AI_연동_입출력.md` 16번, `POST /editing-runs`).

    **비동기다.** 실제 연동 후에는 이 호출이 `run_id`만 즉시 돌려주고, 진행 상태는
    `get_editing_run()`으로 폴링한다(17번). placeholder는 **영원히 `QUEUED`에
    머문다** — 렌더러가 없어 실제 영상이 생기지 않는데 `COMPLETED`로 표시하면
    재생되지 않는 가짜 영상 링크를 사장님이 보게 된다. 다른 placeholder(캡션·
    콘티 등, 텍스트/구조만 있는 값)와 달리 **영상 파일은 지어낼 수 없는 종류의
    값**이라 여기서는 원칙이 다르다.
    """
    if not is_enabled():
        return _placeholder_editing_run()

    if video_format.editing_template_id is None or video_format.editing_template_version is None:
        raise AITemplateNotLinked

    purpose_map = {
        "메뉴소개": "sales",
        "이벤트알리기": "awareness",
        "가게소개": "awareness",
        "고객늘리기": "new_customer",
    }
    subject = dict(project.promotion_detail or {})
    if project.menu_id is not None:
        subject = {"type": "MENU", "menu_id": project.menu_id, **subject}
    elif not subject:
        subject = {"type": "STORE", "name": store.name}

    data = _request_json(
        "POST",
        "/api/v1/editing-runs",
        json_body={
            "project": {
                "project_id": str(project.id),
                "store_id": str(store.id),
                "promotion_subject": subject,
                "promotion_objective": purpose_map.get(str(project.promotion_purpose), "awareness"),
                "face_exposure": _map_face_exposure(project.face_exposure_mode),
            },
            "selected_shortform": {
                "recommendation_id": project.recommendation_id or f"project_{project.id}",
                "editing_template_id": video_format.editing_template_id,
                "editing_template_version": video_format.editing_template_version,
            },
            "videos": [
                {
                    "video_id": footage.video_id,
                    "footage_url": footage.footage_url,
                    "shooting_scene_order": footage.shooting_scene_order,
                }
                for footage in footages
            ],
            "revision": None,
        },
    )
    return _editing_run_from_json(data)


def _placeholder_editing_run() -> EditingRun:
    return EditingRun(run_id=f"edit_placeholder_{uuid.uuid4().hex}", status="QUEUED")


def get_editing_run(run_id: str) -> EditingRun:
    """편집 진행 상태를 폴링한다 (17번, `GET /editing-runs/{run_id}`).

    placeholder는 **처음 만들어졌을 때 상태에 계속 머문다** — 진행이 없어서다.
    수정 요청(`request_revision`)이 만든 run인지는 `run_id` 접두어로 구분한다 —
    별도 상태 저장소가 없는 placeholder 안에서 "이 run이 어떤 종류였는지"를
    유지하는 유일한 방법이다.
    """
    if not is_enabled():
        status = "RUNNING" if run_id.startswith("edit_revision_placeholder_") else "QUEUED"
        return EditingRun(run_id=run_id, status=status)

    data = _request_json("GET", f"/api/v1/editing-runs/{run_id}")
    return _editing_run_from_json(data)


def get_editing_run_result(run_id: str) -> EditingRunResult:
    """완료된 편집 결과를 가져온다 (18번).

    placeholder는 절대 `COMPLETED`가 되지 않으므로(`get_editing_run` 참고) 이
    함수가 호출될 일이 없다 — 호출되면 프로그래밍 오류다.
    """
    if not is_enabled():
        raise NotImplementedError("AI 연동 전에는 편집이 완료되지 않아 결과를 조회할 수 없습니다.")

    data = _request_json("GET", f"/api/v1/editing-runs/{run_id}/result")
    render = data.get("render") or {}
    publishing_data = data.get("publishing")
    publishing = None
    if publishing_data is not None:
        publishing = PublishKit(
            title=str(publishing_data["title"]),
            caption=str(publishing_data["caption"]),
            hashtags=list(publishing_data.get("hashtags") or []),
            post_note=publishing_data.get("post_note"),
            track=publishing_data.get("track"),
        )
    return EditingRunResult(
        recipe=data.get("recipe"),
        video_url=render.get("output_video_url"),
        resolution=render.get("resolution"),
        cover_image_url=render.get("cover_image_url"),
        publishing=publishing,
        missing_scene_roles=list(data.get("missing_scene_roles") or []),
        available_options=list(data.get("available_options") or []),
        warnings=list(data.get("warnings") or []),
    )


def request_revision(
    run_id: str,
    revision_action: str,
    footages: list[FootageInput] | None = None,
) -> EditingRun:
    """수정을 요청한다 (20번, `POST /editing-runs/{run_id}/revisions`).

    **새 run을 만든다** — 기존 EditRecipe는 immutable하게 유지된다. placeholder는
    바로 `RUNNING`으로 표시한다(기존 동작 유지 — 수정 요청은 "처리 중"으로 보여야
    사장님이 재요청 중임을 알 수 있다). 완료되진 않는다(위와 같은 이유).
    """
    if not is_enabled():
        del run_id, revision_action, footages
        return EditingRun(run_id=f"edit_revision_placeholder_{uuid.uuid4().hex}", status="RUNNING")

    data = _request_json(
        "POST",
        f"/api/v1/editing-runs/{run_id}/revisions",
        json_body={
            "revision_action": revision_action,
            "videos": [
                {
                    "video_id": footage.video_id,
                    "footage_url": footage.footage_url,
                    "shooting_scene_order": footage.shooting_scene_order,
                }
                for footage in footages or []
            ],
        },
    )
    return _editing_run_from_json(data)


def generate_publish_kit(store: Store, project: ShortsProject) -> PublishKit:
    """게시자료를 만든다. **placeholder 전용**이다.

    ⚠️ **2026-08-26부터 실제 연동 시에는 이 함수를 쓰지 않는다.** AI 문서 22번이
    "게시자료는 별도 LLM 호출이 없고, 편집 결과(`get_editing_run_result`)의
    `publishing`에 포함된다"고 확정해서다. `is_enabled()`가 `true`면
    `app/services/video_output.py`가 이 함수를 아예 부르지 않고
    `project.publish_kit`(편집 완료 시 채워짐)을 그대로 돌려줘야 한다 — 그래도
    부르면 아래에서 프로그래밍 오류로 막는다.

    **placeholder 모드에서는 계속 이 함수를 쓴다** — 렌더링이 영원히 끝나지
    않는 placeholder 편집(`start_editing_run` 참고)과 달리, 캡션·해시태그는
    문구일 뿐이라 안전하게 지어낼 수 있다. 렌더링 완료를 기다리지 않고도
    프론트가 15.1 화면 흐름을 확인할 수 있게 하려는 의도적인 예외다.
    """
    if not is_enabled():
        return _placeholder_publish_kit(store)

    raise NotImplementedError(
        "AI 연동 후에는 게시자료를 여기서 만들지 않습니다 — "
        "get_editing_run_result().publishing을 쓰세요."
    )


def _placeholder_publish_kit(store: Store) -> PublishKit:
    """AI 연동 전 임시 게시자료.

    **문구를 지어내지 않는다.** 가게 이름·업종처럼 DB에 실제로 있는 값만 쓴다 —
    사장님이 그대로 게시할 수 있는 화면에 나가는 값이라, 사실이 아닌 문장을
    넣으면 잘못된 정보가 그대로 올라갈 수 있다.
    """
    hashtags = [f"#{store.name.replace(' ', '')}"]
    if store.category:
        hashtags.append(f"#{store.category.replace(' ', '')}")
    for fallback in ("#매장소개", "#가게소개", "#동네맛집", "#숏폼", "#릴스"):
        if len(hashtags) >= 5:
            break
        if fallback not in hashtags:
            hashtags.append(fallback)

    return PublishKit(
        title=f"{store.name}을 소개합니다",
        caption=f"{store.name}",
        hashtags=hashtags,
        post_note="AI 연동 전 임시 게시자료입니다. 캡션을 직접 수정해주세요.",
        is_placeholder=True,
    )


# ------------------------------------------------------------- 숏폼 Agent (R06)
#
# `docs/AI_연동_입출력.md` 5~12번 기준(2026-08-26). 기존 "포맷 선택 → 질문형 →
# AI 기획" 구조는 폐기되고, 대화형 세션이 ACTIVE 영상편집템플릿 중 1개를
# 추천하는 구조로 바뀌었다. 추천을 받아들이면(11번) 그 결과로 프로젝트를 만든다
# (`app/services/shortform_session.py`).


@dataclass(frozen=True)
class SessionOption:
    """대화 turn에서 사용자에게 보여줄 선택지."""

    id: str
    label: str


@dataclass(frozen=True)
class Recommendation:
    """숏폼 Agent의 3개 묶음에 포함된 영상편집템플릿 1개.

    `editing_template_id`·`editing_template_version`은 우리 `video_formats`가 아니라
    **AI 서버 쪽 템플릿 카탈로그**를 가리킨다. 세션이 끝나 프로젝트로 확정될 때
    (`accept`) `video_formats`에 없으면 새로 적재한다 — 5.1이 `reference_url` 기준으로
    하는 것과 같은 방식이다.
    """

    recommendation_id: str
    project_title: str
    title: str
    concept: str
    editing_template_id: str
    editing_template_version: int
    reference_url: str | None = None
    guide_video_url: str | None = None
    source_platform: str | None = None


@dataclass(frozen=True)
class SessionGreeting:
    """세션을 막 만들었을 때의 첫 응답."""

    session_token: str
    assistant_message: str
    options: list[SessionOption]
    project_state: dict[str, Any]
    is_placeholder: bool = False


@dataclass(frozen=True)
class TurnResult:
    """대화 turn 하나의 응답 (API명세서 AI_연동_입출력.md 8번)."""

    action: str
    assistant_message: str | None
    project_state: dict[str, Any]
    options: list[SessionOption]
    recommendations: list[Recommendation] = field(default_factory=list)
    is_placeholder: bool = False


def start_shortform_session(
    store: Store, menus: list[StoreMenu], trade_area_insight: str | None
) -> SessionGreeting:
    """숏폼 Agent 세션을 시작한다.

    `menus`(대표메뉴 전체)·`trade_area_insight`(상권분석 인사이트 원문)는 AI 서버의
    `store_context`(`docs/AI_연동_입출력.md` 6번: `store`+`representative_menus`+
    `trade_area`)를 채우는 데 필요하다. placeholder는 참고하지 않지만, 연동 시점에
    호출부(`app/services/shortform_session.py`) 시그니처를 다시 바꾸지 않아도 되도록
    지금부터 받아둔다.

    ⚠️ `trade_area_insight`는 `store_insights.insight_content`(자유 텍스트)를 그대로
    넘긴 것이다. AI가 원하는 `{characteristics: [...], target_age_ranges: [...]}` 구조로
    바꾸는 방법은 아직 없다 — 연동 시점에 정해야 한다.

    AI 서버가 설정돼 있지 않으면 임시 인사말을 돌려준다.
    """
    if not is_enabled():
        if settings.AI_SHORTFORM_PLACEHOLDERS_ENABLED:
            return _placeholder_greeting(store)
        raise AIServiceConfigurationError

    data = _request_json(
        "POST",
        "/api/v1/shortform-sessions",
        json_body={
            "store_context": {
                "store": {
                    "store_id": str(store.id),
                    "store_name": store.name,
                    "category": store.category,
                    "location": {"address": store.address},
                    "atmosphere": [store.brand_tone] if store.brand_tone else [],
                    "representative_color": store.brand_color,
                    "store_photos": [],
                },
                "representative_menus": [
                    {
                        "menu_id": str(menu.id),
                        "name": menu.name,
                        "price": menu.price,
                        "currency": "KRW",
                    }
                    for menu in menus
                ],
                "trade_area": ({"summary": trade_area_insight} if trade_area_insight else None),
            }
        },
    )
    return SessionGreeting(
        session_token=str(data["session_id"]),
        assistant_message=str(data["assistant_message"]),
        options=[_option(item) for item in data.get("options") or []],
        project_state=dict(data.get("project_state") or {}),
    )


def _placeholder_greeting(store: Store) -> SessionGreeting:
    return SessionGreeting(
        session_token=f"sf_placeholder_{uuid.uuid4().hex}",
        assistant_message=f"{store.name}, 오늘 어떤 영상을 찍을까요? (AI 연동 전 임시 응답입니다)",
        options=[
            SessionOption(id="PROMOTION_GUIDE", label="홍보하고 싶은 게 있어요"),
            SessionOption(id="FREE_INPUT", label="직접 입력하기"),
        ],
        project_state={
            "promotion_subject": None,
            "promotion_objective": None,
            "filming_time": None,
            "face_exposure": None,
            "ready_for_confirmation": False,
        },
        is_placeholder=True,
    )


def submit_shortform_turn(
    store: Store,
    session_token: str,
    project_state: dict[str, Any],
    turn_input: dict[str, Any],
    representative_menu: StoreMenu | None,
) -> TurnResult:
    """대화 turn을 처리한다.

    `session_token`(AI 쪽 세션 식별자)과 `turn_input`(사용자가 실제로 입력한
    내용 — `{"type": "TEXT"/"OPTION"/"CONFIRM", ...}`)은 실제 연동 시
    `POST /api/v1/shortform-sessions/{session_id}/turns`에 그대로 실어 보낼 값이다.
    placeholder는 지금 이 값들을 해석하지 않는다 — 지어내면 실제로 나눈 적 없는
    대화가 있었던 것처럼 보인다. 대신 turn이 오면 **곧바로 추천 단계로 진행**해,
    AI 연동 전에도 "대화 → 추천 → 수락" 전체 화면 흐름을 끝까지 확인할 수 있게 한다.

    AI 서버가 설정돼 있지 않으면 임시 결과를 돌려준다.
    """
    if not is_enabled():
        if settings.AI_SHORTFORM_PLACEHOLDERS_ENABLED:
            return _placeholder_turn(store, project_state, representative_menu)
        raise AIServiceConfigurationError

    del store, representative_menu
    data = _request_json(
        "POST",
        f"/api/v1/shortform-sessions/{session_token}/turns",
        json_body={"input": turn_input},
    )
    return TurnResult(
        action=str(data["action"]),
        assistant_message=data.get("assistant_message"),
        project_state=dict(data.get("project_state") or project_state),
        options=[_option(item) for item in data.get("options") or []],
        recommendations=_recommendations(data.get("recommendations")),
    )


def _placeholder_turn(
    store: Store, project_state: dict[str, Any], representative_menu: StoreMenu | None
) -> TurnResult:
    recommendations = [_placeholder_recommendation(store, representative_menu) for _ in range(3)]
    new_state = dict(project_state)
    new_state["ready_for_confirmation"] = True
    if representative_menu is not None:
        new_state["promotion_subject"] = {
            "type": "MENU",
            "name": representative_menu.name,
            "menu_id": representative_menu.id,
        }
    return TurnResult(
        action="RECOMMEND",
        assistant_message=None,
        project_state=new_state,
        options=[],
        recommendations=recommendations,
        is_placeholder=True,
    )


def get_next_shortform_recommendations(
    store: Store,
    session_token: str,
    representative_menu: StoreMenu | None,
    shown_template_ids: list[str],
) -> list[Recommendation]:
    """서로 다른 추천 3개를 한 번에 다시 받는다. 이미 본 템플릿은 제외한다.

    AI 서버가 설정돼 있지 않으면 매번 새 임시 템플릿을 만들어 돌려준다 — 실제로는
    같은 후보를 반복해 추천하지 않는다는 것만 흉내 낸다.
    """
    del shown_template_ids  # placeholder는 항상 새 템플릿을 만들어 자동으로 안 겹친다
    if not is_enabled():
        if settings.AI_SHORTFORM_PLACEHOLDERS_ENABLED:
            return [_placeholder_recommendation(store, representative_menu) for _ in range(3)]
        raise AIServiceConfigurationError

    del store, representative_menu
    data = _request_json(
        "POST",
        f"/api/v1/shortform-sessions/{session_token}/recommendations/next",
    )
    recommendations = _recommendations(data.get("recommendations"))
    if not recommendations:
        raise AIServiceUnavailable
    return recommendations


def _placeholder_recommendation(
    store: Store, representative_menu: StoreMenu | None
) -> Recommendation:
    """AI 연동 전 임시 추천.

    **실제 템플릿 매칭이 아니다.** `editing_template_id`를 매번 새로 만들어, 이
    추천을 수락(accept)하면 `video_formats`에 새 행으로 적재된다(5.1이 `reference_url`
    기준으로 적재하는 것과 같은 자리에서, 이건 `editing_template_id` 기준).
    """
    template_id = f"placeholder-template-{uuid.uuid4().hex[:12]}"
    placeholder_video_url = f"https://www.youtube.com/watch?v={template_id[-11:]}"
    subject = representative_menu.name if representative_menu else store.name
    return Recommendation(
        recommendation_id=f"placeholder-rec-{uuid.uuid4().hex[:12]}",
        project_title=f"{subject} 소개 숏폼",
        title=f"{subject}을(를) 보여주는 숏폼 (AI 연동 전 임시 추천)",
        concept="AI 연동 전이라 실제 컨셉이 아닙니다. 연동 후 매장·메뉴에 맞춰 추천됩니다.",
        editing_template_id=template_id,
        editing_template_version=1,
        reference_url=placeholder_video_url,
        guide_video_url=placeholder_video_url,
        source_platform="YOUTUBE",
    )


# ------------------------------------------------------- 3.5 상권분석


@dataclass(frozen=True)
class TradeAreaInsight:
    """상권분석 결과 (`docs/AI_연동_요청_2026-08-27_상권분석.md`, AI팀과 합의 완료).

    나이·성별 분포는 **실제 인구통계 주장**이다 — 촬영가이드 같은 "임시 뼈대"와
    달리 placeholder에서 지어내면 안 된다(가격·영업시간과 같은 취급). 그래서
    모든 필드가 기본값 `None`이고, placeholder 모드에서는 전부 `None`으로 둔다.
    """

    district_name: str | None = None
    summary: str | None = None
    age_distribution: dict[str, int] | None = None
    gender_distribution: dict[str, int] | None = None


def get_trade_area_insight(store: Store) -> TradeAreaInsight:
    """가게 상권분석을 가져온다 (2026-08-27, 가게 등록 2.2 직후 백그라운드 호출).

    AI 서버가 없으면 전부 `None`이다 — 나이·성별 분포는 지어낼 수 없는 종류의
    값이라, 다른 placeholder(캡션 등 문구성 텍스트)와 원칙이 다르다.
    """
    if not is_enabled():
        return TradeAreaInsight()

    data = _request_json(
        "POST",
        # TODO(AI팀 확정 후 교체): 정확한 경로는 아직 AI팀이 안 정해줬다.
        # 요청/응답 형식만 `docs/AI_연동_요청_2026-08-27_상권분석.md`로 합의됨.
        "/api/v1/stores/trade-area-insight",
        json_body={
            "store": {
                "name": store.name,
                "category": store.category,
                "sub_category": store.sub_category,
                "address": store.address,
                "latitude": float(store.latitude) if store.latitude is not None else None,
                "longitude": float(store.longitude) if store.longitude is not None else None,
            }
        },
    )
    return TradeAreaInsight(
        district_name=data.get("district_name"),
        summary=data.get("summary"),
        age_distribution=data.get("age_distribution"),
        gender_distribution=data.get("gender_distribution"),
    )
