"""Replay a backup dump into a scratch database and compare row counts.

A dump that has never been restored is not a backup. This script proves the
dump is replayable and that the restored data matches the live database.

Usage (from the backend directory):

    python -m scripts.verify_backup
    python -m scripts.verify_backup --dump ../backup/campus_notes_20260101_120000.sql

Safety: this script only ever drops and recreates the *scratch* database named
by --scratch-database (default ``campus_notes_verify``). It refuses to run if
the scratch name equals the configured MYSQL_DATABASE.
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

TABLE_ORDER = (
    "departments",
    "profiles",
    "password_reset_otps",
    "papers",
    "favorites",
    "activity_logs",
)


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements, respecting quotes and comments.

    The dump is machine-generated, but INSERT payloads legitimately contain
    semicolons inside string literals, so a plain ``str.split(";")`` would
    corrupt the data.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)
    in_single = False
    in_double = False
    in_backtick = False

    while index < length:
        char = sql[index]

        if in_single:
            buffer.append(char)
            if char == "\\" and index + 1 < length:
                buffer.append(sql[index + 1])
                index += 2
                continue
            if char == "'":
                in_single = False
            index += 1
            continue

        if in_double:
            buffer.append(char)
            if char == "\\" and index + 1 < length:
                buffer.append(sql[index + 1])
                index += 2
                continue
            if char == '"':
                in_double = False
            index += 1
            continue

        if in_backtick:
            buffer.append(char)
            if char == "`":
                in_backtick = False
            index += 1
            continue

        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue

        if char == "'":
            in_single = True
            buffer.append(char)
            index += 1
            continue

        if char == '"':
            in_double = True
            buffer.append(char)
            index += 1
            continue

        if char == "`":
            in_backtick = True
            buffer.append(char)
            index += 1
            continue

        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


def live_row_counts(connection, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
            counts[table] = int(cursor.fetchone()["n"])
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a Campus-notes backup restores cleanly.")
    parser.add_argument("--dump", type=Path, default=None, help="Dump file to verify (newest by default).")
    parser.add_argument("--scratch-database", default="campus_notes_verify")
    parser.add_argument("--env-file", type=Path, default=BACKEND_DIR / ".env")
    args = parser.parse_args(argv)

    load_dotenv(args.env_file)
    live_database = os.getenv("MYSQL_DATABASE")
    if not live_database:
        print("MYSQL_DATABASE is not configured.", file=sys.stderr)
        return 2
    if args.scratch_database == live_database:
        print("Refusing to run: scratch database must differ from MYSQL_DATABASE.", file=sys.stderr)
        return 2

    dump_path = args.dump
    if dump_path is None:
        dumps = sorted(DEFAULT_OUTPUT_DIR.glob("campus_notes_*.sql"))
        if not dumps:
            print(f"No dump found in {DEFAULT_OUTPUT_DIR}. Run scripts.backup_database first.", file=sys.stderr)
            return 2
        dump_path = dumps[-1]

    if not dump_path.is_file():
        print(f"Dump not found: {dump_path}", file=sys.stderr)
        return 2

    statements = split_statements(dump_path.read_text(encoding="utf-8"))
    if not statements:
        print("Dump contains no statements.", file=sys.stderr)
        return 1

    connection = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{args.scratch_database}`")
            cursor.execute(
                f"CREATE DATABASE `{args.scratch_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(f"USE `{args.scratch_database}`")
            for statement in statements:
                cursor.execute(statement)

        connection.select_db(args.scratch_database)
        restored = live_row_counts(connection, [t for t in TABLE_ORDER])

        connection.select_db(live_database)
        live = live_row_counts(connection, [t for t in TABLE_ORDER])
    finally:
        connection.close()

    print(f"Verified dump : {dump_path.name}")
    print(f"Scratch db    : {args.scratch_database}")
    print(f"Statements    : {len(statements)}")
    print()
    header = f"{'table':22}{'live':>8}{'restored':>10}  status"
    print(header)
    print("-" * len(header))
    failed = False
    for table in TABLE_ORDER:
        match = restored[table] == live[table]
        failed = failed or not match
        print(f"{table:22}{live[table]:>8}{restored[table]:>10}  {'ok' if match else 'MISMATCH'}")
    print()
    if failed:
        print("RESULT: FAIL - restored data does not match the live database.", file=sys.stderr)
        return 1
    print(f"RESULT: PASS - restore verified at {datetime.now().isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
