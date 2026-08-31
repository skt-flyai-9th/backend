"""최종 출력·게시자료 로직 (API명세서 15.1) + 가게 단위 완성 숏폼 목록 (15.2)."""

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError
from app.models.shorts_project import ShortsProject
from app.models.sns import SnsPost
from app.models.store import Store
from app.models.video_format import VideoFormat
from app.models.video_output import RenderStatus, VideoOutput
from app.services import ai_client
from app.services import video_edit as edit_service


class EditNotStarted(BadRequestError):
    error_code = "EDIT_NOT_STARTED"
    message = "편집을 먼저 시작해야 최종 출력을 만들 수 있습니다."


# ---------------------------------------------------------------- 15.1 최종 출력


def create_outputs(
    db: Session, store: Store, project: ShortsProject, target_platforms: list[str]
) -> tuple[list[VideoOutput], dict]:
    """플랫폼별 출력물을 확보하고 게시자료를 만든다 (API명세서 15.1 POST).

    **편집(14.1)이 먼저 있어야 한다.** 최종 출력은 편집 결과를 플랫폼 규격으로
    내보내는 단계라, 원본이 없으면 만들 게 없다.

    이미 그 플랫폼 산출물이 있으면 새로 만들지 않고 최신 것을 그대로 쓴다 —
    출력 화면에서 재시도·새로고침으로 여러 번 호출되기 쉬운데, 그때마다 행이
    쌓이면 15.2의 "프로젝트당 최신 1개"가 매번 바뀌어버린다.
    """
    source = _latest_output(db, project)
    if source is None:
        raise EditNotStarted
    source = edit_service.sync_output(db, source)  # AI 쪽 최신 상태로 갱신

    outputs: list[VideoOutput] = []
    for platform in target_platforms:
        existing = _latest_output(db, project, platform)
        if existing is not None:
            outputs.append(existing)
            continue

        # 편집 결과(source)를 그 플랫폼 규격으로 내보낸 산출물. 실제 트랜스코딩은
        # 렌더러가 붙어야 일어나므로 지금은 레시피·상태를 그대로 물려받는다.
        output = VideoOutput(
            shorts_project_id=project.id,
            edit_recipe=source.edit_recipe,
            video_url=source.video_url,
            cover_image_url=source.cover_image_url,
            target_platform=platform,
            resolution=source.resolution,
            has_licensed_audio=source.has_licensed_audio,
            render_status=source.render_status,
        )
        db.add(output)
        outputs.append(output)

    if not ai_client.is_enabled():
        # AI 연동 전: 편집이 완료될 때까지 기다리지 않고 캡션만 미리 만들어
        # 15.1 화면 흐름을 확인할 수 있게 한다(`generate_publish_kit` 독스트링 참고).
        # 연동 후에는 이 분기가 없어지고 publish_kit은 오직 sync_output()이 편집
        # 완료 시 채운 값만 쓴다(`docs/AI_연동_입출력.md` 22번 — 별도 LLM 호출 없음).
        kit = ai_client.generate_publish_kit(store, project)
        project.publish_kit = {
            "title": kit.title,
            "caption": kit.caption,
            "hashtags": kit.hashtags,
            "post_note": kit.post_note,
            "track": kit.track,
        }

    _ensure_publish_kit_contract(db, project)

    # 새 산출물이 없어도 publish_kit이 바뀌었을 수 있으므로 커밋은 항상 필요하다.
    db.commit()
    for output in outputs:
        db.refresh(output)
    return outputs, project.publish_kit


def get_outputs(db: Session, project: ShortsProject) -> tuple[list[VideoOutput], dict | None]:
    """만들어둔 출력물과 게시자료를 돌려준다 (API명세서 15.1 GET).

    렌더링이 비동기라 사용자가 화면을 나갔다 돌아오면 여기로 재조회한다. 그래서
    POST와 같은 필드 구성으로 응답한다(2026-08-21 확정).

    플랫폼별로 **최신 1개**만 준다 — 14.3 수정 요청으로 같은 플랫폼 산출물이
    여러 개 쌓여도 출력 화면에 보여줄 건 마지막 것 하나다.
    """
    rows = db.scalars(
        select(VideoOutput)
        .where(VideoOutput.shorts_project_id == project.id)
        .order_by(VideoOutput.id.desc())
    ).all()

    latest_by_platform: dict[str | None, VideoOutput] = {}
    for row in rows:
        latest_by_platform.setdefault(row.target_platform, row)

    outputs = [edit_service.sync_output(db, row) for row in latest_by_platform.values()]
    outputs.sort(key=lambda row: row.id)
    _ensure_publish_kit_contract(db, project)
    return outputs, project.publish_kit


def _latest_output(
    db: Session, project: ShortsProject, platform: str | None = None
) -> VideoOutput | None:
    statement = select(VideoOutput).where(VideoOutput.shorts_project_id == project.id)
    if platform is not None:
        statement = statement.where(VideoOutput.target_platform == platform)
    return db.scalars(statement.order_by(VideoOutput.id.desc()).limit(1)).first()


def _ensure_publish_kit_contract(db: Session, project: ShortsProject) -> None:
    """마이그레이션 없이, 저장된 옛 계약의 게시자료를 지금 계약으로 보정한다.

    `title`·`hashtags`(5개 이상)·`track.search_keyword`가 계약에 새로 추가되면서
    (2026-08-26), 그 전에 저장된 `publish_kit`에는 이 필드들이 없다. 매번 여기서
    채워 넣어, 옛 데이터를 가진 프로젝트도 15.1 응답이 새 계약을 그대로 만족하게
    한다.
    """
    if not project.publish_kit:
        return
    kit = dict(project.publish_kit)
    caption = str(kit.get("caption") or "").strip()
    if not kit.get("title"):
        if caption:
            first, *rest = caption.splitlines()
            kit["title"] = (project.project_title or first or "게시글")[:80]
            kit["caption"] = _strip_operational_copy("\n".join(rest).strip() or caption) or (
                "영상의 내용을 확인해 보세요."
            )
        else:
            # caption조차 비어 있으면(예상 못 한 옛 데이터) first를 뽑을 데가
            # 없다 — "".splitlines()가 빈 리스트라 언팩이 그대로 터진다.
            kit["title"] = (project.project_title or "게시글")[:80]
            kit["caption"] = "영상의 내용을 확인해 보세요."

    hashtags = [str(value) for value in (kit.get("hashtags") or [])]
    for fallback in ("#숏폼", "#릴스", "#매장소개", "#가게소개", "#동네맛집"):
        if len(hashtags) >= 5:
            break
        if fallback not in hashtags:
            hashtags.append(fallback)
    kit["hashtags"] = hashtags

    # AI팀이 정확한 초 단위를 못 준다고 확인해(2026-08-26) 항상 null로 둔다 —
    # 필드는 지우지 않는다(`schemas.shorts_project.TrackInfo` 독스트링 참고).
    track = dict(kit.get("track") or {})
    track["start_sec"] = None
    track["end_sec"] = None
    if not track.get("title") and not track.get("search_keyword"):
        video_format = (
            db.get(VideoFormat, project.video_format_id)
            if project.video_format_id is not None
            else None
        )
        keyword = _search_keyword(video_format.format_title if video_format else "트렌드 음원")
        track.update(
            {
                "mode": "SUGGESTED",
                "title": None,
                "artist": None,
                "mood": track.get("mood"),
                "search_keyword": keyword,
            }
        )
        kit["post_note"] = f"플랫폼 음원 검색에서 '{keyword}'을 검색해 직접 추가해주세요."
    kit["track"] = track

    if kit != project.publish_kit:
        project.publish_kit = kit
        db.commit()


def _search_keyword(value: str) -> str:
    keyword = value.strip()
    for suffix in ("트랜지션", "챌린지", "릴스", "숏폼", "포맷"):
        keyword = keyword.replace(suffix, " ")
    return (" ".join(keyword.split()) or "트렌드 음원")[:80]


def _strip_operational_copy(value: str) -> str:
    """캡션에 섞여 들어온 "음원은 플랫폼에서 추가하세요" 같은 운영 안내 문구를 잘라낸다."""
    text = value.strip()
    positions = [
        text.find(marker)
        for marker in ("음악은", "음원은", "플랫폼에서", "업로드 후", "게시 시")
        if marker in text
    ]
    return text[: min(positions)].rstrip(" ,.!·") if positions else text


# ---------------------------------------------------------------- 15.2 완성 숏폼 목록


def list_store_shorts(
    db: Session, store: Store, page: int, size: int
) -> tuple[list[tuple[VideoOutput, ShortsProject, int | None, bool]], int]:
    """가게의 완성 숏폼 목록을 돌려준다 (API명세서 15.2).

    마이페이지 그리드용이라 **완성된 것만**(`render_status = COMPLETED`),
    **프로젝트당 최신 1개**만 최신순으로 준다. 4.1 `GET /shorts-projects`가
    제작 중인 프로젝트까지 포함하는 "작업 목록"인 것과 대비되는 "결과물 갤러리"다.
    """
    latest = _latest_completed_ids(store)

    total = db.scalar(select(func.count()).select_from(latest.subquery())) or 0
    if total == 0:
        return [], 0

    rows = db.execute(
        # 길이는 video_outputs에 없어 포맷의 완성 영상 길이를 함께 가져온다.
        # 포맷을 아직 안 고른 프로젝트도 목록에 나와야 하므로 outerjoin이다.
        select(VideoOutput, ShortsProject, VideoFormat.expected_duration_sec)
        .join(ShortsProject, ShortsProject.id == VideoOutput.shorts_project_id)
        .outerjoin(VideoFormat, VideoFormat.id == ShortsProject.video_format_id)
        .where(VideoOutput.id.in_(latest))
        .order_by(VideoOutput.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    posted = _posted_project_ids(db, [project.id for _, project, _ in rows])
    return [
        (output, project, duration, project.id in posted) for output, project, duration in rows
    ], total


def _latest_completed_ids(store: Store) -> Select[tuple[int]]:
    """가게의 프로젝트별 "완성된 마지막 산출물" id 목록.

    `id`가 클수록 나중에 만들어진 행이라(14.3 수정 요청마다 새 행) `MAX(id)`가
    최신본이다. `created_at`은 같은 초에 여러 행이 생기면 순서가 흔들린다.

    **`video_url`도 같이 확인한다**(2026-08-31, FE 리포트로 발견). `render_status`가
    `COMPLETED`여도 `video_url`이 `None`일 수 있다 — AI 결과에 렌더 URL이 비어
    오면 `_persist_rendered_video`가 예외 없이 `(None, None)`을 돌려주고,
    `video_edit.sync_output`은 그래도 상태를 `COMPLETED`로 그대로 커밋한다
    (`app/services/video_edit.py`). 이 필터가 없으면 재생 안 되는 영상이
    "완성"으로 그리드에 섞여 나온다.
    """
    return (
        select(func.max(VideoOutput.id))
        .join(ShortsProject, ShortsProject.id == VideoOutput.shorts_project_id)
        .where(
            ShortsProject.store_id == store.id,
            VideoOutput.render_status == RenderStatus.COMPLETED,
            VideoOutput.video_url.is_not(None),
        )
        .group_by(VideoOutput.shorts_project_id)
    )


def _posted_project_ids(db: Session, project_ids: list[int]) -> set[int]:
    """게시(공유) 이력이 있는 프로젝트 id 집합.

    한 번에 조회한다 — 항목마다 확인하면 페이지당 N+1 쿼리가 된다.

    **산출물이 아니라 프로젝트 기준이다.** 수정 요청으로 새 산출물이 생겨도
    "이 숏폼은 올린 적 있다"는 사실은 그대로이므로, 그리드 배지가 수정할 때마다
    사라지지 않게 한다.

    R16 게시 로직이 붙기 전까지 `sns_posts`는 비어 있어 항상 빈 집합이다.
    """
    if not project_ids:
        return set()

    return set(
        db.scalars(
            select(VideoOutput.shorts_project_id)
            .join(SnsPost, SnsPost.video_output_id == VideoOutput.id)
            .where(VideoOutput.shorts_project_id.in_(project_ids))
        )
    )
