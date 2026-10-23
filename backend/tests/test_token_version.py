from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import HTTPException
from jose import jwt
from starlette.requests import Request

try:
    from app import main
    from app.schemas import LoginRequest, PasswordResetConfirm, UserUpdate
    from app.security import JWT_ALGORITHM, create_access_token, decode_access_token
except ModuleNotFoundError:
    from backend.app import main
    from backend.app.schemas import LoginRequest, PasswordResetConfirm, UserUpdate
    from backend.app.security import JWT_ALGORITHM, create_access_token, decode_access_token


class FakeDatabase:
    def commit(self) -> None:
        pass


@pytest.fixture
def account_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    accounts = {
        "user-1": {
            "id": "user-1",
            "email": "reader@example.com",
            "name": "Reader",
            "role": "user",
            "department_id": None,
            "password_hash": "old-hash",
            "token_version": 0,
        }
    }

    def query_one(_db: Any, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        if "FROM password_reset_otps" in sql:
            return {
                "user_id": "user-1",
                "code_hash": "valid-code-hash",
                "used_at": None,
                "code_is_active": 1,
                "attempts": 0,
            }
        if "FROM profiles WHERE id = %s" in sql:
            return accounts.get(params[0])
        return None

    def execute(_db: Any, sql: str, params: tuple = ()) -> int:
        if "UPDATE profiles SET token_version = token_version + 1" in sql:
            accounts[params[0]]["token_version"] += 1
        elif "UPDATE profiles SET password_hash = %s, token_version = token_version + 1" in sql:
            accounts[params[-1]]["password_hash"] = params[0]
            accounts[params[-1]]["token_version"] += 1
        elif "UPDATE profiles SET" in sql and "token_version = token_version + 1" in sql:
            accounts[params[-1]]["token_version"] += 1
        elif "UPDATE profiles SET" in sql and "password_hash" in sql:
            accounts[params[-1]]["password_hash"] = params[0]
        return 1

    monkeypatch.setattr(main, "query_one", query_one)
    monkeypatch.setattr(main, "execute", execute)
    monkeypatch.setattr(main, "hash_password", lambda password: f"hashed:{password}")
    monkeypatch.setattr(main, "verify_password", lambda _password, _hash: True)
    monkeypatch.setattr(main, "password_reset_code_hash", lambda _email, _otp: "valid-code-hash")
    return accounts


def bearer_request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/me",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
            "scheme": "http",
        }
    )


def assert_token_authenticates(account: dict[str, Any], token: str) -> None:
    assert main.require_user(bearer_request(token), FakeDatabase()) == account


def assert_token_rejected(token: str) -> None:
    with pytest.raises(HTTPException) as error:
        main.require_user(bearer_request(token), FakeDatabase())
    assert error.value.status_code == 401


def test_logout_invalidates_token_it_was_called_with(account_store):
    db = FakeDatabase()
    account = account_store["user-1"]
    old_token = create_access_token(account["id"], account["token_version"])
    assert_token_authenticates(account, old_token)

    main.logout(user=account, db=db)

    assert account["token_version"] == 1
    assert_token_rejected(old_token)


def test_password_reset_invalidates_old_tokens_and_allows_new_login(
    account_store, monkeypatch: pytest.MonkeyPatch
):
    db = FakeDatabase()
    account = account_store["user-1"]
    old_token = create_access_token(account["id"], account["token_version"])
    assert_token_authenticates(account, old_token)
    monkeypatch.setattr(main, "query_one", lambda _db, sql, _params=(): (
        {
            "user_id": "user-1",
            "code_hash": "valid-code-hash",
            "used_at": None,
            "code_is_active": 1,
            "attempts": 0,
        }
        if "FROM password_reset_otps" in sql
        else account_store.get("user-1")
    ))

    payload = PasswordResetConfirm(
        email="reader@example.com",
        otp="123456",
        new_password="NewPassword!2",
    )
    main.confirm_password_reset(payload=payload, db=db)

    assert account["token_version"] == 1
    assert_token_rejected(old_token)

    monkeypatch.setattr(main, "issue_login_code", lambda _db, _email, _account: 60)

    login_result = main.login(
        LoginRequest(email="reader@example.com", password="NewPassword!2"),
        db=db,
    )
    assert login_result["otp_required"] is True
    assert "session" not in login_result

    def query_login_otp(_db: Any, sql: str, _params: tuple = ()) -> dict[str, Any]:
        if "FROM login_otps" in sql:
            return {
                "user_id": "user-1",
                "code_hash": main.login_code_hash("reader@example.com", "123456"),
                "used_at": None,
                "code_is_active": 1,
                "attempts": 0,
            }
        return account

    monkeypatch.setattr(main, "query_one", query_login_otp)
    verified_result = main.confirm_login_otp(
        main.LoginOtpConfirm(email="reader@example.com", otp="123456"),
        db=db,
    )
    fresh_token = verified_result["session"]["access_token"]
    assert decode_access_token(fresh_token) == ("user-1", 1)
    assert_token_authenticates(account, fresh_token)


def test_super_admin_login_skips_email_otp(monkeypatch: pytest.MonkeyPatch):
    admin = {
        "id": "admin-1",
        "email": "admin@example.com",
        "name": "Administrator",
        "role": "super_admin",
        "password_hash": "stored-hash",
        "email_verified": 1,
        "token_version": 2,
    }
    db = FakeDatabase()
    activity = []
    monkeypatch.setattr(main, "query_one", lambda *_args: admin)
    monkeypatch.setattr(main, "verify_password", lambda _password, _hash: True)
    monkeypatch.setattr(main, "execute", lambda _db, sql, params=(): activity.append((sql, params)) or 1)
    monkeypatch.setattr(
        main,
        "issue_login_code",
        lambda *_args: pytest.fail("Super-admin login must not request an OTP"),
    )

    result = main.admin_login(
        LoginRequest(email="admin@example.com", password="Password!1"),
        db=db,
    )

    assert result["user"]["role"] == "super_admin"
    assert decode_access_token(result["session"]["access_token"]) == ("admin-1", 2)
    assert len(activity) == 1


def test_admin_login_rejects_non_admin(monkeypatch: pytest.MonkeyPatch):
    reader = {
        "id": "reader-1",
        "email": "reader@example.com",
        "password_hash": "stored-hash",
        "role": "user",
    }
    monkeypatch.setattr(main, "query_one", lambda *_args: reader)
    monkeypatch.setattr(main, "verify_password", lambda _password, _hash: True)

    with pytest.raises(HTTPException) as error:
        main.admin_login(
            LoginRequest(email="reader@example.com", password="Password!1"),
            db=FakeDatabase(),
        )

    assert error.value.status_code == 403


def test_put_password_change_invalidates_old_token(account_store):
    db = FakeDatabase()
    account = account_store["user-1"]
    account["role"] = "super_admin"
    old_token = create_access_token(account["id"], account["token_version"])
    assert_token_authenticates(account, old_token)

    main.update_user(
        user_id=account["id"],
        payload=UserUpdate(password="ChangedPassword!2"),
        db=db,
        user=account,
    )

    assert account["token_version"] == 1
    assert_token_rejected(old_token)


def test_token_without_version_claim_is_rejected(account_store):
    account = account_store["user-1"]
    legacy_token = jwt.encode(
        {
            "sub": account["id"],
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        main.JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    assert decode_access_token(legacy_token) is None
    assert_token_rejected(legacy_token)
