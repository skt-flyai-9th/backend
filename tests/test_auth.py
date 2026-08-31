"""인증 API 테스트 (API명세서 1.2~1.4)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import create_refresh_token
from app.models.user import User

SIGNUP_BODY: dict[str, Any] = {
    "email": "boss01@example.com",
    "phone": "01012345678",
    "password": "sarils1234!",
    "name": "김사장",
    "terms_agreed": True,
    "marketing_agreed": False,
}


def _signup(client: TestClient, **overrides: Any) -> Any:
    return client.post("/auth/signup", json={**SIGNUP_BODY, **overrides})


def _login_tokens(client: TestClient) -> dict[str, Any]:
    _signup(client)
    response = client.post(
        "/auth/login",
        json={"email": SIGNUP_BODY["email"], "password": SIGNUP_BODY["password"]},
    )
    assert response.status_code == 200
    return response.json()


def _auth_header(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


# ---------------------------------------------------------------- 1.2 회원가입


def test_signup_returns_spec_fields(client: TestClient) -> None:
    response = _signup(client)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "email",
        "name",
        "is_active",
        "terms_agreed",
        "marketing_agreed",
        "agreed_at",
        "created_at",
    }
    assert body["email"] == SIGNUP_BODY["email"]
    assert body["name"] == SIGNUP_BODY["name"]
    assert body["is_active"] is True
    assert body["terms_agreed"] is True
    assert body["marketing_agreed"] is False
    assert body["agreed_at"].endswith("Z")
    assert body["created_at"].endswith("Z")


def test_signup_stores_password_as_hash(client: TestClient, db_session: Session) -> None:
    _signup(client)

    user = db_session.scalar(select(User).where(User.email == SIGNUP_BODY["email"]))
    assert user is not None
    assert user.password_hash is not None
    assert user.password_hash != SIGNUP_BODY["password"]


def test_signup_rejects_duplicate_email(client: TestClient) -> None:
    _signup(client)

    response = _signup(client, name="다른사장")

    assert response.status_code == 409
    assert response.json()["error_code"] == "EMAIL_ALREADY_EXISTS"


def test_signup_requires_terms_agreement(client: TestClient) -> None:
    response = _signup(client, terms_agreed=False)

    assert response.status_code == 400
    assert response.json()["error_code"] == "TERMS_NOT_AGREED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "not-an-email"),
        ("password", "short"),
        ("name", ""),
    ],
)
def test_signup_validates_body(client: TestClient, field: str, value: str) -> None:
    response = _signup(client, **{field: value})

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert field in body["message"]


def test_signup_rejects_password_over_bcrypt_limit(client: TestClient) -> None:
    # 한글 25자 = 75바이트 > bcrypt 72바이트 한계
    response = _signup(client, password="가" * 25)

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- 1.3 로그인


def test_login_returns_tokens_and_user(client: TestClient) -> None:
    tokens = _login_tokens(client)

    assert set(tokens) == {"access_token", "refresh_token", "expires_in", "user"}
    assert tokens["expires_in"] == 3600
    assert set(tokens["user"]) == {"id", "email", "name"}
    assert tokens["user"]["email"] == SIGNUP_BODY["email"]


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("boss01@example.com", "wrong-password"),
        ("nobody@example.com", "sarils1234!"),
    ],
)
def test_login_rejects_wrong_credentials(client: TestClient, email: str, password: str) -> None:
    _signup(client)

    response = client.post("/auth/login", json={"email": email, "password": password})

    assert response.status_code == 401
    # 가입 여부가 드러나지 않도록 두 경우 모두 같은 에러를 쓴다
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"


def test_login_rejects_withdrawn_account(client: TestClient) -> None:
    tokens = _login_tokens(client)
    client.request("DELETE", "/users/me", headers=_auth_header(tokens["access_token"]))

    response = client.post(
        "/auth/login",
        json={"email": SIGNUP_BODY["email"], "password": SIGNUP_BODY["password"]},
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "INACTIVE_USER"


# ---------------------------------------------------------------- 1.4 로그아웃 / 세션갱신


def test_logout_requires_authentication(client: TestClient) -> None:
    response = client.post("/auth/logout")

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"


def test_logout_returns_message(client: TestClient) -> None:
    tokens = _login_tokens(client)

    response = client.post("/auth/logout", headers=_auth_header(tokens["access_token"]))

    assert response.status_code == 200
    assert response.json() == {"message": "로그아웃 되었습니다."}


def test_refresh_returns_new_access_token(client: TestClient) -> None:
    tokens = _login_tokens(client)

    response = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "expires_in"}
    assert body["expires_in"] == 3600

    # 새 액세스 토큰으로 인증이 통과해야 한다
    protected = client.post("/auth/logout", headers=_auth_header(body["access_token"]))
    assert protected.status_code == 200


def test_refresh_rejects_access_token(client: TestClient) -> None:
    tokens = _login_tokens(client)

    response = client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


def test_refresh_rejects_token_of_unknown_user(client: TestClient) -> None:
    response = client.post(
        "/auth/refresh",
        json={"refresh_token": create_refresh_token(user_id=999_999).token},
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"


# ---------------------------------------------------------------- 1.4 회원탈퇴


def test_withdraw_requires_authentication(client: TestClient) -> None:
    response = client.request("DELETE", "/users/me", json={"reason": "서비스 이용 종료"})

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"


def test_withdraw_deactivates_instead_of_deleting(client: TestClient, db_session: Session) -> None:
    tokens = _login_tokens(client)

    response = client.request(
        "DELETE",
        "/users/me",
        headers=_auth_header(tokens["access_token"]),
        json={"reason": "서비스 이용 종료"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "탈퇴가 완료되었습니다."
    assert body["deleted_at"].endswith("Z")

    user = db_session.scalar(select(User).where(User.email == SIGNUP_BODY["email"]))
    assert user is not None  # 하드 삭제가 아니다
    assert user.is_active is False


def test_withdraw_works_without_body(client: TestClient) -> None:
    tokens = _login_tokens(client)

    response = client.request("DELETE", "/users/me", headers=_auth_header(tokens["access_token"]))

    assert response.status_code == 200


def test_withdrawn_user_cannot_use_access_token(client: TestClient) -> None:
    tokens = _login_tokens(client)
    client.request("DELETE", "/users/me", headers=_auth_header(tokens["access_token"]))

    response = client.post("/auth/logout", headers=_auth_header(tokens["access_token"]))

    assert response.status_code == 401
    assert response.json()["error_code"] == "INACTIVE_USER"


# ---------------------------------------------------------------- 1.5 회원정보 조회 / 수정


def test_get_profile_returns_spec_fields(client: TestClient) -> None:
    tokens = _login_tokens(client)

    response = client.get("/users/me", headers=_auth_header(tokens["access_token"]))

    assert response.status_code == 200
    assert set(response.json()) == {
        "id",
        "email",
        "name",
        "phone",
        "marketing_agreed",
        "created_at",
        "store_id",
    }


def test_get_profile_store_id_is_null_before_registration(client: TestClient) -> None:
    """가게를 등록하지 않은 사용자는 `store_id`가 `null`이다 — 온보딩 필요 신호."""
    tokens = _login_tokens(client)

    response = client.get("/users/me", headers=_auth_header(tokens["access_token"]))

    assert response.json()["store_id"] is None


def test_get_profile_store_id_reflects_registered_store(client: TestClient) -> None:
    """가게 등록 후엔 그 가게 ID가 온다 — 재로그인·재설치 후 앱의 유일한 진입점."""
    tokens = _login_tokens(client)
    headers = _auth_header(tokens["access_token"])
    create_response = client.post(
        "/stores",
        json={"name": "행복분식", "category": "분식", "address": "서울 강남구 테헤란로 1"},
        headers=headers,
    )
    assert create_response.status_code == 201, create_response.text

    response = client.get("/users/me", headers=headers)

    assert response.json()["store_id"] == create_response.json()["id"]


def test_patch_profile_returns_only_changed_fields(client: TestClient) -> None:
    tokens = _login_tokens(client)

    response = client.patch(
        "/users/me",
        json={"name": "박사장"},
        headers=_auth_header(tokens["access_token"]),
    )

    assert response.status_code == 200
    assert set(response.json()) == {"id", "name", "updated_at"}
    assert response.json()["name"] == "박사장"


def test_patch_profile_updates_allowed_fields(client: TestClient) -> None:
    tokens = _login_tokens(client)
    headers = _auth_header(tokens["access_token"])

    client.patch(
        "/users/me",
        json={"name": "박사장", "phone": "01099998888", "marketing_agreed": True},
        headers=headers,
    )

    profile = client.get("/users/me", headers=headers).json()
    assert profile["name"] == "박사장"
    assert profile["phone"] == "01099998888"
    assert profile["marketing_agreed"] is True


def test_patch_profile_cannot_change_email(client: TestClient) -> None:
    """email은 로그인 식별자라 수정 대상이 아니다 — 보내도 무시된다."""
    tokens = _login_tokens(client)
    headers = _auth_header(tokens["access_token"])

    client.patch("/users/me", json={"email": "hacked@example.com"}, headers=headers)

    assert client.get("/users/me", headers=headers).json()["email"] == SIGNUP_BODY["email"]


def test_patch_profile_leaves_unsent_fields(client: TestClient) -> None:
    tokens = _login_tokens(client)
    headers = _auth_header(tokens["access_token"])

    client.patch("/users/me", json={"marketing_agreed": True}, headers=headers)

    profile = client.get("/users/me", headers=headers).json()
    assert profile["marketing_agreed"] is True
    assert profile["name"] == SIGNUP_BODY["name"]  # 안 보낸 필드는 그대로


def test_profile_requires_authentication(client: TestClient) -> None:
    assert client.get("/users/me").status_code == 401
    assert client.patch("/users/me", json={"name": "x"}).status_code == 401
