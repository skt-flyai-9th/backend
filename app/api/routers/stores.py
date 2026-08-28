"""가게 API (API명세서 2.1 통합검색 / 2.2 등록 / 2.3 가져오기 진행상태)."""

from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, TypeVar

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.db.session import SessionLocal
from app.schemas.common import MessageResponse
from app.schemas.shortform_session import SessionCreateResponse, SessionOptionResponse
from app.schemas.store import (
    ImportStatusResponse,
    InsightListResponse,
    LogoUploadResponse,
    MenuCreateRequest,
    MenuCreateResponse,
    MenuListResponse,
    MenuResponse,
    MenuUpdateRequest,
    MenuUpdateResponse,
    PhotoCategory,
    PhotoListResponse,
    PhotoResponse,
    PhotoUpdateRequest,
    PhotoUpdateResponse,
    StoreCreateRequest,
    StoreCreateResponse,
    StoreDetailResponse,
    StoreSearchResponse,
    StoreShortItem,
    StoreShortListResponse,
    StoreUpdateRequest,
    StoreUpdateResponse,
    TargetCustomerCreateRequest,
    TargetCustomerCreateResponse,
    TargetCustomerListResponse,
    TargetCustomerUpdateRequest,
    TargetCustomerUpdateResponse,
)
from app.services import menu_crawl as menu_crawl_service
from app.services import shortform_session as session_service
from app.services import store as store_service
from app.services import store_insight as insight_service
from app.services import store_menu as menu_service
from app.services import store_photo as photo_service
from app.services import store_search
from app.services import store_target_customer as target_service
from app.services import video_output as output_service
from app.storage import Storage, get_storage, to_public_url

router = APIRouter(prefix="/stores", tags=["stores"])

StorageDep = Annotated[Storage, Depends(get_storage)]

_ResponseT = TypeVar("_ResponseT", bound=BaseModel)


def _changed_only(schema: type[_ResponseT], entity: object, changed: set[str]) -> _ResponseT:
    """PATCH 응답을 "바꾼 필드 + id + updated_at"으로만 만든다 (API명세서 3.1/3.2/3.4).

    스키마에 없는 요청 필드(예: 메뉴의 `description`처럼 수정은 되지만 응답 예시엔
    없는 값)는 조용히 무시한다. 응답에 담을 키만 넘겨 생성하므로 라우터의
    `response_model_exclude_unset=True`가 나머지를 걸러낸다.
    """
    data: dict[str, object] = {"id": entity.id, "updated_at": entity.updated_at}
    data.update(
        {field: getattr(entity, field) for field in changed if field in schema.model_fields}
    )
    return schema(**data)


@router.get("/search", response_model=StoreSearchResponse)
async def search_stores(
    user: CurrentUser,
    keyword: Annotated[str, Query(min_length=1, description="상호명 또는 주소")],
    latitude: Annotated[
        Decimal | None, Query(description="기준 위도. 주면 distance_m이 채워진다")
    ] = None,
    longitude: Annotated[Decimal | None, Query(description="기준 경도")] = None,
) -> StoreSearchResponse:
    """NAVER·Kakao에서 가게 후보를 찾아 중복을 합쳐 돌려준다.

    결과가 없으면 빈 배열이다(에러가 아니다) — 프론트는 이때 직접 입력을 제안한다
    (기능명세서 S02.1.1).
    """
    del user  # 로그인 확인 용도
    results = await store_search.search_stores(keyword, latitude, longitude)
    return StoreSearchResponse(results=results)


@router.post("", response_model=StoreCreateResponse, status_code=HTTPStatus.CREATED)
def create_store(
    payload: StoreCreateRequest, user: CurrentUser, db: DbSession, background_tasks: BackgroundTasks
) -> StoreCreateResponse:
    """가게를 등록한다. 후보확정 / 직접입력 / URL보완 세 경로를 함께 처리한다.

    검색 단계에서 카카오 플레이스 링크가 잡혀 있으면, 응답을 내보낸 뒤
    백그라운드로 대표 메뉴 몇 개를 자동으로 채운다(실패해도 등록에는 영향 없음).
    상권분석도 같은 방식으로 백그라운드에서 미리 만들어 캐시해둔다(2026-08-27) —
    인사이트 화면(3.5)은 이렇게 저장된 값만 조회하고 그 자리에서 AI를 부르지 않는다.
    """
    store = store_service.create_store(db, user, payload)

    # 2.1 응답의 kakao_place_id를 그대로 돌려받은 게 있으면 그걸 우선한다 —
    # 사장님이 등록 시 external_channel_url을 인스타그램 등 다른 링크로 바꾸면
    # (2026-08-26 실제 사례) URL 파싱만으로는 카카오 ID를 놓친다. 안 보내면
    # 기존처럼 external_channel_url에서 파싱을 시도한다(구버전 클라이언트 대응).
    # 그래도 없으면(네이버로만 잡혔거나 프론트에서 값이 유실됐으면) `enrich_menu`가
    # 이름+좌표로 한 번 더 찾아본다(2026-08-28 추가).
    place_id = payload.kakao_place_id or menu_crawl_service.kakao_place_id(store)
    background_tasks.add_task(menu_crawl_service.enrich_menu, store.id, place_id, SessionLocal)

    background_tasks.add_task(insight_service.generate_trade_area_insight, store.id, SessionLocal)

    status = store_service.get_import_status(db, store)
    return StoreCreateResponse(
        id=store.id,
        name=store.name,
        category=store.category,
        address=store.address,
        info_source=store.info_source,
        import_status=status.overall_status,
        created_at=store.created_at,
    )


@router.get("/{store_id}/import-status", response_model=ImportStatusResponse)
def get_import_status(store_id: int, user: CurrentUser, db: DbSession) -> ImportStatusResponse:
    """외부데이터 가져오기 진행상태를 항목별로 돌려준다."""
    store = store_service.get_owned_store(db, user, store_id)
    return store_service.get_import_status(db, store)


# ---------------------------------------------------------------- 3.1 기본정보 + 브랜드톤


@router.get("/{store_id}", response_model=StoreDetailResponse)
def get_store(
    store_id: int, user: CurrentUser, db: DbSession, storage: StorageDep
) -> StoreDetailResponse:
    """가게 기본정보와 브랜드톤을 돌려준다.

    `logo_url`은 3.6으로 올린 파일이면 저장소 키가 들어 있어 전체 URL로 바꿔서
    내보낸다. 검색으로 가져온 외부 URL은 `to_public_url`이 그대로 통과시킨다.
    """
    store = store_service.get_owned_store(db, user, store_id)
    response = StoreDetailResponse.model_validate(store)
    response.logo_url = to_public_url(storage, store.logo_url)
    return response


@router.patch("/{store_id}", response_model=StoreUpdateResponse, response_model_exclude_unset=True)
def update_store(
    store_id: int, payload: StoreUpdateRequest, user: CurrentUser, db: DbSession
) -> StoreUpdateResponse:
    """가게 정보를 부분 수정한다.

    명세서대로 **바꾼 필드만** 응답에 담는다(`response_model_exclude_unset`).
    """
    store = store_service.get_owned_store(db, user, store_id)
    changed = set(payload.model_dump(exclude_unset=True))
    store = store_service.update_store(db, store, payload)
    return _changed_only(StoreUpdateResponse, store, changed)


# ---------------------------------------------------------------- 3.2 대표메뉴


def _menu_response(storage: Storage, menu: object) -> MenuResponse:
    """DB에는 저장소 키가 들어있을 수 있으므로(3.6 로고와 같은 이유) 응답에서
    전체 URL로 바꿔 내보낸다. 지금까지 이 변환이 빠져 있어(2026-08-27 FE 리포트),
    저장은 되는데 조회한 값으로는 이미지에 접근할 수 없는 문제가 있었다.
    """
    return MenuResponse(
        id=menu.id,
        name=menu.name,
        price=menu.price,
        description=menu.description,
        image_url=to_public_url(storage, menu.image_url),
        is_new_menu=menu.is_new_menu,
        is_event_menu=menu.is_event_menu,
        is_sold_out=menu.is_sold_out,
    )


@router.get("/{store_id}/menus", response_model=MenuListResponse)
def list_menus(
    store_id: int, user: CurrentUser, db: DbSession, storage: StorageDep
) -> MenuListResponse:
    store = store_service.get_owned_store(db, user, store_id)
    menus = menu_service.list_menus(db, store)
    return MenuListResponse(menus=[_menu_response(storage, menu) for menu in menus])


@router.post("/{store_id}/menus", response_model=MenuCreateResponse, status_code=HTTPStatus.CREATED)
def create_menu(
    store_id: int, payload: MenuCreateRequest, user: CurrentUser, db: DbSession
) -> MenuCreateResponse:
    store = store_service.get_owned_store(db, user, store_id)
    return MenuCreateResponse.model_validate(menu_service.create_menu(db, store, payload))


@router.patch(
    "/{store_id}/menus/{menu_id}",
    response_model=MenuUpdateResponse,
    response_model_exclude_unset=True,
)
def update_menu(
    store_id: int,
    menu_id: int,
    payload: MenuUpdateRequest,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
) -> MenuUpdateResponse:
    store = store_service.get_owned_store(db, user, store_id)
    menu = menu_service.get_menu(db, store, menu_id)
    changed = set(payload.model_dump(exclude_unset=True))
    menu = menu_service.update_menu(db, menu, payload)
    response = _changed_only(MenuUpdateResponse, menu, changed)
    if response.image_url is not None:
        response.image_url = to_public_url(storage, response.image_url)
    return response


@router.delete("/{store_id}/menus/{menu_id}", response_model=MessageResponse)
def delete_menu(store_id: int, menu_id: int, user: CurrentUser, db: DbSession) -> MessageResponse:
    store = store_service.get_owned_store(db, user, store_id)
    menu_service.delete_menu(db, menu_service.get_menu(db, store, menu_id))
    return MessageResponse(message="메뉴가 삭제되었습니다.")


# ---------------------------------------------------------------- 3.4 타깃고객


@router.get("/{store_id}/target-customers", response_model=TargetCustomerListResponse)
def list_target_customers(
    store_id: int, user: CurrentUser, db: DbSession
) -> TargetCustomerListResponse:
    store = store_service.get_owned_store(db, user, store_id)
    return TargetCustomerListResponse(
        target_customers=target_service.list_target_customers(db, store)
    )


@router.post(
    "/{store_id}/target-customers",
    response_model=TargetCustomerCreateResponse,
    status_code=HTTPStatus.CREATED,
)
def create_target_customer(
    store_id: int, payload: TargetCustomerCreateRequest, user: CurrentUser, db: DbSession
) -> TargetCustomerCreateResponse:
    store = store_service.get_owned_store(db, user, store_id)
    return TargetCustomerCreateResponse.model_validate(
        target_service.create_target_customer(db, store, payload)
    )


@router.patch(
    "/{store_id}/target-customers/{target_id}",
    response_model=TargetCustomerUpdateResponse,
    response_model_exclude_unset=True,
)
def update_target_customer(
    store_id: int,
    target_id: int,
    payload: TargetCustomerUpdateRequest,
    user: CurrentUser,
    db: DbSession,
) -> TargetCustomerUpdateResponse:
    store = store_service.get_owned_store(db, user, store_id)
    target = target_service.get_target_customer(db, store, target_id)
    changed = set(payload.model_dump(exclude_unset=True))
    target = target_service.update_target_customer(db, target, payload)
    return _changed_only(TargetCustomerUpdateResponse, target, changed)


# ---------------------------------------------------------------- 3.5 인사이트


@router.get("/{store_id}/insights", response_model=InsightListResponse)
def list_insights(
    store_id: int,
    user: CurrentUser,
    db: DbSession,
    type: Annotated[str | None, Query(description="인사이트 유형(상권분석/카드뉴스 등)")] = None,
) -> InsightListResponse:
    """가게 인사이트를 최신순으로 돌려준다. `type`을 주면 해당 유형만 거른다."""
    store = store_service.get_owned_store(db, user, store_id)
    return InsightListResponse(insights=insight_service.list_insights(db, store, type))


# ---------------------------------------------------------------- 3.3 가게사진


def _photo_response(storage: Storage, photo: object) -> PhotoResponse:
    """DB에는 저장소 키가 들어있으므로 응답에서 전체 URL로 바꿔 내보낸다."""
    return PhotoResponse(
        id=photo.id,
        file_url=to_public_url(storage, photo.file_url) or "",
        category=photo.category,
        has_sensitive_info=photo.has_sensitive_info,
        created_at=photo.created_at,
    )


@router.get("/{store_id}/photos", response_model=PhotoListResponse)
def list_photos(
    store_id: int,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    category: Annotated[str | None, Query(description="사진 분류(간판/외관/내부 등)")] = None,
) -> PhotoListResponse:
    store = store_service.get_owned_store(db, user, store_id)
    photos = photo_service.list_photos(db, store, category)
    return PhotoListResponse(photos=[_photo_response(storage, photo) for photo in photos])


@router.post("/{store_id}/photos", response_model=PhotoResponse, status_code=HTTPStatus.CREATED)
def upload_photo(
    store_id: int,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    file: Annotated[UploadFile, File(description="이미지 파일")],
    category: Annotated[PhotoCategory | None, Form(description="사진 분류. 생략하면 기타")] = None,
) -> PhotoResponse:
    """가게 사진을 업로드한다.

    `category`는 선택이다 — AI 자동분류(기능명세서 S03.2.1)가 붙기 전까지 프론트가
    지정하고, 없으면 `기타`로 저장된다.
    """
    store = store_service.get_owned_store(db, user, store_id)
    photo = photo_service.create_photo(db, storage, store, file, category)
    return _photo_response(storage, photo)


@router.patch("/{store_id}/photos/{photo_id}", response_model=PhotoUpdateResponse)
def update_photo(
    store_id: int, photo_id: int, payload: PhotoUpdateRequest, user: CurrentUser, db: DbSession
) -> PhotoUpdateResponse:
    """사진 분류를 사장님이 직접 고친다 (2026-08-26 신설).

    AI 자동분류가 붙기 전까지 잘못 분류된 사진을 고칠 방법이 없었다 — 기능명세서
    S03.2.1 "분류를 수정할 수 있다"를 충족한다. `has_sensitive_info`는 여기서
    다루지 않는다(AI 판별 몫).
    """
    store = store_service.get_owned_store(db, user, store_id)
    photo = photo_service.get_photo(db, store, photo_id)
    photo = photo_service.update_photo_category(db, photo, payload.category)
    return PhotoUpdateResponse(id=photo.id, category=photo.category)


@router.delete("/{store_id}/photos/{photo_id}", response_model=MessageResponse)
def delete_photo(
    store_id: int, photo_id: int, user: CurrentUser, db: DbSession, storage: StorageDep
) -> MessageResponse:
    store = store_service.get_owned_store(db, user, store_id)
    photo_service.delete_photo(db, storage, photo_service.get_photo(db, store, photo_id))
    return MessageResponse(message="사진이 삭제되었습니다.")


# ---------------------------------------------------------------- 3.6 가게 로고 업로드


@router.post("/{store_id}/logo", response_model=LogoUploadResponse)
def upload_logo(
    store_id: int,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    file: Annotated[UploadFile, File(description="로고 이미지 파일")],
) -> LogoUploadResponse:
    """가게 로고를 업로드한다.

    로고는 파일이라 `multipart/form-data`가 필요하고 `PATCH /stores/{storeId}`(3.1)는
    JSON 부분수정이라, 3.3(가게사진)·9.2(촬영본)와 같이 별도 엔드포인트로 뒀다.
    """
    store = store_service.get_owned_store(db, user, store_id)
    store = store_service.upload_logo(db, storage, store, file)
    return LogoUploadResponse(
        store_id=store.id,
        logo_url=to_public_url(storage, store.logo_url),
        updated_at=store.updated_at,
    )


# ---------------------------------------------------------------- 15.2 완성 숏폼 목록


@router.get("/{store_id}/shorts", response_model=StoreShortListResponse)
def list_store_shorts(
    store_id: int,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    page: Annotated[int, Query(ge=1, description="페이지 번호(1부터)")] = 1,
    size: Annotated[int, Query(ge=1, le=100, description="페이지 크기")] = 20,
) -> StoreShortListResponse:
    """마이페이지의 완성 숏폼 그리드 목록.

    **완성된 영상만** 최신순으로, 프로젝트당 1개씩 준다. 제작 중인 프로젝트까지
    보려면 4.1 `GET /shorts-projects`를 쓴다.
    """
    store = store_service.get_owned_store(db, user, store_id)
    rows, total = output_service.list_store_shorts(db, store, page, size)
    return StoreShortListResponse(
        items=[
            StoreShortItem(
                video_output_id=output.id,
                shorts_project_id=project.id,
                project_title=project.project_title,
                promotion_purpose=project.promotion_purpose,
                video_url=to_public_url(storage, output.video_url),
                cover_image_url=to_public_url(storage, output.cover_image_url),
                duration_sec=duration,
                is_posted=is_posted,
                created_at=output.created_at,
            )
            for output, project, duration, is_posted in rows
        ],
        page=page,
        size=size,
        total=total,
    )


@router.post(
    "/{store_id}/shortform-sessions",
    response_model=SessionCreateResponse,
    status_code=HTTPStatus.CREATED,
)
def create_shortform_session(
    store_id: int, user: CurrentUser, db: DbSession
) -> SessionCreateResponse:
    """숏폼 Agent와의 대화를 시작한다 (R06, 2026-08-26 재설계).

    프로젝트 없이 가게만으로 시작한다 — 대화로 홍보 목적을 정하고 나서(6.x → accept)
    그 결과로 4.1의 프로젝트를 만든다. 대화는
    `POST /shortform-sessions/{sessionId}/turns`로 이어간다.
    """
    session, greeting = session_service.create_session(db, user, store_id)
    return SessionCreateResponse(
        id=session.id,
        status=session.status,
        assistant_message=greeting.assistant_message,
        options=[SessionOptionResponse(id=o.id, label=o.label) for o in greeting.options],
        project_state=greeting.project_state,
    )
