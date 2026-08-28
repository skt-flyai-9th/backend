"""카카오맵 대표 메뉴 자동 수집 테스트 (`app/services/menu_crawl.py`).

실제 카카오 API는 부르지 않는다 — `httpx.get`을 가짜로 대체해 **우리 코드의
판단**(실패를 조용히 삼키기, 이미 메뉴가 있으면 자동 수집을 건너뛰기, 헤더를
제대로 보내는지)만 검증한다. 카카오 쪽 응답이 실제로 이 모양인지는
2026-08-27에 실측으로 확인했다(`docs/IMPLEMENTATION.md` 참고).
"""

from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.models.store import Store
from app.models.store_menu import StoreMenu
from app.models.user import User
from app.services import menu_crawl


def _make_store(
    db_session: Session,
    external_channel_url: str | None,
    *,
    name: str = "행복분식",
    latitude: str | None = None,
    longitude: str | None = None,
) -> Store:
    user = User(
        email="owner@example.com",
        name="김사장",
        is_active=True,
        terms_agreed=True,
        marketing_agreed=False,
    )
    db_session.add(user)
    db_session.flush()

    store = Store(
        user_id=user.id,
        name=name,
        external_channel_url=external_channel_url,
        latitude=latitude,
        longitude=longitude,
    )
    db_session.add(store)
    db_session.commit()
    db_session.refresh(store)
    return store


def _session_factory(db_session: Session) -> sessionmaker:
    """테스트 세션을 재사용하는 팩토리. 실제로는 매번 새 세션을 여는 자리다."""
    return lambda: db_session


def _fake_response(items: list[dict[str, Any]] | None, status: int = 200) -> httpx.Response:
    body = {"menu": {"menus": {"items": items}}} if items is not None else {"menu": {"menus": {}}}
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://x"))


# ---------------------------------------------------------------- kakao_place_id


def test_kakao_place_id_extracts_from_url(db_session: Session) -> None:
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")

    assert menu_crawl.kakao_place_id(store) == "10534102"


def test_kakao_place_id_none_for_naver_link(db_session: Session) -> None:
    """네이버 소스로 잡힌 가게(예: 사장님 자체 홈페이지 링크)는 재검색하지 않는다."""
    store = _make_store(db_session, "https://our-restaurant.example.com")

    assert menu_crawl.kakao_place_id(store) is None


def test_kakao_place_id_none_when_missing(db_session: Session) -> None:
    """직접 입력으로 등록된 가게는 채널 URL 자체가 없다."""
    store = _make_store(db_session, None)

    assert menu_crawl.kakao_place_id(store) is None


# ---------------------------------------------------------------- enrich_menu_from_kakao


def test_enrich_saves_fetched_items(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")
    items = [
        {"name": "브루드 커피", "price": 4500, "photo_url": "http://cdn.example.com/a.jpg"},
        {"name": "카푸치노", "price": 5200},
    ]

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> httpx.Response:
        assert url == "https://place-api.map.kakao.com/places/panel3/10534102"
        assert headers["Referer"] == "https://place.map.kakao.com/10534102"
        assert headers["Origin"] == "https://place.map.kakao.com"
        assert headers["pf"] == "web"
        return _fake_response(items)

    monkeypatch.setattr(httpx, "get", fake_get)

    menu_crawl.enrich_menu_from_kakao(store.id, "10534102", _session_factory(db_session))

    menus = db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).all()
    assert {m.name for m in menus} == {"브루드 커피", "카푸치노"}
    assert {m.price for m in menus} == {4500, 5200}
    coffee = next(m for m in menus if m.name == "브루드 커피")
    assert coffee.image_url == "http://cdn.example.com/a.jpg"


def test_enrich_converts_negative_price_to_null(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """카카오가 가격 미표기를 `-1`로 준다(실측, 2026-08-28) — 그대로 저장하면

    지어낸 적 없는 "-1원"이 화면에 실제 가격처럼 보인다. 3.2(수동 입력)가
    `price >= 0`을 요구하는 것과 같은 기준으로 걸러야 한다.
    """
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")
    items = [{"name": "가격 미표기 메뉴", "price": -1}]

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(items))

    menu_crawl.enrich_menu_from_kakao(store.id, "10534102", _session_factory(db_session))

    menu = db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).one()
    assert menu.price is None


def test_enrich_silently_ignores_http_error(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실패해도 예외가 밖으로 새면 안 된다 — 백그라운드 작업이라 아무도 못 본다."""
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> httpx.Response:
        return _fake_response(None, status=406)

    monkeypatch.setattr(httpx, "get", fake_get)

    menu_crawl.enrich_menu_from_kakao(store.id, "10534102", _session_factory(db_session))

    assert db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).count() == 0


def test_enrich_silently_ignores_network_error(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> httpx.Response:
        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    menu_crawl.enrich_menu_from_kakao(store.id, "10534102", _session_factory(db_session))

    assert db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).count() == 0


def test_enrich_handles_store_with_no_menu(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """카카오에 메뉴가 아예 등록 안 된 가게 — 에러가 아니라 빈 결과다."""
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(None))

    menu_crawl.enrich_menu_from_kakao(store.id, "10534102", _session_factory(db_session))

    assert db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).count() == 0


def test_enrich_does_not_overwrite_existing_menu(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """사장님이 이미 직접 입력해뒀으면 자동 수집으로 덮어쓰지 않는다."""
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")
    db_session.add(StoreMenu(store_id=store.id, name="사장님이 입력한 메뉴", price=1000))
    db_session.commit()

    called = False

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> httpx.Response:
        nonlocal called
        called = True
        return _fake_response([{"name": "카카오 메뉴", "price": 5000}])

    monkeypatch.setattr(httpx, "get", fake_get)

    menu_crawl.enrich_menu_from_kakao(store.id, "10534102", _session_factory(db_session))

    assert called  # 호출은 하되(못 미리 알 방법이 없다), 결과를 저장하지 않는다
    menus = db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).all()
    assert [m.name for m in menus] == ["사장님이 입력한 메뉴"]


def test_enrich_skips_items_without_name(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")

    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _fake_response([{"name": "  ", "price": 1000}])
    )

    menu_crawl.enrich_menu_from_kakao(store.id, "10534102", _session_factory(db_session))

    assert db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).count() == 0


def test_enrich_limits_item_count(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(db_session, "http://place.map.kakao.com/10534102")
    items = [{"name": f"메뉴{i}", "price": 1000 * i} for i in range(10)]

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(items))

    menu_crawl.enrich_menu_from_kakao(store.id, "10534102", _session_factory(db_session))

    assert db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).count() == 5


# ---------------------------------------------------------------- find_place_id_by_location


def _fake_keyword_response(documents: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200, json={"documents": documents}, request=httpx.Request("GET", "https://x")
    )


def test_find_place_id_by_location_returns_none_without_coordinates() -> None:
    """좌표가 없으면 이름만으로 재검색하지 않는다(오매칭 위험, PM_DECISIONS.md 2026-08-21)."""
    assert menu_crawl.find_place_id_by_location("행복분식", None, None) is None


def test_find_place_id_by_location_matches_same_name_within_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = [
        {"id": "999", "place_name": "행복분식", "distance": "40"},
    ]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_keyword_response(documents))

    result = menu_crawl.find_place_id_by_location("행복분식", "37.5000000", "127.0000000")

    assert result == "999"


def test_find_place_id_by_location_rejects_far_away_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이름이 같아도 100m보다 멀면 다른 지점으로 보고 채택하지 않는다(동명 프랜차이즈)."""
    documents = [
        {"id": "999", "place_name": "행복분식", "distance": "500"},
    ]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_keyword_response(documents))

    assert menu_crawl.find_place_id_by_location("행복분식", "37.5000000", "127.0000000") is None


def test_find_place_id_by_location_rejects_different_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """가까워도 상호명이 다르면(옆 가게) 채택하지 않는다."""
    documents = [
        {"id": "999", "place_name": "다른가게", "distance": "10"},
    ]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_keyword_response(documents))

    assert menu_crawl.find_place_id_by_location("행복분식", "37.5000000", "127.0000000") is None


def test_find_place_id_by_location_silently_ignores_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("GET", "https://x"))

    monkeypatch.setattr(httpx, "get", fake_get)

    assert menu_crawl.find_place_id_by_location("행복분식", "37.5000000", "127.0000000") is None


# ---------------------------------------------------------------- enrich_menu


def test_enrich_menu_uses_given_place_id_directly(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """place_id가 이미 있으면 위치 조회를 시도하지 않고 바로 크롤링한다."""
    store = _make_store(db_session, None)
    called_keyword_search = False

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        nonlocal called_keyword_search
        if url == menu_crawl._PLACE_SEARCH_URL:
            called_keyword_search = True
            return _fake_keyword_response([])
        return _fake_response([{"name": "브루드 커피", "price": 4500}])

    monkeypatch.setattr(httpx, "get", fake_get)

    menu_crawl.enrich_menu(store.id, "10534102", _session_factory(db_session))

    assert not called_keyword_search
    menus = db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).all()
    assert [m.name for m in menus] == ["브루드 커피"]


def test_enrich_menu_falls_back_to_location_when_place_id_missing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """네이버로 등록됐거나(kakao_place_id 유실) place_id가 없으면 이름+좌표로 찾아본다."""
    store = _make_store(
        db_session,
        "http://www.starbucks.co.kr/",
        name="스타벅스 더북한산점",
        latitude="37.6554576",
        longitude="126.9475563",
    )

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        if url == menu_crawl._PLACE_SEARCH_URL:
            return _fake_keyword_response(
                [{"id": "76206032", "place_name": "스타벅스 더북한산점", "distance": "5"}]
            )
        assert url == "https://place-api.map.kakao.com/places/panel3/76206032"
        return _fake_response([{"name": "브루드 커피", "price": 4500}])

    monkeypatch.setattr(httpx, "get", fake_get)

    menu_crawl.enrich_menu(store.id, None, _session_factory(db_session))

    menus = db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).all()
    assert [m.name for m in menus] == ["브루드 커피"]


def test_enrich_menu_does_nothing_when_resolution_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(
        db_session, None, name="스타벅스 더북한산점", latitude="37.6554576", longitude="126.9475563"
    )

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_keyword_response([]))

    menu_crawl.enrich_menu(store.id, None, _session_factory(db_session))

    assert db_session.query(StoreMenu).filter(StoreMenu.store_id == store.id).count() == 0


def test_enrich_menu_does_nothing_without_place_id_or_coordinates(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """직접 입력 매장처럼 place_id도 좌표도 없으면 외부 호출 자체를 안 한다."""
    store = _make_store(db_session, None)
    called = False

    def fake_get(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal called
        called = True
        return _fake_keyword_response([])

    monkeypatch.setattr(httpx, "get", fake_get)

    menu_crawl.enrich_menu(store.id, None, _session_factory(db_session))

    assert not called
