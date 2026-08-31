"""공통 테스트 픽스처.

테스트는 MySQL 없이 돌아가야 하므로(CI에도 DB 컨테이너가 없다) 인메모리 SQLite를
쓰고, `get_db` 의존성을 그 세션으로 갈아끼운다. 모델을 SQLite 호환으로 유지하는
장치는 `app/models/types.py` 참고.
"""

from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.main import app as fastapi_app

# 모델을 임포트해야 Base.metadata에 테이블이 등록된다.
from app.models import Store, StorePhoto, User  # noqa: F401


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """테스트 하나당 비어있는 인메모리 DB 세션 하나."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 인메모리 DB를 커넥션 간에 공유하려면 필요하다
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """`get_db`가 테스트 세션을 쓰도록 갈아끼운 앱 클라이언트."""
    yield from _client_for(fastapi_app, db_session)


def _client_for(app: FastAPI, db_session: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    """회원가입 + 로그인까지 마친 사용자의 인증 헤더.

    인증이 필요한 API를 테스트할 때 `headers=auth_headers`로 넘긴다.
    """
    signup = client.post(
        "/auth/signup",
        json={
            "email": "owner@example.com",
            "password": "sarils1234!",
            "name": "김사장",
            "terms_agreed": True,
        },
    )
    assert signup.status_code == 201, signup.text

    login = client.post(
        "/auth/login",
        json={"email": "owner@example.com", "password": "sarils1234!"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture(autouse=True)
def no_real_ai_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AI_SERVER_URL`을 기본적으로 비워서 테스트가 실제 AI 서버를 못 부르게 막는다.

    로컬 `.env`엔 실제 내부망 IP(`AI_SERVER_URL=http://172.31.x.x:8000`)가 들어있다
    — 개발 서버 실행용 값인데, 이걸 명시적으로 mock하지 않은 테스트가 하나라도
    있으면 그 테스트가 실제로 그 주소에 HTTP 요청을 보내고, 로컬에서는 당연히
    도달 못 하니 `AI_REQUEST_TIMEOUT_SECONDS`(기본 30초)만큼 그대로 멈춘다.
    이런 테스트가 여러 개 흩어져 있으면 전체 스위트가 몇 분씩 걸리게 된다
    (2026-08-31 발견 — `ai_client.is_enabled()`가 `AI_SERVER_URL` 존재 여부만
    본다).

    AI 연동 자체를 검증하는 테스트(`test_ai_client_integration.py`,
    `test_shortform_sessions.py`, `test_trend_formats.py`, `test_video_edit.py`
    등)는 이미 자기 안에서 `AI_SERVER_URL`과 `httpx`를 직접 monkeypatch하므로
    이 기본값을 필요할 때 그대로 덮어써 영향받지 않는다.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "AI_SERVER_URL", "")


@pytest.fixture(autouse=True)
def no_real_store_background_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /stores`가 예약하는 백그라운드 작업들이 테스트 중 실제로 돌지 않게 막는다.

    `menu_crawl_service.enrich_menu`는 `session_factory`로 실제 `SessionLocal`
    (로컬 개발 DB)을 그대로 받는다 — `client` 픽스처가 갈아끼우는 건 `get_db`
    (요청 스코프)뿐이라 이 경로는 격리되지 않는다. 안 막으면 `client.post
    ("/stores", ...)`를 쓰는 모든 테스트가 실제 MySQL 네임드 락을 잡고, 실제
    카카오 API를 부르고, 실제 헤드리스 크롬을 띄운다. 2026-08-24에 이것만으로도
    pytest가 40분 넘게 걸린 사고가 있었는데, 그때 만든 차단 픽스처가 이후 리팩터
    (`enrich_menu_from_kakao` → `enrich_menu` 개명)를 거치며 유실됐고 2026-08-31에
    다시 겪었다.

    **`insight_service.generate_trade_area_insight`는 별도로 막지 않는다** —
    같은 파일의 `no_real_ai_server` 픽스처가 `AI_SERVER_URL`을 비워서
    `ai_client.is_enabled()`가 항상 `False`가 되므로, 이 함수는 이미 안전하게
    조기 반환한다. 여기서도 모듈째 바꿔치기하면 `insight_service.list_insights`
    (순수 DB 조회, `GET /stores/{storeId}/insights`가 쓰는 안전한 함수)까지
    같이 막혀버린다 — 2026-08-31에 실제로 이 실수로 관련 테스트 3개가 깨졌다.

    **`enrich_menu`만 함수명이 아니라 모듈 이름째 바꿔치기한다** — 실제 함수를
    patch하면 이름이 또 바뀔 때마다 조용히 무력화되지만, 모듈 참조를 막으면
    `stores.py`가 그 안의 아무 함수를 새로 불러써도 이 차단이 계속 유효하다.
    `enrich_menu` 자체를 검증하는 테스트는 `test_menu_crawl.py`에서 라우터를
    거치지 않고 직접 호출하므로 영향받지 않는다.
    """
    from app.services import menu_crawl

    monkeypatch.setattr(
        "app.api.routers.stores.menu_crawl_service",
        # kakao_place_id()는 URL 파싱이 전부인 순수 함수라 실제 것을 그대로 둔다
        # (몇몇 테스트가 이 파싱 결과 자체를 검증한다). enrich_menu()만 막는다 —
        # 그 안에서 subprocess 크롤링·MySQL 네임드 락을 실제로 잡기 때문이다.
        SimpleNamespace(
            enrich_menu=lambda *a, **kw: None, kakao_place_id=menu_crawl.kakao_place_id
        ),
    )


@pytest.fixture(autouse=True)
def temp_media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """업로드 파일이 테스트마다 임시 디렉터리에 격리되게 한다.

    autouse라 모든 테스트에 적용된다 — 저장소를 안 쓰는 테스트에도 걸어두는 편이,
    새 테스트가 실수로 실제 `media/`에 파일을 남기는 걸 막는다.
    `get_storage`는 lru_cache라 설정을 바꾼 뒤 캐시를 비워야 한다.
    """
    from app.core import config
    from app.storage import factory

    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(config.settings, "MEDIA_ROOT", str(root))
    factory.get_storage.cache_clear()
    yield root
    factory.get_storage.cache_clear()
