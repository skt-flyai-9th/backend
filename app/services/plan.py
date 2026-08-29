"""가게 맞춤 기획 로직 (API명세서 7.1, 7.2)."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError
from app.models.shooting_task import ShootingTask, TaskStatus
from app.models.shorts_project import ShortsProject
from app.models.store import Store
from app.models.store_menu import StoreMenu
from app.models.storyboard_scene import StoryboardScene
from app.schemas.shorts_project import SceneUpdateRequest, ShootingSummary
from app.services import ai_client
from app.services.video_format import get_format


class SceneNotInProject(BadRequestError):
    error_code = "SCENE_NOT_IN_PROJECT"
    message = "이 프로젝트에 속하지 않은 장면이 포함되어 있습니다."


def generate_plan(db: Session, project: ShortsProject, video_format_id: int) -> ShortsProject:
    """포맷을 프로젝트에 연결하고 촬영 가이드로 콘티·태스크를 만든다 (API명세서 7.1).

    **`video_format_id`를 저장하는 유일한 경로다**(2026-08-23 확정). 4.2 PATCH에는
    이 필드가 없다.

    **콘티와 촬영 태스크를 함께 만든다** — 태스크 생성 API가 따로 없고, 기능명세서
    S08.1.1이 "선택 포맷과 콘티를 분해한다"고 규정한다.

    **재호출하면 기존 장면·태스크를 지우고 새로 넣는다.** 포맷을 바꿔 다시 만들었을 때
    옛 데이터가 남아 섞이면 사장님이 찍지 않아도 될 컷을 찍게 된다.

    2026-08-26: R06(숏폼 Agent) 6.4(추천 수락)도 이 함수를 그대로 재사용한다 —
    AI팀 지침(`docs/AI_연동_입출력.md` 13번, "기존 기획·콘티 생성 방식은 사용하지
    않는다")에 따라 AI 호출을 `generate_plan`(즉석 생성)에서 `get_shooting_guide`
    (템플릿 조회)로 바꿨다. 콘티 내용 자체는 가게별로 다시 만들어지지 않고 템플릿에
    고정돼 있지만, AI팀이 "가게/프로젝트 컨텍스트도 함께 넘겨달라"고 확인해줘서
    (2026-08-26) `store`도 함께 조회해 넘긴다.
    """
    video_format = get_format(db, video_format_id)  # 없는 포맷이면 404
    store = db.get(Store, project.store_id)
    assert store is not None  # 프로젝트가 있으면 가게도 있다(FK)

    menu = db.get(StoreMenu, project.menu_id) if project.menu_id is not None else None
    guide = ai_client.get_shooting_guide(
        video_format,
        store,
        project,
        menu_name=menu.name if menu is not None else None,
    )

    # 기존 장면·태스크 제거 후 재생성. 같은 트랜잭션에서 처리해 중간 상태가 남지 않게 한다.
    # 태스크를 함께 지우지 않으면 옛 포맷의 태스크가 남아 찍지 않아도 될 컷을 찍게 된다.
    db.execute(delete(ShootingTask).where(ShootingTask.shorts_project_id == project.id))
    db.execute(delete(StoryboardScene).where(StoryboardScene.shorts_project_id == project.id))

    scenes = [
        StoryboardScene(
            shorts_project_id=project.id,
            scene_order=scene.scene_order,
            scene_description=scene.scene_description,
            scene_dialogue=scene.scene_dialogue,
            scene_subtitle=scene.scene_subtitle,
            shot_type=scene.shot_type,
            target_duration_sec=scene.target_duration_sec,
        )
        for scene in guide.scenes
    ]
    db.add_all(scenes)
    # 태스크가 장면을 FK로 참조하므로 id를 먼저 확보한다.
    db.flush()

    db.add_all(
        ShootingTask(
            shorts_project_id=project.id,
            scene_id=(
                scenes[task.scene_index].id
                # 하한도 확인한다 — 음수면 파이썬이 마지막 장면으로 조용히
                # 해석해버린다(2026-08-28, 코드리뷰로 발견).
                if task.scene_index is not None and 0 <= task.scene_index < len(scenes)
                else None
            ),
            task_type=task.task_type,
            task_title=task.task_title,
            task_status=TaskStatus.NOT_STARTED,
            display_order=task.display_order,
            guide=task.guide,
        )
        for task in guide.tasks
    )

    project.video_format_id = video_format.id
    project.estimated_shooting_sec = guide.estimated_shooting_sec
    # 템플릿의 고정 메타데이터다 — 사용자가 입력하는 값이 아니다(2026-08-26 AI팀 확인).
    project.required_people = guide.required_people
    project.props = guide.props
    project.shooting_difficulty = guide.difficulty
    db.commit()
    db.refresh(project)
    return project


def list_scenes(db: Session, project: ShortsProject) -> list[StoryboardScene]:
    """장면을 순서대로 돌려준다."""
    return list(
        db.scalars(
            select(StoryboardScene)
            .where(StoryboardScene.shorts_project_id == project.id)
            .order_by(StoryboardScene.scene_order, StoryboardScene.id)
        )
    )


def build_summary(project: ShortsProject) -> ShootingSummary | None:
    """촬영 준비 요약. 7.1을 호출한 적 없으면 None이다.

    DB 컬럼명(`estimated_shooting_sec`)과 API 필드명(`expected_duration_sec`)이
    다른 유일한 지점이다 — 5.1의 동명 필드(완성 영상 길이)와 뜻이 달라 구분했다.
    """
    if project.video_format_id is None:
        return None
    return ShootingSummary(
        expected_duration_sec=project.estimated_shooting_sec,
        required_people=project.required_people,
        props=project.props or [],
        difficulty=project.shooting_difficulty,
    )


def update_scenes(db: Session, project: ShortsProject, payload: SceneUpdateRequest) -> int:
    """여러 장면을 한 번에 수정하고 수정된 개수를 돌려준다 (API명세서 7.2 PATCH).

    **다른 프로젝트의 장면 ID가 섞여 있으면 하나도 반영하지 않고 400이다.** 일부만
    적용하면 프론트는 성공으로 알고 넘어가는데 실제로는 절반만 저장된 상태가 된다.
    """
    requested_ids = [item.id for item in payload.scenes]
    scenes = {
        scene.id: scene
        for scene in db.scalars(
            select(StoryboardScene).where(StoryboardScene.id.in_(requested_ids))
        )
    }

    missing = [
        scene_id
        for scene_id in requested_ids
        if scene_id not in scenes or scenes[scene_id].shorts_project_id != project.id
    ]
    if missing:
        raise SceneNotInProject(f"이 프로젝트에 속하지 않은 장면이 포함되어 있습니다: {missing}")

    updated = 0
    for item in payload.scenes:
        changes = item.model_dump(exclude_unset=True, exclude={"id"})
        if not changes:
            continue
        for field, value in changes.items():
            setattr(scenes[item.id], field, value)
        updated += 1

    db.commit()
    return updated
