"""인증 관련 요청/응답 스키마 (API명세서 1.2~1.4)."""

from pydantic import EmailStr, Field, field_validator

from app.core.security import MAX_PASSWORD_BYTES
from app.schemas.common import BaseSchema, UtcDatetime

MIN_PASSWORD_LENGTH = 8


def _validate_password_bytes(value: str) -> str:
    # bcrypt는 72바이트를 넘는 입력을 거부한다. 한글은 글자당 3바이트라 글자 수만으로는
    # 막을 수 없어 바이트 길이로 검사한다.
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"비밀번호는 UTF-8 기준 {MAX_PASSWORD_BYTES}바이트를 넘을 수 없습니다.")
    return value


class SignupRequest(BaseSchema):
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    name: str = Field(min_length=1, max_length=100)
    terms_agreed: bool
    marketing_agreed: bool = False

    _check_password = field_validator("password")(_validate_password_bytes)


class SignupResponse(BaseSchema):
    id: int
    email: str
    name: str
    is_active: bool
    terms_agreed: bool
    marketing_agreed: bool
    agreed_at: UtcDatetime | None
    created_at: UtcDatetime


class LoginRequest(BaseSchema):
    email: EmailStr
    password: str

    _check_password = field_validator("password")(_validate_password_bytes)


class LoginUser(BaseSchema):
    id: int
    email: str
    name: str


class LoginResponse(BaseSchema):
    access_token: str
    refresh_token: str
    expires_in: int
    user: LoginUser


class RefreshRequest(BaseSchema):
    refresh_token: str


class RefreshResponse(BaseSchema):
    access_token: str
    expires_in: int


class WithdrawRequest(BaseSchema):
    """회원탈퇴 요청. `reason`은 저장하지 않고 로그로만 남긴다."""

    reason: str | None = Field(default=None, max_length=500)


class WithdrawResponse(BaseSchema):
    message: str
    deleted_at: UtcDatetime


class UserProfileResponse(BaseSchema):
    """1.5 GET — 회원정보 조회."""

    id: int
    email: str
    name: str
    phone: str | None
    marketing_agreed: bool
    created_at: UtcDatetime
    # 등록해둔 가게가 있으면 그 ID, 없으면 null(온보딩 필요). 앱이 재로그인·
    # 재설치 후에도 이 값으로 바로 GET /stores/{storeId}를 불러 기존 가게로
    # 들어갈 수 있다(2026-08-31 추가, User 모델엔 없는 값이라 라우터에서 채운다).
    store_id: int | None = None


class UserProfileUpdateRequest(BaseSchema):
    """1.5 PATCH — 회원정보 수정.

    `email`(로그인 식별자)과 비밀번호는 여기서 바꾸지 않는다. `terms_agreed`도
    철회 개념이 아니라 제외했다 — 철회는 탈퇴(`DELETE /users/me`)다.
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    marketing_agreed: bool | None = None


class UserProfileUpdateResponse(BaseSchema):
    """바꾼 필드 + id + updated_at만 담는다 (3.1·3.2·3.4와 같은 방식)."""

    id: int
    name: str | None = None
    phone: str | None = None
    marketing_agreed: bool | None = None
    updated_at: UtcDatetime
