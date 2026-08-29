"""메인 백엔드와 AI 서버 사이의 내부 HTTP 계약 테스트."""

from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.models.shorts_project import PromotionPurpose, ShortsProject
from app.models.store import Store
from app.models.store_menu import StoreMenu
from app.models.video_format import VideoFormat
from app.services import ai_client


def test_internal_request_sends_shared_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal/")
    monkeypatch.setattr(settings, "AI_SERVER_API_KEY", "shared-secret")
    monkeypatch.setattr(httpx, "request", fake_request)

    assert ai_client._request_json("GET", "/api/v1/agents") == {"ok": True}
    assert captured["url"] == "http://ai.internal/api/v1/agents"
    assert captured["headers"] == {"X-Internal-API-Key": "shared-secret"}


def test_internal_auth_failure_becomes_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        return httpx.Response(401, json={"detail": "no"}, request=httpx.Request(method, url))

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(ai_client.AIServiceConfigurationError):
        ai_client._request_json("GET", "/api/v1/agents")


def test_shortform_session_maps_store_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(method=method, path=path, **kwargs)
        return {
            "session_id": "sf_123",
            "assistant_message": "오늘 어떤 영상을 찍을까요?",
            "options": [{"id": "FREE_INPUT", "label": "직접 입력하기"}],
            "project_state": {"ready_for_confirmation": False},
        }

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(ai_client, "_request_json", fake_request)
    store = Store(id=10, user_id=1, name="행복분식", category="분식", address="서울")
    menu = StoreMenu(id=20, store_id=10, name="떡볶이", price=4000)

    result = ai_client.start_shortform_session(store, [menu], "직장인 상권")

    assert result.session_token == "sf_123"
    body = captured["json_body"]["store_context"]
    assert body["store"]["store_id"] == "10"
    assert body["representative_menus"][0]["menu_id"] == "20"
    assert body["trade_area"] == {"summary": "직장인 상권"}


def test_shooting_guide_maps_scene_and_task(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "estimated_shooting_sec": 480,
            "difficulty": "하",
            "scenes": [{"scene_order": 1, "scene_description": "완성 메뉴"}],
            "tasks": [
                {
                    "display_order": 1,
                    "task_title": "완성 메뉴 촬영",
                    "shooting_scene_order": 1,
                }
            ],
        }

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(ai_client, "_request_json", fake_request)
    video_format = VideoFormat(
        editing_template_id="edit_template_014",
        editing_template_version=1,
        format_title="메뉴 소개",
        reference_url="internal://template",
    )

    guide = ai_client.get_shooting_guide(
        video_format,
        Store(id=10, user_id=1, name="행복분식", category="분식"),
        ShortsProject(id=30, store_id=10, promotion_purpose=PromotionPurpose.MENU),
        menu_name="떡볶이",
    )

    assert guide.scenes[0].scene_description == "완성 메뉴"
    assert guide.tasks[0].scene_index == 0
    assert captured["query_params"]["store_name"] == "행복분식"
    assert captured["query_params"]["business_type"] == "분식"
    assert captured["query_params"]["menu_name"] == "떡볶이"
    assert captured["query_params"]["promotion_subject"] == "떡볶이"


def test_shooting_guide_treats_zero_scene_order_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shooting_scene_order`는 1-인덱스 계약이다. 0(또는 그 이하)이 오면 `-1` 같은

    음수로 변환돼 파이썬 음수 인덱싱 때문에 마지막 장면에 조용히 잘못 연결될 수
    있어(2026-08-28, 코드리뷰로 발견), 대신 "모른다"(`None`)로 떨어뜨린다.
    """
    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(
        ai_client,
        "_request_json",
        lambda *args, **kwargs: {
            "estimated_shooting_sec": 480,
            "difficulty": "하",
            "scenes": [{"scene_order": 1, "scene_description": "완성 메뉴"}],
            "tasks": [
                {"display_order": 1, "task_title": "완성 메뉴 촬영", "shooting_scene_order": 0}
            ],
        },
    )
    video_format = VideoFormat(
        editing_template_id="edit_template_014",
        editing_template_version=1,
        format_title="메뉴 소개",
        reference_url="internal://template",
    )

    guide = ai_client.get_shooting_guide(
        video_format,
        Store(id=10, user_id=1, name="행복분식", category="분식"),
        ShortsProject(id=30, store_id=10, promotion_purpose=PromotionPurpose.MENU),
        menu_name="떡볶이",
    )

    assert guide.tasks[0].scene_index is None


# ---------------------------------------------------------------- get_template_shooting_sec


def test_get_template_shooting_sec_returns_ai_value(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(
            200,
            json={"estimated_shooting_sec": 60},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(settings, "AI_SERVER_API_KEY", "shared-secret")
    monkeypatch.setattr(httpx, "request", fake_request)

    result = ai_client.get_template_shooting_sec("gt_cafe_recommendation", 4)

    assert result == 60
    assert "gt_cafe_recommendation/versions/4/shooting-guide" in captured["url"]


def test_get_template_shooting_sec_returns_none_when_ai_disabled() -> None:
    assert ai_client.get_template_shooting_sec("gt_x", 1) is None


def test_get_template_shooting_sec_returns_none_on_ai_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """카탈로그 부가 정보일 뿐이라 실패해도 예외를 밖으로 새지 않는다."""

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request(method, url))

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(settings, "AI_SERVER_API_KEY", "shared-secret")
    monkeypatch.setattr(httpx, "request", fake_request)

    assert ai_client.get_template_shooting_sec("gt_x", 1) is None


def test_editing_status_maps_queue_and_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(
        ai_client,
        "_request_json",
        lambda *args, **kwargs: {
            "id": "edit_123",
            "status": "QUEUED",
            "stage": "QUEUED",
            "progress": 12,
            "queue_position": 3,
            "estimated_wait_sec": 1800,
            "stage_elapsed_sec": 25,
        },
    )

    run = ai_client.get_editing_run("edit_123")

    assert run.progress == 12
    assert run.queue_position == 3
    assert run.estimated_wait_sec == 1800
    assert run.stage_elapsed_sec == 25


def test_shooting_guide_builds_tasks_when_template_only_has_scenes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(
        ai_client,
        "_request_json",
        lambda *args, **kwargs: {
            "scenes": [{"scene_order": 1, "scene_description": "메뉴 클로즈업"}],
            "tasks": [],
        },
    )
    video_format = VideoFormat(
        editing_template_id="edit_template_014",
        editing_template_version=1,
        format_title="메뉴 소개",
        reference_url="internal://template",
    )

    guide = ai_client.get_shooting_guide(
        video_format,
        Store(id=10, user_id=1, name="행복분식"),
        ShortsProject(id=30, store_id=10),
    )

    assert guide.tasks[0].task_title == "메뉴 클로즈업 촬영"
    assert guide.tasks[0].scene_index == 0


def test_shooting_guide_rejects_dialogue_over_nine_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(
        ai_client,
        "_request_json",
        lambda *args, **kwargs: {
            "scenes": [
                {
                    "scene_order": 1,
                    "scene_description": "메뉴",
                    "scene_dialogue": "열글자가넘는대사입니다",
                }
            ]
        },
    )

    with pytest.raises(ai_client.AIServiceUnavailable):
        ai_client.get_shooting_guide(
            VideoFormat(
                editing_template_id="edit_template_014",
                editing_template_version=1,
                format_title="메뉴 소개",
                reference_url="internal://template",
            ),
            Store(id=10, user_id=1, name="행복분식"),
            ShortsProject(id=30, store_id=10),
        )


def test_shooting_guide_defaults_task_type_and_guide_type_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI가 task_type/guide_type을 안 줘도 안전한 기본값으로 채운다(2026-08-26 합의)."""
    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(
        ai_client,
        "_request_json",
        lambda *args, **kwargs: {
            "scenes": [{"scene_order": 1, "scene_description": "완성 메뉴"}],
            "tasks": [
                {
                    "display_order": 1,
                    "task_title": "완성 메뉴 촬영",
                    "shooting_scene_order": 1,
                    "guide": {"instructions": ["메뉴가 화면 중앙에 보이도록 촬영하세요."]},
                }
            ],
        },
    )
    video_format = VideoFormat(
        editing_template_id="edit_template_014",
        editing_template_version=1,
        format_title="메뉴 소개",
        reference_url="internal://template",
    )

    guide = ai_client.get_shooting_guide(
        video_format,
        Store(id=10, user_id=1, name="행복분식"),
        ShortsProject(id=30, store_id=10),
    )

    task = guide.tasks[0]
    assert task.task_type == "영상촬영"
    assert task.guide is not None
    assert task.guide["guide_type"] == "OVERLAY"
    assert task.guide["instructions"] == ["메뉴가 화면 중앙에 보이도록 촬영하세요."]


def test_shooting_guide_keeps_task_type_and_guide_type_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI가 값을 직접 주면 기본값으로 덮어쓰지 않는다."""
    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(
        ai_client,
        "_request_json",
        lambda *args, **kwargs: {
            "scenes": [{"scene_order": 1, "scene_description": "안무"}],
            "tasks": [
                {
                    "display_order": 1,
                    "task_title": "안무 촬영",
                    "task_type": "안무",
                    "shooting_scene_order": 1,
                    "guide": {"guide_type": "DANCE", "instructions": []},
                }
            ],
        },
    )
    video_format = VideoFormat(
        editing_template_id="edit_template_dance",
        editing_template_version=1,
        format_title="댄스 챌린지",
        reference_url="internal://template",
    )

    guide = ai_client.get_shooting_guide(
        video_format,
        Store(id=10, user_id=1, name="행복분식"),
        ShortsProject(id=30, store_id=10),
    )

    task = guide.tasks[0]
    assert task.task_type == "안무"
    assert task.guide is not None
    assert task.guide["guide_type"] == "DANCE"


def test_informational_shooting_guide_uses_scene_linked_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정보형도 AI가 반환한 컷별 장면과 태스크를 그대로 사용한다."""
    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(
        ai_client,
        "_request_json",
        lambda *args, **kwargs: {
            "format_type": "정보형",
            "scenes": [
                {
                    "scene_order": 1,
                    "scene_description": "제조 과정",
                },
                {
                    "scene_order": 2,
                    "scene_description": "대표 메뉴",
                },
            ],
            "tasks": [
                {"display_order": 1, "task_title": "제조 과정", "scene_index": 0},
                {"display_order": 2, "task_title": "대표 메뉴", "scene_index": 1},
            ],
        },
    )
    video_format = VideoFormat(
        editing_template_id="edit_template_info",
        editing_template_version=1,
        format_title="카페 정보형",
        reference_url="internal://template",
    )

    guide = ai_client.get_shooting_guide(
        video_format,
        Store(id=10, user_id=1, name="행복분식"),
        ShortsProject(id=30, store_id=10),
    )

    assert len(guide.scenes) == 2
    assert [task.task_title for task in guide.tasks] == ["제조 과정", "대표 메뉴"]
    assert [task.scene_index for task in guide.tasks] == [0, 1]


def test_editing_run_uses_template_and_video_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(method=method, path=path, **kwargs)
        return {"run_id": "edit_123", "status": "QUEUED", "task_id": "task_123"}

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(ai_client, "_request_json", fake_request)
    project = ShortsProject(
        id=30,
        store_id=10,
        menu_id=20,
        recommendation_id="rec_123",
        promotion_purpose=PromotionPurpose.MENU,
        face_exposure_mode="노출있음",
    )
    video_format = VideoFormat(
        editing_template_id="edit_template_014",
        editing_template_version=3,
        format_title="메뉴 소개",
        reference_url="internal://template",
    )

    run = ai_client.start_editing_run(
        Store(id=10, user_id=1, name="행복분식"),
        project,
        video_format,
        [ai_client.FootageInput("task_1", "https://signed.example/take.mp4", 1)],
    )

    assert run.run_id == "edit_123"
    body = captured["json_body"]
    assert body["selected_shortform"]["editing_template_version"] == 3
    # 한국어 얼굴노출모드가 AI 쪽 영문 토큰으로 변환되어야 한다(원문 그대로 보내면 안 됨).
    assert body["project"]["face_exposure"] == "allowed"
    assert body["videos"][0]["shooting_scene_order"] == 1


def test_face_exposure_unknown_value_falls_back_to_not_allowed() -> None:
    """모르는 값(구버전 4모드 잔재 등)은 동의 안 된 얼굴 노출보다 안전한 쪽으로 떨어진다."""
    assert ai_client._map_face_exposure(None) == "not_allowed"
    assert ai_client._map_face_exposure("일부노출") == "not_allowed"
    assert ai_client._map_face_exposure("노출없음") == "not_allowed"
    assert ai_client._map_face_exposure("노출있음") == "allowed"


def test_editing_result_maps_nested_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(
        ai_client,
        "_request_json",
        lambda *args, **kwargs: {
            "run_id": "edit_123",
            "status": "COMPLETED",
            "recipe": {"recipe_version": 1},
            "render": {
                "output_video_url": "http://renderer.internal/files/result.mp4",
                "resolution": "1080x1920",
                "cover_image_url": None,
            },
            "publishing": {
                "title": "오늘의 행복분식",
                "caption": "오늘의 메뉴",
                "hashtags": ["#행복분식", "#분식", "#매장소개", "#동네맛집", "#숏폼"],
                "track": {
                    "mode": "SUGGESTED",
                    "title": None,
                    "artist": None,
                    "start_sec": None,
                    "end_sec": None,
                    "mood": None,
                    "search_keyword": "분식 릴스",
                },
                "post_note": "플랫폼에서 '분식 릴스'를 검색해 음원을 추가하세요.",
            },
            "warnings": ["SOURCE_ROLE_MATCH_FALLBACK"],
            "missing_scene_roles": ["REACTION"],
        },
    )

    result = ai_client.get_editing_run_result("edit_123")

    assert result.video_url == "http://renderer.internal/files/result.mp4"
    assert result.resolution == "1080x1920"
    assert result.publishing is not None
    assert result.publishing.title == "오늘의 행복분식"
    assert len(result.publishing.hashtags) == 5
    assert result.publishing.track["search_keyword"] == "분식 릴스"
    assert result.warnings == ["SOURCE_ROLE_MATCH_FALLBACK"]
    assert result.missing_scene_roles == ["REACTION"]
