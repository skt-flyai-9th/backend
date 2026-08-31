"""최종 출력·게시자료 (15.1) / 가게 완성 숏폼 목록 (15.2) / 가게 로고 (3.6) 테스트."""

import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.sns import SnsPost
from app.models.video_format import VideoFormat
from app.models.video_output import RenderStatus, VideoOutput

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32

# 1x1 PNG. 실제 이미지 바이트라야 업로드 경로를 그대로 통과한다.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

STORE_BODY: dict[str, Any] = {
    "name": "행복분식",
    "category": "분식",
    "address": "서울 강남구 테헤란로 1길 10",
}


@pytest.fixture
def video_format(db_session: Session) -> VideoFormat:
    item = VideoFormat(
        format_title="가격 공개 반전 챌린지",
        format_type="밈",
        reference_url="https://youtu.be/1",
        source_platform="YOUTUBE",
        expected_duration_sec=24,
        shooting_difficulty="하",
        requires_face=False,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


@pytest.fixture
def store_id(client: TestClient, auth_headers: dict[str, str]) -> int:
    return client.post("/stores", json=STORE_BODY, headers=auth_headers).json()["id"]


@pytest.fixture
def project_id(
    client: TestClient, auth_headers: dict[str, str], store_id: int, video_format: VideoFormat
) -> int:
    """기획까지 끝나 태스크가 만들어진 프로젝트."""
    project_id = client.post(
        "/shorts-projects",
        json={"store_id": store_id, "promotion_purpose": "메뉴소개"},
        headers=auth_headers,
    ).json()["id"]
    client.post(
        f"/shorts-projects/{project_id}/plan",
        json={"video_format_id": video_format.id},
        headers=auth_headers,
    )
    return project_id


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


def _edited_project(client: TestClient, headers: dict[str, str], project_id: int) -> None:
    """모든 태스크에 촬영본을 올리고 편집까지 시작한 상태로 만든다."""
    tasks = client.get(f"/shorts-projects/{project_id}/tasks", headers=headers).json()["tasks"]
    for task in tasks:
        client.post(
            f"/tasks/{task['id']}/footage",
            files={"file": ("take.mp4", io.BytesIO(MP4_BYTES), "video/mp4")},
            data={"footage_type": "VIDEO"},
            headers=headers,
        )
    response = client.post(
        f"/shorts-projects/{project_id}/edit",
        json={"target_platform": "INSTAGRAM"},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _complete_outputs(db_session: Session, project_id: int) -> None:
    """렌더링이 끝난 상태로 만든다 — 15.2는 완성본만 보여준다."""
    outputs = db_session.query(VideoOutput).filter_by(shorts_project_id=project_id).all()
    for output in outputs:
        output.render_status = RenderStatus.COMPLETED
        output.video_url = f"outputs/{output.id}.mp4"
    db_session.commit()


# ---------------------------------------------------------------- 3.6 가게 로고 업로드


def _upload_logo(
    client: TestClient,
    headers: dict[str, str],
    store_id: int,
    *,
    content: bytes = PNG_BYTES,
    filename: str = "logo.png",
    content_type: str = "image/png",
) -> Any:
    return client.post(
        f"/stores/{store_id}/logo",
        files={"file": (filename, io.BytesIO(content), content_type)},
        headers=headers,
    )


def test_logo_upload_returns_full_url(
    client: TestClient, auth_headers: dict[str, str], store_id: int
) -> None:
    response = _upload_logo(client, auth_headers, store_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["store_id"] == store_id
    assert body["logo_url"].startswith("http")
    assert body["updated_at"].endswith("Z")


def test_logo_appears_in_store_detail(
    client: TestClient, auth_headers: dict[str, str], store_id: int
) -> None:
    """3.6으로 올린 로고가 3.1 조회 응답에 그대로 보여야 한다."""
    uploaded = _upload_logo(client, auth_headers, store_id).json()["logo_url"]

    detail = client.get(f"/stores/{store_id}", headers=auth_headers).json()
    assert detail["logo_url"] == uploaded


def test_logo_replaces_previous_file(
    client: TestClient, auth_headers: dict[str, str], store_id: int, tmp_path: Any
) -> None:
    """로고는 가게당 1장이다 — 새로 올리면 이전 파일이 남지 않는다."""
    first = _upload_logo(client, auth_headers, store_id).json()["logo_url"]
    second = _upload_logo(client, auth_headers, store_id).json()["logo_url"]

    assert first != second, "캐시 때문에 키는 매번 달라야 한다"

    detail = client.get(f"/stores/{store_id}", headers=auth_headers).json()
    assert detail["logo_url"] == second

    # 이전 파일은 지워졌다 — 저장소에 남은 로고 파일은 하나뿐이다
    logo_dir = tmp_path / "media" / "stores" / str(store_id) / "logo"
    assert len(list(logo_dir.iterdir())) == 1


def test_logo_rejects_non_image(
    client: TestClient, auth_headers: dict[str, str], store_id: int
) -> None:
    response = _upload_logo(
        client, auth_headers, store_id, filename="clip.mp4", content_type="video/mp4"
    )

    assert response.status_code == 415
    assert response.json()["error_code"] == "UNSUPPORTED_FILE_TYPE"
    assert "이미지" in response.json()["message"]


def test_logo_rejects_empty_file(
    client: TestClient, auth_headers: dict[str, str], store_id: int
) -> None:
    response = _upload_logo(client, auth_headers, store_id, content=b"")

    assert response.status_code == 400
    assert response.json()["error_code"] == "EMPTY_FILE"


def test_logo_hidden_from_other_user(
    client: TestClient, other_headers: dict[str, str], store_id: int
) -> None:
    response = _upload_logo(client, other_headers, store_id)

    assert response.status_code == 404
    assert response.json()["error_code"] == "STORE_NOT_FOUND"


def test_logo_requires_authentication(client: TestClient, store_id: int) -> None:
    response = client.post(
        f"/stores/{store_id}/logo",
        files={"file": ("logo.png", io.BytesIO(PNG_BYTES), "image/png")},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------- 15.1 최종 출력·게시자료


def test_outputs_require_edit_first(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """편집 결과가 없으면 내보낼 원본이 없다."""
    response = client.post(
        f"/shorts-projects/{project_id}/outputs",
        json={"target_platforms": ["INSTAGRAM"]},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "EDIT_NOT_STARTED"


def test_outputs_created_per_platform(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    _edited_project(client, auth_headers, project_id)

    response = client.post(
        f"/shorts-projects/{project_id}/outputs",
        json={"target_platforms": ["INSTAGRAM", "YOUTUBE"]},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    platforms = {output["target_platform"] for output in body["outputs"]}
    assert platforms == {"INSTAGRAM", "YOUTUBE"}
    assert body["publish_kit"]["caption"]
    assert body["publish_kit"]["title"]
    assert len(body["publish_kit"]["hashtags"]) >= 5


def test_outputs_are_idempotent_per_platform(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """같은 플랫폼을 다시 요청해도 산출물이 쌓이지 않는다."""
    _edited_project(client, auth_headers, project_id)
    body = {"target_platforms": ["INSTAGRAM", "YOUTUBE"]}

    first = client.post(
        f"/shorts-projects/{project_id}/outputs", json=body, headers=auth_headers
    ).json()
    second = client.post(
        f"/shorts-projects/{project_id}/outputs", json=body, headers=auth_headers
    ).json()

    assert [o["id"] for o in first["outputs"]] == [o["id"] for o in second["outputs"]]


def test_publish_kit_uses_only_real_store_data(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """AI 연동 전 임시 게시자료는 문구를 지어내지 않는다 — DB에 있는 값만 쓴다."""
    _edited_project(client, auth_headers, project_id)

    kit = client.post(
        f"/shorts-projects/{project_id}/outputs",
        json={"target_platforms": ["INSTAGRAM"]},
        headers=auth_headers,
    ).json()["publish_kit"]

    assert kit["caption"] == STORE_BODY["name"]
    assert kit["title"] == f"{STORE_BODY['name']}을 소개합니다"
    assert "#행복분식" in kit["hashtags"]
    assert "#분식" in kit["hashtags"]


def test_get_outputs_matches_post_shape(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """GET이 POST와 같은 필드 구성이라야 프론트가 POST 응답을 캐시하지 않아도 된다."""
    _edited_project(client, auth_headers, project_id)
    posted = client.post(
        f"/shorts-projects/{project_id}/outputs",
        json={"target_platforms": ["INSTAGRAM"]},
        headers=auth_headers,
    ).json()

    fetched = client.get(f"/shorts-projects/{project_id}/outputs", headers=auth_headers).json()

    assert fetched.keys() == posted.keys()
    assert fetched["outputs"][0].keys() == posted["outputs"][0].keys()
    assert fetched["publish_kit"] == posted["publish_kit"]


def test_get_outputs_before_post_has_no_publish_kit(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """편집만 하고 15.1을 아직 안 불렀으면 게시자료는 null이다."""
    _edited_project(client, auth_headers, project_id)

    body = client.get(f"/shorts-projects/{project_id}/outputs", headers=auth_headers).json()

    assert body["publish_kit"] is None
    assert len(body["outputs"]) == 1


def test_get_outputs_returns_latest_per_platform(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """14.3 수정으로 산출물이 쌓여도 플랫폼별 최신 1개만 준다."""
    _edited_project(client, auth_headers, project_id)
    output_id = client.get(
        f"/shorts-projects/{project_id}/edit/result", headers=auth_headers
    ).json()["video_output_id"]
    revised = client.post(
        f"/video-outputs/{output_id}/revise",
        json={"request_type": "quick_button", "action": "자막 크게"},
        headers=auth_headers,
    ).json()["video_output_id"]

    body = client.get(f"/shorts-projects/{project_id}/outputs", headers=auth_headers).json()

    assert len(body["outputs"]) == 1
    assert body["outputs"][0]["id"] == revised


def test_outputs_hidden_from_other_user(
    client: TestClient, other_headers: dict[str, str], project_id: int
) -> None:
    response = client.get(f"/shorts-projects/{project_id}/outputs", headers=other_headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "PROJECT_NOT_FOUND"


def test_outputs_require_authentication(client: TestClient, project_id: int) -> None:
    assert client.get(f"/shorts-projects/{project_id}/outputs").status_code == 401


# ---------------------------------------------------------------- 15.2 완성 숏폼 목록


def test_store_shorts_returns_spec_fields(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    store_id: int,
    project_id: int,
) -> None:
    _edited_project(client, auth_headers, project_id)
    _complete_outputs(db_session, project_id)

    response = client.get(f"/stores/{store_id}/shorts", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["shorts_project_id"] == project_id
    assert item["promotion_purpose"] == "메뉴소개"
    assert item["duration_sec"] == 24, "포맷의 완성 영상 길이에서 온다"
    assert item["is_posted"] is False
    assert item["video_url"].startswith("http")
    assert item["created_at"].endswith("Z")


def test_store_shorts_excludes_unfinished(
    client: TestClient, auth_headers: dict[str, str], store_id: int, project_id: int
) -> None:
    """렌더링이 끝나지 않은 산출물은 갤러리에 나오지 않는다."""
    _edited_project(client, auth_headers, project_id)

    body = client.get(f"/stores/{store_id}/shorts", headers=auth_headers).json()

    assert body["items"] == []
    assert body["total"] == 0


def test_store_shorts_excludes_completed_without_video_url(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    store_id: int,
    project_id: int,
) -> None:
    """`render_status`가 COMPLETED여도 `video_url`이 없으면 갤러리에 안 나온다.

    AI 결과에 렌더 URL이 비어 오면 실제로 이 상태가 만들어진다
    (`app/services/video_output.py::_latest_completed_ids` 참고, 2026-08-31).
    """
    _edited_project(client, auth_headers, project_id)
    outputs = db_session.query(VideoOutput).filter_by(shorts_project_id=project_id).all()
    for output in outputs:
        output.render_status = RenderStatus.COMPLETED
        # video_url은 일부러 안 채운다 — 재생 안 되는 "완성" 케이스를 재현한다.
    db_session.commit()

    body = client.get(f"/stores/{store_id}/shorts", headers=auth_headers).json()

    assert body["items"] == []
    assert body["total"] == 0


def test_store_shorts_returns_one_per_project(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    store_id: int,
    project_id: int,
) -> None:
    """수정본이 여러 개여도 프로젝트당 최신 완성본 1개만 나온다."""
    _edited_project(client, auth_headers, project_id)
    output_id = client.get(
        f"/shorts-projects/{project_id}/edit/result", headers=auth_headers
    ).json()["video_output_id"]
    revised = client.post(
        f"/video-outputs/{output_id}/revise",
        json={"request_type": "quick_button", "action": "자막 크게"},
        headers=auth_headers,
    ).json()["video_output_id"]
    _complete_outputs(db_session, project_id)

    body = client.get(f"/stores/{store_id}/shorts", headers=auth_headers).json()

    assert body["total"] == 1
    assert body["items"][0]["video_output_id"] == revised


def test_store_shorts_is_posted_reflects_sns_posts(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    store_id: int,
    project_id: int,
) -> None:
    """R16이 붙으면 게시 이력이 배지로 나타난다."""
    _edited_project(client, auth_headers, project_id)
    _complete_outputs(db_session, project_id)
    output_id = client.get(f"/stores/{store_id}/shorts", headers=auth_headers).json()["items"][0][
        "video_output_id"
    ]

    db_session.add(SnsPost(video_output_id=output_id, post_platform="INSTAGRAM"))
    db_session.commit()

    body = client.get(f"/stores/{store_id}/shorts", headers=auth_headers).json()
    assert body["items"][0]["is_posted"] is True


def test_store_shorts_paginates(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    store_id: int,
    video_format: VideoFormat,
) -> None:
    for _ in range(3):
        project_id = client.post(
            "/shorts-projects",
            json={"store_id": store_id, "promotion_purpose": "메뉴소개"},
            headers=auth_headers,
        ).json()["id"]
        client.post(
            f"/shorts-projects/{project_id}/plan",
            json={"video_format_id": video_format.id},
            headers=auth_headers,
        )
        _edited_project(client, auth_headers, project_id)
        _complete_outputs(db_session, project_id)

    first = client.get(f"/stores/{store_id}/shorts?page=1&size=2", headers=auth_headers).json()
    second = client.get(f"/stores/{store_id}/shorts?page=2&size=2", headers=auth_headers).json()

    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    ids = [item["video_output_id"] for item in first["items"] + second["items"]]
    assert ids == sorted(ids, reverse=True), "최신순 고정"


def test_store_shorts_hidden_from_other_user(
    client: TestClient, other_headers: dict[str, str], store_id: int
) -> None:
    response = client.get(f"/stores/{store_id}/shorts", headers=other_headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "STORE_NOT_FOUND"


def test_store_shorts_requires_authentication(client: TestClient, store_id: int) -> None:
    assert client.get(f"/stores/{store_id}/shorts").status_code == 401


def test_publish_kit_includes_track_key(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """음원 가이드는 정확한 곡을 못 찾아도 검색 키워드로 항상 채워진다.

    `start_sec`/`end_sec`은 AI팀이 정확한 초 단위를 못 준다고 확인해(2026-08-26)
    항상 `null`이다 — 필드 자체는 지우지 않는다.
    """
    _edited_project(client, auth_headers, project_id)

    kit = client.post(
        f"/shorts-projects/{project_id}/outputs",
        json={"target_platforms": ["INSTAGRAM"]},
        headers=auth_headers,
    ).json()["publish_kit"]

    assert kit["track"]["mode"] == "SUGGESTED"
    assert kit["track"]["search_keyword"]
    assert kit["track"]["start_sec"] is None


def test_track_survives_get(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """POST에서 저장한 음원 가이드가 GET에서도 같은 모양으로 나와야 한다."""
    _edited_project(client, auth_headers, project_id)
    posted = client.post(
        f"/shorts-projects/{project_id}/outputs",
        json={"target_platforms": ["INSTAGRAM"]},
        headers=auth_headers,
    ).json()["publish_kit"]

    fetched = client.get(f"/shorts-projects/{project_id}/outputs", headers=auth_headers).json()[
        "publish_kit"
    ]

    assert fetched == posted
