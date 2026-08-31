"""가게 등록·조회 로직 (API명세서 2.2, 2.3)."""

import uuid

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.models.store import Store
from app.models.store_insight import StoreInsight
from app.models.store_menu import StoreMenu
from app.models.store_photo import StorePhoto
from app.models.user import User
from app.schemas.store import (
    ImportItemStatus,
    ImportStatusItem,
    ImportStatusResponse,
    StoreCreateRequest,
    StoreUpdateRequest,
)
from app.services import store_photo as photo_service
from app.storage import Storage

# content_type -> 확장자. 원본 파일명을 믿지 않고 여기서 정한다(3.3과 같은 규칙).
LOGO_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


class StoreNotFound(NotFoundError):
    error_code = "STORE_NOT_FOUND"
    message = "가게 정보를 찾을 수 없습니다."


def create_store(db: Session, owner: User, payload: StoreCreateRequest) -> Store:
    """가게를 등록한다.

    후보확정(2.1 검색 결과 선택) / 직접입력 / URL보완 세 경로가 같은 Body를 쓰며,
    무엇으로 등록했는지는 `info_source`가 구분한다(NAVER/KAKAO/MANUAL 등).
    """
    store = Store(
        user_id=owner.id,
        name=payload.name,
        category=payload.category,
        address=payload.address,
        phone=payload.phone,
        info_source=payload.info_source,
        external_channel_url=payload.external_channel_url,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    db.add(store)
    db.commit()
    db.refresh(store)
    return store


def update_store(db: Session, store: Store, payload: StoreUpdateRequest) -> Store:
    """가게 정보를 부분 수정한다 (API명세서 3.1 PATCH).

    요청에 담겨 온 필드만 반영한다. `exclude_unset=True`라서 아예 보내지 않은 필드와
    `null`을 명시적으로 보낸 필드가 구분된다 — 후자는 값을 비우려는 의도로 본다.
    """
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(store, field, value)
    db.commit()
    db.refresh(store)
    return store


def get_store_id_for_user(db: Session, owner: User) -> int | None:
    """이 사용자의 가게 ID를 찾는다(없으면 `None`).

    앱이 "가게 1개" 전제로 짜여 있어 `GET /stores` 같은 목록 조회가 없다 —
    재로그인·재설치 후에도 기존 가게로 바로 들어가려면 이게 유일한 진입점이라
    1.5(`GET /users/me`) 응답에 실어 보낸다(2026-08-31 추가). 여러 개면(원래
    전제에 안 맞는 상태) 가장 먼저 만든 것을 기준으로 삼는다.
    """
    return db.scalar(select(Store.id).where(Store.user_id == owner.id).order_by(Store.id).limit(1))


def get_owned_store(db: Session, owner: User, store_id: int) -> Store:
    """본인 소유 가게를 가져온다.

    남의 가게를 조회하면 403이 아니라 404로 응답한다 — 403은 "그 ID의 가게가
    존재하긴 한다"는 사실을 알려주는 셈이라, 존재 여부 자체를 숨긴다.
    """
    store = db.get(Store, store_id)
    if store is None or store.user_id != owner.id:
        raise StoreNotFound
    return store


def get_import_status(db: Session, store: Store) -> ImportStatusResponse:
    """외부데이터 가져오기 진행상태를 계산한다 (API명세서 2.3).

    상태를 저장하는 컬럼/테이블을 두지 않고 **실제 데이터가 있는지로 계산한다**
    (결정: `docs/IMPLEMENTATION.md` 2026-08-23). 가게가 등록됐다는 것 자체가
    기본정보 수집 완료를 뜻하므로 기본정보는 항상 SUCCESS다.

    메뉴는 `store_menus`, 사진은 `store_photos`, 상권분석은 `store_insights`
    (유형=상권분석)에 데이터가 있으면 SUCCESS다.
    """
    items = [
        # 가게 레코드가 존재한다는 것 자체가 기본정보 수집 완료를 뜻한다
        ImportStatusItem(field="기본정보", status=ImportItemStatus.SUCCESS),
        ImportStatusItem(field="메뉴", status=_status_of(_has_menu(db, store))),
        ImportStatusItem(field="사진", status=_status_of(_has_photo(db, store))),
        ImportStatusItem(field="상권분석", status=_status_of(_has_market_insight(db, store))),
    ]
    return ImportStatusResponse(
        store_id=store.id,
        overall_status=summarize_status([item.status for item in items]),
        items=items,
    )


MARKET_INSIGHT_TYPE = "상권분석"


def _status_of(exists: bool) -> ImportItemStatus:
    """데이터가 있으면 수집 완료, 없으면 아직 안 된 것으로 본다."""
    return ImportItemStatus.SUCCESS if exists else ImportItemStatus.PENDING


def _has_menu(db: Session, store: Store) -> bool:
    return (
        db.scalar(select(StoreMenu.id).where(StoreMenu.store_id == store.id).limit(1)) is not None
    )


def _has_photo(db: Session, store: Store) -> bool:
    return (
        db.scalar(select(StorePhoto.id).where(StorePhoto.store_id == store.id).limit(1)) is not None
    )


def _has_market_insight(db: Session, store: Store) -> bool:
    return (
        db.scalar(
            select(StoreInsight.id)
            .where(
                StoreInsight.store_id == store.id,
                StoreInsight.insight_type == MARKET_INSIGHT_TYPE,
            )
            .limit(1)
        )
        is not None
    )


def summarize_status(statuses: list[ImportItemStatus]) -> ImportItemStatus:
    """항목별 상태를 전체 상태 하나로 요약한다.

    한 소스가 실패해도 전체를 실패로 보지 않는다(기능명세서 S02.2.3
    "한 소스 실패가 전체 등록을 막지 않는다") — 남은 항목이 진행 중이면 IN_PROGRESS다.
    """
    if all(status is ImportItemStatus.SUCCESS for status in statuses):
        return ImportItemStatus.SUCCESS
    if all(status is ImportItemStatus.FAILED for status in statuses):
        return ImportItemStatus.FAILED
    return ImportItemStatus.IN_PROGRESS


def upload_logo(db: Session, storage: Storage, store: Store, upload: UploadFile) -> Store:
    """가게 로고를 저장소에 올리고 `stores.logo_url`에 반영한다 (API명세서 3.6).

    **로고는 가게당 1장이다.** 사진(3.3)처럼 목록을 갖지 않으므로 새로 올리면 이전
    파일을 지운다. 파일명에 UUID를 넣어 매번 키가 달라지게 하는 이유는, 같은 키를
    덮어쓰면 CDN·클라이언트 캐시 때문에 예전 이미지가 계속 보일 수 있어서다.

    DB를 먼저 커밋하고 이전 파일을 나중에 지운다(3.3 삭제와 같은 순서) — 파일 삭제가
    실패해도 사장님에겐 새 로고가 보여야 하고, 남은 건 어디서도 참조되지 않는
    고아 파일이라 나중에 정리할 수 있다.
    """
    extension = photo_service.validate_upload(
        upload,
        allowed_types=settings.allowed_image_type_set,
        extensions=LOGO_EXTENSIONS,
        max_bytes=settings.max_upload_size_bytes,
        limit_mb=settings.MAX_UPLOAD_SIZE_MB,
        unsupported_message="지원하지 않는 파일 형식입니다. 이미지 파일만 업로드할 수 있습니다.",
    )

    previous = store.logo_url
    key = f"stores/{store.id}/logo/{uuid.uuid4().hex}{extension}"
    storage.save(key, upload.file, upload.content_type)

    store.logo_url = key
    db.commit()
    db.refresh(store)

    # 외부 URL(검색으로 가져온 로고)은 우리 저장소 파일이 아니라 지울 대상이 아니다.
    if previous and not previous.startswith(("http://", "https://")):
        storage.delete(previous)
    return store
