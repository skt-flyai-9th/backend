"""가게 API 테스트 (API명세서 2.1~2.3)."""

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.store import Store
from app.schemas.store import SearchSource, StoreSearchResult
from app.services.store_search import merge_duplicates

STORE_BODY: dict[str, Any] = {
    "name": "행복분식 강남점",
    "category": "분식",
    "address": "서울 강남구 테헤란로 1길 10",
    "phone": "02-1234-5678",
    "info_source": "NAVER",
    "external_channel_url": "https://map.naver.com/p/entry/place/12345",
}


def _create_store(client: TestClient, headers: dict[str, str], **overrides: Any) -> Any:
    body = {**STORE_BODY, **overrides}
    return client.post("/stores", json=body, headers=headers)


def _result(**overrides: Any) -> StoreSearchResult:
    base: dict[str, Any] = {
        "source": SearchSource.NAVER,
        "name": "행복분식 강남점",
        "address": "서울 강남구 테헤란로 1길 10",
        "jibun_address": None,
        "phone": None,
        "latitude": None,
        "longitude": None,
        "category": None,
        "distance_m": None,
        "external_channel_url": None,
    }
    return StoreSearchResult(**{**base, **overrides})


# ---------------------------------------------------------------- 2.1 통합검색 (중복 병합)


def test_merges_same_place_from_two_sources() -> None:
    """상호명·주소가 같으면 출처가 달라도 한 건으로 합친다."""
    merged = merge_duplicates(
        [
            _result(source=SearchSource.NAVER, phone="02-1234-5678"),
            _result(
                source=SearchSource.KAKAO,
                category="분식",
                distance_m=120,
                jibun_address="서울 강남구 역삼동 800-1",
            ),
        ]
    )

    assert len(merged) == 1
    only = merged[0]
    assert only.source is SearchSource.NAVER  # 먼저 찾은 출처를 유지한다
    assert only.phone == "02-1234-5678"  # NAVER가 가진 값
    assert only.category == "분식"  # KAKAO에서 채워온 값
    assert only.distance_m == 120
    assert only.jibun_address == "서울 강남구 역삼동 800-1"  # KAKAO에서 채워온 값


def test_merges_by_coordinates_when_address_notation_differs() -> None:
    """주소 표기(지번/도로명)가 달라도 좌표가 가까우면 같은 가게로 본다."""
    merged = merge_duplicates(
        [
            _result(
                address="서울 강남구 테헤란로 1길 10",
                latitude=Decimal("37.4995"),
                longitude=Decimal("127.0312"),
            ),
            _result(
                source=SearchSource.KAKAO,
                address="서울 강남구 역삼동 800-1",
                latitude=Decimal("37.4996"),
                longitude=Decimal("127.0313"),
            ),
        ]
    )

    assert len(merged) == 1


def test_does_not_merge_different_names_at_same_building() -> None:
    """한 건물에 있는 서로 다른 가게를 좌표만으로 합치면 안 된다."""
    merged = merge_duplicates(
        [
            _result(name="행복분식", latitude=Decimal("37.4995"), longitude=Decimal("127.0312")),
            _result(name="행복카페", latitude=Decimal("37.4995"), longitude=Decimal("127.0312")),
        ]
    )

    assert len(merged) == 2


def test_does_not_merge_same_name_at_different_places() -> None:
    """상호명만 같고 주소가 다른 체인점은 별도 후보로 남는다."""
    merged = merge_duplicates(
        [
            _result(name="행복분식", address="서울 강남구 테헤란로 1길 10"),
            _result(name="행복분식", address="서울 마포구 양화로 100"),
        ]
    )

    assert len(merged) == 2


def test_search_requires_authentication(client: TestClient) -> None:
    response = client.get("/stores/search", params={"keyword": "행복분식"})

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"


def test_search_returns_merged_results(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """외부 API는 목킹한다 — CI에는 키가 없고, 테스트가 외부 상태에 흔들리면 안 된다."""

    async def fake_search(keyword: str, latitude: Any = None, longitude: Any = None) -> list:
        assert keyword == "행복분식"
        return [_result(source=SearchSource.KAKAO, distance_m=120)]

    monkeypatch.setattr("app.api.routers.stores.store_search.search_stores", fake_search)

    response = client.get("/stores/search", params={"keyword": "행복분식"}, headers=auth_headers)

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["source"] == "KAKAO"
    assert results[0]["distance_m"] == 120


def test_search_returns_empty_list_when_nothing_found(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """결과 없음은 에러가 아니다 — 프론트가 직접 입력을 제안하는 분기다."""

    async def fake_search(keyword: str, latitude: Any = None, longitude: Any = None) -> list:
        return []

    monkeypatch.setattr("app.api.routers.stores.store_search.search_stores", fake_search)

    response = client.get("/stores/search", params={"keyword": "없는가게"}, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["results"] == []


def test_search_requires_keyword(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/stores/search", headers=auth_headers)

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- 2.2 가게 등록


def test_create_store_returns_spec_fields(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = _create_store(client, auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "name",
        "category",
        "address",
        "info_source",
        "import_status",
        "created_at",
    }
    assert body["name"] == STORE_BODY["name"]
    assert body["info_source"] == "NAVER"
    # 방금 등록해서 메뉴·사진·상권분석은 아직 없으므로 진행 중이다
    assert body["import_status"] == "IN_PROGRESS"
    assert body["created_at"].endswith("Z")


def test_create_store_belongs_to_requesting_user(
    client: TestClient, auth_headers: dict[str, str], db_session: Session
) -> None:
    store_id = _create_store(client, auth_headers).json()["id"]

    store = db_session.scalar(select(Store).where(Store.id == store_id))
    assert store is not None
    assert store.user_id is not None
    assert store.external_channel_url == STORE_BODY["external_channel_url"]


def test_create_store_accepts_coordinates_from_search(
    client: TestClient, auth_headers: dict[str, str], db_session: Session
) -> None:
    """검색 결과로 등록하는 경로에서 좌표를 그대로 넘길 수 있다."""
    response = _create_store(client, auth_headers, latitude=37.4995, longitude=127.0312)

    store = db_session.get(Store, response.json()["id"])
    assert store is not None
    assert store.latitude == Decimal("37.4995000")
    assert store.longitude == Decimal("127.0312000")


def test_create_store_allows_channel_url_without_address(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """직접입력·URL보완 경로 — 주소 없이 외부 채널 URL만으로도 등록된다."""
    response = _create_store(client, auth_headers, address=None)

    assert response.status_code == 201


def test_create_store_rejects_missing_address_and_channel(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """기능명세서 S02.1.3 — 주소 또는 온라인 채널 중 하나는 반드시 있어야 한다."""
    response = _create_store(client, auth_headers, address=None, external_channel_url=None)

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(("field", "value"), [("name", ""), ("category", "")])
def test_create_store_validates_required_fields(
    client: TestClient, auth_headers: dict[str, str], field: str, value: str
) -> None:
    response = _create_store(client, auth_headers, **{field: value})

    assert response.status_code == 422
    assert field in response.json()["message"]


def test_create_store_requires_authentication(client: TestClient) -> None:
    response = client.post("/stores", json=STORE_BODY)

    assert response.status_code == 401


def test_create_store_prefers_payload_kakao_place_id_over_url_parsing(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`external_channel_url`이 카카오 링크가 아니어도 kakao_place_id로 대표메뉴 자동수집이 걸린다.

    실제로 겪은 문제(2026-08-26): 사장님들이 등록 시 `external_channel_url`을
    인스타그램 등 다른 링크로 바꾸는 경우가 많아, URL 파싱만으로는 검색 단계에서
    이미 확보한 카카오 ID를 놓친다.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.api.routers.stores.menu_crawl_service.enrich_menu",
        lambda store_id, place_id, session_factory: captured.update(
            store_id=store_id, place_id=place_id
        ),
    )

    response = _create_store(
        client,
        auth_headers,
        external_channel_url="https://instagram.com/happy_bunsik",
        kakao_place_id="27557389",
    )

    assert response.status_code == 201
    assert captured == {"store_id": response.json()["id"], "place_id": "27557389"}


def test_create_store_triggers_trade_area_insight_generation(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """가게 등록 직후 상권분석도 메뉴 자동수집과 같은 방식으로 백그라운드 생성된다."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.api.routers.stores.insight_service.generate_trade_area_insight",
        lambda store_id, session_factory: captured.update(store_id=store_id),
    )

    response = _create_store(client, auth_headers)

    assert response.status_code == 201
    assert captured == {"store_id": response.json()["id"]}


def test_create_store_falls_back_to_url_when_kakao_place_id_missing(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """kakao_place_id를 안 보내면 기존처럼 external_channel_url에서 파싱한다(구버전 대응)."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.api.routers.stores.menu_crawl_service.enrich_menu",
        lambda store_id, place_id, session_factory: captured.update(
            store_id=store_id, place_id=place_id
        ),
    )

    response = _create_store(
        client,
        auth_headers,
        external_channel_url="https://place.map.kakao.com/98765",
    )

    assert response.status_code == 201
    assert captured == {"store_id": response.json()["id"], "place_id": "98765"}


# ---------------------------------------------------------------- 2.3 가져오기 진행상태


def test_import_status_returns_spec_shape(client: TestClient, auth_headers: dict[str, str]) -> None:
    store_id = _create_store(client, auth_headers).json()["id"]

    response = client.get(f"/stores/{store_id}/import-status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["store_id"] == store_id
    assert body["overall_status"] == "IN_PROGRESS"
    assert [item["field"] for item in body["items"]] == ["기본정보", "메뉴", "사진", "상권분석"]
    # 가게가 등록됐다는 것 자체가 기본정보 수집 완료를 뜻한다
    assert body["items"][0]["status"] == "SUCCESS"
    # 방금 등록해서 메뉴·사진·상권분석 데이터는 아직 없다
    assert {item["status"] for item in body["items"][1:]} == {"PENDING"}


def test_import_status_hides_other_users_store(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """남의 가게는 403이 아니라 404 — 그 ID의 가게가 존재한다는 사실 자체를 숨긴다."""
    store_id = _create_store(client, auth_headers).json()["id"]

    client.post(
        "/auth/signup",
        json={
            "email": "other@example.com",
            "password": "sarils1234!",
            "name": "다른사장",
            "terms_agreed": True,
        },
    )
    other_login = client.post(
        "/auth/login", json={"email": "other@example.com", "password": "sarils1234!"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    response = client.get(f"/stores/{store_id}/import-status", headers=other_headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "STORE_NOT_FOUND"


def test_import_status_returns_404_for_unknown_store(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/stores/999999/import-status", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "STORE_NOT_FOUND"


def test_import_status_requires_authentication(client: TestClient) -> None:
    response = client.get("/stores/1/import-status")

    assert response.status_code == 401


# ---------------------------------------------------------------- 외부 응답 파싱


def test_naver_title_html_tags_are_stripped() -> None:
    """NAVER 지역검색의 title은 검색어가 <b>로 감싸여 온다."""
    from app.services.store_search import _parse_naver

    parsed = _parse_naver({"title": "<b>행복</b>분식 강남점", "roadAddress": "서울 강남구"})

    assert parsed.name == "행복분식 강남점"


def test_naver_coordinates_are_scaled_to_degrees() -> None:
    """mapx/mapy는 WGS84를 10^7배한 정수 문자열로 온다."""
    from app.services.store_search import _parse_naver

    parsed = _parse_naver({"title": "행복분식", "mapx": "1270312345", "mapy": "374995678"})

    assert parsed.longitude == Decimal("127.0312345")
    assert parsed.latitude == Decimal("37.4995678")


def test_naver_coordinates_already_in_degrees_are_kept() -> None:
    """도 단위로 오는 응답도 방어적으로 허용한다."""
    from app.services.store_search import _parse_naver

    parsed = _parse_naver({"title": "행복분식", "mapx": "127.0312345", "mapy": "37.4995678"})

    assert parsed.longitude == Decimal("127.0312345")


def test_naver_missing_coordinates_are_none() -> None:
    from app.services.store_search import _parse_naver

    parsed = _parse_naver({"title": "행복분식"})

    assert parsed.latitude is None
    assert parsed.longitude is None
    assert parsed.distance_m is None


def test_naver_keeps_jibun_address_separate_from_road_address() -> None:
    """NAVER는 지번(address)·도로명(roadAddress)을 둘 다 준다 — 지번을 버리면 안 된다.

    실제로 겪은 문제(2026-08-26, FE 리포트): `address`가 `roadAddress`로 덮여
    화면에 지번 주소를 낼 방법이 없었다.
    """
    from app.services.store_search import _parse_naver

    parsed = _parse_naver(
        {
            "title": "스타벅스 한국프레스센터점",
            "address": "서울특별시 중구 태평로1가 25",
            "roadAddress": "서울특별시 중구 세종대로 124 (태평로1가)",
        }
    )

    assert parsed.address == "서울특별시 중구 세종대로 124 (태평로1가)"
    assert parsed.jibun_address == "서울특별시 중구 태평로1가 25"


def test_kakao_document_is_parsed_into_result() -> None:
    """Kakao는 x=경도, y=위도이며 place_url이 곧 external_channel_url이다."""
    from app.services.store_search import _parse_kakao

    parsed = _parse_kakao(
        {
            "place_name": "행복분식",
            "road_address_name": "서울 강남구 테헤란로 1길 10",
            "address_name": "서울 강남구 역삼동 800-1",
            "phone": "02-1234-5678",
            "x": "127.0312345",
            "y": "37.4995678",
            "category_group_name": "음식점",
            "distance": "120",
            "place_url": "https://place.map.kakao.com/98765",
            "id": "98765",
        }
    )

    assert parsed.source is SearchSource.KAKAO
    assert parsed.address == "서울 강남구 테헤란로 1길 10"  # 도로명주소를 우선한다
    assert parsed.jibun_address == "서울 강남구 역삼동 800-1"
    assert parsed.longitude == Decimal("127.0312345")
    assert parsed.latitude == Decimal("37.4995678")
    assert parsed.distance_m == 120
    assert parsed.external_channel_url == "https://place.map.kakao.com/98765"
    assert parsed.kakao_place_id == "98765"


def test_coordinates_are_serialized_as_numbers(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """좌표는 문자열이 아니라 숫자로 나가야 한다.

    Pydantic은 Decimal을 기본적으로 문자열로 직렬화한다. 프론트가 값을 그대로 지도
    SDK에 넘기므로 명세서 예시(37.4995)대로 숫자여야 한다.
    """

    async def fake_search(keyword: str, latitude: Any = None, longitude: Any = None) -> list:
        return [_result(latitude=Decimal("37.4995000"), longitude=Decimal("127.0312000"))]

    monkeypatch.setattr("app.api.routers.stores.store_search.search_stores", fake_search)

    response = client.get("/stores/search", params={"keyword": "행복분식"}, headers=auth_headers)

    first = response.json()["results"][0]
    assert isinstance(first["latitude"], float)
    assert isinstance(first["longitude"], float)
    assert first["latitude"] == 37.4995


def test_merges_when_only_sido_notation_differs() -> None:
    """출처별 시도 표기가 달라도 도로명·번지가 같으면 병합한다.

    아래 두 주소는 2026-08-23 실제 응답에서 그대로 가져온 것이다.
    좌표가 없는 후보에서는 주소 비교가 유일한 병합 근거가 된다.
    """
    merged = merge_duplicates(
        [
            _result(
                name="스타벅스 강남파이낸스센터점",
                address="서울특별시 강남구 테헤란로 152 (역삼동)",
            ),
            _result(
                source=SearchSource.KAKAO,
                name="스타벅스 강남파이낸스센터점",
                address="서울 강남구 테헤란로 152",
                external_channel_url="http://place.map.kakao.com/2018390336",
            ),
        ]
    )

    assert len(merged) == 1
    # NAVER 지역검색은 장소 URL을 주지 않으므로 Kakao 값으로 채워져야 한다
    assert merged[0].external_channel_url == "http://place.map.kakao.com/2018390336"


def test_does_not_merge_same_road_different_building() -> None:
    """도로명이 같아도 번지가 다르면 다른 가게다."""
    merged = merge_duplicates(
        [
            _result(name="스타벅스", address="서울특별시 강남구 테헤란로 152"),
            _result(source=SearchSource.KAKAO, name="스타벅스", address="서울 강남구 테헤란로 300"),
        ]
    )

    assert len(merged) == 2


def test_import_status_reflects_actual_data(
    client: TestClient, auth_headers: dict[str, str], db_session: Session
) -> None:
    """상태를 저장하지 않고 실제 데이터 존재 여부로 계산한다.

    메뉴를 등록하면 별도 상태 갱신 없이 곧바로 SUCCESS가 되어야 한다.
    """
    store_id = _create_store(client, auth_headers).json()["id"]

    client.post(
        f"/stores/{store_id}/menus", json={"name": "떡볶이", "price": 4000}, headers=auth_headers
    )

    items = {
        item["field"]: item["status"]
        for item in client.get(f"/stores/{store_id}/import-status", headers=auth_headers).json()[
            "items"
        ]
    }
    assert items["메뉴"] == "SUCCESS"
    assert items["상권분석"] == "PENDING"
    # 사진은 store_photos 테이블이 생기는 3.3 작업에서 연결된다
    assert items["사진"] == "PENDING"


def test_import_status_counts_only_market_insight(
    client: TestClient, auth_headers: dict[str, str], db_session: Session
) -> None:
    """상권분석 항목은 유형이 '상권분석'인 인사이트만 본다 — 카드뉴스가 있다고 켜지면 안 된다."""
    from app.models.mixins import utcnow
    from app.models.store_insight import StoreInsight

    store_id = _create_store(client, auth_headers).json()["id"]
    db_session.add(
        StoreInsight(
            store_id=store_id,
            insight_type="카드뉴스",
            insight_title="카드뉴스",
            generated_at=utcnow(),
        )
    )
    db_session.commit()

    items = {
        item["field"]: item["status"]
        for item in client.get(f"/stores/{store_id}/import-status", headers=auth_headers).json()[
            "items"
        ]
    }
    assert items["상권분석"] == "PENDING"
