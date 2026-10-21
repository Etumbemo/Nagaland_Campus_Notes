"""Write a restorable SQL dump of the Campus-notes database.

The mysql/mysqldump command-line tools are not always installed (XAMPP and
standalone Python setups often lack them), so this script uses the same
PyMySQL connection the application uses and emits portable SQL.

Usage (from the backend directory):

    python -m scripts.backup_database
    python -m scripts.backup_database --output ../backup

The dump contains CREATE TABLE statements (without the database-selection
header) followed by INSERT statements, so it can be replayed into a scratch
database for verification:

    mysql -u <user> -p campus_notes_verify < campus_notes_YYYYmmdd_HHMMSS.sql

Restore verification is deliberately separate from this script: a backup that
has never been replayed is not a backup.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = BACKEND_DIR.parent / "backup"

# Table creation order matters when replaying, because of foreign keys.
TABLE_ORDER = (
    "departments",
    "profiles",
    "password_reset_otps",
    "papers",
    "favorites",
    "activity_logs",
)

INSERT_BATCH_SIZE = 200


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bytes):
        return "0x" + value.hex().upper()
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x00", "\\0")
    )
    return f"'{escaped}'"


def _create_table_statement(connection, table: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute(f"SHOW CREATE TABLE `{table}`")
        row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"Table {table} disappeared while dumping")
    # pymysql returns a tuple of column values for SHOW CREATE TABLE.
    if isinstance(row, dict):
        statement = next(value for key, value in row.items() if str(key).lower().startswith("create"))
    else:
        statement = row[1]
    return f"{statement};"


def _table_columns(connection, table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(f"SHOW COLUMNS FROM `{table}`")
        return [str(row["Field"]) for row in cursor.fetchall()]


def dump_table(connection, table: str, handle) -> int:
    columns = _table_columns(connection, table)
    if not columns:
        return 0
    column_list = ", ".join(f"`{column}`" for column in columns)
    with connection.cursor(DictCursor) as cursor:
        cursor.execute(f"SELECT {column_list} FROM `{table}`")
        rows = cursor.fetchall()

    if not rows:
        return 0

    handle.write(f"\n--\n-- Data for `{table}` ({len(rows)} rows)\n--\n")
    for start in range(0, len(rows), INSERT_BATCH_SIZE):
        batch = rows[start:start + INSERT_BATCH_SIZE]
        values = ",\n  ".join(
            "(" + ", ".join(_sql_literal(row[column]) for column in columns) + ")"
            for row in batch
        )
        handle.write(f"INSERT INTO `{table}` ({column_list}) VALUES\n  {values};\n")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump the Campus-notes database to SQL.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory that receives the dump file (default: <repo>/backup).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=BACKEND_DIR / ".env",
        help="Path to the backend .env file.",
    )
    args = parser.parse_args(argv)

    load_dotenv(args.env_file)
    database = os.getenv("MYSQL_DATABASE")
    if not database:
        print("MYSQL_DATABASE is not configured; nothing to dump.", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_path = args.output / f"campus_notes_{timestamp}.sql"

    connection = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=10,
    )
    try:
        with dump_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"-- Campus-notes backup of database `{database}`\n")
            handle.write(f"-- Generated {datetime.now().isoformat(timespec='seconds')}\n")
            handle.write("-- Restore into a fresh database, then verify row counts.\n")
            handle.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n")

            with connection.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                existing = {str(next(iter(row.values()))) for row in cursor.fetchall()}

            missing = [table for table in TABLE_ORDER if table not in existing]
            extra = sorted(existing - set(TABLE_ORDER))
            if missing:
                raise RuntimeError(
                    "Database is missing expected tables: " + ", ".join(missing)
                )
            for table in extra:
                print(f"note: dumping unexpected table `{table}`", file=sys.stderr)

            total_rows = 0
            for table in [*TABLE_ORDER, *extra]:
                if table not in existing:
                    continue
                handle.write(f"\n--\n-- Table structure for `{table}`\n--\n")
                handle.write(_create_table_statement(connection, table) + "\n")
                total_rows += dump_table(connection, table, handle)

            handle.write("\nSET FOREIGN_KEY_CHECKS=1;\n")

        # Verify the dump before reporting success.
        if dump_path.stat().st_size == 0:
            raise RuntimeError("Dump file is empty")
        print(f"Dumped {total_rows} rows to {dump_path}")
        print("Restore check: replay this file into a scratch database and compare row counts.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
