"""촬영 가이드·촬영본 업로드·자동저장 테스트 (API명세서 9.1~9.3)."""

import io
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.shooting_task import ShootingTask
from app.models.video_format import VideoFormat

# 최소한의 mp4 헤더. 실제 재생은 안 되지만 업로드 경로 검증에는 충분하다.
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32

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
        reference_url="https://www.youtube.com/watch?v=abc",
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
def project_id(client: TestClient, auth_headers: dict[str, str]) -> int:
    store_id = client.post("/stores", json=STORE_BODY, headers=auth_headers).json()["id"]
    return client.post(
        "/shorts-projects",
        json={"store_id": store_id, "promotion_purpose": "메뉴소개"},
        headers=auth_headers,
    ).json()["id"]


@pytest.fixture
def task_id(
    client: TestClient, auth_headers: dict[str, str], project_id: int, video_format: VideoFormat
) -> int:
    client.post(
        f"/shorts-projects/{project_id}/plan",
        json={"video_format_id": video_format.id},
        headers=auth_headers,
    )
    return client.get(f"/shorts-projects/{project_id}/tasks", headers=auth_headers).json()["tasks"][
        0
    ]["id"]


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


def _upload(
    client: TestClient,
    headers: dict[str, str],
    task_id: int,
    *,
    content: bytes = MP4_BYTES,
    filename: str = "take.mp4",
    content_type: str = "video/mp4",
    duration: int | None = 8,
) -> Any:
    data: dict[str, Any] = {"footage_type": "VIDEO"}
    if duration is not None:
        data["footage_duration_sec"] = duration
    return client.post(
        f"/tasks/{task_id}/footage",
        files={"file": (filename, io.BytesIO(content), content_type)},
        data=data,
        headers=headers,
    )


# ---------------------------------------------------------------- 9.1 촬영 가이드


def test_guide_returns_spec_fields(
    client: TestClient, auth_headers: dict[str, str], task_id: int
) -> None:
    response = client.get(f"/tasks/{task_id}/guide", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"guide_type", "overlay", "reference_video", "broll_shot"}


def test_guide_overlay_instructions_empty_before_ai(
    client: TestClient, auth_headers: dict[str, str], task_id: int
) -> None:
    """AI 연동 전이라 지시문은 비어 있다 — 지어내면 가짜 안내가 진짜처럼 보인다."""
    body = client.get(f"/tasks/{task_id}/guide", headers=auth_headers).json()

    assert body["guide_type"] == "OVERLAY"
    assert body["overlay"]["instructions"] == []


def test_guide_takes_shot_type_from_scene(
    client: TestClient, auth_headers: dict[str, str], task_id: int, project_id: int
) -> None:
    """shot_type은 태스크에 중복 저장하지 않고 콘티에서 가져온다."""
    scenes = client.get(f"/shorts-projects/{project_id}/scenes", headers=auth_headers).json()[
        "scenes"
    ]
    body = client.get(f"/tasks/{task_id}/guide", headers=auth_headers).json()

    assert body["broll_shot"]["shot_type"] == scenes[0]["shot_type"]


def test_overlay_guide_also_includes_reference_video(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    video_format: VideoFormat,
) -> None:
    """`reference_video`는 이제 guide_type과 무관하게 항상 채워진다(2026-08-26).

    AI가 guide_type을 계약에서 제거하면서 "가이드를 제공할 때는 항상 참고영상
    구간을 함께 준다"는 쪽으로 바뀌었다. 예전엔 DANCE에서만 채웠는데, 이제
    guide_type이 항상 OVERLAY로 고정되는 상황이라 그 기준으로 걸면 영원히
    안 나가게 된다 — 그래서 OVERLAY 태스크에서도 채워야 한다.
    """
    body = client.get(f"/tasks/{task_id}/guide", headers=auth_headers).json()

    assert body["guide_type"] == "OVERLAY"
    assert body["reference_video"] is not None
    assert body["reference_video"]["reference_url"] == video_format.reference_url


def test_dance_guide_uses_format_reference_video(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    video_format: VideoFormat,
    db_session: Session,
) -> None:
    """안무 영상은 포맷 하나당 하나 — 태스크별 컬럼 없이 포맷에서 가져온다."""
    task = db_session.get(ShootingTask, task_id)
    assert task is not None
    task.guide = {"guide_type": "DANCE"}
    db_session.commit()

    body = client.get(f"/tasks/{task_id}/guide", headers=auth_headers).json()

    assert body["guide_type"] == "DANCE"
    # 가이드 영상이 없는 포맷이라 대표 영상으로 떨어진다.
    assert body["reference_video"]["reference_url"] == video_format.reference_url
    assert body["reference_video"]["source_platform"] == "YOUTUBE"
    # 명세서상 DANCE는 나머지 블록이 null이다
    assert body["overlay"] is None
    assert body["broll_shot"] is None


def test_dance_guide_prefers_guide_video(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    video_format: VideoFormat,
    db_session: Session,
) -> None:
    """촬영 중에 따라 추는 영상이므로 대표 영상이 아니라 **가이드 영상**을 준다."""
    task = db_session.get(ShootingTask, task_id)
    assert task is not None
    task.guide = {"guide_type": "DANCE"}
    video_format.guide_video_url = "https://www.youtube.com/shorts/GUIDEvideo1"
    db_session.commit()

    body = client.get(f"/tasks/{task_id}/guide", headers=auth_headers).json()

    assert body["reference_video"]["reference_url"] == "https://www.youtube.com/shorts/GUIDEvideo1"
    assert body["reference_video"]["reference_url"] != video_format.reference_url


def test_dance_guide_includes_task_specific_segment(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    db_session: Session,
) -> None:
    """같은 안무 영상을 태스크(컷)마다 다른 구간으로 잘라 보여준다(2026-08-26 추가).

    영상 자체는 포맷 하나당 하나지만, start_ms/end_ms는 태스크마다 달라야 해서
    포맷이 아니라 태스크의 guide에서 온다.
    """
    task = db_session.get(ShootingTask, task_id)
    assert task is not None
    task.guide = {"guide_type": "DANCE", "start_ms": 1800, "end_ms": 4300}
    db_session.commit()

    body = client.get(f"/tasks/{task_id}/guide", headers=auth_headers).json()

    assert body["reference_video"]["start_ms"] == 1800
    assert body["reference_video"]["end_ms"] == 4300


def test_dance_guide_segment_is_null_when_ai_omits_it(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    db_session: Session,
) -> None:
    """AI가 구간을 안 주면(연동 전·구버전) 지어내지 않고 null로 둔다.

    프론트는 이때 영상 전체를 보여주면 된다.
    """
    task = db_session.get(ShootingTask, task_id)
    assert task is not None
    task.guide = {"guide_type": "DANCE"}
    db_session.commit()

    body = client.get(f"/tasks/{task_id}/guide", headers=auth_headers).json()

    assert body["reference_video"]["start_ms"] is None
    assert body["reference_video"]["end_ms"] is None


def test_guide_hidden_from_other_user(
    client: TestClient, task_id: int, other_headers: dict[str, str]
) -> None:
    response = client.get(f"/tasks/{task_id}/guide", headers=other_headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "TASK_NOT_FOUND"


def test_guide_requires_authentication(client: TestClient, task_id: int) -> None:
    assert client.get(f"/tasks/{task_id}/guide").status_code == 401


# ---------------------------------------------------------------- 9.2 촬영본 업로드


def test_upload_accepts_short_valid_footage(
    client: TestClient, auth_headers: dict[str, str], task_id: int
) -> None:
    """컷별 촬영은 별도 최소 길이를 지어내지 않고 업로드한다."""
    response = _upload(client, auth_headers, task_id, duration=1)

    assert response.status_code == 200, response.text


def test_upload_returns_spec_fields(
    client: TestClient, auth_headers: dict[str, str], task_id: int
) -> None:
    response = _upload(client, auth_headers, task_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "task_id",
        "file_url",
        "footage_type",
        "footage_duration_sec",
        "task_status",
        "thumbnail_url",
    }
    assert body["file_url"].startswith("http://")


def test_upload_marks_task_done(
    client: TestClient, auth_headers: dict[str, str], task_id: int, project_id: int
) -> None:
    """업로드 성공이 DONE이 되는 유일한 정상 경로다 (2026-08-21 확정)."""
    assert _upload(client, auth_headers, task_id).json()["task_status"] == "DONE"

    board = client.get(f"/shorts-projects/{project_id}/tasks", headers=auth_headers).json()
    changed = next(task for task in board["tasks"] if task["id"] == task_id)
    assert changed["task_status"] == "DONE"
    assert board["progress_rate"] == 25  # 4개 중 1개


def test_retake_overwrites_previous_file(
    client: TestClient, auth_headers: dict[str, str], task_id: int, temp_media_root: Path
) -> None:
    """재촬영은 덮어쓴다 — 테이크 이력을 남기지 않는다(ERD 코멘트).

    옛 파일을 지우지 않으면 아무도 참조하지 않는 파일이 계속 쌓인다.
    """
    first = _upload(client, auth_headers, task_id).json()["file_url"]

    second = _upload(client, auth_headers, task_id, content=MP4_BYTES + b"second").json()[
        "file_url"
    ]

    assert first != second
    saved = list(temp_media_root.rglob("*.mp4"))
    assert len(saved) == 1


# ---------------------------------------------------------------- 9.2 촬영본 썸네일 (2026-08-28)


def test_upload_returns_generated_thumbnail(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """앱을 껐다 켜도 미리보기를 그릴 수 있도록, 업로드 시 대표 프레임을 저장해둔다."""
    from app.services import footage

    monkeypatch.setattr(footage, "generate_thumbnail", lambda storage, source_path, key: key)

    body = _upload(client, auth_headers, task_id).json()

    assert body["thumbnail_url"] is not None
    assert body["thumbnail_url"].startswith("http://")


def test_upload_thumbnail_null_when_generation_fails(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg 실패(코덱 미지원 등)는 부가 기능 실패일 뿐이라 업로드 자체는 성공해야 한다."""
    from app.services import footage

    monkeypatch.setattr(footage, "generate_thumbnail", lambda storage, source_path, key: None)

    response = _upload(client, auth_headers, task_id)

    assert response.status_code == 200, response.text
    assert response.json()["thumbnail_url"] is None


def test_retake_deletes_previous_thumbnail(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    temp_media_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """재촬영은 썸네일도 같이 덮어쓴다 — 옛 썸네일이 계속 쌓이면 안 된다."""
    from app.services import footage

    def fake_generate_thumbnail(storage, source_path, key):
        del source_path
        storage.save(key, io.BytesIO(b"thumb"), "image/jpeg")
        return key

    monkeypatch.setattr(footage, "generate_thumbnail", fake_generate_thumbnail)

    _upload(client, auth_headers, task_id)
    _upload(client, auth_headers, task_id, content=MP4_BYTES + b"second")

    assert len(list(temp_media_root.rglob("*.jpg"))) == 1


def test_upload_rejects_non_video(
    client: TestClient, auth_headers: dict[str, str], task_id: int
) -> None:
    response = _upload(
        client,
        auth_headers,
        task_id,
        content=b"not a video",
        filename="photo.png",
        content_type="image/png",
    )

    assert response.status_code == 415
    assert response.json()["error_code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_rejects_oversized_video(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import config

    monkeypatch.setattr(config.settings, "MAX_VIDEO_UPLOAD_SIZE_MB", 0)

    response = _upload(client, auth_headers, task_id)

    assert response.status_code == 413
    assert response.json()["error_code"] == "FILE_TOO_LARGE"


def test_video_limit_is_separate_from_photo_limit(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사진 제한(10MB)을 그대로 쓰면 정상 촬영분도 막힌다 — 별도 설정이어야 한다."""
    from app.core import config

    monkeypatch.setattr(config.settings, "MAX_UPLOAD_SIZE_MB", 0)

    assert _upload(client, auth_headers, task_id).status_code == 200


def test_upload_hidden_from_other_user(
    client: TestClient, task_id: int, other_headers: dict[str, str]
) -> None:
    assert _upload(client, other_headers, task_id).status_code == 404


def test_upload_requires_authentication(client: TestClient, task_id: int) -> None:
    response = client.post(
        f"/tasks/{task_id}/footage",
        files={"file": ("take.mp4", io.BytesIO(MP4_BYTES), "video/mp4")},
        data={"footage_type": "VIDEO"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------- 9.3 자동저장


def test_draft_is_empty_before_save(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    body = client.get(f"/shorts-projects/{project_id}/draft", headers=auth_headers).json()

    assert set(body) == {"project_id", "last_saved_at", "current_step"}
    assert body["current_step"] is None
    assert body["last_saved_at"] is None


def test_draft_round_trip(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    saved = client.put(
        f"/shorts-projects/{project_id}/draft",
        json={"current_step": "SHOOTING", "client_state": {"last_task_id": 702}},
        headers=auth_headers,
    )

    assert saved.status_code == 200
    assert saved.json()["message"] == "임시저장 되었습니다."
    assert saved.json()["last_saved_at"].endswith("Z")

    body = client.get(f"/shorts-projects/{project_id}/draft", headers=auth_headers).json()
    assert body["current_step"] == "SHOOTING"
    assert body["last_saved_at"] is not None


def test_draft_last_saved_at_is_not_updated_at(
    client: TestClient, auth_headers: dict[str, str], project_id: int, task_id: int
) -> None:
    """태스크 상태를 바꿔도 last_saved_at은 그대로여야 한다 — updated_at과 다른 값이다."""
    client.put(
        f"/shorts-projects/{project_id}/draft",
        json={"current_step": "SHOOTING"},
        headers=auth_headers,
    )
    before = client.get(f"/shorts-projects/{project_id}/draft", headers=auth_headers).json()[
        "last_saved_at"
    ]

    client.patch(f"/tasks/{task_id}", json={"task_status": "DONE"}, headers=auth_headers)

    after = client.get(f"/shorts-projects/{project_id}/draft", headers=auth_headers).json()[
        "last_saved_at"
    ]
    assert after == before


def test_draft_hidden_from_other_user(
    client: TestClient, project_id: int, other_headers: dict[str, str]
) -> None:
    assert (
        client.get(f"/shorts-projects/{project_id}/draft", headers=other_headers).status_code == 404
    )
    assert (
        client.put(
            f"/shorts-projects/{project_id}/draft",
            json={"current_step": "SHOOTING"},
            headers=other_headers,
        ).status_code
        == 404
    )


def test_unsupported_message_mentions_video_not_image(
    client: TestClient, auth_headers: dict[str, str], task_id: int
) -> None:
    """촬영본 업로드인데 "이미지 파일만"이라고 안내하면 안 된다.

    사진(3.3)과 검증 로직을 공유하므로 문구가 섞이기 쉽다.
    """
    body = _upload(
        client, auth_headers, task_id, content=b"x", filename="p.png", content_type="image/png"
    ).json()

    assert "영상" in body["message"]
    assert "이미지" not in body["message"]
