import os
from collections.abc import Generator
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

load_dotenv()

MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE")

missing_settings = [
    name for name, value in (
        ("MYSQL_HOST", MYSQL_HOST),
        ("MYSQL_USER", MYSQL_USER),
        ("MYSQL_DATABASE", MYSQL_DATABASE),
    ) if not value
]
if missing_settings:
    raise RuntimeError(f"Configure {', '.join(missing_settings)} in backend/.env")
if not MYSQL_DATABASE.replace("_", "").isalnum():
    raise RuntimeError("MYSQL_DATABASE may contain only letters, numbers, and underscores")


def connect_mysql(*, database: str | None = MYSQL_DATABASE):
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=10,
    )


def get_db() -> Generator[pymysql.connections.Connection, None, None]:
    connection = connect_mysql()
    try:
        yield connection
    finally:
        connection.close()


def ensure_database_exists() -> None:
    connection = connect_mysql(database=None)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        connection.commit()
    finally:
        connection.close()


def create_tables() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "schema.sql"
    schema_statements = (
        statement.strip()
        for statement in schema_path.read_text(encoding="utf-8").split(";")
    )
    statements = [
        statement
        for statement in schema_statements
        if statement.upper().startswith("CREATE TABLE IF NOT EXISTS")
    ]
    if not statements:
        raise RuntimeError(f"No table definitions found in {schema_path}")

    connection = connect_mysql()
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            cursor.execute("SHOW COLUMNS FROM profiles LIKE 'approval_status'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE profiles ADD COLUMN approval_status "
                    "VARCHAR(20) NOT NULL DEFAULT 'approved' AFTER role"
                )
            cursor.execute("SHOW COLUMNS FROM profiles LIKE 'token_version'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE profiles ADD COLUMN token_version "
                    "INT UNSIGNED NOT NULL DEFAULT 0 AFTER role"
                )
            cursor.execute("SHOW COLUMNS FROM profiles LIKE 'institute_name'")
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE profiles ADD COLUMN institute_name VARCHAR(200) NULL AFTER approval_status"
                )
            for column, definition in (
                ("phone", "VARCHAR(32) NULL AFTER institute_name"),
                ("designation", "VARCHAR(120) NULL AFTER phone"),
                ("institute_document_path", "VARCHAR(500) NULL AFTER designation"),
                ("institute_document_name", "VARCHAR(255) NULL AFTER institute_document_path"),
            ):
                cursor.execute(f"SHOW COLUMNS FROM profiles LIKE '{column}'")
                if not cursor.fetchone():
                    cursor.execute(f"ALTER TABLE profiles ADD COLUMN {column} {definition}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def verify_schema(connection) -> None:
    required_tables = {
        "activity_logs",
        "departments",
        "favorites",
        "papers",
        "password_reset_otps",
        "profiles",
    }
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW TABLES")
            existing_tables = {
                str(next(iter(row.values())))
                for row in cursor.fetchall()
            }
            missing_tables = required_tables - existing_tables
            if missing_tables:
                missing = ", ".join(sorted(missing_tables))
                raise RuntimeError(
                    f"Database schema is incomplete (missing tables: {missing}). "
                    "Apply backend/schema.sql or restart with SCHEMA_AUTO_MIGRATE=true."
                )
            cursor.execute("SHOW COLUMNS FROM profiles")
            profile_columns = {
                str(row["Field"])
                for row in cursor.fetchall()
            }
            if "token_version" not in profile_columns:
                raise RuntimeError(
                    "Database schema is missing profiles.token_version. "
                    "Restart with SCHEMA_AUTO_MIGRATE=true to apply the additive migration."
                )
    except pymysql.MySQLError as error:
        raise RuntimeError(
            "Unable to verify the Campus-notes database schema. Confirm the configured "
            "database exists and apply backend/schema.sql, or restart with "
            "SCHEMA_AUTO_MIGRATE=true."
        ) from error
