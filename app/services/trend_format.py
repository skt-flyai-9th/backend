"""AI 트렌드 클러스터를 `video_formats`에 반영한다.

`scripts/seed_video_formats.py`가 예고한 자리다 — "실제 포맷 발굴과 랭킹은 AI 서버가
담당하며, 연동되면 AI가 내려준 목록이 같은 방식으로 쌓인다". 그 연동이 여기다.

**왜 필요한가.** 지금 `video_formats`에 쌓이는 행은 R06 추천을 수락할 때 생기는
`internal://editing-template/{id}/v{version}` 뿐이다. 그건 AI 서버 내부 자산 주소라
앱에서 썸네일도 못 만들고 재생도 안 된다 — 5.1 피드가 요구하는 "따라 만들 원본
영상"과 성격이 다르다. 트렌드 클러스터가 그 원본을 갖고 있다.

**챌린지 하나당 대표 행을 하나만 유지한다.** 같은 실제 챌린지가 R06 추천 경로(먼저
`internal://` 자산 주소로 적재됨)와 트렌드 동기화 경로(실제 유튜브 URL을 가짐) 양쪽에서
각각 행을 만드는 경우가 있다 — 이 둘을 별개로 두면 (a) `editing_template_id`+`version`에
걸린 UNIQUE 제약을 두 행이 나눠 가지려다 커밋이 실패하거나, (b) 운 좋게 커밋되더라도
실제 유튜브 URL은 한쪽 행에, 활성화(`is_active`)는 다른 쪽 행에 떨어져 화면엔 재생 안 되는
`internal://` 카드가 활성 상태로 노출된다(2026-08-26 실서버에서 둘 다 실제로 겪음).
그래서 매 동기화마다 "이 챌린지를 대표할 행"을 하나만 골라 그 행에만 전체 정보(제목,
실제 URL, 템플릿 연결, 활성화 여부)를 채우고, 경합하는 다른 행은 은퇴시킨다.

**서로 다른 챌린지가 같은 대표 영상 URL을 공유할 수 있다**(2026-08-28, AI팀 확인 —
의도된 동작. 예: "가게 홍보 버전"과 "챌린지 버전"이 같은 예시 클립을 씀). 그래서
`reference_url`은 더 이상 챌린지 식별에 쓰지 않고(UNIQUE 제약도 뺐다), 챌린지
정체성은 오직 `editing_template_id`+`version` 또는 `trend_challenge_id`로만
판단한다 — 실서버에서 이 가정이 깨져 동기화 전체가 UNIQUE 제약 위반으로 실패하는
사고를 겪은 뒤 정정했다.
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.video_format import VideoFormat
from app.services import ai_client


def _apply_ai_metadata(video_format: VideoFormat, challenge: ai_client.TrendChallenge) -> None:
    """Apply only values the AI actually supplied; never erase curated data with null."""

    for field in (
        "format_type",
        "expected_duration_sec",
        "shooting_difficulty",
        "requires_face",
    ):
        value = getattr(challenge, field)
        if value is not None:
            setattr(video_format, field, value)


def _select_representative_row(
    db: Session, challenge: ai_client.TrendChallenge
) -> VideoFormat | None:
    """이 챌린지를 대표할 기존 행을 찾는다. 우선순위대로 시도한다.

    1. 같은 (editing_template_id, version) — R06 추천이 먼저 적재해둔 행일 수 있다.
       템플릿 연결이 이미 돼 있는 행이 있으면 그걸 우선한다(정보를 더 많이 갖고 있다).
    2. 같은 trend_challenge_id — 이전 동기화가 만든 행.

    **`reference_url`로는 찾지 않는다**(2026-08-28 결정) — AI팀 확인: 서로 다른
    챌린지 둘이 같은 대표 영상을 의도적으로 공유할 수 있다(예: "가게 홍보 버전"과
    "챌린지 버전"이 같은 예시 클립을 씀). `reference_url`이 같다고 "같은 챌린지"로
    보면 둘 중 하나가 다른 하나로 잘못 병합된다 — 챌린지 정체성은 오직
    `editing_template_id`+`version` 또는 `trend_challenge_id`로만 판단한다.
    """
    template_id = challenge.editing_template_id
    version = challenge.editing_template_version
    if template_id is not None and version is not None:
        by_template = db.scalar(
            select(VideoFormat).where(
                VideoFormat.editing_template_id == template_id,
                VideoFormat.editing_template_version == version,
            )
        )
        if by_template is not None:
            return by_template

    return db.scalar(select(VideoFormat).where(VideoFormat.trend_challenge_id == challenge.id))


def _retire_conflicting_rows(
    db: Session, keep: VideoFormat, challenge: ai_client.TrendChallenge
) -> None:
    """대표 행으로 안 뽑힌 다른 행이 같은 challenge_id/템플릿을 들고 있으면 비운다.

    `trend_challenge_id`엔 UNIQUE 제약이 있어, 대표 행에 값을 쓰기 전에 경합하는
    값을 먼저 비워야 나중에 값이 겹치지 않는다. 물리적으로 지우지 않고 비활성화 +
    자기 자신을 가리키는 고유 주소로 바꿔 UNIQUE 제약을 피한다 — 즐겨찾기·프로젝트
    참조가 걸려 있을 수 있어서다.

    **`reference_url`은 더 이상 이 조건에 넣지 않는다**(2026-08-28) — 같은 영상을
    공유하는 게 이제 정상이라, URL이 같다는 이유만으로 다른 챌린지 행을 은퇴시키면
    안 된다. 은퇴 조건은 오직 "이 챌린지 자체의 옛 행"(같은 trend_challenge_id)과
    "이 템플릿의 옛 버전 행"(같은 editing_template_id, 다른 버전)뿐이다.

    **같은 `editing_template_id`의 옛 버전 행도 여기서 잡는다.** R06 추천 수락이
    만든 행은 `trend_challenge_id`가 없어서(트렌드 동기화를 거친 적이 없어서)
    위 조건만으로는 안 걸리는데, AI가 같은 챌린지의 템플릿 버전을 올리면
    (v2→v4) 대표 행 선정은 새 버전 쪽(예: v4)으로 넘어가면서 옛 버전 행은
    그대로 활성 상태로 방치된다(2026-08-26 실서버에서 실제로 겪음 — v2 행이
    `internal://` 주소를 가진 채 v4 행과 나란히 활성 상태였다). `editing_template_id`
    자체는 지우지 않는다 — 이미 그 버전으로 만들어진 프로젝트가 있으면
    `get_shooting_guide`가 여전히 그 값을 참조하기 때문이다.
    """
    conditions = [VideoFormat.trend_challenge_id == challenge.id]
    if challenge.editing_template_id is not None:
        conditions = [
            conditions[0] | (VideoFormat.editing_template_id == challenge.editing_template_id)
        ]
    conflicting_condition = conditions[0]
    if keep.id is not None:
        conflicting_condition = conflicting_condition & (VideoFormat.id != keep.id)
    conflicting = db.scalars(select(VideoFormat).where(conflicting_condition)).all()
    for row in conflicting:
        row.trend_challenge_id = None
        row.reference_url = f"internal://retired-trend-row/{row.id}"
        row.guide_video_url = None
        row.is_active = False


def sync_trend_formats(db: Session) -> tuple[int, int, int]:
    """트렌드 클러스터를 받아 포맷 카탈로그에 반영한다.

    (추가, 갱신, 건너뜀) 개수를 돌려준다. **여러 번 돌려도 안전하다**(멱등).

    대표 영상 URL이 없는 챌린지는 건너뛴다 — 피드 카드는 영상 없이 성립하지 않고,
    빈 값으로 행을 만들면 앱에 재생 안 되는 카드가 그대로 노출된다.

    **AI 목록에서 완전히 빠진 챌린지는 비활성화한다.** 이 루프는 "지금 응답에
    있는 것"만 갱신하므로, AI가 목록 자체를 줄이면(예: 48건 → 3건) 빠진 챌린지의
    예전 `is_active` 값이 그대로 남는다 — 그래서 루프 뒤에 마무리 반영이 따로
    필요하다(2026-08-26 실서버에서 실제로 겪음: 응답이 48건에서 3건으로 줄었는데
    나머지 45건이 활성 상태로 남아 있었다).
    """
    challenges = ai_client.list_trend_challenges()
    seen_challenge_ids = [challenge.id for challenge in challenges]

    added = updated = skipped = 0
    for challenge in challenges:
        reference_url = challenge.representative_youtube_url
        if not reference_url:
            skipped += 1
            continue

        video_format = _select_representative_row(db, challenge)
        is_new = video_format is None
        if is_new:
            video_format = VideoFormat()
            db.add(video_format)

        # 대표 행이 새로 만들어졌든 기존 행이든, 다른 행이 같은 challenge_id/
        # 템플릿(다른 버전 포함)을 들고 있을 수 있다 — 새 행이라고 경합이 없다는
        # 보장은 없다(예: 옛 템플릿 버전 행이 대표로 뽑히지 않고 남아있는 경우).
        _retire_conflicting_rows(db, video_format, challenge)

        video_format.format_title = challenge.name
        video_format.reference_url = reference_url
        video_format.guide_video_url = challenge.guide_youtube_url
        video_format.source_platform = "YOUTUBE"
        video_format.trend_challenge_id = challenge.id
        video_format.trend_rank = challenge.rank
        _apply_ai_metadata(video_format, challenge)
        if (
            challenge.editing_template_id is not None
            and challenge.editing_template_version is not None
        ):
            video_format.editing_template_id = challenge.editing_template_id
            video_format.editing_template_version = challenge.editing_template_version
        # 트렌드 인기 여부(challenge.active)가 아니라 "촬영가이드 템플릿이 실제로
        # 있는가"로 활성화 여부를 정한다(2026-08-26 정정) — 발굴은 됐지만 아직
        # 승인 전인 챌린지는 트렌드로는 active여도 고르면 기획 생성이 막힌다.
        video_format.is_active = video_format.editing_template_id is not None

        if is_new:
            added += 1
        else:
            updated += 1

    # AI가 응답을 준 경우(연동 꺼짐이 아닌 경우)에만 마무리 비활성화를 한다 —
    # AI_SERVER_URL이 없어 challenges가 빈 목록일 때 트렌드 행을 전부 꺼버리면
    # 안 된다.
    if ai_client.is_enabled():
        reconcile = update(VideoFormat).where(VideoFormat.trend_challenge_id.is_not(None))
        if seen_challenge_ids:
            reconcile = reconcile.where(VideoFormat.trend_challenge_id.not_in(seen_challenge_ids))
        db.execute(reconcile.values(is_active=False))

    db.commit()
    return added, updated, skipped
