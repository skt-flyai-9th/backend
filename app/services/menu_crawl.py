"""가게 등록 직후 카카오맵에서 대표 메뉴를 자동으로 채우는 기능 (2.2 후속 처리).

**가게 등록 자체를 절대 막지 않는다.** 이 모듈의 모든 실패는 조용히 넘어간다 —
조회가 안 되면 사장님은 3.2에서 그대로 직접 입력하면 된다. 실패를 사장님에게
보여줄 이유가 없는 부가 기능이라는 게 이 모듈 전체를 관통하는 설계 원칙이다.

**동명 매장 오매칭을 피하려고 이름으로 재검색하지 않는다**(`docs/PM_DECISIONS.md`
2026-08-21 결정과 같은 원칙). 2.1 검색 단계에서 이미 잡아둔 `kakao_place_id`(또는
`external_channel_url`이 카카오 플레이스 링크일 때 거기서 뽑은 ID)가 있을 때만
조회한다. 없으면(네이버로만 잡혔거나 직접 입력한 가게) 시도 자체를 안 한다.

2026-08-27: 헤드리스 크롬으로 카카오맵 화면을 스크래핑하던 방식(`scripts/
crawl_kakao_menu.py`, 이제 삭제)을 카카오맵 웹사이트 자체가 화면을 그릴 때 쓰는
비공식 내부 API(`place-api.map.kakao.com`) 직접 호출로 교체했다. 실서버에서
메뉴 자동 수집이 계속 조용히 실패하던 걸 조사하다가 FE가 이 API를 찾아냈다
(2026-08-27, 실측 15회 이상 연속 호출 전부 성공).

**여전히 비공식 크롤링이라는 성격은 그대로다** — 공식 문서가 없고, 카카오가
내부 구조를 바꾸면 예고 없이 깨질 수 있다. 다만 무거운 크롬 프로세스(200~350MB)
대신 가벼운 HTTP 요청 하나라, 여러 가게 등록이 겹쳐도 API 프로세스에 부담이
없다 — 그래서 기존에 있던 "서버 전체 동시 1개만 크롤링" MySQL 락, 서브프로세스
격리, Chrome/Selenium 의존성을 전부 걷어냈다.
"""

import logging
import re
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.store import Store
from app.models.store_menu import StoreMenu

logger = logging.getLogger(__name__)

_MENU_LIMIT = 5
_REQUEST_TIMEOUT_SEC = 10.0
_MENU_API_URL = "https://place-api.map.kakao.com/places/panel3/{place_id}"
_KAKAO_PLACE_RE = re.compile(r"place\.map\.kakao\.com/(\d+)")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 이름+좌표 보조조회용(2026-08-28 추가). 카카오 키워드 검색 API — 2.1 검색이 쓰는
# 것과 같은 엔드포인트다.
_PLACE_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
# 같은 매장으로 볼 좌표 오차 한도. 네이버·카카오가 지오코딩을 각자 하므로 완전히
# 같은 좌표가 오진 않는다 — 건물 하나 폭 정도로 넉넉히 잡는다.
_MATCH_DISTANCE_M = 100


def kakao_place_id(store: Store) -> str | None:
    """가게의 `external_channel_url`이 카카오 플레이스 링크면 ID를 뽑는다.

    NAVER 소스로 등록됐어도, 검색 단계의 후보 병합에서 카카오 링크가 채워져
    있을 수 있다(`store_search.merge_duplicates`가 NAVER 결과에 링크가 없으면
    카카오 것으로 채운다) — 그래서 `info_source`가 아니라 URL 자체를 본다.
    """
    if not store.external_channel_url:
        return None
    match = _KAKAO_PLACE_RE.search(store.external_channel_url)
    return match.group(1) if match else None


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()


def find_place_id_by_location(
    name: str, latitude: Decimal | None, longitude: Decimal | None
) -> str | None:
    """이름+좌표로, 정확히 같은 위치에 있는 카카오 매장을 찾는다.

    **이름만으로 재검색하지 않는다**(`docs/PM_DECISIONS.md` 2026-08-21 결정과 같은
    원칙 — 동명 프랜차이즈 오매칭 위험). 여기서는 이미 확정된 이 가게의 좌표를
    같이 쓰므로 안전하다: 카카오 결과가 `_MATCH_DISTANCE_M` 이내에 없거나
    상호명이 정확히 같지 않으면 채택하지 않는다.

    네이버로 등록됐지만(2.1 검색·병합 단계에서 카카오 후보를 못 찾았거나, 병합은
    됐는데 프론트에서 `kakao_place_id`가 유실된 경우) 좌표는 있는 매장을 구제하기
    위한 보조 경로다.
    """
    if latitude is None or longitude is None:
        return None

    try:
        response = httpx.get(
            _PLACE_SEARCH_URL,
            params={
                "query": name,
                "x": str(longitude),
                "y": str(latitude),
                "sort": "distance",
                "size": 5,
            },
            headers={"Authorization": f"KakaoAK {settings.KAKAO_REST_API_KEY}"},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        documents = response.json().get("documents", [])
    except (httpx.HTTPError, ValueError):
        logger.info("카카오 위치 기반 매장 조회 실패: name=%s", name)
        return None

    target = _normalize_name(name)
    for doc in documents:
        distance = doc.get("distance")
        if distance is None or int(distance) > _MATCH_DISTANCE_M:
            continue
        if _normalize_name(doc.get("place_name", "")) == target:
            return doc.get("id")
    return None


def enrich_menu(store_id: int, place_id: str | None, session_factory: sessionmaker) -> None:
    """백그라운드 진입점 (API명세서 2.2 후속 처리).

    `place_id`가 이미 있으면(카카오로 등록됐거나, 프론트가 2.1 응답의 값을 그대로
    보냈으면) 바로 크롤링한다. 없으면 가게의 이름+좌표로 `find_place_id_by_location`을
    한 번 더 시도한다 — 네이버로만 잡혔거나, 병합은 됐는데 값이 유실된 경우를 구제한다.
    """
    resolved = place_id
    if not resolved:
        db = session_factory()
        try:
            store = db.get(Store, store_id)
            if store is None:
                return
            resolved = find_place_id_by_location(store.name, store.latitude, store.longitude)
        except Exception:
            logger.exception(
                "카카오 위치 기반 매장 조회 중 처리되지 않은 예외: store_id=%s", store_id
            )
            return

        finally:
            db.close()

    if resolved:
        enrich_menu_from_kakao(store_id, resolved, session_factory)


def enrich_menu_from_kakao(store_id: int, place_id: str, session_factory: sessionmaker) -> None:
    """백그라운드에서 실행된다(FastAPI `BackgroundTasks`).

    응답이 이미 나간 뒤에 돌기 때문에 원래 요청의 DB 세션은 재사용할 수 없다 —
    `session_factory`로 새 세션을 직접 연다.
    """
    db = session_factory()
    try:
        _crawl_and_save(db, store_id, place_id)
    except Exception:
        # 백그라운드 작업의 예외는 아무도 안 본다 — 여기서 잡아 로그로만 남긴다.
        logger.exception("메뉴 자동 수집 중 처리되지 않은 예외: store_id=%s", store_id)
    finally:
        db.close()


def _fetch_menu_items(place_id: str) -> list[dict]:
    """카카오맵 웹사이트가 화면을 그릴 때 쓰는 내부 API에서 메뉴를 가져온다.

    공식 API가 아니라서 실패를 예외적인 상황으로 다루지 않는다 — 호출 실패,
    비정상 응답, 메뉴가 아예 없는 가게 전부 빈 리스트로 수렴시켜 호출부가 한
    가지 방식으로만 처리하면 되게 한다. `Referer`/`Origin`/`pf` 헤더가 없으면
    406이 난다(2026-08-27 실측) — 이 웹사이트에서 온 요청인지를 이걸로 거른다.
    """
    try:
        response = httpx.get(
            _MENU_API_URL.format(place_id=place_id),
            headers={
                "Referer": f"https://place.map.kakao.com/{place_id}",
                "Origin": "https://place.map.kakao.com",
                "pf": "web",
                "User-Agent": _USER_AGENT,
            },
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        logger.info("카카오 메뉴 조회 실패: place_id=%s", place_id)
        return []

    items = data.get("menu", {}).get("menus", {}).get("items") or []
    return items[:_MENU_LIMIT]


def _crawl_and_save(db: Session, store_id: int, place_id: str) -> None:
    items = _fetch_menu_items(place_id)
    if not items:
        return

    # 가게가 그 사이 삭제됐거나(경합), 사장님이 이미 메뉴를 직접 입력해뒀으면
    # 자동 수집 결과로 덮어쓰지 않는다 — 사람이 넣은 값이 우선이다.
    store = db.get(Store, store_id)
    if store is None:
        return
    if db.query(StoreMenu).filter(StoreMenu.store_id == store_id).first() is not None:
        logger.info("메뉴 이미 존재해 자동 수집 건너뜀: store_id=%s", store_id)
        return

    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        db.add(
            StoreMenu(
                store_id=store_id,
                name=name[:200],
                price=_clean_price(item.get("price")),
                image_url=item.get("photo_url"),
            )
        )
    db.commit()
    logger.info("메뉴 자동 수집 완료: store_id=%s count=%d", store_id, len(items))


def _clean_price(price: object) -> int | None:
    """카카오가 가격 미표기를 `-1`로 준다(실측, 2026-08-28) — `null`로 바꾼다.

    3.2(수동 입력)는 `price >= 0`을 요구하는데, 이 자동수집 경로는 스키마를
    거치지 않고 바로 모델을 만들어 검증이 없었다. 그대로 두면 화면에
    "-1원"처럼 지어낸 적 없는 값이 마치 실제 가격인 것처럼 보인다.
    """
    if not isinstance(price, int) or price < 0:
        return None
    return price
