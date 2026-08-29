"""AI 자동편집 로직 (API명세서 14.1~14.3).

2026-08-26: AI팀 지침(`docs/AI_연동_입출력.md` 15~21번)에 따라 **비동기(run 생성
+ 폴링) 구조로 재설계**했다. FE가 보는 계약(14.1이 `render_status`를 즉시 돌려주고
14.2가 폴링하는 모양)은 원래도 이 모양이었어서 바뀌지 않는다 — 안쪽에서 AI를
동기 호출 한 번으로 끝내던 것을, run을 만들고 GET마다 상태를 동기화하는 방식으로
바꿨을 뿐이다.
"""

import json
import logging
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BadRequestError, NotFoundError
from app.models.mixins import utcnow
from app.models.shooting_task import ShootingTask
from app.models.shorts_project import ShortsProject, ShortsStatus
from app.models.store import Store
from app.models.storyboard_scene import StoryboardScene
from app.models.user import User
from app.models.video_format import VideoFormat
from app.models.video_output import RenderStatus, VideoOutput
from app.schemas.shorts_project import TimelineItem
from app.services import ai_client
from app.services.media_thumbnail import generate_thumbnail
from app.storage import StorageError, get_storage, to_public_url

logger = logging.getLogger(__name__)

# R14가 "최대 10분 넘게 걸릴 수 있다"던 것의 4배 가까운 여유값이다. 이보다 오래
# PENDING/PROCESSING이면 AI 쪽이 응답 없이 멈춘 것으로 보고 우리 쪽에서 포기한다
# — FE 리포트(2026-08-27)로 발견: AI가 내부적으로 무한 재시도하는 동안 우리 쪽
# 상태는 영원히 PENDING/PROCESSING에 머물러 완료 푸시도 영영 안 가고, 폴링을
# 멈추지 않는 한 계속 AI에 상태를 물어 비용도 쌓인다.
_STUCK_TIMEOUT = timedelta(minutes=40)

# 렌더링 진행률. ⚠️ 실제 진행률이 아니라 상태에서 매핑한 근사값이다.
# 렌더러가 붙으면 실제 값을 받아 이 표를 대체한다.
_PROGRESS_BY_STATUS = {
    RenderStatus.PENDING: 0,
    RenderStatus.PROCESSING: 50,
    RenderStatus.COMPLETED: 100,
    RenderStatus.FAILED: 0,
    RenderStatus.SOURCE_GAP: 0,
}

# AI가 쓰는 상태 문자열 -> 우리 RenderStatus. 모르는 값은 안전하게 PENDING으로 본다.
_AI_STATUS_MAP = {
    "QUEUED": RenderStatus.PENDING,
    "RUNNING": RenderStatus.PROCESSING,
    "COMPLETED": RenderStatus.COMPLETED,
    "FAILED": RenderStatus.FAILED,
    "SOURCE_GAP": RenderStatus.SOURCE_GAP,
}


class OutputNotFound(NotFoundError):
    error_code = "OUTPUT_NOT_FOUND"
    message = "편집 결과를 찾을 수 없습니다."


class TasksIncomplete(BadRequestError):
    error_code = "TASKS_INCOMPLETE"
    message = "아직 촬영하지 않은 태스크가 있어 편집을 시작할 수 없습니다."


def _map_status(ai_status: str) -> RenderStatus:
    return _AI_STATUS_MAP.get(ai_status, RenderStatus.PENDING)


def _build_footage_inputs(db: Session, project: ShortsProject) -> list[ai_client.FootageInput]:
    """촬영본 목록을 AI 요청 형식으로 만든다 (`docs/AI_연동_입출력.md` 16번 `videos[]`).

    포맷과 관계없이 태스크가 연결된 장면 순서를 보낸다.
    """
    rows = db.execute(
        select(ShootingTask, StoryboardScene.scene_order)
        .outerjoin(StoryboardScene, StoryboardScene.id == ShootingTask.scene_id)
        .where(
            ShootingTask.shorts_project_id == project.id,
            ShootingTask.footage_url.is_not(None),
        )
        .order_by(ShootingTask.display_order, ShootingTask.id)
    ).all()
    storage = get_storage()
    footages: list[ai_client.FootageInput] = []
    for task, scene_order in rows:
        footages.append(
            ai_client.FootageInput(
                video_id=f"task_{task.id}",
                footage_url=to_public_url(storage, task.footage_url) or "",
                shooting_scene_order=scene_order or task.display_order,
            )
        )
    return footages


def _find_active_output(
    db: Session, project: ShortsProject, target_platform: str
) -> VideoOutput | None:
    return db.scalar(
        select(VideoOutput)
        .where(
            VideoOutput.shorts_project_id == project.id,
            VideoOutput.target_platform == target_platform,
            VideoOutput.render_status.in_((RenderStatus.PENDING, RenderStatus.PROCESSING)),
        )
        .order_by(VideoOutput.id.desc())
        .limit(1)
    )


def start_edit(db: Session, project: ShortsProject, target_platform: str) -> VideoOutput:
    """편집을 시작한다 (API명세서 14.1).

    **모든 태스크가 촬영본을 가져야 시작할 수 있다**(2026-08-21 확정). 필수/선택을
    구분하지 않는 이유는 건너뛰기·교체 기능이 스코프에서 빠져 "선택 태스크"를
    구분해도 할 수 있는 게 없기 때문이다.

    검증 기준은 `task_status`가 아니라 **`footage_url` 존재 여부**다 — 8.2로 상태만
    `DONE`으로 바꿔도 촬영본이 없으면 편집할 재료가 없다.

    **이미 진행 중인(`PENDING`/`PROCESSING`) 편집이 있으면 새로 걸지 않고 그걸
    그대로 돌려준다**(2026-08-26, FE 리포트로 발견). 편집 화면(RenderScreen)이
    재진입할 때마다 이 API를 다시 부르는데, 매번 새 렌더를 걸면 AI 쪽에 같은
    프로젝트의 렌더가 중복으로 쌓이고 어느 게 "진짜" 결과인지도 불분명해진다.
    이미 끝난(`COMPLETED`/`FAILED`) 편집을 다시 시작하는 건 재시도로 보고 그대로
    새로 만든다.
    """
    db.execute(
        select(ShortsProject.id).where(ShortsProject.id == project.id).with_for_update()
    ).scalar_one()
    existing = _find_active_output(db, project, target_platform)
    if existing is not None:
        return sync_output(db, existing)

    _require_all_footage(db, project)
    store = db.get(Store, project.store_id)
    assert store is not None  # 프로젝트가 있으면 가게도 있다(FK)
    video_format = db.get(VideoFormat, project.video_format_id)
    assert video_format is not None

    run = ai_client.start_editing_run(
        store,
        project,
        video_format,
        _build_footage_inputs(db, project),
    )
    output = VideoOutput(
        shorts_project_id=project.id,
        ai_run_id=run.run_id,
        target_platform=target_platform,
        render_status=_map_status(run.status),
    )
    db.add(output)
    db.commit()
    db.refresh(output)
    return output


def _require_all_footage(db: Session, project: ShortsProject) -> None:
    tasks = list(
        db.scalars(
            select(ShootingTask)
            .where(ShootingTask.shorts_project_id == project.id)
            .order_by(ShootingTask.display_order, ShootingTask.id)
        )
    )
    if not tasks:
        # 7.1을 호출한 적 없어 태스크 자체가 없는 경우. 편집할 재료가 없다.
        raise TasksIncomplete(
            "촬영 태스크가 없습니다. 기획을 먼저 생성해주세요.", extra={"incomplete_tasks": []}
        )

    incomplete = [task for task in tasks if not task.footage_url]
    if incomplete:
        # 어떤 태스크가 비었는지 알려줘야 프론트가 태스크 보드로 안내할 수 있다.
        raise TasksIncomplete(
            extra={
                "incomplete_tasks": [
                    {"id": task.id, "task_title": task.task_title} for task in incomplete
                ]
            }
        )


def sync_output(db: Session, output: VideoOutput) -> VideoOutput:
    """AI 쪽 편집 실행 상태를 우리 산출물에 반영한다.

    14.2(`GET .../edit/result`)를 부를 때마다, 그리고 15.1이 최신 산출물을
    참조하기 직전에 호출한다 — 렌더링이 비동기라 우리가 먼저 알 방법이 없고,
    폴링 요청이 올 때 AI 쪽 상태를 확인하는 수밖에 없다("poll-through").

    이미 끝난 상태(`COMPLETED`/`FAILED`/`SOURCE_GAP`)거나 `ai_run_id`가 없으면
    (레거시 데이터) 그대로 돌려준다 — 끝난 편집은 다시 진행되지 않는다.

    **`stage`/`progress`는 상태 전환 여부와 무관하게 매번 갱신한다**(2026-08-27
    추가). 예전엔 상태(`PROCESSING` 등)가 안 바뀌면 아무것도 안 하고 돌아갔는데,
    그러면 같은 `PROCESSING` 안에서 실제 진행이 20%→80%로 올라가도 화면엔 절대
    안 보였다. `error_message`는 AI가 실패 사유를 실어서 주는데도(`docs/AI_연동_
    입출력.md` 17번) 지금까지 버리고 있었다 — 실서버 편집 실패(project 56/50)를
    조사하다가 발견, DB 어디에도 실패 이유가 안 남아 있어 진단이 안 됐다.

    **`_STUCK_TIMEOUT`을 넘기면 AI를 다시 묻지 않고 바로 FAILED로 끊는다**
    (2026-08-28 추가). AI를 호출하기 전에 먼저 검사하므로, 멈춘 편집에 대해
    더는 폴링 요청마다 AI를 부르지 않는다.

    **렌더가 COMPLETED가 되면 `project.shorts_status`도 `COMPLETED`로 바꾼다**
    (2026-08-28 추가, FE 리포트). 이전엔 `산출물`만 갱신하고 프로젝트 상태는
    안 건드려서, 완성된 프로젝트가 "만들던 영상" 목록(DRAFT/IN_PROGRESS)에
    계속 남아 있었다. `IN_PROGRESS` 쪽 전이는 스코프 밖이다 — 지금 요청받은
    건 "렌더 완료 시 COMPLETED 전이"뿐이라, 수정 요청(14.3)으로 재렌더가 도는
    동안 상태를 되돌리는 것까지는 하지 않는다.
    """
    still_in_progress = output.render_status in (RenderStatus.PENDING, RenderStatus.PROCESSING)
    if not still_in_progress or not output.ai_run_id:
        return output

    if utcnow() - output.created_at > _STUCK_TIMEOUT:
        output.render_status = RenderStatus.FAILED
        output.error_message = "편집이 시간 내에 완료되지 않았습니다. 다시 시도해주세요."
        db.commit()
        db.refresh(output)
        return output

    run = ai_client.get_editing_run(output.ai_run_id)
    new_status = _map_status(run.status)
    output.render_stage = run.stage
    output.render_progress = run.progress
    if new_status is RenderStatus.FAILED:
        output.error_message = run.error_message
    output.queue_position = run.queue_position
    output.estimated_wait_sec = run.estimated_wait_sec
    output.stage_elapsed_sec = run.stage_elapsed_sec
    if new_status == output.render_status:
        db.commit()
        db.refresh(output)
        return output

    output.render_status = new_status
    if new_status is RenderStatus.COMPLETED:
        result = ai_client.get_editing_run_result(output.ai_run_id)
        output.edit_recipe = json.dumps(result.recipe or {}, ensure_ascii=False)
        output.video_url, output.cover_image_url = _persist_rendered_video(
            output.shorts_project_id, result.video_url
        )
        output.resolution = result.resolution
        output.warnings = result.warnings or []
        output.missing_scene_roles = result.missing_scene_roles or []
        output.available_options = result.available_options or []
        # 배경음악을 직접 입히지 않기로 확정돼 항상 false다(2026-08-24 결정,
        # `docs/AI_연동_입출력.md` 19번).
        output.has_licensed_audio = False
        project = db.get(ShortsProject, output.shorts_project_id)
        assert project is not None
        project.shorts_status = ShortsStatus.COMPLETED
        if result.publishing is not None:
            project.publish_kit = {
                "title": result.publishing.title,
                "caption": result.publishing.caption,
                "hashtags": result.publishing.hashtags,
                "post_note": result.publishing.post_note,
                "track": result.publishing.track,
            }
    elif new_status is RenderStatus.SOURCE_GAP:
        result = ai_client.get_editing_run_result(output.ai_run_id)
        output.missing_scene_roles = result.missing_scene_roles or []
        output.available_options = result.available_options or []
        output.warnings = result.warnings or []

    db.commit()
    db.refresh(output)
    return output


def _persist_rendered_video(
    project_id: int, source_url: str | None
) -> tuple[str | None, str | None]:
    """인증된 AI 결과를 저장하고 최종 영상에서 백엔드 커버를 생성한다."""
    if not source_url:
        return None, None

    _validate_renderer_url(source_url)
    identifier = uuid.uuid4().hex
    video_key = f"projects/{project_id}/outputs/{identifier}.mp4"
    cover_key = f"projects/{project_id}/outputs/{identifier}.jpg"
    source_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as stream:
            source_path = Path(stream.name)
            with httpx.stream(
                "GET",
                source_url,
                headers={"X-Internal-API-Key": settings.AI_SERVER_API_KEY},
                timeout=httpx.Timeout(180.0, connect=5.0),
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "video/mp4").split(";", 1)[0]
                for chunk in response.iter_bytes():
                    stream.write(chunk)
        storage = get_storage()
        with source_path.open("rb") as video_stream:
            storage.save(video_key, video_stream, content_type)
        generated_cover = generate_thumbnail(storage, source_path, cover_key)
    except (httpx.HTTPError, OSError, StorageError) as exc:
        raise ai_client.AIServiceUnavailable from exc
    finally:
        if source_path is not None:
            source_path.unlink(missing_ok=True)
    return video_key, generated_cover


def _validate_renderer_url(source_url: str) -> None:
    source = urlsplit(source_url)
    ai_server = urlsplit(settings.AI_SERVER_URL)
    allowed_hosts = set(settings.ai_renderer_allowed_host_set)
    if ai_server.hostname:
        allowed_hosts.add(ai_server.hostname.lower())
    if source.scheme not in {"http", "https"} or not source.hostname:
        raise ai_client.AIServiceUnavailable
    if source.hostname.lower() not in allowed_hosts:
        logger.error("허용되지 않은 AI 렌더러 호스트: %s", source.hostname)
        raise ai_client.AIServiceUnavailable


def latest_output(db: Session, project: ShortsProject) -> VideoOutput:
    """가장 최근 산출물. 프로젝트당 여러 개(플랫폼별·수정 이력)가 쌓인다."""
    output = db.scalar(
        select(VideoOutput)
        .where(VideoOutput.shorts_project_id == project.id)
        .order_by(VideoOutput.created_at.desc(), VideoOutput.id.desc())
        .limit(1)
    )
    if output is None:
        raise OutputNotFound
    return sync_output(db, output)


def progress_percent(output: VideoOutput) -> int:
    """렌더링 진행률.

    **AI가 준 실제 값(`render_progress`)이 있으면 그걸 쓴다**(2026-08-27부터 —
    AI 응답에 이미 실려 있었는데 지금까지 안 읽고 있었다). placeholder 모드처럼
    AI가 진행률을 안 주는 경우에만 상태 기반 근사값(`PENDING`=0, `PROCESSING`=50,
    `COMPLETED`=100)으로 대체한다.
    """
    if output.render_progress is not None:
        return output.render_progress
    return _PROGRESS_BY_STATUS.get(RenderStatus(output.render_status), 0)


def build_timeline(db: Session, project: ShortsProject) -> list[TimelineItem]:
    """타임라인 요약을 콘티에서 만든다.

    `effect`(전환 효과)는 AI 편집 레시피에서 나오는 값이라 연동 전까지 `null`이다.
    지어내면 실제로 적용되지 않은 효과가 화면에 표시된다.
    """
    scenes = db.scalars(
        select(StoryboardScene)
        .where(StoryboardScene.shorts_project_id == project.id)
        .order_by(StoryboardScene.scene_order, StoryboardScene.id)
    )
    return [
        TimelineItem(
            scene_order=scene.scene_order,
            duration_sec=scene.target_duration_sec,
            effect=None,
        )
        for scene in scenes
    ]


def get_owned_output(db: Session, owner: User, output_id: int) -> VideoOutput:
    """본인 소유 산출물. 산출물 → 프로젝트 → 가게 → 사용자로 거슬러 확인한다."""
    output = db.get(VideoOutput, output_id)
    if output is None:
        raise OutputNotFound

    project = db.get(ShortsProject, output.shorts_project_id)
    store = db.get(Store, project.store_id) if project else None
    if store is None or store.user_id != owner.id:
        raise OutputNotFound
    return output


def revise(db: Session, output: VideoOutput, request_type: str, action: str) -> VideoOutput:
    """편집 수정을 요청한다 (API명세서 14.3).

    **기존 산출물을 고치지 않고 새 행을 만든다** — ERD의 `created_at` 코멘트가
    "수정 요청마다 새 행이 쌓여 자연스럽게 버전 이력이 됨"이다. 이전 버전으로
    돌아갈 수 있고, 어떤 지시로 만들어졌는지도 레시피에 남는다.

    AI 쪽도 **새 run**을 만든다(`docs/AI_연동_입출력.md` 20번) — 기존 EditRecipe는
    immutable하게 유지된다.
    """
    del request_type  # AI 연동 시 프롬프트 구성에 사용한다

    project = db.get(ShortsProject, output.shorts_project_id)
    assert project is not None
    run = ai_client.request_revision(
        output.ai_run_id or "",
        action,
        _build_footage_inputs(db, project),
    )
    revised = VideoOutput(
        shorts_project_id=output.shorts_project_id,
        ai_run_id=run.run_id,
        target_platform=output.target_platform,
        render_status=_map_status(run.status),
    )
    db.add(revised)
    db.commit()
    db.refresh(revised)
    return revised


def revision_number(db: Session, output: VideoOutput) -> int:
    """프로젝트 내 산출물 순번(1부터).

    저장 컬럼을 만들지 않고 계산한다 — 산출물이 시간순으로 쌓이므로 순서가 곧
    버전이다. 컬럼을 두면 행 삭제 시 어긋날 수 있다.
    """
    ids = list(
        db.scalars(
            select(VideoOutput.id)
            .where(VideoOutput.shorts_project_id == output.shorts_project_id)
            .order_by(VideoOutput.created_at, VideoOutput.id)
        )
    )
    return ids.index(output.id) + 1
