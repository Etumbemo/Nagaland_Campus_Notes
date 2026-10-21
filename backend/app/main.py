import os
import re
import hashlib
import hmac
import logging
import secrets
import smtplib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import pymysql
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pymysql.cursors import DictCursor

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
    generate_with_fallback,
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
from .file_storage import delete_institute_document, delete_uploaded_file
from .mailer import send_password_reset_otp
from .rate_limit import SlidingWindowLimiter, client_address
from .schemas import (
    ActivityCreate,
    AIPreparationGenerateRequest,
    DepartmentCreate,
    DepartmentUpdate,
    InstituteApprovalUpdate,
    InstituteRegisterRequest,
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    PaperCreate,
    PaperUpdate,
    RegisterRequest,
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
ADMIN_DASHBOARD_DIR = BACKEND_DIR / "admin"
MAX_UPLOAD_MB = _configured_int("MAX_UPLOAD_MB", 20)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
INSTITUTE_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)

# Throttles. Keyed per client address for anonymous endpoints and per user id
# for the authenticated AI endpoint, so students sharing one campus NAT are not
# punished for each other's traffic.
LOGIN_LIMITER = SlidingWindowLimiter(limit=15, window_seconds=15 * 60)
REGISTER_LIMITER = SlidingWindowLimiter(limit=5, window_seconds=60 * 60)
PASSWORD_RESET_LIMITER = SlidingWindowLimiter(limit=5, window_seconds=60 * 60)
AI_HOURLY_LIMITER = SlidingWindowLimiter(limit=5, window_seconds=60 * 60)
AI_DAILY_LIMITER = SlidingWindowLimiter(limit=20, window_seconds=24 * 60 * 60)


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
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
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
        "approval_status": user.get("approval_status", "approved"),
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
    if not token_version_is_current(token_version, user["token_version"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
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

    Department administrators are scoped to their own department. Institute
    accounts are approved institution staff and may publish and correct papers
    across departments, but deleting is reserved for administrators so that one
    college cannot remove another college's material.
    """
    require_roles(user, {"dept_admin", "super_admin", "institute"})
    if user["role"] == "dept_admin" and user.get("department_id") != department_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot manage another department")
    if user["role"] == "institute" and action == "delete":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Institute accounts cannot delete question papers",
        )


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


def password_reset_code_hash(email: str, otp: str) -> str:
    return hmac.new(
        JWT_SECRET.encode("utf-8"),
        f"{email}:{otp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def initialize_database() -> None:
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
            id, email, name, password_hash, role, approval_status, institute_name, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
    return {
        "user": user_json(user),
        "session": {"access_token": create_access_token(profile_id, user["token_version"]), "token_type": "bearer"},
    }


@app.post("/api/auth/register/institute", status_code=status.HTTP_201_CREATED)
async def register_institute(
    payload: Annotated[InstituteRegisterRequest, Form()],
    file: UploadFile = File(...),
    db=Depends(get_db),
):
    name = payload.name.strip()
    phone = payload.phone.strip()
    designation = payload.designation.strip()
    institute_name = payload.institute_name.strip()
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

    email = payload.email.lower()
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
                phone, designation, institute_document_path, institute_document_name,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 'institute', 'pending', %s, %s, %s, %s, %s,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                profile_id,
                email,
                name,
                hash_password(payload.password),
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
    return {
        "user": user_json(user),
        "session": {"access_token": create_access_token(profile_id, user["token_version"]), "token_type": "bearer"},
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
    require_roles(user, {"super_admin"})
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
    require_roles(user, {"super_admin"})
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

    The foreign keys cascade to papers and favourites, and department
    administrators are removed explicitly. Because that is a lot of data to lose
    behind a single click, the caller must confirm the exact impact first and
    pass force=true.
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
                WHERE p.department_id = %s) AS favorites,
            (SELECT COUNT(*) FROM profiles
                WHERE department_id = %s AND role = 'dept_admin') AS department_admins
        """,
        (department_id, department_id, department_id),
    ) or {"papers": 0, "favorites": 0, "department_admins": 0}

    file_rows = query_all(db, "SELECT file_url FROM papers WHERE department_id = %s", (department_id,))

    if impact["papers"] and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            (
                f"This department still has {impact['papers']} question paper(s), which would also "
                f"delete {impact['favorites']} favourite(s) and {impact['department_admins']} department "
                "administrator account(s). Re-send with force=true to proceed."
            ),
        )

    execute(db, "DELETE FROM profiles WHERE department_id = %s AND role = 'dept_admin'", (department_id,))
    execute(db, "DELETE FROM departments WHERE id = %s", (department_id,))
    db.commit()

    for row in file_rows:
        delete_uploaded_file(row.get("file_url"), UPLOAD_DIR)

    return {
        "message": "Department deleted",
        "deleted": {
            "papers": impact["papers"],
            "favorites": impact["favorites"],
            "department_admins": impact["department_admins"],
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
    return {"subjects": build_subject_options(rows, UPLOAD_DIR)}


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

    # Charge the quota only after the cheap validation above, so a typo does not
    # burn a student's allowance, but before the expensive PDF/OCR work below.
    quota_key = str(user.get("id") or user.get("email") or "unknown-user")
    for limiter, message in (
        (AI_HOURLY_LIMITER, "You have reached the hourly AI generation limit. Try again later."),
        (AI_DAILY_LIMITER, "You have reached the daily AI generation limit. Try again tomorrow."),
    ):
        result = limiter.check(quota_key)
        if not result.allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                message,
                headers={"Retry-After": str(result.retry_after)},
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
        provider_response, provider = generate_with_fallback(
            prompt,
            allow_hosted_fallback=payload.hosted_fallback_consent,
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
            "Source PDF text was sent to the configured hosted AI provider."
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
def increment_views(paper_id: str, db=Depends(get_db)):
    if execute(db, "UPDATE papers SET views = views + 1 WHERE id = %s", (paper_id,)) == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    db.commit()
    return {"message": "Views incremented"}


@app.post("/api/papers/{paper_id}/downloads")
def increment_downloads(paper_id: str, db=Depends(get_db)):
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
    require_roles(user, {"super_admin", "dept_admin"})
    conditions = []
    params = []
    if user["role"] == "dept_admin":
        conditions.append("department_id = %s")
        params.append(user.get("department_id"))
    elif department:
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
    if payload.role not in {"dept_admin", "super_admin"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid user role")
    if payload.role == "dept_admin" and not payload.department_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Department is required for department admins")
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
    return user_json(query_one(db, "SELECT * FROM profiles WHERE id = %s", (user_id,)))


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
        if changes["role"] not in {"dept_admin", "super_admin"}:
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
    if user["role"] == "dept_admin":
        rows = query_all(
            db,
            "SELECT * FROM activity_logs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user["id"], limit),
        )
    elif user_id:
        rows = query_all(
            db,
            "SELECT * FROM activity_logs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
    else:
        rows = query_all(db, "SELECT * FROM activity_logs ORDER BY created_at DESC LIMIT %s", (limit,))
    return rows


@app.post("/api/activity", status_code=status.HTTP_201_CREATED)
def create_activity(
    payload: ActivityCreate,
    db=Depends(get_db),
    user: dict = Depends(require_user),
):
    activity_id = str(uuid4())
    execute(
        db,
        """
        INSERT INTO activity_logs (id, type, message, user_id, created_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (activity_id, payload.type, payload.message, user["id"]),
    )
    db.commit()
    return query_one(db, "SELECT * FROM activity_logs WHERE id = %s", (activity_id,))


@app.get("/api/stats")
def get_stats(db=Depends(get_db), user: dict = Depends(require_user)):
    require_roles(user, {"super_admin", "dept_admin"})
    paper_filter = ""
    params = ()
    if user["role"] == "dept_admin":
        paper_filter = " WHERE department_id = %s"
        params = (user.get("department_id"),)
    totals = query_one(
        db,
        f"""
        SELECT COUNT(*) AS total_papers,
               COALESCE(SUM(downloads), 0) AS total_downloads,
               COALESCE(SUM(views), 0) AS total_views
        FROM papers{paper_filter}
        """,
        params,
    )
    if user["role"] == "dept_admin":
        departments = query_all(
            db,
            "SELECT id, name, code FROM departments WHERE id = %s ORDER BY name",
            (user.get("department_id"),),
        )
    else:
        departments = query_all(db, "SELECT id, name, code FROM departments ORDER BY name")
    total_departments = len(departments)
    if user["role"] == "dept_admin":
        admin_count = query_one(
            db,
            "SELECT COUNT(*) AS count FROM profiles WHERE role = 'dept_admin' AND department_id = %s",
            (user.get("department_id"),),
        )["count"]
    else:
        admin_count = query_one(db, "SELECT COUNT(*) AS count FROM profiles WHERE role = 'dept_admin'")["count"]
    papers_by_dept = query_all(
        db,
        f"""
        SELECT d.name AS department, d.code, COUNT(p.id) AS count
        FROM departments AS d LEFT JOIN papers AS p ON p.department_id = d.id
        {'WHERE d.id = %s' if user['role'] == 'dept_admin' else ''}
        GROUP BY d.id, d.name, d.code ORDER BY d.name
        """,
        params,
    )
    exam_types = ["Weekly Test 1", "Weekly Test 2", "CIA 1", "CIA 2", "End Semester"]
    exam_rows = query_all(
        db,
        f"""
        SELECT exam_type AS type, COUNT(*) AS count FROM papers
        {"WHERE department_id = %s AND " if user["role"] == "dept_admin" else "WHERE "}exam_type IN ({", ".join(["%s"] * len(exam_types))})
        GROUP BY exam_type
        """,
        (*params, *exam_types),
    )
    exam_counts = {row["type"]: row["count"] for row in exam_rows}
    semester_rows = query_all(
        db,
        f"""
        SELECT semester, COUNT(*) AS count FROM papers
        {paper_filter}
        GROUP BY semester
        """,
        params,
    )
    semester_counts = {row["semester"]: row["count"] for row in semester_rows}
    recent_rows = query_all(
        db,
        PAPER_SELECT + paper_filter.replace("department_id", "p.department_id") + " ORDER BY p.created_at DESC LIMIT 10",
        params,
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
    user: dict = Depends(require_user),
):
    require_roles(user, {"dept_admin", "super_admin", "institute"})
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
    # A relative URL is stored. Building it from request.base_url baked the
    # uploader's host name into the database, so every existing paper broke when
    # the server's LAN IP changed and a spoofed Host header could poison it.
    return {
        "url": f"/uploads/{file_name}",
        "path": file_name,
        "file_name": file.filename,
    }
