"""숏폼 포맷 API 테스트 (API명세서 5.1~5.2)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.video_format import VideoFormat


def _add_format(db: Session, **overrides: Any) -> VideoFormat:
    base: dict[str, Any] = {
        "format_title": "가격 공개 반전 챌린지",
        "format_type": "밈",
        "reference_url": "https://www.youtube.com/watch?v=aaa",
        "source_platform": "YOUTUBE",
        "expected_duration_sec": 25,
        "shooting_difficulty": "하",
        "requires_face": False,
    }
    video_format = VideoFormat(**{**base, **overrides})
    db.add(video_format)
    db.commit()
    db.refresh(video_format)
    return video_format


@pytest.fixture
def formats(db_session: Session) -> list[VideoFormat]:
    return [
        _add_format(
            db_session,
            format_title="가격 공개 반전 챌린지",
            format_type="밈",
            reference_url="https://youtu.be/1",
            requires_face=False,
        ),
        _add_format(
            db_session,
            format_title="사장님 메뉴 추천",
            format_type="잔잔한 소개",
            reference_url="https://youtu.be/2",
            requires_face=True,
        ),
        _add_format(
            db_session,
            format_title="가게 한 바퀴",
            format_type="잔잔한 소개",
            reference_url="https://youtu.be/3",
            requires_face=False,
        ),
    ]


# ---------------------------------------------------------------- 5.1 목록


def test_list_returns_spec_fields(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    response = client.get("/video-formats", headers=auth_headers)

    assert response.status_code == 200
    assert set(response.json()["formats"][0]) == {
        "id",
        "format_title",
        "format_type",
        "reference_duration_sec",
        "estimated_shooting_sec",
        "shooting_difficulty",
        "requires_face",
        "reference_url",
        "guide_video_url",
        "source_platform",
        "is_favorite",
        "recommend_reasons",
    }


def test_recommend_reasons_is_empty_before_ai(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """AI 연동 전이라 추천 이유는 비어 있다. 키 자체는 계약대로 존재해야 한다."""
    body = client.get("/video-formats", headers=auth_headers).json()

    assert all(item["recommend_reasons"] == [] for item in body["formats"])


def test_list_filters_by_format_type(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    body = client.get("/video-formats", params={"format_type": "밈"}, headers=auth_headers).json()

    assert [f["format_title"] for f in body["formats"]] == ["가격 공개 반전 챌린지"]


def test_list_filters_by_requires_face(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    body = client.get(
        "/video-formats", params={"requires_face": False}, headers=auth_headers
    ).json()

    assert len(body["formats"]) == 2


def test_list_searches_by_keyword(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    body = client.get("/video-formats", params={"keyword": "가게"}, headers=auth_headers).json()

    assert [f["format_title"] for f in body["formats"]] == ["가게 한 바퀴"]


def test_list_combines_filters(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    body = client.get(
        "/video-formats",
        params={"format_type": "잔잔한 소개", "requires_face": False},
        headers=auth_headers,
    ).json()

    assert [f["format_title"] for f in body["formats"]] == ["가게 한 바퀴"]


def test_list_returns_empty_when_nothing_matches(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """0건은 에러가 아니다 — 프론트가 조건 완화를 제안하는 분기다(S05.2.2)."""
    response = client.get(
        "/video-formats", params={"keyword": "존재하지않는포맷"}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["formats"] == []


def test_list_paginates(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """피드 무한 스크롤용 페이지네이션."""
    page1 = client.get(
        "/video-formats", params={"page": 1, "size": 2}, headers=auth_headers
    ).json()["formats"]
    page2 = client.get(
        "/video-formats", params={"page": 2, "size": 2}, headers=auth_headers
    ).json()["formats"]

    assert len(page1) == 2
    assert len(page2) == 1
    assert {f["id"] for f in page1}.isdisjoint({f["id"] for f in page2})


def test_list_works_without_project_id(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """홈 피드를 프로젝트 생성 전에 볼 수 있어야 한다 — project_id는 선택이다."""
    response = client.get("/video-formats", headers=auth_headers)

    assert response.status_code == 200
    assert len(response.json()["formats"]) == 3


def test_list_rejects_unknown_sort(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/video-formats", params={"sort": "없는정렬"}, headers=auth_headers)

    assert response.status_code == 422


def test_list_requires_authentication(client: TestClient) -> None:
    assert client.get("/video-formats").status_code == 401


# ---------------------------------------------------------------- 5.2 단건 상세


def test_detail_returns_spec_fields(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    response = client.get(f"/video-formats/{formats[0].id}", headers=auth_headers)

    assert response.status_code == 200
    assert set(response.json()) == {
        "id",
        "format_title",
        "format_type",
        "reference_url",
        "guide_video_url",
        "source_platform",
        "reference_duration_sec",
        "estimated_shooting_sec",
        "shooting_difficulty",
        "requires_face",
        "is_favorite",
    }


def test_detail_returns_404_for_unknown_format(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/video-formats/999999", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "FORMAT_NOT_FOUND"


def test_detail_requires_authentication(client: TestClient) -> None:
    assert client.get("/video-formats/1").status_code == 401


# ---------------------------------------------------------------- 프로젝트 연결


def test_project_can_reference_a_format(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """R04에서 FK 없이 두었던 video_format_id가 이제 실제 포맷을 가리킨다.

    다만 **포맷을 저장하는 API는 아직 없다**(`docs/PM_DECISIONS.md` 「확인 대기 중」).
    여기서는 조회 시 null로 나오는 것까지만 확인한다.
    """
    store_id = client.post(
        "/stores",
        json={"name": "행복분식", "category": "분식", "address": "서울 강남구"},
        headers=auth_headers,
    ).json()["id"]
    project_id = client.post(
        "/shorts-projects",
        json={"store_id": store_id, "promotion_purpose": "메뉴소개"},
        headers=auth_headers,
    ).json()["id"]

    body = client.get(f"/shorts-projects/{project_id}", headers=auth_headers).json()

    assert body["video_format_id"] is None


# ---------------------------------------------------------------- 5.3 찜


def _favorite(client: TestClient, headers: dict[str, str], format_id: int) -> Any:
    return client.post(f"/video-formats/{format_id}/favorite", headers=headers)


def test_favorite_returns_spec_fields(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    response = _favorite(client, auth_headers, formats[0].id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"video_format_id", "is_favorite", "created_at"}
    assert body["video_format_id"] == formats[0].id
    assert body["is_favorite"] is True
    assert body["created_at"].endswith("Z")


def test_favorite_is_idempotent(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """하트 연타·네트워크 재시도로 중복 요청이 와도 409를 던지지 않는다."""
    first = _favorite(client, auth_headers, formats[0].id)
    second = _favorite(client, auth_headers, formats[0].id)

    assert first.status_code == 200
    assert second.status_code == 200
    # 두 번 눌러도 찜은 하나다
    favorites = client.get("/video-formats/favorites", headers=auth_headers).json()["formats"]
    assert len(favorites) == 1


def test_unfavorite_is_idempotent(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """찜하지 않은 포맷을 해제해도 200 — 이미 원하는 상태다."""
    response = client.delete(f"/video-formats/{formats[0].id}/favorite", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"message": "찜을 해제했습니다."}


def test_favorite_then_unfavorite(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    _favorite(client, auth_headers, formats[0].id)
    client.delete(f"/video-formats/{formats[0].id}/favorite", headers=auth_headers)

    assert client.get("/video-formats/favorites", headers=auth_headers).json()["formats"] == []


def test_favorite_unknown_format_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = _favorite(client, auth_headers, 999999)

    assert response.status_code == 404
    assert response.json()["error_code"] == "FORMAT_NOT_FOUND"


def test_favorites_list_matches_feed_shape(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """찜 목록 응답이 5.1과 같은 형태여야 프론트가 카드 컴포넌트를 재사용할 수 있다."""
    _favorite(client, auth_headers, formats[0].id)

    favorites = client.get("/video-formats/favorites", headers=auth_headers).json()["formats"]
    feed = client.get("/video-formats", headers=auth_headers).json()["formats"]

    assert set(favorites[0]) == set(feed[0])
    assert favorites[0]["is_favorite"] is True
    assert favorites[0]["recommend_reasons"] == []


def test_favorites_list_is_newest_first(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    _favorite(client, auth_headers, formats[0].id)
    _favorite(client, auth_headers, formats[2].id)

    favorites = client.get("/video-formats/favorites", headers=auth_headers).json()["formats"]

    assert [f["id"] for f in favorites] == [formats[2].id, formats[0].id]


def test_favorites_are_per_user(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """찜은 계정 단위 — 다른 사용자의 찜이 보이면 안 된다."""
    _favorite(client, auth_headers, formats[0].id)

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
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/video-formats/favorites", headers=other).json()["formats"] == []
    feed = client.get("/video-formats", headers=other).json()["formats"]
    assert all(f["is_favorite"] is False for f in feed)


def test_feed_reflects_favorite_state(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    """피드에서 하트가 채워졌는지 그리려면 is_favorite이 정확해야 한다."""
    _favorite(client, auth_headers, formats[1].id)

    feed = client.get("/video-formats", headers=auth_headers).json()["formats"]
    state = {f["id"]: f["is_favorite"] for f in feed}

    assert state[formats[1].id] is True
    assert state[formats[0].id] is False


def test_detail_reflects_favorite_state(
    client: TestClient, auth_headers: dict[str, str], formats: list[VideoFormat]
) -> None:
    _favorite(client, auth_headers, formats[0].id)

    detail = client.get(f"/video-formats/{formats[0].id}", headers=auth_headers).json()

    assert detail["is_favorite"] is True


def test_favorites_path_is_not_parsed_as_format_id(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """/video-formats/favorites 가 /{format_id}로 잡히면 422가 난다 — 선언 순서 회귀 테스트."""
    response = client.get("/video-formats/favorites", headers=auth_headers)

    assert response.status_code == 200


def test_favorite_requires_authentication(client: TestClient, formats: list[VideoFormat]) -> None:
    assert client.post(f"/video-formats/{formats[0].id}/favorite").status_code == 401
    assert client.delete(f"/video-formats/{formats[0].id}/favorite").status_code == 401
    assert client.get("/video-formats/favorites").status_code == 401
