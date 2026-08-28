"""숏폼 Agent 세션 API 테스트 (R06 재설계, 2026-08-26).

`docs/AI_연동_입출력.md` 5~12번 기준. AI_SERVER_URL이 없는 테스트 환경에서는
placeholder 경로(`app/services/ai_client.py`)가 동작한다 — 실제 대화 로직 대신,
turn을 보내면 곧바로 추천으로 넘어가는지와 accept가 프로젝트를 만드는지를 검증한다.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.video_format import VideoFormat
from app.services import ai_client

STORE_BODY: dict[str, Any] = {
    "name": "행복분식",
    "category": "분식",
    "address": "서울 강남구 테헤란로 1길 10",
}


def _ai_recommendation(template_id: str) -> dict[str, Any]:
    return {
        "recommendation_id": f"rec-{template_id}",
        "project_title": f"project-{template_id}",
        "title": f"title-{template_id}",
        "concept": f"concept-{template_id}",
        "editing_template_id": template_id,
        "editing_template_version": 1,
    }


def test_ai_recommendation_batch_requires_three_distinct_templates() -> None:
    assert ai_client._recommendations([]) == []
    valid = [_ai_recommendation(f"template-{index}") for index in range(3)]
    assert len(ai_client._recommendations(valid)) == 3

    duplicate = [valid[0], valid[1], _ai_recommendation("template-1")]
    with pytest.raises(ai_client.AIServiceUnavailable):
        ai_client._recommendations(duplicate)


@pytest.fixture(autouse=True)
def enable_shortform_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_SHORTFORM_PLACEHOLDERS_ENABLED", True)


@pytest.fixture
def store_id(client: TestClient, auth_headers: dict[str, str]) -> int:
    return client.post("/stores", json=STORE_BODY, headers=auth_headers).json()["id"]


@pytest.fixture
def menu_id(client: TestClient, auth_headers: dict[str, str], store_id: int) -> int:
    response = client.post(
        f"/stores/{store_id}/menus",
        json={"name": "떡볶이", "price": 4000},
        headers=auth_headers,
    )
    return response.json()["id"]


@pytest.fixture
def session_id(client: TestClient, auth_headers: dict[str, str], store_id: int) -> int:
    response = client.post(f"/stores/{store_id}/shortform-sessions", headers=auth_headers)
    return response.json()["id"]


@pytest.fixture
def other_headers(client: TestClient) -> dict[str, str]:
    client.post(
        "/auth/signup",
        json={
            "email": "other@example.com",
            "password": "sarils1234!",
            "name": "다른사장",
            "terms_agreed": True,
        },
    )
    login = client.post(
        "/auth/login", json={"email": "other@example.com", "password": "sarils1234!"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ---------------------------------------------------------------- 세션 생성


def test_create_session_returns_greeting(
    client: TestClient, auth_headers: dict[str, str], store_id: int
) -> None:
    response = client.post(f"/stores/{store_id}/shortform-sessions", headers=auth_headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "status", "assistant_message", "options", "project_state"}
    assert body["status"] == "ACTIVE"
    assert body["project_state"]["ready_for_confirmation"] is False
    assert body["project_state"]["promotion_subject"] is None


def test_create_session_for_other_store_is_404(
    client: TestClient, other_headers: dict[str, str], store_id: int
) -> None:
    response = client.post(f"/stores/{store_id}/shortform-sessions", headers=other_headers)
    assert response.status_code == 404
    assert response.json()["error_code"] == "STORE_NOT_FOUND"


def test_create_session_never_returns_placeholder_unless_explicitly_enabled(
    client: TestClient,
    auth_headers: dict[str, str],
    store_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AI_SHORTFORM_PLACEHOLDERS_ENABLED", False)

    response = client.post(f"/stores/{store_id}/shortform-sessions", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["error_code"] == "AI_SERVICE_CONFIGURATION_ERROR"


# ---------------------------------------------------------------- turns


def test_turn_moves_straight_to_recommend(
    client: TestClient, auth_headers: dict[str, str], session_id: int
) -> None:
    """placeholder는 실제 대화를 못 하므로 첫 turn에 바로 추천으로 넘어간다."""
    response = client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "떡볶이 홍보하고 싶어요"}},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "RECOMMEND"
    # AI가 한 번의 응답으로 내린 서로 다른 카드 3장을 그대로 보여준다.
    assert len(body["recommendations"]) == 3
    for recommendation in body["recommendations"]:
        assert set(recommendation) == {
            "recommendation_id",
            "project_title",
            "title",
            "concept",
            "editing_template_id",
            "editing_template_version",
            "video_format_id",
            "reference_url",
            "guide_video_url",
            "source_platform",
        }
    # 3장이 서로 다른 템플릿이어야 한다 — 같은 카드가 중복으로 뜨면 안 된다.
    template_ids = {r["editing_template_id"] for r in body["recommendations"]}
    assert len(template_ids) == 3
    assert body["project_state"]["ready_for_confirmation"] is True
    # 추천을 화면에 내리기 전에 실제로 재생 가능한 영상 포맷이 연결돼야 한다.
    assert all(r["video_format_id"] is not None for r in body["recommendations"])
    assert all(
        r["reference_url"].startswith("https://www.youtube.com/") for r in body["recommendations"]
    )
    assert all(
        r["guide_video_url"].startswith("https://www.youtube.com/") for r in body["recommendations"]
    )
    assert all(r["source_platform"] == "YOUTUBE" for r in body["recommendations"])


def test_find_video_format_id_returns_existing_match(db_session: Session) -> None:
    """이미 한 번 채택돼 video_formats에 적재된 템플릿이면 그 id를 그대로 준다."""
    from app.services.shortform_session import find_video_format_id

    video_format = VideoFormat(
        format_title="추천 포맷",
        reference_url="internal://editing-template/gt_test/v1",
        editing_template_id="gt_test",
        editing_template_version=1,
    )
    db_session.add(video_format)
    db_session.commit()

    assert find_video_format_id(db_session, "gt_test", 1) == video_format.id
    assert find_video_format_id(db_session, "gt_test", 2) is None
    assert find_video_format_id(db_session, "gt_missing", 1) is None


def test_resolve_video_format_allows_sharing_reference_url_with_other_template(
    db_session: Session,
) -> None:
    """서로 다른 챌린지가 같은 대표 영상을 공유해도 거부·병합하지 않는다(2026-08-28).

    실서버에서 실제로 겪음: `video_formats.reference_url` UNIQUE 제약을 뺀 뒤
    (트렌드 동기화가 같은 이유로 이미 고쳐짐), "챌린지 버전"·"매장 홍보 버전"처럼
    서로 다른 템플릿 둘이 같은 영상을 쓸 수 있게 됐는데, 이 함수는 여전히 "URL이
    같은 다른 행이 있으면 충돌"로 보고 정상적인 추천을 `RECOMMENDATION_MEDIA_
    UNAVAILABLE`로 거부하거나 그 행을 은퇴시키고 있었다.
    """
    from app.services.shortform_session import _resolve_video_format

    shared_url = "https://www.youtube.com/shorts/6duJ3WOzeuQ"
    other_template = VideoFormat(
        format_title="동그리오(챌린지)",
        reference_url=shared_url,
        editing_template_id="gt_donggeurio_challenge",
        editing_template_version=1,
        is_active=True,
    )
    db_session.add(other_template)
    db_session.commit()

    resolved = _resolve_video_format(
        db_session,
        {
            "editing_template_id": "gt_donggeurio_store_promotion",
            "editing_template_version": 1,
            "title": "동그리오(매장 홍보)",
            "reference_url": shared_url,
            "guide_video_url": shared_url,
        },
    )

    assert resolved.id != other_template.id
    assert resolved.reference_url == shared_url

    db_session.refresh(other_template)
    # 다른 템플릿 행이 은퇴되지 않고 그대로 활성 상태로 남아 있어야 한다.
    assert other_template.is_active is True
    assert other_template.reference_url == shared_url
    assert other_template.editing_template_id == "gt_donggeurio_challenge"


def test_turn_uses_representative_menu_as_subject(
    client: TestClient, auth_headers: dict[str, str], session_id: int, menu_id: int
) -> None:
    response = client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "메뉴 홍보하고 싶어요"}},
        headers=auth_headers,
    )

    subject = response.json()["project_state"]["promotion_subject"]
    assert subject == {"type": "MENU", "name": "떡볶이", "menu_id": menu_id}


def test_turn_on_other_session_is_404(
    client: TestClient, other_headers: dict[str, str], session_id: int
) -> None:
    response = client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "hi"}},
        headers=other_headers,
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "SESSION_NOT_FOUND"


# ---------------------------------------------------------------- 다시 추천


def test_next_recommendation_accumulates_shown_ids(
    client: TestClient, auth_headers: dict[str, str], session_id: int
) -> None:
    first = client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "떡볶이 홍보하고 싶어요"}},
        headers=auth_headers,
    ).json()
    first_template_ids = {r["editing_template_id"] for r in first["recommendations"]}

    response = client.post(
        f"/shortform-sessions/{session_id}/recommendations/next", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["recommendations"]) == 3
    next_template_ids = {r["editing_template_id"] for r in body["recommendations"]}
    # 이전에 보여준 템플릿과 이번에 새로 받은 템플릿이 전부 "이미 본 목록"에 쌓인다.
    assert first_template_ids | next_template_ids <= set(body["shown_template_ids"])
    # 두 번째 묶음은 첫 번째와 겹치지 않아야 한다.
    assert first_template_ids.isdisjoint(next_template_ids)


# ---------------------------------------------------------------- accept


def test_accept_without_recommendation_is_409(
    client: TestClient, auth_headers: dict[str, str], session_id: int
) -> None:
    response = client.post(
        f"/shortform-sessions/{session_id}/accept",
        json={"recommendation_id": "rec_아무거나"},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "RECOMMENDATION_NOT_READY"


def test_accept_unknown_recommendation_id_is_404(
    client: TestClient, auth_headers: dict[str, str], session_id: int
) -> None:
    """3장 중에 없는 ID를 보내면 못 찾는다 — 화면에 안 보여준 카드를 몰래 수락 못 함."""
    client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "떡볶이 홍보하고 싶어요"}},
        headers=auth_headers,
    )

    response = client.post(
        f"/shortform-sessions/{session_id}/accept",
        json={"recommendation_id": "rec_없는거"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "RECOMMENDATION_NOT_FOUND"


def test_accept_creates_project_with_title_and_format(
    client: TestClient,
    auth_headers: dict[str, str],
    session_id: int,
    store_id: int,
    menu_id: int,
    db_session: Session,
) -> None:
    turn = client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "떡볶이 홍보하고 싶어요"}},
        headers=auth_headers,
    ).json()
    # 3장 중 마지막 카드를 골라도 정확히 그 카드가 반영돼야 한다(첫 번째만
    # 우연히 맞는 게 아니라는 걸 확인).
    chosen = turn["recommendations"][-1]

    response = client.post(
        f"/shortform-sessions/{session_id}/accept",
        json={"recommendation_id": chosen["recommendation_id"]},
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["store_id"] == store_id
    assert body["project_title"] == chosen["project_title"]
    assert body["promotion_purpose"] == "메뉴소개"
    assert body["menu_id"] == menu_id
    assert body["shorts_status"] == "DRAFT"

    video_format = db_session.get(VideoFormat, body["video_format_id"])
    assert video_format is not None
    assert video_format.editing_template_id == chosen["editing_template_id"]
    assert video_format.editing_template_version == chosen["editing_template_version"]
    assert video_format.reference_url == chosen["reference_url"]
    assert video_format.guide_video_url == chosen["guide_video_url"]


def test_accept_populates_scenes_and_tasks(
    client: TestClient, auth_headers: dict[str, str], session_id: int
) -> None:
    """6.4(수락)가 7.1과 같은 로직을 재사용해 콘티·태스크까지 즉시 채운다."""
    turn = client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "떡볶이 홍보하고 싶어요"}},
        headers=auth_headers,
    ).json()
    chosen = turn["recommendations"][0]
    project = client.post(
        f"/shortform-sessions/{session_id}/accept",
        json={"recommendation_id": chosen["recommendation_id"]},
        headers=auth_headers,
    ).json()

    scenes = client.get(f"/shorts-projects/{project['id']}/scenes", headers=auth_headers).json()
    tasks = client.get(f"/shorts-projects/{project['id']}/tasks", headers=auth_headers).json()

    assert len(scenes["scenes"]) > 0
    assert len(tasks["tasks"]) > 0


def test_accept_twice_is_conflict(
    client: TestClient, auth_headers: dict[str, str], session_id: int
) -> None:
    turn = client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "떡볶이 홍보하고 싶어요"}},
        headers=auth_headers,
    ).json()
    chosen = turn["recommendations"][0]
    first = client.post(
        f"/shortform-sessions/{session_id}/accept",
        json={"recommendation_id": chosen["recommendation_id"]},
        headers=auth_headers,
    )
    assert first.status_code == 201

    second = client.post(
        f"/shortform-sessions/{session_id}/accept",
        json={"recommendation_id": chosen["recommendation_id"]},
        headers=auth_headers,
    )
    assert second.status_code == 409
    assert second.json()["error_code"] == "SESSION_NOT_ACTIVE"


# ---------------------------------------------------------------- 종료(새로고침)


def test_discard_session_is_idempotent(
    client: TestClient, auth_headers: dict[str, str], session_id: int
) -> None:
    first = client.delete(f"/shortform-sessions/{session_id}", headers=auth_headers)
    second = client.delete(f"/shortform-sessions/{session_id}", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200


def test_turn_after_discard_is_conflict(
    client: TestClient, auth_headers: dict[str, str], session_id: int
) -> None:
    client.delete(f"/shortform-sessions/{session_id}", headers=auth_headers)

    response = client.post(
        f"/shortform-sessions/{session_id}/turns",
        json={"input": {"type": "TEXT", "text": "hi"}},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "SESSION_NOT_ACTIVE"
