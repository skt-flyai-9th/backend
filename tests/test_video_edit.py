"""AI 자동편집 테스트 (API명세서 14.1 편집시작 / 14.2 결과조회 / 14.3 수정요청)."""

import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.shooting_task import ShootingTask
from app.models.shorts_project import ShortsProject
from app.models.video_format import VideoFormat
from app.models.video_output import VideoOutput
from app.services import video_edit

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32

STORE_BODY: dict[str, Any] = {
    "name": "행복분식",
    "category": "분식",
    "address": "서울 강남구 테헤란로 1길 10",
}


def test_renderer_download_uses_internal_auth_and_persists_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    saved: dict[str, bytes] = {}

    class FakeResponse:
        headers = {"content-type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield MP4_BYTES

    class FakeStorage:
        def save(self, key, stream, content_type=None):
            del content_type
            saved[key] = stream.read()
            return key

    def fake_stream(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://renderer.internal:8000")
    monkeypatch.setattr(settings, "AI_SERVER_API_KEY", "shared-secret")
    monkeypatch.setattr(video_edit.httpx, "stream", fake_stream)
    monkeypatch.setattr(video_edit, "get_storage", lambda: FakeStorage())
    monkeypatch.setattr(
        video_edit,
        "generate_thumbnail",
        lambda storage, source_path, cover_key: cover_key,
    )

    video_key, cover_key = video_edit._persist_rendered_video(
        10, "http://renderer.internal:8080/files/result.mp4"
    )

    assert saved[video_key] == MP4_BYTES
    assert cover_key.endswith(".jpg")
    assert captured["headers"] == {"X-Internal-API-Key": "shared-secret"}


def test_build_footage_inputs_falls_back_to_task_order_without_scene(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = ShortsProject(id=1, store_id=1)
    db_session.add(project)
    db_session.flush()
    task = ShootingTask(
        shorts_project_id=project.id,
        scene_id=None,
        task_title="대표 메뉴",
        display_order=2,
        footage_url="footage/info.mp4",
    )
    db_session.add(task)
    db_session.commit()
    monkeypatch.setattr(video_edit, "get_storage", lambda: object())
    monkeypatch.setattr(video_edit, "to_public_url", lambda storage, key: f"https://cdn/{key}")

    result = video_edit._build_footage_inputs(db_session, project)

    assert len(result) == 1
    assert result[0].shooting_scene_order == 2


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
def project_id(client: TestClient, auth_headers: dict[str, str], video_format: VideoFormat) -> int:
    """기획까지 끝나 태스크가 만들어진 프로젝트."""
    store_id = client.post("/stores", json=STORE_BODY, headers=auth_headers).json()["id"]
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


def _upload_all(client: TestClient, headers: dict[str, str], project_id: int) -> None:
    """모든 태스크에 촬영본을 올린다."""
    tasks = client.get(f"/shorts-projects/{project_id}/tasks", headers=headers).json()["tasks"]
    for task in tasks:
        client.post(
            f"/tasks/{task['id']}/footage",
            files={"file": ("take.mp4", io.BytesIO(MP4_BYTES), "video/mp4")},
            data={"footage_type": "VIDEO"},
            headers=headers,
        )


def _start_edit(client: TestClient, headers: dict[str, str], project_id: int) -> Any:
    return client.post(
        f"/shorts-projects/{project_id}/edit",
        json={"target_platform": "INSTAGRAM"},
        headers=headers,
    )


# ---------------------------------------------------------------- 14.1 편집 시작


def test_edit_blocked_when_tasks_incomplete(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """모든 태스크에 촬영본이 있어야 시작할 수 있다 (2026-08-21 확정)."""
    response = _start_edit(client, auth_headers, project_id)

    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "TASKS_INCOMPLETE"
    # 어떤 태스크가 남았는지 알려줘야 프론트가 보드로 안내할 수 있다
    assert len(body["incomplete_tasks"]) == 4
    assert set(body["incomplete_tasks"][0]) == {"id", "task_title"}


def test_edit_blocked_when_one_task_missing(
    client: TestClient, auth_headers: dict[str, str], project_id: int, db_session: Session
) -> None:
    """하나만 비어도 막힌다. 그 하나만 응답에 담긴다."""
    _upload_all(client, auth_headers, project_id)

    task = db_session.scalars(
        db_session.query(ShootingTask)
        .filter(ShootingTask.shorts_project_id == project_id)
        .statement
    ).first()
    assert task is not None
    task.footage_url = None
    db_session.commit()

    body = _start_edit(client, auth_headers, project_id).json()

    assert body["error_code"] == "TASKS_INCOMPLETE"
    assert [item["id"] for item in body["incomplete_tasks"]] == [task.id]


def test_edit_checks_footage_not_task_status(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """검증 기준은 task_status가 아니라 footage_url이다.

    8.2로 상태만 DONE으로 바꿔도 촬영본이 없으면 편집할 재료가 없다.
    """
    tasks = client.get(f"/shorts-projects/{project_id}/tasks", headers=auth_headers).json()["tasks"]
    for task in tasks:
        client.patch(f"/tasks/{task['id']}", json={"task_status": "DONE"}, headers=auth_headers)

    response = _start_edit(client, auth_headers, project_id)

    assert response.status_code == 400
    assert response.json()["error_code"] == "TASKS_INCOMPLETE"


def test_edit_starts_when_all_footage_uploaded(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    _upload_all(client, auth_headers, project_id)

    response = _start_edit(client, auth_headers, project_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"video_output_id", "render_status"}
    assert body["render_status"] == "PENDING"


def test_edit_reentry_does_not_duplicate_in_progress_render(
    client: TestClient, auth_headers: dict[str, str], project_id: int, db_session: Session
) -> None:
    """편집 화면 재진입으로 14.1이 다시 불려도 진행 중인 편집을 그대로 돌려준다.

    실제로 겪은 문제(2026-08-26, FE 리포트): RenderScreen이 마운트될 때마다
    14.1을 다시 호출하는데, 기존 코드는 매번 새 VideoOutput을 만들고 AI에
    새 렌더를 또 걸었다. 그러면 같은 프로젝트에 렌더가 중복으로 쌓인다.
    """
    _upload_all(client, auth_headers, project_id)

    first = _start_edit(client, auth_headers, project_id).json()
    second = _start_edit(client, auth_headers, project_id).json()

    assert second["video_output_id"] == first["video_output_id"]
    rows = list(db_session.scalars(select(VideoOutput)))
    assert len(rows) == 1


def test_edit_blocked_without_plan(client: TestClient, auth_headers: dict[str, str]) -> None:
    """7.1을 호출한 적 없으면 태스크 자체가 없다 — 편집할 재료가 없다."""
    store_id = client.post("/stores", json=STORE_BODY, headers=auth_headers).json()["id"]
    project_id = client.post(
        "/shorts-projects",
        json={"store_id": store_id, "promotion_purpose": "메뉴소개"},
        headers=auth_headers,
    ).json()["id"]

    body = _start_edit(client, auth_headers, project_id).json()

    assert body["error_code"] == "TASKS_INCOMPLETE"
    assert body["incomplete_tasks"] == []


def test_edit_hidden_from_other_user(
    client: TestClient, project_id: int, other_headers: dict[str, str]
) -> None:
    assert _start_edit(client, other_headers, project_id).status_code == 404


def test_edit_requires_authentication(client: TestClient, project_id: int) -> None:
    response = client.post(
        f"/shorts-projects/{project_id}/edit", json={"target_platform": "INSTAGRAM"}
    )

    assert response.status_code == 401


# ---------------------------------------------------------------- 14.2 결과 조회


def test_edit_result_returns_spec_fields(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    _upload_all(client, auth_headers, project_id)
    _start_edit(client, auth_headers, project_id)

    response = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "video_output_id",
        "render_status",
        "progress_percent",
        "stage",
        "queue_position",
        "estimated_wait_sec",
        "stage_elapsed_sec",
        "preview_video_url",
        "timeline_summary",
        "missing_scene_roles",
        "available_options",
        "error_message",
        "warnings",
    }
    assert body["progress_percent"] == 0  # PENDING
    assert body["missing_scene_roles"] is None  # SOURCE_GAP 전용, 평소엔 null
    assert body["available_options"] is None
    assert body["error_message"] is None  # FAILED 전용, 평소엔 null
    assert set(body["timeline_summary"][0]) == {"scene_order", "duration_sec", "effect"}


def test_timeline_comes_from_storyboard(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """타임라인은 콘티에서 파생한다. effect는 AI 몫이라 연동 전까지 null이다."""
    _upload_all(client, auth_headers, project_id)
    _start_edit(client, auth_headers, project_id)

    scenes = client.get(f"/shorts-projects/{project_id}/scenes", headers=auth_headers).json()[
        "scenes"
    ]
    timeline = client.get(
        f"/shorts-projects/{project_id}/edit/result", headers=auth_headers
    ).json()["timeline_summary"]

    assert [item["scene_order"] for item in timeline] == [s["scene_order"] for s in scenes]
    assert all(item["effect"] is None for item in timeline)


def test_edit_result_404_before_edit(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    response = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "OUTPUT_NOT_FOUND"


# ---------------------------------------------------------------- 14.3 수정 요청


def test_revise_creates_new_version(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """기존 산출물을 고치지 않고 새 행을 만든다 (ERD 코멘트: 버전 이력)."""
    _upload_all(client, auth_headers, project_id)
    first_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]

    response = client.post(
        f"/video-outputs/{first_id}/revise",
        json={"request_type": "quick_button", "action": "자막 크게"},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"video_output_id", "render_status", "revision_id"}
    assert body["video_output_id"] != first_id  # 새 행이다
    assert body["render_status"] == "PROCESSING"
    assert body["revision_id"] == 2  # 첫 산출물이 1


def test_revision_id_increases(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    _upload_all(client, auth_headers, project_id)
    output_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]

    for expected in (2, 3, 4):
        body = client.post(
            f"/video-outputs/{output_id}/revise",
            json={"request_type": "natural_language", "action": "더 빠르게"},
            headers=auth_headers,
        ).json()
        assert body["revision_id"] == expected
        output_id = body["video_output_id"]


def test_result_returns_latest_version(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    """산출물이 쌓이면 14.2는 가장 최근 것을 준다."""
    _upload_all(client, auth_headers, project_id)
    first_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]
    revised_id = client.post(
        f"/video-outputs/{first_id}/revise",
        json={"request_type": "quick_button", "action": "자막 크게"},
        headers=auth_headers,
    ).json()["video_output_id"]

    result = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers).json()

    assert result["video_output_id"] == revised_id
    assert result["progress_percent"] == 50  # PROCESSING


def test_result_reflects_real_ai_progress(
    client: TestClient,
    auth_headers: dict[str, str],
    project_id: int,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI가 실제 진행률을 주면 상태 기반 근사값(50) 대신 그 값을 써야 한다 (2026-08-27)."""
    from app.services import ai_client

    _upload_all(client, auth_headers, project_id)
    output_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]

    monkeypatch.setattr(
        ai_client,
        "get_editing_run",
        lambda run_id: ai_client.EditingRun(
            run_id=run_id, status="RUNNING", stage="RENDERING", progress=73
        ),
    )

    result = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers).json()

    assert result["progress_percent"] == 73
    assert result["stage"] == "RENDERING"

    output = db_session.get(VideoOutput, output_id)
    assert output.render_progress == 73
    assert output.render_stage == "RENDERING"


def test_result_updates_progress_within_same_status(
    client: TestClient,
    auth_headers: dict[str, str],
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """상태(RUNNING)가 안 바뀌어도, 그 안의 진행률 변화는 매 폴링마다 반영돼야 한다."""
    from app.services import ai_client

    _upload_all(client, auth_headers, project_id)
    _start_edit(client, auth_headers, project_id)

    monkeypatch.setattr(
        ai_client,
        "get_editing_run",
        lambda run_id: ai_client.EditingRun(run_id=run_id, status="RUNNING", progress=20),
    )
    first = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers).json()

    monkeypatch.setattr(
        ai_client,
        "get_editing_run",
        lambda run_id: ai_client.EditingRun(run_id=run_id, status="RUNNING", progress=85),
    )
    second = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers).json()

    assert first["progress_percent"] == 20
    assert second["progress_percent"] == 85


def test_result_stores_error_message_on_failure(
    client: TestClient,
    auth_headers: dict[str, str],
    project_id: int,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED가 되면 AI가 준 실패 사유를 저장해서 조회할 수 있어야 한다 (2026-08-27).

    실제 장애(project 56/50)를 진단하다가, AI가 응답에 실어주는 `error_message`를
    지금까지 통째로 버리고 있어 DB 어디에도 실패 이유가 안 남는 걸 발견했다.
    """
    from app.services import ai_client

    _upload_all(client, auth_headers, project_id)
    output_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]

    monkeypatch.setattr(
        ai_client,
        "get_editing_run",
        lambda run_id: ai_client.EditingRun(
            run_id=run_id,
            status="FAILED",
            stage="FAILED",
            progress=80,
            error_message="RendererError: recipe 검증 실패",
        ),
    )

    result = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers).json()

    assert result["render_status"] == "FAILED"
    assert result["error_message"] == "RendererError: recipe 검증 실패"

    output = db_session.get(VideoOutput, output_id)
    assert output.error_message == "RendererError: recipe 검증 실패"


def test_render_completion_marks_project_completed(
    client: TestClient,
    auth_headers: dict[str, str],
    project_id: int,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """렌더가 끝나면 프로젝트도 COMPLETED가 돼야 "만들던 영상" 목록에서 빠진다.

    FE 리포트(2026-08-28): 렌더는 끝나도 `shorts_status`가 안 바뀌어 완성된
    프로젝트가 계속 진행 중 목록에 남아 있었다.
    """
    from app.services import ai_client

    _upload_all(client, auth_headers, project_id)
    _start_edit(client, auth_headers, project_id)

    monkeypatch.setattr(
        ai_client,
        "get_editing_run",
        lambda run_id: ai_client.EditingRun(run_id=run_id, status="COMPLETED"),
    )
    monkeypatch.setattr(
        ai_client, "get_editing_run_result", lambda run_id: ai_client.EditingRunResult()
    )

    client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers)

    project = db_session.get(ShortsProject, project_id)
    assert project.shorts_status == "COMPLETED"


def test_stuck_edit_times_out_without_calling_ai(
    client: TestClient,
    auth_headers: dict[str, str],
    project_id: int,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """40분 넘게 PENDING/PROCESSING이면 AI를 더 묻지 않고 FAILED로 끊는다 (2026-08-28).

    AI가 내부적으로 무한 재시도하는 동안 우리 쪽 상태가 영원히 멈춰 있으면 완료
    푸시도 안 가고, 폴링마다 AI를 불러 비용만 쌓인다(FE 리포트).
    """
    from datetime import timedelta

    from app.models.mixins import utcnow
    from app.services import ai_client

    _upload_all(client, auth_headers, project_id)
    output_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]

    output = db_session.get(VideoOutput, output_id)
    output.created_at = utcnow() - timedelta(minutes=41)
    db_session.commit()

    def _fail_if_called(run_id: str) -> "ai_client.EditingRun":
        raise AssertionError("타임아웃 처리된 편집은 AI를 다시 묻지 않아야 한다")

    monkeypatch.setattr(ai_client, "get_editing_run", _fail_if_called)

    result = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers).json()

    assert result["render_status"] == "FAILED"
    assert "시간 내" in result["error_message"]


def test_edit_within_timeout_still_polls_ai(
    client: TestClient,
    auth_headers: dict[str, str],
    project_id: int,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """타임아웃 전에는 평소처럼 AI 상태를 그대로 반영해야 한다(회귀 방지)."""
    from datetime import timedelta

    from app.models.mixins import utcnow
    from app.services import ai_client

    _upload_all(client, auth_headers, project_id)
    output_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]

    output = db_session.get(VideoOutput, output_id)
    output.created_at = utcnow() - timedelta(minutes=39)
    db_session.commit()

    monkeypatch.setattr(
        ai_client,
        "get_editing_run",
        lambda run_id: ai_client.EditingRun(run_id=run_id, status="RUNNING", progress=50),
    )

    result = client.get(f"/shorts-projects/{project_id}/edit/result", headers=auth_headers).json()

    assert result["render_status"] == "PROCESSING"
    assert result["progress_percent"] == 50


def test_revise_rejects_unknown_request_type(
    client: TestClient, auth_headers: dict[str, str], project_id: int
) -> None:
    _upload_all(client, auth_headers, project_id)
    output_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]

    response = client.post(
        f"/video-outputs/{output_id}/revise",
        json={"request_type": "없는타입", "action": "x"},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_revise_hidden_from_other_user(
    client: TestClient,
    auth_headers: dict[str, str],
    project_id: int,
    other_headers: dict[str, str],
) -> None:
    """산출물에는 사용자 정보가 없다 — 프로젝트·가게를 거슬러 확인해야 한다."""
    _upload_all(client, auth_headers, project_id)
    output_id = _start_edit(client, auth_headers, project_id).json()["video_output_id"]

    response = client.post(
        f"/video-outputs/{output_id}/revise",
        json={"request_type": "quick_button", "action": "자막 크게"},
        headers=other_headers,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "OUTPUT_NOT_FOUND"


def test_revise_unknown_output_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/video-outputs/999999/revise",
        json={"request_type": "quick_button", "action": "자막 크게"},
        headers=auth_headers,
    )

    assert response.status_code == 404
