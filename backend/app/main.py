import os
import re
import hashlib
import hmac
import logging
import secrets
import smtplib
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import uuid4

import pymysql
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pymysql.cursors import DictCursor
from pydantic import EmailStr
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from .ai_preparation import (
    AIProviderError,
    AIUnavailableError,
    ai_provider_status,
    MAX_IMPORTANT_QUESTIONS,
    MAX_MOCK_QUESTIONS,
    MAX_SOURCE_PAPERS,
    MAX_TOTAL_SOURCE_PAGES,
    PDFProcessingError,
    build_ai_prompt,
    build_subject_options,
    extract_pdf_pages,
    generate_with_groq,
    parse_ai_questions,
    select_papers_for_year_range,
    validate_and_enrich_questions,
)
from .authorization import permitted_user_changes
from .database import (
    connect_mysql,
    create_tables,
    ensure_database_exists,
    get_db,
    verify_schema,
)
from .dev_seed import seed_demo_data
from .file_storage import (
    delete_institute_document,
    delete_uploaded_file,
    resolve_uploaded_file,
)
from .object_storage import (
    ObjectStorageConfigurationError,
    delete_stored_resource_file,
    get_resource_object,
    is_object_storage_path,
    store_resource_file,
    validate_resource_storage_configuration,
)
from .mailer import (
    send_email_verification_otp,
    send_institute_approval_email,
    send_login_otp,
    send_password_reset_otp,
)
from .rate_limit import SlidingWindowLimiter, client_address
from .schemas import (
    AIPreparationGenerateRequest,
    DepartmentCreate,
    DepartmentUpdate,
    EmailVerificationConfirm,
    EmailVerificationRequest,
    InstituteApprovalUpdate,
    LoginRequest,
    LoginOtpConfirm,
    LoginOtpResend,
    Password,
    PasswordResetConfirm,
    PasswordResetRequest,
    PaperCreate,
    PaperUpdate,
    Phone,
    RegistrationText,
    RegisterRequest,
    ResourceReviewUpdate,
    ResourceUploadPermissionUpdate,
    UserCreate,
    UserUpdate,
)
from .security import (
    JWT_SECRET,
    create_access_token,
    decode_access_token,
    hash_password,
    token_version_is_current,
    verify_password,
)

load_dotenv()

logger = logging.getLogger(__name__)
PASSWORD_RESET_COOLDOWN_SECONDS = 60
PASSWORD_RESET_EXPIRY_MINUTES = 10
PASSWORD_RESET_MAX_ATTEMPTS = 5
EMAIL_VERIFICATION_COOLDOWN_SECONDS = 60
EMAIL_VERIFICATION_EXPIRY_MINUTES = 10
EMAIL_VERIFICATION_MAX_ATTEMPTS = 5
LOGIN_OTP_COOLDOWN_SECONDS = 60
LOGIN_OTP_EXPIRY_MINUTES = 10
LOGIN_OTP_MAX_ATTEMPTS = 5

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _configured_directory(setting: str, default: str) -> Path:
    """Resolve a configured directory against the backend folder.

    Resolving against the current working directory made the upload location
    depend on where the process was started from, so starting the API from the
    repository root silently wrote files to a different place than the
    /uploads mount served.
    """
    configured = Path(os.getenv(setting, default))
    if not configured.is_absolute():
        configured = BACKEND_DIR / configured
    return configured.resolve()


def _configured_int(setting: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(setting, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{setting} must be a whole number, got {raw!r}") from error
    if value < minimum:
        raise RuntimeError(f"{setting} must be at least {minimum}, got {value}")
    return value


UPLOAD_DIR = _configured_directory("UPLOAD_DIR", "uploads")
INSTITUTE_DOCUMENT_DIR = (UPLOAD_DIR.parent / "institute_documents").resolve()
RESOURCE_UPLOAD_DIR = _configured_directory("RESOURCE_UPLOAD_DIR", "resource_uploads")
ADMIN_DASHBOARD_DIR = BACKEND_DIR / "admin"
MAX_UPLOAD_MB = _configured_int("MAX_UPLOAD_MB", 20)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
INSTITUTE_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
if (
    RESOURCE_UPLOAD_DIR.resolve() == UPLOAD_DIR.resolve()
    or UPLOAD_DIR.resolve() in RESOURCE_UPLOAD_DIR.resolve().parents
):
    raise RuntimeError("RESOURCE_UPLOAD_DIR must be outside the public UPLOAD_DIR")
RESOURCE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Throttles. Keyed per client address for anonymous endpoints and per user id
# for the authenticated AI endpoint, so students sharing one campus NAT are not
# punished for each other's traffic.
LOGIN_LIMITER = SlidingWindowLimiter(limit=15, window_seconds=15 * 60)
REGISTER_LIMITER = SlidingWindowLimiter(limit=5, window_seconds=60 * 60)
PASSWORD_RESET_LIMITER = SlidingWindowLimiter(limit=5, window_seconds=60 * 60)
EMAIL_VERIFICATION_REQUEST_LIMITER = SlidingWindowLimiter(limit=5, window_seconds=60 * 60)
EMAIL_VERIFICATION_CONFIRM_LIMITER = SlidingWindowLimiter(limit=10, window_seconds=60 * 60)
LOGIN_OTP_RESEND_LIMITER = SlidingWindowLimiter(limit=5, window_seconds=60 * 60)
LOGIN_OTP_CONFIRM_LIMITER = SlidingWindowLimiter(limit=10, window_seconds=60 * 60)
AI_HOURLY_LIMIT = 5
AI_DAILY_LIMIT = 20
AI_HOURLY_LIMITER = SlidingWindowLimiter(limit=AI_HOURLY_LIMIT, window_seconds=60 * 60)
AI_DAILY_LIMITER = SlidingWindowLimiter(limit=AI_DAILY_LIMIT, window_seconds=24 * 60 * 60)
AI_QUOTA_LOCK = threading.Lock()


def ai_generation_quota(user: dict) -> dict:
    quota_key = str(user.get("id") or user.get("email") or "unknown-user")
    return {
        "hourly": AI_HOURLY_LIMITER.status(quota_key),
        "daily": AI_DAILY_LIMITER.status(quota_key),
    }


def consume_ai_generation_quota(user: dict) -> None:
    quota_key = str(user.get("id") or user.get("email") or "unknown-user")
    limits = (
        ("hourly", AI_HOURLY_LIMITER, AI_HOURLY_LIMIT,
         "You have reached your per-account hourly AI request limit."),
        ("daily", AI_DAILY_LIMITER, AI_DAILY_LIMIT,
         "You have reached your per-account daily AI request limit."),
    )
    # Check both windows before consuming either one, so a daily rejection
    # does not accidentally use an hourly request.
    with AI_QUOTA_LOCK:
        for _name, limiter, limit, message in limits:
            result = limiter.peek(quota_key)
            if not result.allowed:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    f"{message} The limit is {limit} requests; wait for the displayed retry time.",
                    headers={"Retry-After": str(result.retry_after)},
                )
        AI_HOURLY_LIMITER.check(quota_key)
        AI_DAILY_LIMITER.check(quota_key)


def rate_limit_dependency(limiter: SlidingWindowLimiter, message: str):
    """Build a FastAPI dependency that throttles a route per client address."""

    def dependency(request: Request) -> None:
        result = limiter.check(client_address(request))
        if not result.allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                message,
                headers={"Retry-After": str(result.retry_after)},
            )

    return dependency


login_limit = rate_limit_dependency(
    LOGIN_LIMITER, "Too many sign-in attempts. Wait a few minutes and try again."
)
register_limit = rate_limit_dependency(
    REGISTER_LIMITER, "Too many account creations from this device. Try again later."
)
password_reset_limit = rate_limit_dependency(
    PASSWORD_RESET_LIMITER, "Too many reset requests. Try again later."
)
email_verification_request_limit = rate_limit_dependency(
    EMAIL_VERIFICATION_REQUEST_LIMITER, "Too many verification requests. Try again later."
)
email_verification_confirm_limit = rate_limit_dependency(
    EMAIL_VERIFICATION_CONFIRM_LIMITER, "Too many verification attempts. Try again later."
)
login_otp_resend_limit = rate_limit_dependency(
    LOGIN_OTP_RESEND_LIMITER, "Too many sign-in code requests. Try again later."
)
login_otp_confirm_limit = rate_limit_dependency(
    LOGIN_OTP_CONFIRM_LIMITER, "Too many sign-in code attempts. Try again later."
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the startup checks and surface them through FastAPI's lifespan API."""
    initialize_database()
    yield


app = FastAPI(title="Campus-notes API", version="1.0.0", lifespan=lifespan)
origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:8081").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/admin", StaticFiles(directory=ADMIN_DASHBOARD_DIR, html=True), name="admin")


def query_one(db, sql: str, params: tuple = ()):
    with db.cursor(DictCursor) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


def query_all(db, sql: str, params: tuple = ()):
    with db.cursor(DictCursor) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def execute(db, sql: str, params: tuple = ()) -> int:
    with db.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def user_json(user: dict) -> dict:
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user.get("username"),
        "name": user["name"],
        "role": user["role"],
        "email_verified": bool(user.get("email_verified", 1)),
        "approval_status": user.get("approval_status", "approved"),
        "resource_upload_permission_status": user.get("resource_upload_permission_status", "not_requested"),
        "institute_name": user.get("institute_name"),
        "phone": user.get("phone"),
        "designation": user.get("designation"),
        "institute_document_name": user.get("institute_document_name"),
        "has_institute_document": bool(user.get("institute_document_path")),
        "department_id": user.get("department_id"),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


def paper_json(paper: dict) -> dict:
    return {
        "id": paper["id"],
        "subject": paper["subject"],
        "subject_code": paper.get("subject_code"),
        "department_id": paper["department_id"],
        "department_name": paper.get("department_name"),
        "semester": paper["semester"],
        "exam_type": paper["exam_type"],
        "year": paper["year"],
        "description": paper.get("description"),
        "file_url": paper.get("file_url"),
        "uploaded_by": paper.get("uploaded_by"),
        "uploaded_by_name": paper.get("uploaded_by_name"),
        "views": paper["views"],
        "downloads": paper["downloads"],
        "created_at": paper.get("created_at"),
        "updated_at": paper.get("updated_at"),
    }


def resource_json(resource: dict) -> dict:
    return {
        "id": resource["id"],
        "title": resource["title"],
        "resource_type": resource["resource_type"],
        "department_id": resource.get("department_id"),
        "department_name": resource.get("department_name"),
        "subject": resource.get("subject"),
        "semester": resource.get("semester"),
        "academic_year": resource.get("academic_year"),
        "description": resource.get("description"),
        "file_url": f"/api/resources/{resource['id']}/file",
        "original_file_name": resource["original_file_name"],
        "uploaded_by": resource.get("uploaded_by"),
        "uploaded_by_name": resource.get("uploaded_by_name"),
        "status": resource["status"],
        "reviewed_by": resource.get("reviewed_by"),
        "reviewed_at": resource.get("reviewed_at"),
        "created_at": resource.get("created_at"),
        "updated_at": resource.get("updated_at"),
    }


def resolve_resource_file(file_path: object) -> Path | None:
    if not isinstance(file_path, str) or not file_path.strip() or is_object_storage_path(file_path):
        return None
    if Path(file_path).name != file_path:
        return None
    root = RESOURCE_UPLOAD_DIR.resolve()
    candidate = (root / file_path).resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def delete_resource_file(file_path: object) -> bool:
    return delete_stored_resource_file(file_path, RESOURCE_UPLOAD_DIR)


PAPER_SELECT = """
    SELECT p.*, d.name AS department_name
    FROM papers AS p
    LEFT JOIN departments AS d ON d.id = p.department_id
"""

AI_PAPER_SELECT = """
    SELECT p.id, p.subject, p.subject_code, p.department_id,
           d.name AS department_name, p.semester, p.exam_type, p.year,
           p.file_url, p.created_at
    FROM papers AS p
    LEFT JOIN departments AS d ON d.id = p.department_id
"""


def require_user(
    request: Request,
    db=Depends(get_db),
) -> dict:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    token_data = decode_access_token(token)
    if not token_data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user_id, token_version = token_data
    user = query_one(db, "SELECT * FROM profiles WHERE id = %s", (user_id,))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User account not found")
    if not bool(user.get("email_verified", 1)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verify your email before using the app")
    if not token_version_is_current(token_version, user["token_version"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return user


def get_optional_user(
    request: Request,
    db=Depends(get_db),
) -> dict | None:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    token_data = decode_access_token(token)
    if not token_data:
        return None
    user_id, token_version = token_data
    user = query_one(db, "SELECT * FROM profiles WHERE id = %s", (user_id,))
    if (
        not user
        or not bool(user.get("email_verified", 1))
        or not token_version_is_current(token_version, user["token_version"])
    ):
        return None
    return user


def require_roles(user: dict, roles: set[str]) -> None:
    if user["role"] == "institute" and user.get("approval_status") != "approved":
        status_message = (
            "Institute account approval is pending"
            if user.get("approval_status") == "pending"
            else "Institute account is not approved"
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, status_message)
    if user["role"] not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")


def can_manage_department(user: dict, department_id: str, action: str = "write") -> None:
    """Authorise a paper write/delete against a department.

    System administrators may manage all departments. Approved institute
    accounts may publish and correct papers across departments, but deleting
    is reserved for administrators so one college cannot remove another
    college's material.
    """
    require_roles(user, {"super_admin", "institute"})
    if user["role"] == "institute" and action == "delete":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Institute accounts cannot delete question papers",
        )


def downgrade_legacy_department_admins(db) -> int:
    downgraded = execute(
        db,
        """
        UPDATE profiles
        SET role = 'user', department_id = NULL, token_version = token_version + 1
        WHERE role = 'dept_admin'
        """,
    )
    if downgraded:
        db.commit()
        logger.warning(
            "Converted %s legacy department administrator account(s) to reader accounts",
            downgraded,
        )
    return downgraded


def commit_or_conflict(db) -> None:
    try:
        db.commit()
    except pymysql.err.IntegrityError as error:
        db.rollback()
        if error.args and error.args[0] == 1062:
            raise HTTPException(status.HTTP_409_CONFLICT, "A record with these details already exists") from error
        raise
    except Exception:
        db.rollback()
        raise


def bump_token_version(db, user_id: str) -> None:
    execute(
        db,
        "UPDATE profiles SET token_version = token_version + 1 WHERE id = %s",
        (user_id,),
    )


def _actor_label(user: dict) -> str:
    """A human-readable actor name for activity messages.

    Callers and tests may pass a partial profile, so fall back rather than
    raising and losing the audit row.
    """
    return user.get("name") or user.get("email") or "Staff member"


def record_activity(db, activity_type: str, message: str, user_id: str | None = None) -> None:
    """Insert an activity-log row from trusted server-side code only.

    This is deliberately not exposed over HTTP: the public create endpoint was
    removed so students cannot forge entries in the admin activity feed.
    """
    execute(
        db,
        """
        INSERT INTO activity_logs (id, type, message, user_id, created_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (str(uuid4()), activity_type, message, user_id),
    )


def password_reset_code_hash(email: str, otp: str) -> str:
    return hmac.new(
        JWT_SECRET.encode("utf-8"),
        f"{email}:{otp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def email_verification_code_hash(email: str, otp: str) -> str:
    return hmac.new(
        JWT_SECRET.encode("utf-8"),
        f"email-verification:{email}:{otp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def login_code_hash(email: str, otp: str) -> str:
    return hmac.new(
        JWT_SECRET.encode("utf-8"),
        f"login-otp:{email}:{otp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_login_code(db, email: str, account: dict) -> int:
    execute(db, """
        INSERT IGNORE INTO login_otps (email, user_id, code_hash, expires_at, resend_after, attempts, used_at)
        VALUES (%s, NULL, NULL, NULL, UTC_TIMESTAMP(), 0, NULL)
    """, (email,))
    record = query_one(db, """
        SELECT *, resend_after > UTC_TIMESTAMP() AS cooldown_active,
            GREATEST(0, TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), resend_after)) AS cooldown_seconds
        FROM login_otps WHERE email = %s FOR UPDATE
    """, (email,))
    if record["cooldown_active"]:
        db.commit()
        return max(1, record["cooldown_seconds"])
    otp = f"{secrets.randbelow(1_000_000):06d}"
    execute(db, """
        UPDATE login_otps SET user_id = %s, code_hash = %s,
            expires_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE),
            resend_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND),
            attempts = 0, used_at = NULL WHERE email = %s
    """, (account["id"], login_code_hash(email, otp), LOGIN_OTP_EXPIRY_MINUTES,
           LOGIN_OTP_COOLDOWN_SECONDS, email))
    db.commit()
    try:
        send_login_otp(email, account["name"], otp)
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
        logger.exception("Unable to send sign-in code")
        execute(db, "UPDATE login_otps SET code_hash = NULL, expires_at = NULL, resend_after = UTC_TIMESTAMP() WHERE email = %s", (email,))
        db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not send sign-in code. Check email configuration and try again.")
    return LOGIN_OTP_COOLDOWN_SECONDS


def issue_email_verification_code(db, email: str, account: dict) -> int:
    """Create one expiring OTP, persist its HMAC, and deliver it by email."""
    execute(
        db,
        """
        INSERT IGNORE INTO email_verification_otps (
            email, user_id, code_hash, expires_at, resend_after, attempts, used_at
        ) VALUES (%s, NULL, NULL, NULL, UTC_TIMESTAMP(), 0, NULL)
        """,
        (email,),
    )
    verification = query_one(
        db,
        """
        SELECT *,
            resend_after > UTC_TIMESTAMP() AS cooldown_active,
            GREATEST(0, TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), resend_after)) AS cooldown_seconds
        FROM email_verification_otps
        WHERE email = %s
        FOR UPDATE
        """,
        (email,),
    )
    if verification["cooldown_active"]:
        db.commit()
        return max(1, verification["cooldown_seconds"])

    otp = f"{secrets.randbelow(1_000_000):06d}"
    execute(
        db,
        """
        UPDATE email_verification_otps
        SET user_id = %s,
            code_hash = %s,
            expires_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE),
            resend_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND),
            attempts = 0,
            used_at = NULL
        WHERE email = %s
        """,
        (
            account["id"],
            email_verification_code_hash(email, otp),
            EMAIL_VERIFICATION_EXPIRY_MINUTES,
            EMAIL_VERIFICATION_COOLDOWN_SECONDS,
            email,
        ),
    )
    db.commit()
    try:
        send_email_verification_otp(email, account["name"], otp)
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
        logger.exception("Unable to send email verification code")
        execute(
            db,
            """
            UPDATE email_verification_otps
            SET code_hash = NULL, expires_at = NULL, resend_after = UTC_TIMESTAMP()
            WHERE email = %s
            """,
            (email,),
        )
        db.commit()
    return EMAIL_VERIFICATION_COOLDOWN_SECONDS


def initialize_database() -> None:
    validate_resource_storage_configuration()
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if bool(admin_email) != bool(admin_password):
        raise RuntimeError("Configure both ADMIN_EMAIL and ADMIN_PASSWORD, or leave both empty")

    schema_auto_migrate = os.getenv("SCHEMA_AUTO_MIGRATE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    seed_demo_enabled = os.getenv("SEED_DEMO_DATA", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if schema_auto_migrate:
        logger.warning("SCHEMA_AUTO_MIGRATE is enabled; applying schema setup at startup")
        ensure_database_exists()
        create_tables()
    else:
        logger.info(
            "Schema creation/migration skipped; set SCHEMA_AUTO_MIGRATE=true to enable it"
        )

    try:
        connection = connect_mysql()
    except pymysql.MySQLError as error:
        raise RuntimeError(
            "Unable to connect to the configured Campus-notes database. Confirm MYSQL_* "
            "settings and apply backend/schema.sql, or enable SCHEMA_AUTO_MIGRATE=true."
        ) from error
    try:
        verify_schema(connection)
        downgrade_legacy_department_admins(connection)
        if seed_demo_enabled:
            inserted = seed_demo_data(connection)
            connection.commit()
            logger.info("Development demo seed enabled; inserted %s missing records", inserted)
        else:
            logger.info("Development demo seed skipped; set SEED_DEMO_DATA=true to enable it")

        if admin_email:
            existing = query_one(
                connection,
                "SELECT id FROM profiles WHERE email = %s",
                (admin_email,),
            )
            if not existing:
                execute(
                    connection,
                    """
                    INSERT INTO profiles (id, email, name, password_hash, role, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, 'super_admin', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        str(uuid4()),
                        admin_email,
                        os.getenv("ADMIN_NAME", "Campus-notes Administrator"),
                        hash_password(admin_password),
                    ),
                )
                commit_or_conflict(connection)
        else:
            logger.info("Admin bootstrap skipped; ADMIN_EMAIL and ADMIN_PASSWORD are unset")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@app.get("/api/health")
def health():
    """Liveness plus non-secret AI provider diagnostics."""
    return {"status": "ok", "version": app.version, "ai": ai_provider_status()}


@app.get("/uploads/{filename}")
def get_uploaded_paper(filename: str, _user: dict = Depends(require_user)):
    file_path = resolve_uploaded_file(f"/uploads/{filename}", UPLOAD_DIR)
    if file_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper file not found")
    return FileResponse(
        file_path,
        media_type="application/pdf",
        filename=file_path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@app.post(
    "/api/auth/register",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(register_limit)],
)
def register(payload: RegisterRequest, db=Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Name is required")

    email = payload.email.lower()
    if query_one(db, "SELECT id FROM profiles WHERE LOWER(email) = %s", (email,)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")

    profile_id = str(uuid4())
    execute(
        db,
        """
        INSERT INTO profiles (
            id, email, name, password_hash, role, email_verified, approval_status,
            institute_name, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 0, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            profile_id,
            email,
            name,
            hash_password(payload.password),
            "user",
            "approved",
            None,
        ),
    )
    execute(
        db,
        """
        INSERT INTO activity_logs (id, type, message, user_id, created_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (
            str(uuid4()),
            "registration",
            f"{name} registered a user account",
            profile_id,
        ),
    )
    commit_or_conflict(db)
    user = query_one(db, "SELECT * FROM profiles WHERE id = %s", (profile_id,))
    resend_after = issue_email_verification_code(db, email, user)
    return {
        "verification_required": True,
        "email": email,
        "resend_after_seconds": resend_after,
        "message": "Account created. Enter the verification code sent to your email before signing in.",
    }


@app.post(
    "/api/auth/register/institute",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(register_limit)],
)
async def register_institute(
    name: Annotated[RegistrationText, Form(max_length=160)],
    email: Annotated[EmailStr, Form()],
    password: Annotated[Password, Form()],
    phone: Annotated[Phone, Form()],
    designation: Annotated[RegistrationText, Form(max_length=120)],
    institute_name: Annotated[RegistrationText, Form(max_length=200)],
    file: UploadFile = File(...),
    db=Depends(get_db),
):
    name = name.strip()
    phone = phone.strip()
    designation = designation.strip()
    institute_name = institute_name.strip()
    if not all((name, phone, designation, institute_name)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "All institute staff details are required")

    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    allowed_suffixes = {".pdf", ".jpg", ".jpeg", ".png"}
    if suffix not in allowed_suffixes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Supporting document must be a PDF, JPG, or PNG file")

    contents = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(contents) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Maximum supporting document size is {MAX_UPLOAD_MB} MB",
        )

    valid_content = (
        (suffix == ".pdf" and contents.startswith(b"%PDF"))
        or (suffix in {".jpg", ".jpeg"} and contents.startswith(b"\xff\xd8\xff"))
        or (suffix == ".png" and contents.startswith(b"\x89PNG\r\n\x1a\n"))
    )
    if not valid_content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Supporting document does not match its file type")

    email = email.lower()
    if query_one(db, "SELECT id FROM profiles WHERE LOWER(email) = %s", (email,)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")

    profile_id = str(uuid4())
    document_path = INSTITUTE_DOCUMENT_DIR / f"{profile_id}{suffix}"
    try:
        document_path.write_bytes(contents)
        execute(
            db,
            """
            INSERT INTO profiles (
                id, email, name, password_hash, role, approval_status, institute_name,
                email_verified, phone, designation, institute_document_path, institute_document_name,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 'institute', 'pending', %s, 0, %s, %s, %s, %s,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                profile_id,
                email,
                name,
                hash_password(password),
                institute_name,
                phone,
                designation,
                str(document_path),
                filename,
            ),
        )
        execute(
            db,
            """
            INSERT INTO activity_logs (id, type, message, user_id, created_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid4()),
                "registration",
                f"{name} registered an institute staff account pending approval",
                profile_id,
            ),
        )
        commit_or_conflict(db)
    except Exception:
        document_path.unlink(missing_ok=True)
        raise

    user = query_one(db, "SELECT * FROM profiles WHERE id = %s", (profile_id,))
    resend_after = issue_email_verification_code(db, email, user)
    return {
        "verification_required": True,
        "email": email,
        "resend_after_seconds": resend_after,
        "message": "Account created. Verify your email first; institute access also requires administrator approval.",
    }


@app.post(
    "/api/auth/email-verification/request",
    dependencies=[Depends(email_verification_request_limit)],
)
def request_email_verification(
    payload: EmailVerificationRequest,
    db=Depends(get_db),
):
    email = payload.email.lower()
    account = query_one(
        db,
        "SELECT id, name FROM profiles WHERE LOWER(email) = %s AND email_verified = 0",
        (email,),
    )
    if account:
        resend_after = issue_email_verification_code(db, email, account)
    else:
        db.commit()
        resend_after = EMAIL_VERIFICATION_COOLDOWN_SECONDS
    return {
        "message": "If this email needs verification, a code has been sent.",
        "resend_after_seconds": resend_after,
    }


@app.post(
    "/api/auth/email-verification/confirm",
    dependencies=[Depends(email_verification_confirm_limit)],
)
def confirm_email_verification(
    payload: EmailVerificationConfirm,
    db=Depends(get_db),
):
    email = payload.email.lower()
    verification = query_one(
        db,
        """
        SELECT *, expires_at > UTC_TIMESTAMP() AS code_is_active
        FROM email_verification_otps
        WHERE email = %s
        FOR UPDATE
        """,
        (email,),
    )
    account = query_one(
        db,
        "SELECT * FROM profiles WHERE LOWER(email) = %s FOR UPDATE",
        (email,),
    )
    valid = bool(
        account
        and not account.get("email_verified")
        and verification
        and verification["user_id"] == account["id"]
        and verification["code_hash"]
        and not verification["used_at"]
        and verification["code_is_active"]
        and verification["attempts"] < EMAIL_VERIFICATION_MAX_ATTEMPTS
        and hmac.compare_digest(
            verification["code_hash"],
            email_verification_code_hash(email, payload.otp),
        )
    )
    if not valid:
        if verification and verification["attempts"] < EMAIL_VERIFICATION_MAX_ATTEMPTS:
            execute(
                db,
                "UPDATE email_verification_otps SET attempts = attempts + 1 WHERE email = %s",
                (email,),
            )
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired verification code")

    execute(db, "UPDATE profiles SET email_verified = 1 WHERE id = %s", (account["id"],))
    execute(
        db,
        "UPDATE email_verification_otps SET used_at = UTC_TIMESTAMP() WHERE email = %s",
        (email,),
    )
    db.commit()
    account = query_one(db, "SELECT * FROM profiles WHERE id = %s", (account["id"],))
    return {
        "user": user_json(account),
        "session": {
            "access_token": create_access_token(account["id"], account["token_version"]),
            "token_type": "bearer",
        },
    }


@app.post("/api/auth/login", dependencies=[Depends(login_limit)])
def login(payload: LoginRequest, db=Depends(get_db)):
    user = query_one(
        db,
        "SELECT * FROM profiles WHERE LOWER(email) = %s",
        (payload.email.lower(),),
    )
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not bool(user.get("email_verified", 1)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Verify your email before signing in. Request a new code if needed.",
        )
    resend_after = issue_login_code(db, user["email"].lower(), user)
    return {
        "otp_required": True,
        "email": user["email"],
        "resend_after_seconds": resend_after,
        "message": "If the account is eligible, a sign-in code has been sent.",
    }


@app.post("/api/auth/admin/login", dependencies=[Depends(login_limit)])
def admin_login(payload: LoginRequest, db=Depends(get_db)):
    user = query_one(
        db,
        "SELECT * FROM profiles WHERE LOWER(email) = %s",
        (payload.email.lower(),),
    )
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if user["role"] != "super_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator account required")
    if not bool(user.get("email_verified", 1)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Verify your email before signing in.",
        )
    execute(
        db,
        """
        INSERT INTO activity_logs (id, type, message, user_id, created_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (str(uuid4()), "login", f"{user['name']} logged in", user["id"]),
    )
    db.commit()
    return {
        "user": user_json(user),
        "session": {
            "access_token": create_access_token(user["id"], user["token_version"]),
            "token_type": "bearer",
        },
    }


@app.post("/api/auth/login/otp/resend", dependencies=[Depends(login_otp_resend_limit)])
def resend_login_otp(payload: LoginOtpResend, db=Depends(get_db)):
    user = query_one(db, "SELECT * FROM profiles WHERE LOWER(email) = %s", (payload.email.lower(),))
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not bool(user.get("email_verified", 1)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verify your email before signing in.")
    resend_after = issue_login_code(db, user["email"].lower(), user)
    return {"message": "If the account is eligible, a sign-in code has been sent.", "resend_after_seconds": resend_after}


@app.post("/api/auth/login/otp/confirm", dependencies=[Depends(login_otp_confirm_limit)])
def confirm_login_otp(payload: LoginOtpConfirm, db=Depends(get_db)):
    email = payload.email.lower()
    code = query_one(db, "SELECT *, expires_at > UTC_TIMESTAMP() AS code_is_active FROM login_otps WHERE email = %s FOR UPDATE", (email,))
    account = query_one(db, "SELECT * FROM profiles WHERE LOWER(email) = %s FOR UPDATE", (email,))
    valid = bool(
        account and bool(account.get("email_verified", 1)) and code
        and code["user_id"] == account["id"] and code["code_hash"] and not code["used_at"]
        and code["code_is_active"] and code["attempts"] < LOGIN_OTP_MAX_ATTEMPTS
        and hmac.compare_digest(code["code_hash"], login_code_hash(email, payload.otp))
    )
    if not valid:
        if code and not code["used_at"] and code["attempts"] < LOGIN_OTP_MAX_ATTEMPTS:
            execute(db, "UPDATE login_otps SET attempts = attempts + 1 WHERE email = %s", (email,))
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired sign-in code")
    execute(db, "UPDATE login_otps SET used_at = UTC_TIMESTAMP() WHERE email = %s", (email,))
    execute(
        db,
        """
        INSERT INTO activity_logs (id, type, message, user_id, created_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (str(uuid4()), "login", f"{account['name']} logged in", account["id"]),
    )
    db.commit()
    account = query_one(db, "SELECT * FROM profiles WHERE id = %s", (account["id"],))
    return {
        "user": user_json(account),
        "session": {
            "access_token": create_access_token(account["id"], account["token_version"]),
            "token_type": "bearer",
        },
    }


@app.post(
    "/api/auth/password-reset/request",
    dependencies=[Depends(password_reset_limit)],
)
def request_password_reset(payload: PasswordResetRequest, db=Depends(get_db)):
    email = payload.email.lower()
    account = query_one(
        db,
        "SELECT id, name FROM profiles WHERE LOWER(email) = %s",
        (email,),
    )
    execute(
        db,
        """
        INSERT IGNORE INTO password_reset_otps (
            email, user_id, code_hash, expires_at, resend_after, attempts, used_at
        ) VALUES (%s, NULL, NULL, NULL, UTC_TIMESTAMP(), 0, NULL)
        """,
        (email,),
    )
    reset = query_one(
        db,
        """
        SELECT *,
            resend_after > UTC_TIMESTAMP() AS cooldown_active,
            GREATEST(0, TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), resend_after)) AS cooldown_seconds
        FROM password_reset_otps
        WHERE email = %s
        FOR UPDATE
        """,
        (email,),
    )
    if reset["cooldown_active"]:
        db.commit()
        return {
            "message": "If an account exists for this email, a reset code has been sent.",
            "resend_after_seconds": max(1, reset["cooldown_seconds"]),
        }

    otp = f"{secrets.randbelow(1_000_000):06d}" if account else None
    execute(
        db,
        """
        UPDATE password_reset_otps
        SET user_id = %s,
            code_hash = %s,
            expires_at = IF(%s IS NULL, NULL, DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE)),
            resend_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND),
            attempts = 0,
            used_at = NULL
        WHERE email = %s
        """,
        (
            account["id"] if account else None,
            password_reset_code_hash(email, otp) if otp else None,
            otp,
            PASSWORD_RESET_EXPIRY_MINUTES,
            PASSWORD_RESET_COOLDOWN_SECONDS,
            email,
        ),
    )
    db.commit()

    if account and otp:
        try:
            send_password_reset_otp(email, account["name"], otp)
        except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
            # Returning a different status here would tell an attacker whether
            # the address has an account (a signup/enumeration oracle). Keep the
            # response identical for every address, and make the failure visible
            # in the server log instead.
            logger.exception("Unable to send password reset email")
            execute(
                db,
                """
                UPDATE password_reset_otps
                SET code_hash = NULL, expires_at = NULL, resend_after = UTC_TIMESTAMP()
                WHERE email = %s
                """,
                (email,),
            )
            db.commit()

    return {
        "message": "If an account exists for this email, a reset code has been sent.",
        "resend_after_seconds": PASSWORD_RESET_COOLDOWN_SECONDS,
    }


@app.post("/api/auth/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirm, db=Depends(get_db)):
    email = payload.email.lower()
    reset = query_one(
        db,
        """
        SELECT *, expires_at > UTC_TIMESTAMP() AS code_is_active
        FROM password_reset_otps
        WHERE email = %s
        FOR UPDATE
        """,
        (email,),
    )
    valid = bool(
        reset
        and reset["user_id"]
        and reset["code_hash"]
        and not reset["used_at"]
        and reset["code_is_active"]
        and reset["attempts"] < PASSWORD_RESET_MAX_ATTEMPTS
        and hmac.compare_digest(reset["code_hash"], password_reset_code_hash(email, payload.otp))
    )
    if not valid:
        if reset and reset["attempts"] < PASSWORD_RESET_MAX_ATTEMPTS:
            execute(
                db,
                "UPDATE password_reset_otps SET attempts = attempts + 1 WHERE email = %s",
                (email,),
            )
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset code")

    execute(
        db,
        "UPDATE profiles SET password_hash = %s, token_version = token_version + 1 WHERE id = %s",
        (hash_password(payload.new_password), reset["user_id"]),
    )
    execute(
        db,
        "UPDATE password_reset_otps SET used_at = UTC_TIMESTAMP() WHERE email = %s",
        (email,),
    )
    execute(
        db,
        """
        INSERT INTO activity_logs (id, type, message, user_id, created_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (str(uuid4()), "password_reset", "Password reset by email code", reset["user_id"]),
    )
    db.commit()
    return {"message": "Password reset successfully. Sign in with your new password."}


@app.post("/api/auth/logout")
def logout(user: dict = Depends(require_user), db=Depends(get_db)):
    bump_token_version(db, user["id"])
    execute(
        db,
        """
        INSERT INTO activity_logs (id, type, message, user_id, created_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (str(uuid4()), "logout", f"{user['name']} logged out", user["id"]),
    )
    db.commit()
    return {"message": "Logged out successfully"}


@app.get("/api/auth/me")
def current_user(user: dict = Depends(require_user)):
    return {"user": user_json(user)}


@app.get("/api/departments")
def list_departments(db=Depends(get_db)):
    return query_all(db, "SELECT * FROM departments ORDER BY name")


@app.get("/api/departments/{department_id}")
def get_department(department_id: str, db=Depends(get_db)):
    department = query_one(db, "SELECT * FROM departments WHERE id = %s", (department_id,))
    if not department:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    return department


@app.post("/api/departments", status_code=status.HTTP_201_CREATED)
def create_department(
    payload: DepartmentCreate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin", "institute", "user"})
    if (
        user["role"] == "user"
        and user.get("resource_upload_permission_status") != "approved"
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Reader upload permission is required to add a department.",
        )
    department_id = str(uuid4())
    execute(
        db,
        """
        INSERT INTO departments (id, name, code, created_at, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (department_id, payload.name.strip(), payload.code.strip().upper()),
    )
    commit_or_conflict(db)
    return query_one(db, "SELECT * FROM departments WHERE id = %s", (department_id,))


@app.put("/api/departments/{department_id}")
def update_department(
    department_id: str,
    payload: DepartmentUpdate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin", "institute"})
    if not query_one(db, "SELECT id FROM departments WHERE id = %s", (department_id,)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return query_one(db, "SELECT * FROM departments WHERE id = %s", (department_id,))
    updates = []
    values = []
    for key, value in changes.items():
        updates.append(f"`{key}` = %s")
        values.append(value.strip().upper() if key == "code" else value.strip() if value else value)
    values.append(department_id)
    execute(db, f"UPDATE departments SET {', '.join(updates)} WHERE id = %s", tuple(values))
    commit_or_conflict(db)
    return query_one(db, "SELECT * FROM departments WHERE id = %s", (department_id,))


@app.delete("/api/departments/{department_id}")
def delete_department(
    department_id: str,
    force: bool = Query(default=False),
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    """Delete a department and everything that cascades from it.

    The foreign keys cascade to papers and favourites. Because that can remove
    user data, the caller must confirm the exact impact first and pass force=true.
    """
    require_roles(user, {"super_admin"})
    if not query_one(db, "SELECT id FROM departments WHERE id = %s", (department_id,)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")

    impact = query_one(
        db,
        """
        SELECT
            (SELECT COUNT(*) FROM papers WHERE department_id = %s) AS papers,
            (SELECT COUNT(*) FROM favorites AS f
                JOIN papers AS p ON p.id = f.paper_id
                WHERE p.department_id = %s) AS favorites
        """,
        (department_id, department_id),
    ) or {"papers": 0, "favorites": 0}

    file_rows = query_all(db, "SELECT file_url FROM papers WHERE department_id = %s", (department_id,))

    if impact["papers"] and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            (
                f"This department still has {impact['papers']} question paper(s), which would also "
                f"delete {impact['favorites']} favourite(s). Re-send with force=true to proceed."
            ),
        )

    execute(db, "DELETE FROM departments WHERE id = %s", (department_id,))
    record_activity(db, "department_deleted", f"{_actor_label(user)} deleted department {department_id}", user.get("id"))
    db.commit()

    for row in file_rows:
        delete_uploaded_file(row.get("file_url"), UPLOAD_DIR)

    return {
        "message": "Department deleted",
        "deleted": {
            "papers": impact["papers"],
            "favorites": impact["favorites"],
        },
    }


@app.get("/api/papers")
def list_papers(
    department: str | None = None,
    semester: int | None = Query(default=None, ge=1, le=12),
    examType: str | None = None,
    year: str | None = None,
    search: str | None = None,
    sortBy: str = "newest",
    db=Depends(get_db),
):
    conditions = []
    params = []
    if department:
        conditions.append("p.department_id = %s")
        params.append(department)
    if semester:
        conditions.append("p.semester = %s")
        params.append(semester)
    if examType:
        conditions.append("p.exam_type = %s")
        params.append(examType)
    if year:
        conditions.append("p.year = %s")
        params.append(year)
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append("(p.subject LIKE %s OR p.subject_code LIKE %s OR p.description LIKE %s)")
        params.extend((pattern, pattern, pattern))
    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sort_clause = {
        "newest": "p.created_at DESC",
        "oldest": "p.created_at ASC",
        "name": "p.subject ASC",
        "downloads": "p.downloads DESC",
    }.get(sortBy, "p.created_at DESC")
    rows = query_all(db, PAPER_SELECT + where_clause + f" ORDER BY {sort_clause}", tuple(params))
    return [paper_json(paper) for paper in rows]


@app.get("/api/resources")
def list_resources(
    department: str | None = None,
    resource_type: Literal["notes", "assignments", "other"] | None = None,
    search: str | None = None,
    db=Depends(get_db),
    user: dict | None = Depends(get_optional_user),
):
    conditions = []
    params = []
    if user is None:
        conditions.append("r.status = 'approved'")
    elif user["role"] != "super_admin":
        conditions.append("(r.status = 'approved' OR r.uploaded_by = %s)")
        params.append(user["id"])
    if department:
        conditions.append("r.department_id = %s")
        params.append(department)
    if resource_type:
        conditions.append("r.resource_type = %s")
        params.append(resource_type)
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append("(r.title LIKE %s OR r.subject LIKE %s OR r.description LIKE %s)")
        params.extend((pattern, pattern, pattern))
    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = query_all(
        db,
        """
        SELECT r.*, d.name AS department_name
        FROM resources AS r
        LEFT JOIN departments AS d ON d.id = r.department_id
        """ + where_clause + " ORDER BY r.created_at DESC",
        tuple(params),
    )
    return [resource_json(resource) for resource in rows]


@app.post("/api/resources", status_code=status.HTTP_201_CREATED)
async def create_resource(
    file: UploadFile = File(...),
    title: str = Form(min_length=2, max_length=200),
    resource_type: Literal["notes", "assignments", "other"] = Form(...),
    department_id: str = Form(min_length=1, max_length=36),
    subject: str | None = Form(default=None, max_length=200),
    semester: int | None = Form(default=None, ge=1, le=12),
    academic_year: str | None = Form(default=None, max_length=8),
    description: str | None = Form(default=None, max_length=2000),
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"user", "super_admin"})
    if user["role"] == "user" and user.get("resource_upload_permission_status") != "approved":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Request and receive administrator permission before uploading study resources",
        )
    title = title.strip()
    if len(title) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Title must contain at least 2 characters")
    if academic_year and not re.fullmatch(r"\d{4}(?:-\d{2})?", academic_year.strip()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Academic year must be YYYY or YYYY-YY")
    if not query_one(db, "SELECT id FROM departments WHERE id = %s", (department_id,)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    if file.content_type != "application/pdf" or Path(file.filename or "").suffix.lower() != ".pdf":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only PDF files are allowed")
    contents = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(contents) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Maximum upload size is {MAX_UPLOAD_MB} MB")
    if not contents.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File content is not a valid PDF")

    resource_id = str(uuid4())
    file_name = f"{resource_id}.pdf"
    try:
        stored_file_path = await run_in_threadpool(
            store_resource_file,
            file_name,
            contents,
            RESOURCE_UPLOAD_DIR,
        )
    except ObjectStorageConfigurationError:
        logger.exception("Resource storage configuration is invalid")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Resource storage is not configured") from None
    except Exception:
        logger.exception("Could not store uploaded resource")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not store uploaded resource") from None
    review_status = "approved" if user["role"] in {"super_admin", "user"} else "pending"
    try:
        execute(
            db,
            """
            INSERT INTO resources (
                id, title, resource_type, department_id, subject, semester, academic_year,
                description, file_path, original_file_name, uploaded_by, uploaded_by_name,
                status, reviewed_by, reviewed_at, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, IF(%s = 'approved', CURRENT_TIMESTAMP, NULL), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (
                resource_id,
                title,
                resource_type,
                department_id,
                subject.strip() if subject and subject.strip() else None,
                semester,
                academic_year.strip() if academic_year else None,
                description.strip() if description and description.strip() else None,
                stored_file_path,
                Path(file.filename or "resource.pdf").name,
                user["id"],
                user["name"],
                review_status,
                user["id"] if review_status == "approved" else None,
                review_status,
            ),
        )
        record_activity(
            db,
            "resource_submitted" if review_status == "pending" else "resource_published",
            f"{_actor_label(user)} submitted {resource_type} resource '{title}'",
            user.get("id"),
        )
        commit_or_conflict(db)
    except Exception:
        db.rollback()
        delete_resource_file(stored_file_path)
        raise
    resource = query_one(
        db,
        """
        SELECT r.*, d.name AS department_name
        FROM resources AS r LEFT JOIN departments AS d ON d.id = r.department_id
        WHERE r.id = %s
        """,
        (resource_id,),
    )
    return resource_json(resource)


@app.post("/api/resources/upload-permission")
def request_resource_upload_permission(
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"user"})
    current = user.get("resource_upload_permission_status", "not_requested")
    if current == "approved":
        return {"status": current}
    if current == "pending":
        return {"status": current}
    execute(
        db,
        "UPDATE profiles SET resource_upload_permission_status = 'pending' WHERE id = %s",
        (user["id"],),
    )
    record_activity(db, "resource_upload_permission_requested", f"{user['name']} requested study resource upload permission", user["id"])
    commit_or_conflict(db)
    return {"status": "pending"}


@app.put("/api/users/{user_id}/resource-upload-permission")
def update_resource_upload_permission(
    user_id: str,
    payload: ResourceUploadPermissionUpdate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin"})
    target = query_one(db, "SELECT id, name, role FROM profiles WHERE id = %s", (user_id,))
    if not target or target["role"] != "user":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reader account not found")
    execute(
        db,
        "UPDATE profiles SET resource_upload_permission_status = %s WHERE id = %s",
        (payload.status, user_id),
    )
    record_activity(
        db,
        f"resource_upload_permission_{payload.status}",
        f"{_actor_label(user)} {payload.status} study resource upload permission for {target['name']}",
        user["id"],
    )
    commit_or_conflict(db)
    return {"user_id": user_id, "status": payload.status}


@app.get("/api/resources/{resource_id}/file")
def get_resource_file(
    resource_id: str,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    resource = query_one(db, "SELECT * FROM resources WHERE id = %s", (resource_id,))
    if not resource:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    can_view = (
        resource["status"] == "approved"
        or user["role"] == "super_admin"
        or resource.get("uploaded_by") == user["id"]
    )
    if not can_view:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    stored_path = resource.get("file_path")
    if is_object_storage_path(stored_path):
        try:
            stored_object = get_resource_object(stored_path)
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code")
            if error_code in {"NoSuchKey", "NoSuchObject", "404", "NotFound"}:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource file not found") from error
            logger.exception("Could not retrieve private resource from object storage")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not retrieve resource file") from None
        except Exception:
            logger.exception("Could not retrieve private resource from object storage")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not retrieve resource file") from None
        if stored_object is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource file not found")

        body = stored_object["Body"]

        def stream_object():
            try:
                yield from body.iter_chunks(chunk_size=64 * 1024)
            finally:
                body.close()

        filename = Path(resource["original_file_name"]).name
        headers = {
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
        }
        if stored_object.get("ContentLength") is not None:
            headers["Content-Length"] = str(stored_object["ContentLength"])
        return StreamingResponse(stream_object(), media_type="application/pdf", headers=headers)

    path = resolve_resource_file(stored_path)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource file not found")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=Path(resource["original_file_name"]).name,
        headers={"Cache-Control": "private, no-store"},
    )


@app.put("/api/resources/{resource_id}/review")
def review_resource(
    resource_id: str,
    payload: ResourceReviewUpdate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin"})
    resource = query_one(db, "SELECT title, resource_type FROM resources WHERE id = %s", (resource_id,))
    if not resource:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    execute(
        db,
        """
        UPDATE resources
        SET status = %s, reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (payload.status, user["id"], resource_id),
    )
    record_activity(
        db,
        f"resource_{payload.status}",
        f"{_actor_label(user)} {payload.status} {resource['resource_type']} resource '{resource['title']}'",
        user.get("id"),
    )
    commit_or_conflict(db)
    return {"id": resource_id, "status": payload.status}


@app.delete("/api/resources/{resource_id}")
def delete_resource(
    resource_id: str,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    resource = query_one(db, "SELECT * FROM resources WHERE id = %s", (resource_id,))
    if not resource:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    if user["role"] != "super_admin" and (
        resource.get("uploaded_by") != user["id"] or resource["status"] == "approved"
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only remove your own unapproved submissions")
    execute(db, "DELETE FROM resources WHERE id = %s", (resource_id,))
    record_activity(
        db,
        "resource_deleted",
        f"{_actor_label(user)} deleted resource '{resource['title']}'",
        user.get("id"),
    )
    commit_or_conflict(db)
    delete_resource_file(resource.get("file_path"))
    return {"message": "Resource deleted"}


@app.get("/api/ai-prep/options")
def ai_preparation_options(
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"user"})
    rows = query_all(
        db,
        AI_PAPER_SELECT + " WHERE p.file_url IS NOT NULL AND TRIM(p.file_url) <> ''",
    )
    return {
        "subjects": build_subject_options(rows, UPLOAD_DIR),
        "quota": ai_generation_quota(user),
    }


@app.get("/api/ai-prep/quota")
def ai_preparation_quota(user: dict = Depends(require_user)):
    require_roles(user, {"user"})
    return ai_generation_quota(user)


@app.post("/api/ai-prep/generate")
def generate_ai_preparation(
    payload: AIPreparationGenerateRequest,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"user"})
    count_limit = (
        MAX_MOCK_QUESTIONS
        if payload.feature == "mock_test"
        else MAX_IMPORTANT_QUESTIONS
    )
    if payload.question_count > count_limit:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"This feature supports at most {count_limit} questions.",
        )
    if payload.start_year > payload.end_year:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The start year must be before or equal to the end year.",
        )

    rows = query_all(
        db,
        AI_PAPER_SELECT
        + """
        WHERE p.subject = %s
          AND p.department_id = %s
          AND p.semester = %s
          AND p.subject_code <=> %s
        """,
        (
            payload.subject,
            payload.department_id,
            payload.semester,
            payload.subject_code,
        ),
    )
    papers, missing_years = select_papers_for_year_range(
        rows,
        payload.start_year,
        payload.end_year,
        UPLOAD_DIR,
    )
    if not papers:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No uploaded question-paper PDFs are available for this subject and year range.",
        )
    available_years = {paper["_year"] for paper in papers}
    if payload.start_year not in available_years or payload.end_year not in available_years:
        years = ", ".join(str(year) for year in sorted(available_years)) or "none"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Choose start and end years from available PDF years: {years}.",
        )
    if len(papers) > MAX_SOURCE_PAPERS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"The selected range includes {len(papers)} source PDFs; narrow it to "
            f"{MAX_SOURCE_PAPERS} or fewer.",
        )

    # Don't use a request allowance for an invalid or empty paper selection.
    # Consume it before expensive PDF/OCR work starts.
    consume_ai_generation_quota(user)

    pages_by_paper = {}
    try:
        total_pages = 0
        for paper in papers:
            paper_id = str(paper["id"])
            pages_by_paper[paper_id] = extract_pdf_pages(paper["_path"])
            total_pages += len(pages_by_paper[paper_id])
            if total_pages > MAX_TOTAL_SOURCE_PAGES:
                raise PDFProcessingError(
                    f"The selected PDFs exceed the {MAX_TOTAL_SOURCE_PAGES}-page analysis limit. "
                    "Narrow the year range or choose fewer source papers."
                )
        prompt = build_ai_prompt(
            payload.feature,
            papers,
            pages_by_paper,
            payload.question_count,
        )
    except PDFProcessingError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error

    try:
        provider_response, provider = generate_with_groq(
            prompt,
            consent_to_groq=payload.hosted_fallback_consent,
        )
        questions = parse_ai_questions(provider_response)
        questions = validate_and_enrich_questions(
            questions,
            papers,
            pages_by_paper,
            feature=payload.feature,
            question_count=payload.question_count,
        )
    except AIUnavailableError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    except AIProviderError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    generated_count = len(questions)
    return {
        "feature": payload.feature,
        "provider": provider,
        "hosted_provider_disclosure": (
            "Source PDF text was sent to Groq."
            if provider == "hosted"
            else None
        ),
        "source_papers_found": len(papers),
        "missing_years": missing_years,
        "requested_count": payload.question_count,
        "generated_count": generated_count,
        "questions": questions,
        "message": (
            f"Only {generated_count} distinct questions could be supported by the selected "
            "papers; no duplicate questions were added to reach the requested count."
            if generated_count < payload.question_count
            else None
        ),
        "historical_note": (
            "Importance is estimated from the selected historical papers and does not "
            "guarantee what will appear on a future examination."
            if payload.feature == "important_questions"
            else None
        ),
    }


@app.get("/api/papers/{paper_id}")
def get_paper(paper_id: str, db=Depends(get_db)):
    paper = query_one(db, PAPER_SELECT + " WHERE p.id = %s", (paper_id,))
    if not paper:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    return paper_json(paper)


@app.post("/api/papers/{paper_id}/views")
def increment_views(
    paper_id: str,
    db=Depends(get_db),
    _user: dict = Depends(require_user),
):
    if execute(db, "UPDATE papers SET views = views + 1 WHERE id = %s", (paper_id,)) == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    db.commit()
    return {"message": "Views incremented"}


@app.post("/api/papers/{paper_id}/downloads")
def increment_downloads(
    paper_id: str,
    db=Depends(get_db),
    _user: dict = Depends(require_user),
):
    if execute(db, "UPDATE papers SET downloads = downloads + 1 WHERE id = %s", (paper_id,)) == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    db.commit()
    return {"message": "Downloads incremented"}


@app.post("/api/papers", status_code=status.HTTP_201_CREATED)
def create_paper(
    payload: PaperCreate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    can_manage_department(user, payload.department_id)
    if not query_one(db, "SELECT id FROM departments WHERE id = %s", (payload.department_id,)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    paper_id = str(uuid4())
    values = payload.model_dump()
    execute(
        db,
        """
        INSERT INTO papers (
            id, subject, subject_code, department_id, semester, exam_type, year,
            description, file_url, uploaded_by, uploaded_by_name, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            paper_id,
            values["subject"],
            values["subject_code"],
            values["department_id"],
            values["semester"],
            values["exam_type"],
            values["year"],
            values["description"],
            values["file_url"],
            user["id"],
            user["name"],
        ),
    )
    commit_or_conflict(db)
    return paper_json(query_one(db, PAPER_SELECT + " WHERE p.id = %s", (paper_id,)))


@app.put("/api/papers/{paper_id}")
def update_paper(
    paper_id: str,
    payload: PaperUpdate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    paper = query_one(db, "SELECT * FROM papers WHERE id = %s", (paper_id,))
    if not paper:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    changes = payload.model_dump(exclude_unset=True)
    can_manage_department(user, changes.get("department_id") or paper["department_id"])
    previous_file_url = paper.get("file_url")
    if "department_id" in changes and not query_one(
        db, "SELECT id FROM departments WHERE id = %s", (changes["department_id"],)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    if changes:
        updates = [f"`{key}` = %s" for key in changes]
        params = [*changes.values(), paper_id]
        execute(db, f"UPDATE papers SET {', '.join(updates)} WHERE id = %s", tuple(params))
        commit_or_conflict(db)
        if "file_url" in changes and previous_file_url != changes["file_url"]:
            delete_uploaded_file(previous_file_url, UPLOAD_DIR)
    return paper_json(query_one(db, PAPER_SELECT + " WHERE p.id = %s", (paper_id,)))


@app.delete("/api/papers/{paper_id}")
def delete_paper(
    paper_id: str,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    paper = query_one(db, "SELECT department_id, file_url FROM papers WHERE id = %s", (paper_id,))
    if not paper:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    can_manage_department(user, paper["department_id"], action="delete")
    execute(db, "DELETE FROM papers WHERE id = %s", (paper_id,))
    record_activity(db, "paper_deleted", f"{_actor_label(user)} deleted question paper {paper_id}", user.get("id"))
    db.commit()
    # Only after the row is committed: deleting first would destroy the PDF even
    # if the database rejected the delete.
    delete_uploaded_file(paper.get("file_url"), UPLOAD_DIR)
    return {"message": "Paper deleted"}


@app.get("/api/favorites")
def list_favorites(db=Depends(get_db), user: dict = Depends(require_user)):
    rows = query_all(db, "SELECT paper_id FROM favorites WHERE user_id = %s", (user["id"],))
    return [row["paper_id"] for row in rows]


@app.post("/api/favorites/{paper_id}")
def toggle_favorite(paper_id: str, db=Depends(get_db), user: dict = Depends(require_user)):
    if not query_one(db, "SELECT id FROM papers WHERE id = %s", (paper_id,)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    favorite = query_one(
        db,
        "SELECT id FROM favorites WHERE user_id = %s AND paper_id = %s",
        (user["id"], paper_id),
    )
    if favorite:
        execute(db, "DELETE FROM favorites WHERE id = %s", (favorite["id"],))
        db.commit()
        return {"favorited": False}
    execute(
        db,
        """
        INSERT INTO favorites (id, user_id, paper_id, created_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (str(uuid4()), user["id"], paper_id),
    )
    commit_or_conflict(db)
    return {"favorited": True}


@app.get("/api/users")
def list_users(
    department: str | None = None,
    role: str | None = None,
    approval_status: str | None = None,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin"})
    conditions = []
    params = []
    if department:
        conditions.append("department_id = %s")
        params.append(department)
    if role:
        conditions.append("role = %s")
        params.append(role)
    if approval_status:
        conditions.append("approval_status = %s")
        params.append(approval_status)
    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = query_all(db, "SELECT * FROM profiles" + where_clause + " ORDER BY created_at DESC", tuple(params))
    return [user_json(profile) for profile in rows]


@app.get("/api/users/{user_id}/institute-document")
def get_institute_document(
    user_id: str,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin"})
    target = query_one(
        db,
        "SELECT role, institute_document_path, institute_document_name FROM profiles WHERE id = %s",
        (user_id,),
    )
    if not target or target["role"] != "institute":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Institute document not found")
    stored_path = target.get("institute_document_path")
    if not stored_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Institute document not found")

    document_path = Path(stored_path).resolve()
    if document_path.parent != INSTITUTE_DOCUMENT_DIR or not document_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Institute document not found")
    media_type = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(document_path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        document_path,
        media_type=media_type,
        filename=Path(target.get("institute_document_name") or document_path.name).name,
    )


@app.post("/api/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin"})
    if payload.role != "super_admin":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid user role")
    if payload.department_id and not query_one(
        db, "SELECT id FROM departments WHERE id = %s", (payload.department_id,)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    profile_id = str(uuid4())
    execute(
        db,
        """
        INSERT INTO profiles (
            id, email, username, name, password_hash, role, department_id, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            profile_id,
            payload.email.lower(),
            payload.username,
            payload.name.strip(),
            hash_password(payload.password),
            payload.role,
            payload.department_id,
        ),
    )
    commit_or_conflict(db)
    return user_json(query_one(db, "SELECT * FROM profiles WHERE id = %s", (profile_id,)))


@app.put("/api/users/{user_id}/approval")
def update_institute_approval(
    user_id: str,
    payload: InstituteApprovalUpdate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin"})
    target = query_one(db, "SELECT * FROM profiles WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target["role"] != "institute":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only institute accounts require approval")
    if payload.approval_status == "approved" and not bool(target.get("email_verified", 1)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Verify the institute email before approving this account")
    approval_changed = target["approval_status"] != payload.approval_status
    execute(
        db,
        "UPDATE profiles SET approval_status = %s WHERE id = %s",
        (payload.approval_status, user_id),
    )
    execute(
        db,
        """
        INSERT INTO activity_logs (id, type, message, user_id, created_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (
            str(uuid4()),
            "institute_approval",
            f"{target['name']}'s institute account was {payload.approval_status}",
            user["id"],
        ),
    )
    db.commit()
    result = user_json(query_one(db, "SELECT * FROM profiles WHERE id = %s", (user_id,)))
    if payload.approval_status == "approved" and approval_changed:
        try:
            send_institute_approval_email(
                target["email"],
                target["name"],
                target.get("institute_name") or "your institute",
            )
            result["approval_email_sent"] = True
        except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
            logger.exception("Unable to send institute approval email")
            result["approval_email_sent"] = False
    return result


@app.put("/api/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    target = query_one(db, "SELECT * FROM profiles WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    changes = payload.model_dump(exclude_unset=True)
    permitted_changes = permitted_user_changes(user, target, changes)
    if permitted_changes is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
    changes = permitted_changes
    if "email" in changes:
        if changes["email"] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Email cannot be empty")
        changes["email"] = changes["email"].strip().lower()
    if "role" in changes:
        require_roles(user, {"super_admin"})
        if changes["role"] != "super_admin":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid user role")
    if "department_id" in changes and changes["department_id"] and not query_one(
        db, "SELECT id FROM departments WHERE id = %s", (changes["department_id"],)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    password_changed = "password" in changes
    if password_changed:
        changes["password_hash"] = hash_password(changes.pop("password"))
    changes = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in changes.items()
    }
    if changes:
        updates = [f"`{key}` = %s" for key in changes]
        if password_changed:
            updates.append("token_version = token_version + 1")
        execute(
            db,
            f"UPDATE profiles SET {', '.join(updates)} WHERE id = %s",
            tuple([*changes.values(), user_id]),
        )
        commit_or_conflict(db)
    return user_json(query_one(db, "SELECT * FROM profiles WHERE id = %s", (user_id,)))


@app.delete("/api/users/{user_id}")
def delete_user(
    user_id: str,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin"})
    target = query_one(
        db,
        "SELECT role, institute_document_path FROM profiles WHERE id = %s",
        (user_id,),
    )
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target["role"] == "super_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot delete a super admin")
    execute(db, "DELETE FROM profiles WHERE id = %s", (user_id,))
    record_activity(db, "user_deleted", f"{_actor_label(user)} deleted user account {user_id}", user.get("id"))
    db.commit()
    # The private affiliation document must not outlive the account.
    delete_institute_document(target.get("institute_document_path"), INSTITUTE_DOCUMENT_DIR)
    return {"message": "User deleted"}


@app.get("/api/activity")
def list_activity(
    limit: int = Query(default=50, ge=1, le=200),
    user_id: str | None = None,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin"})
    if user_id:
        rows = query_all(
            db,
            "SELECT * FROM activity_logs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
    else:
        rows = query_all(db, "SELECT * FROM activity_logs ORDER BY created_at DESC LIMIT %s", (limit,))
    return rows


@app.get("/api/stats")
def get_stats(db=Depends(get_db), user: dict = Depends(require_user)):
    require_roles(user, {"super_admin"})
    totals = query_one(
        db,
        """
        SELECT COUNT(*) AS total_papers,
               COALESCE(SUM(downloads), 0) AS total_downloads,
               COALESCE(SUM(views), 0) AS total_views
        FROM papers
        """,
    )
    departments = query_all(db, "SELECT id, name, code FROM departments ORDER BY name")
    total_departments = len(departments)
    admin_count = query_one(
        db, "SELECT COUNT(*) AS count FROM profiles WHERE role = 'super_admin'"
    )["count"]
    papers_by_dept = query_all(
        db,
        """
        SELECT d.name AS department, d.code, COUNT(p.id) AS count
        FROM departments AS d LEFT JOIN papers AS p ON p.department_id = d.id
        GROUP BY d.id, d.name, d.code ORDER BY d.name
        """,
    )
    exam_types = ["Weekly Test 1", "Weekly Test 2", "CIA 1", "CIA 2", "End Semester"]
    exam_rows = query_all(
        db,
        f"""
        SELECT exam_type AS type, COUNT(*) AS count FROM papers
        WHERE exam_type IN ({", ".join(["%s"] * len(exam_types))})
        GROUP BY exam_type
        """,
        tuple(exam_types),
    )
    exam_counts = {row["type"]: row["count"] for row in exam_rows}
    semester_rows = query_all(
        db,
        """
        SELECT semester, COUNT(*) AS count FROM papers
        GROUP BY semester
        """,
    )
    semester_counts = {row["semester"]: row["count"] for row in semester_rows}
    recent_rows = query_all(
        db,
        PAPER_SELECT + " ORDER BY p.created_at DESC LIMIT 10",
    )
    return {
        "totalPapers": totals["total_papers"],
        "totalDepartments": total_departments,
        "totalAdmins": admin_count,
        "totalDownloads": totals["total_downloads"],
        "totalViews": totals["total_views"],
        "papersByDept": papers_by_dept,
        "papersByExam": [{"type": exam, "count": exam_counts.get(exam, 0)} for exam in exam_types],
        "papersBySemester": [
            {"semester": semester, "count": semester_counts.get(semester, 0)}
            for semester in range(1, 9)
        ],
        "recentUploads": [paper_json(paper) for paper in recent_rows],
    }


@app.post("/api/uploads", status_code=status.HTTP_201_CREATED)
async def upload_pdf(
    file: UploadFile = File(...),
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    require_roles(user, {"super_admin", "institute"})
    if file.content_type != "application/pdf" or Path(file.filename or "").suffix.lower() != ".pdf":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only PDF files are allowed")
    contents = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(contents) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Maximum upload size is {MAX_UPLOAD_MB} MB")
    if not contents.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File content is not a valid PDF")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "paper.pdf").name)
    file_name = f"{uuid4()}_{safe_name}"
    (UPLOAD_DIR / file_name).write_bytes(contents)
    record_activity(db, "upload", f"{_actor_label(user)} uploaded '{file.filename}'", user.get("id"))
    db.commit()
    # A relative URL is stored. Building it from request.base_url baked the
    # uploader's host name into the database, so every existing paper broke when
    # the server's LAN IP changed and a spoofed Host header could poison it.
    return {
        "url": f"/uploads/{file_name}",
        "path": file_name,
        "file_name": file.filename,
    }
