"""AI 트렌드 클러스터 → `video_formats` 동기화 테스트."""

from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.video_format import VideoFormat
from app.services import ai_client
from app.services.trend_format import sync_trend_formats

# AI 레포 `exports/trendcluster.json` 원문 형태 그대로.
TRENDCLUSTER: dict[str, Any] = {
    "generated_at": "2026-08-24T22:00:00.000Z",
    "count": 3,
    "results": [
        {
            "id": "jujutsu_transition",
            "rank": 1,
            "name": "주술회전 트랜지션",
            "representative_youtube_url": "https://www.youtube.com/shorts/Yc7ZjC0n7oY?si=abc",
            "guide_youtube_url": "https://www.youtube.com/shorts/Yc7ZjC0n7oY?si=abc",
            "format_type": "밈",
            "expected_duration_sec": 10,
            "shooting_difficulty": "중",
            "requires_face": False,
        },
        {
            "id": "cafe_recommendation_reels",
            "rank": 2,
            "name": "카페 추천 리뷰 릴스",
            "representative_youtube_url": "https://www.youtube.com/shorts/OWnLiuJU8Ks",
            "guide_youtube_url": "https://www.youtube.com/shorts/OWnLiuJU8Ks",
            "format_type": "정보형",
            "expected_duration_sec": 13,
            "shooting_difficulty": "중",
            "requires_face": False,
        },
        {
            "id": "otsukare_summer_challenge",
            "rank": 3,
            "name": "오츠카레 썸머 챌린지",
            "representative_youtube_url": "https://www.youtube.com/shorts/e-dU9yQfmik",
            "guide_youtube_url": "https://www.youtube.com/shorts/e-dU9yQfmik",
            "format_type": "챌린지",
            "expected_duration_sec": 12,
            "shooting_difficulty": "중",
            "requires_face": True,
        },
    ],
}


def _stub_ai(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(settings, "AI_SERVER_API_KEY", "shared-secret")
    monkeypatch.setattr(httpx, "request", fake_request)


def test_returns_empty_without_ai_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """가짜 URL을 만들지 않는다 — 재생되지 않는 카드가 피드에 나가면 안 된다."""
    monkeypatch.setattr(settings, "AI_SERVER_URL", "")
    assert ai_client.list_trend_challenges() == []


def test_sync_loads_trend_cluster(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ai(monkeypatch, TRENDCLUSTER)

    added, updated, skipped = sync_trend_formats(db_session)
    assert (added, updated, skipped) == (3, 0, 0)

    formats = list(db_session.scalars(select(VideoFormat).order_by(VideoFormat.trend_rank)))
    assert [f.format_title for f in formats] == [
        "주술회전 트랜지션",
        "카페 추천 리뷰 릴스",
        "오츠카레 썸머 챌린지",
    ]
    first = formats[0]
    assert first.reference_url == "https://www.youtube.com/shorts/Yc7ZjC0n7oY?si=abc"
    assert first.guide_video_url == "https://www.youtube.com/shorts/Yc7ZjC0n7oY?si=abc"
    assert first.source_platform == "YOUTUBE"
    assert first.trend_challenge_id == "jujutsu_transition"
    # 아직 컷 분해 템플릿이 없는 챌린지라 비활성이다 — 트렌드 자체는 인기여도
    # 골랐을 때 기획 생성이 안 되면 피드에 노출하면 안 된다.
    assert first.is_active is False
    assert (
        first.format_type,
        first.expected_duration_sec,
        first.shooting_difficulty,
        first.requires_face,
    ) == ("밈", 10, "중", False)


def test_sync_links_editing_template_when_approved(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """승인 완료된 챌린지는 editing_template_id/version이 그대로 연결된다."""
    payload = {
        "results": [
            {
                **TRENDCLUSTER["results"][0],
                "editing_template_id": "gt_jujutsu_transition",
                "editing_template_version": 4,
            }
        ]
    }
    _stub_ai(monkeypatch, payload)

    sync_trend_formats(db_session)

    linked = db_session.scalar(
        select(VideoFormat).where(VideoFormat.trend_challenge_id == "jujutsu_transition")
    )
    assert linked is not None
    assert linked.editing_template_id == "gt_jujutsu_transition"
    assert linked.editing_template_version == 4
    # 템플릿이 있으니 트렌드 인기 여부와 무관하게 활성화된다.
    assert linked.is_active is True


def test_sync_caches_estimated_shooting_sec_for_linked_template(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """템플릿이 연결되면 촬영 소요시간도 같이 캐싱한다(2026-08-30, FE 요청).

    가게 컨텍스트 없이(트렌드 동기화 시점에) 조회하는 값이라, 챌린지 목록
    엔드포인트와 촬영가이드 엔드포인트가 서로 다른 응답을 주는 걸 구분해서
    반영하는지가 핵심이다.
    """
    payload = {
        "results": [
            {
                **TRENDCLUSTER["results"][0],
                "editing_template_id": "gt_jujutsu_transition",
                "editing_template_version": 4,
            }
        ]
    }

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        if "shooting-guide" in url:
            return httpx.Response(
                200, json={"estimated_shooting_sec": 60}, request=httpx.Request(method, url)
            )
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(settings, "AI_SERVER_API_KEY", "shared-secret")
    monkeypatch.setattr(httpx, "request", fake_request)

    sync_trend_formats(db_session)

    linked = db_session.scalar(
        select(VideoFormat).where(VideoFormat.trend_challenge_id == "jujutsu_transition")
    )
    assert linked is not None
    assert linked.estimated_shooting_sec == 60


def test_sync_keeps_previous_estimated_shooting_sec_when_ai_lookup_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """촬영가이드 조회가 실패해도 이전 동기화가 채워둔 값을 지우지 않는다.

    `_apply_ai_metadata`가 다른 필드에 적용하는 "null로 덮어쓰지 않는다" 원칙과
    같다 — 카탈로그 부가 정보라 AI가 일시적으로 응답을 못 줘도 화면이 갑자기
    빈 값을 보여주면 안 된다.
    """
    payload = {
        "results": [
            {
                **TRENDCLUSTER["results"][0],
                "editing_template_id": "gt_jujutsu_transition",
                "editing_template_version": 4,
            }
        ]
    }

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        if "shooting-guide" in url:
            return httpx.Response(500, request=httpx.Request(method, url))
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(settings, "AI_SERVER_API_KEY", "shared-secret")
    monkeypatch.setattr(httpx, "request", fake_request)

    existing = VideoFormat(
        format_title="주술회전 트랜지션",
        reference_url="https://www.youtube.com/shorts/Yc7ZjC0n7oY?si=abc",
        trend_challenge_id="jujutsu_transition",
        editing_template_id="gt_jujutsu_transition",
        editing_template_version=4,
        estimated_shooting_sec=45,
    )
    db_session.add(existing)
    db_session.commit()

    sync_trend_formats(db_session)

    db_session.refresh(existing)
    assert existing.estimated_shooting_sec == 45


def test_sync_merges_into_existing_template_row_with_real_url(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R06 추천이 이미 만든 행과 같은 (template_id, version)이면 그 행을 대표로 삼는다.

    실서버에서 실제로 겪은 문제(2026-08-26, 두 단계):
    1) 같은 챌린지가 R06 경로와 트렌드 경로로 각각 행을 만들었는데, 트렌드 동기화가
       같은 (template_id, version) 쌍을 새 행에도 쓰려다가 UNIQUE 제약 위반으로
       커밋 전체가 실패했다.
    2) 그걸 "기존 행을 대신 켠다"로 고쳤더니 이번엔 실제 유튜브 URL이 반영되지 않은
       채로(R06 행은 `internal://` 자산 주소를 그대로 가진 채) 활성화만 돼서, 화면엔
       재생 안 되는 카드가 떴다. 대표 행 하나에 실제 값을 전부 합쳐 써야 한다.
    """
    r06_row = VideoFormat(
        format_title="R06 추천으로 만들어진 행",
        reference_url="internal://editing-template/gt_jujutsu_transition/v4",
        editing_template_id="gt_jujutsu_transition",
        editing_template_version=4,
        is_active=False,
    )
    db_session.add(r06_row)
    db_session.commit()

    payload = {
        "results": [
            {
                **TRENDCLUSTER["results"][0],
                "editing_template_id": "gt_jujutsu_transition",
                "editing_template_version": 4,
            }
        ]
    }
    _stub_ai(monkeypatch, payload)

    added, updated, skipped = sync_trend_formats(db_session)
    assert (added, updated, skipped) == (0, 1, 0)

    db_session.refresh(r06_row)
    assert r06_row.is_active is True
    assert r06_row.trend_challenge_id == "jujutsu_transition"
    # R06 행이 대표 행이 됐으니, 실제 유튜브 URL이 그 위에 그대로 반영돼야 한다 —
    # 예전엔 이 값이 갱신되지 않아 활성 카드가 재생 안 되는 internal:// 를 가리켰다.
    assert r06_row.reference_url == "https://www.youtube.com/shorts/Yc7ZjC0n7oY?si=abc"
    assert r06_row.guide_video_url == "https://www.youtube.com/shorts/Yc7ZjC0n7oY?si=abc"

    # 같은 챌린지를 가리키는 다른 행은 남아 있지 않다 — 대표 행 하나로 합쳐졌다.
    all_rows = list(db_session.scalars(select(VideoFormat)))
    assert len(all_rows) == 1


def test_sync_retires_stale_template_version_left_active_from_before(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 챌린지의 옛 템플릿 버전 행이 활성 상태로 남아있으면 은퇴시킨다.

    실서버에서 실제로 겪은 문제(2026-08-26): R06 추천 수락이 v2 시절에 만든 행은
    `trend_challenge_id`가 없어서 트렌드 동기화 기준으로는 "이 챌린지와 무관한
    행"처럼 보인다. AI가 나중에 같은 챌린지의 템플릿을 v4로 새로 승인하면 대표
    행 선정은 v4 쪽으로 넘어가는데, v2 행은 어떤 조건에도 안 걸려 활성 상태로
    그대로 남아 재생 안 되는 카드가 계속 노출됐다.
    """
    stale_v2_row = VideoFormat(
        format_title="예전 v2로 만들어진 행",
        reference_url="internal://editing-template/gt_jujutsu_transition/v2",
        editing_template_id="gt_jujutsu_transition",
        editing_template_version=2,
        is_active=True,
    )
    db_session.add(stale_v2_row)
    db_session.commit()

    payload = {
        "results": [
            {
                **TRENDCLUSTER["results"][0],
                "editing_template_id": "gt_jujutsu_transition",
                "editing_template_version": 4,
            }
        ]
    }
    _stub_ai(monkeypatch, payload)

    sync_trend_formats(db_session)

    db_session.refresh(stale_v2_row)
    assert stale_v2_row.is_active is False
    # editing_template_id/version은 남겨둔다 — 이미 이 버전으로 만들어진 프로젝트가
    # 있으면 get_shooting_guide가 여전히 이 값을 참조하기 때문이다.
    assert stale_v2_row.editing_template_id == "gt_jujutsu_transition"
    assert stale_v2_row.editing_template_version == 2

    representative = db_session.scalar(
        select(VideoFormat).where(VideoFormat.trend_challenge_id == "jujutsu_transition")
    )
    assert representative is not None
    assert representative.id != stale_v2_row.id
    assert representative.editing_template_version == 4
    assert representative.is_active is True


def test_sync_leaves_editing_template_unlinked_when_not_yet_approved(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """승인 전(필드 없음)이면 editing_template_id를 비워둔다 — 지어내지 않는다."""
    _stub_ai(monkeypatch, TRENDCLUSTER)

    sync_trend_formats(db_session)

    linked = db_session.scalar(
        select(VideoFormat).where(VideoFormat.trend_challenge_id == "jujutsu_transition")
    )
    assert linked is not None
    assert linked.editing_template_id is None
    assert linked.editing_template_version is None
    # 발굴은 됐지만 아직 승인 전이라 비활성 — 골라도 기획 생성이 막히는 카드를
    # 피드에 보여주면 안 된다.
    assert linked.is_active is False


def test_sync_activation_follows_template_not_ai_trend_flag(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """활성화 기준은 AI의 트렌드 인기 여부(`active`)가 아니라 템플릿 존재 여부다.

    실측 사례(2026-08-26): 새로 발굴된 챌린지는 트렌드로는 `active: true`인데
    아직 컷 분해 승인 전이고, 반대로 예전에 승인된 챌린지는 트렌드 순위에서는
    `active: false`로 빠졌는데도 템플릿은 여전히 유효했다. `active` 값만 보고
    켜고 끄면 정확히 거꾸로 된 결과가 나온다.
    """
    payload = {
        "results": [
            {
                # 트렌드로는 인기 있음(active=true)이지만 아직 템플릿 없음.
                "id": "trending_not_approved",
                "rank": 1,
                "name": "새로 뜨는 챌린지",
                "representative_youtube_url": "https://youtu.be/trending",
                "active": True,
            },
            {
                # 트렌드 순위에서는 빠졌지만(active=false) 템플릿은 있음.
                "id": "old_but_approved",
                "rank": 99,
                "name": "예전 챌린지",
                "representative_youtube_url": "https://youtu.be/old",
                "active": False,
                "editing_template_id": "gt_old_but_approved",
                "editing_template_version": 4,
            },
        ]
    }
    _stub_ai(monkeypatch, payload)

    sync_trend_formats(db_session)

    trending = db_session.scalar(
        select(VideoFormat).where(VideoFormat.trend_challenge_id == "trending_not_approved")
    )
    old = db_session.scalar(
        select(VideoFormat).where(VideoFormat.trend_challenge_id == "old_but_approved")
    )
    assert trending is not None and trending.is_active is False
    assert old is not None and old.is_active is True


def test_list_trend_challenges_requests_inactive_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """비활성 챌린지도 받아와야 is_active 동기화가 가능하다."""
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        return httpx.Response(200, json={"results": []}, request=httpx.Request(method, url))

    monkeypatch.setattr(settings, "AI_SERVER_URL", "http://ai.internal")
    monkeypatch.setattr(httpx, "request", fake_request)

    ai_client.list_trend_challenges()

    assert "include_inactive=true" in captured["url"]


def test_sync_is_idempotent_and_updates(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_ai(monkeypatch, TRENDCLUSTER)
    sync_trend_formats(db_session)

    moved = {
        **TRENDCLUSTER,
        "results": [
            {
                **TRENDCLUSTER["results"][0],
                "rank": 5,
                "name": "주술회전 트랜지션 (개정)",
                "representative_youtube_url": "https://www.youtube.com/shorts/NEWvideoid1",
            },
            *TRENDCLUSTER["results"][1:],
        ],
    }
    _stub_ai(monkeypatch, moved)
    added, updated, skipped = sync_trend_formats(db_session)
    assert (added, updated, skipped) == (0, 3, 0)

    changed = db_session.scalar(
        select(VideoFormat).where(VideoFormat.trend_challenge_id == "jujutsu_transition")
    )
    assert changed is not None
    # 대표 영상이 교체돼도 같은 행이 갱신된다(URL 기준이면 새 행이 생긴다).
    assert changed.reference_url == "https://www.youtube.com/shorts/NEWvideoid1"
    assert changed.format_title == "주술회전 트랜지션 (개정)"
    assert changed.trend_rank == 5
    assert len(list(db_session.scalars(select(VideoFormat)))) == 3


def test_sync_skips_challenge_without_video(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_ai(
        monkeypatch,
        {
            "generated_at": None,
            "count": 1,
            "results": [
                {
                    "id": "no_video",
                    "rank": 1,
                    "name": "영상 없는 챌린지",
                    "representative_youtube_url": None,
                    "guide_youtube_url": None,
                }
            ],
        },
    )

    added, updated, skipped = sync_trend_formats(db_session)
    assert (added, updated, skipped) == (0, 0, 1)
    assert list(db_session.scalars(select(VideoFormat))) == []


def test_sync_allows_two_challenges_to_share_reference_url(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """서로 다른 챌린지가 같은 대표 영상을 의도적으로 공유할 수 있다(2026-08-28, AI팀 확인).

    실서버에서 실제로 겪음: "가게 홍보 버전"·"챌린지 버전"처럼 서로 다른 챌린지
    둘이 같은 예시 클립을 대표 영상으로 썼는데, 예전 코드는 `reference_url`로
    챌린지를 식별해서 둘째 챌린지를 "이미 있는 챌린지"로 오인해 새 행을 안
    만들거나(잘못된 병합), UNIQUE 제약에 걸려 동기화 전체가 실패했다. 이제는
    `reference_url`이 같아도 `trend_challenge_id`가 다르면 완전히 별개 행이다.
    """
    existing = VideoFormat(
        format_title="예전 이름",
        reference_url="https://www.youtube.com/shorts/OWnLiuJU8Ks",
        source_platform="YOUTUBE",
        trend_challenge_id="unrelated_challenge",
        expected_duration_sec=25,
        shooting_difficulty="하",
    )
    db_session.add(existing)
    db_session.commit()

    _stub_ai(monkeypatch, TRENDCLUSTER)
    added, updated, skipped = sync_trend_formats(db_session)

    # TRENDCLUSTER의 cafe_recommendation_reels가 같은 URL을 쓰지만, 기존 행과는
    # 별개의 새 챌린지라 병합되지 않고 3개 다 새로 추가된다.
    assert (added, updated, skipped) == (3, 0, 0)

    db_session.refresh(existing)
    assert existing.trend_challenge_id == "unrelated_challenge"
    assert existing.format_title == "예전 이름"

    new_row = db_session.scalar(
        select(VideoFormat).where(VideoFormat.trend_challenge_id == "cafe_recommendation_reels")
    )
    assert new_row is not None
    assert new_row.id != existing.id
    assert (
        new_row.reference_url
        == existing.reference_url
        == "https://www.youtube.com/shorts/OWnLiuJU8Ks"
    )


def test_sync_creates_two_new_rows_sharing_url_in_same_batch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실서버에서 실제로 겪은 장애를 그대로 재현한다(2026-08-28).

    한 번의 AI 응답 안에 서로 다른 챌린지 둘("가게 홍보 버전"·"챌린지 버전")이
    똑같은 대표 영상 URL을 쓰고 있었다. 이전 코드는 `reference_url` UNIQUE
    제약 때문에 둘째 챌린지의 INSERT에서 IntegrityError가 나서 동기화 전체가
    롤백됐다(정상적인 다른 챌린지 갱신까지 전부 취소됨). 이제는 두 챌린지 모두
    독립된 행으로 만들어져야 한다.
    """
    shared_url = "https://www.youtube.com/shorts/6duJ3WOzeuQ"
    payload = {
        "results": [
            {
                "id": "donggeurio_store_promotion",
                "rank": 5,
                "name": "동그리오(매장 홍보)",
                "representative_youtube_url": shared_url,
                "guide_youtube_url": shared_url,
                "editing_template_id": "gt_donggeurio_store_promotion",
                "editing_template_version": 1,
            },
            {
                "id": "donggeurio_challenge",
                "rank": 6,
                "name": "동그리오(챌린지)",
                "representative_youtube_url": shared_url,
                "guide_youtube_url": shared_url,
                "editing_template_id": "gt_donggeurio_challenge",
                "editing_template_version": 1,
            },
        ]
    }
    _stub_ai(monkeypatch, payload)

    added, updated, skipped = sync_trend_formats(db_session)

    assert (added, updated, skipped) == (2, 0, 0)
    rows = list(
        db_session.scalars(
            select(VideoFormat).where(
                VideoFormat.trend_challenge_id.in_(
                    ["donggeurio_store_promotion", "donggeurio_challenge"]
                )
            )
        )
    )
    assert len(rows) == 2
    assert {row.reference_url for row in rows} == {shared_url}
    assert all(row.is_active for row in rows)


def test_sync_does_not_erase_curated_metadata_when_ai_omits_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = VideoFormat(
        format_title="기존 포맷",
        reference_url="https://youtu.be/existing",
        trend_challenge_id="existing",
        format_type="직접 입력",
        expected_duration_sec=25,
        shooting_difficulty="하",
        requires_face=False,
    )
    db_session.add(existing)
    db_session.commit()
    payload = {
        "results": [
            {
                "id": "existing",
                "rank": 1,
                "name": "기존 포맷 갱신",
                "representative_youtube_url": "https://youtu.be/existing",
            }
        ]
    }
    _stub_ai(monkeypatch, payload)

    sync_trend_formats(db_session)
    db_session.refresh(existing)
    assert existing.format_type == "직접 입력"
    assert existing.expected_duration_sec == 25
    assert existing.shooting_difficulty == "하"
    assert existing.requires_face is False


def test_sync_deactivates_rows_dropped_from_ai_list(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI 목록에서 완전히 빠진 챌린지는 (루프가 안 건드려도) 비활성화된다.

    실서버에서 실제로 겪음(2026-08-26): AI 응답이 48건에서 3건으로 줄었는데,
    빠진 45건의 예전 is_active=true가 그대로 남아 있었다. 두 챌린지 모두 처음엔
    승인된 템플릿이 있어 정상적으로 활성화되지만, 그중 하나가 다음 목록에서
    완전히 사라지면 루프가 그 행을 아예 안 건드리므로 마무리 반영이 필요하다.
    """
    payload = {
        "results": [
            {
                **TRENDCLUSTER["results"][0],
                "editing_template_id": "gt_jujutsu_transition",
                "editing_template_version": 4,
            },
            {
                **TRENDCLUSTER["results"][1],
                "editing_template_id": "gt_cafe_recommendation",
                "editing_template_version": 2,
            },
        ]
    }
    _stub_ai(monkeypatch, payload)
    sync_trend_formats(db_session)
    assert {
        f.trend_challenge_id
        for f in db_session.scalars(select(VideoFormat).where(VideoFormat.is_active.is_(True)))
    } == {"jujutsu_transition", "cafe_recommendation_reels"}

    # 다음 동기화에서 AI가 jujutsu_transition만 준다 — cafe_recommendation_reels는
    # 목록에서 완전히 빠졌다(루프가 그 행을 아예 안 건드린다).
    narrowed = {"results": [payload["results"][0]]}
    _stub_ai(monkeypatch, narrowed)
    sync_trend_formats(db_session)

    remaining_active_ids = {
        f.trend_challenge_id
        for f in db_session.scalars(select(VideoFormat).where(VideoFormat.is_active.is_(True)))
    }
    assert remaining_active_ids == {"jujutsu_transition"}


def test_sync_does_not_touch_existing_rows_when_ai_disabled(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI_SERVER_URL이 없어 목록이 비어도 기존 트렌드 행을 통째로 끄면 안 된다."""
    existing = VideoFormat(
        format_title="기존 트렌드 포맷",
        reference_url="https://youtu.be/existing2",
        trend_challenge_id="existing2",
        is_active=True,
    )
    db_session.add(existing)
    db_session.commit()

    monkeypatch.setattr(settings, "AI_SERVER_URL", "")
    sync_trend_formats(db_session)

    db_session.refresh(existing)
    assert existing.is_active is True


def test_trending_sort_uses_trend_rank(db_session: Session) -> None:
    """`sort=trending`이 트렌드 순위를 따르고, 순위 없는 포맷은 뒤로 간다."""
    from app.schemas.video_format import FormatSort
    from app.services.video_format import list_formats

    db_session.add_all(
        [
            VideoFormat(
                format_title="순위 없음(R06 템플릿)",
                reference_url="internal://editing-template/tpl/v1",
                editing_template_id="tpl",
                editing_template_version=1,
            ),
            VideoFormat(
                format_title="2위",
                reference_url="https://youtu.be/rank2",
                trend_challenge_id="rank2",
                trend_rank=2,
            ),
            VideoFormat(
                format_title="1위",
                reference_url="https://youtu.be/rank1",
                trend_challenge_id="rank1",
                trend_rank=1,
            ),
        ]
    )
    db_session.commit()

    trending = list_formats(db_session, sort=FormatSort.TRENDING)
    assert [f.format_title for f in trending] == ["1위", "2위", "순위 없음(R06 템플릿)"]

    # 최신순은 기존 동작 그대로 — 트렌드 순위를 보지 않는다.
    latest = list_formats(db_session, sort=FormatSort.LATEST)
    assert latest[0].format_title == "1위"  # 가장 마지막에 추가된 행
