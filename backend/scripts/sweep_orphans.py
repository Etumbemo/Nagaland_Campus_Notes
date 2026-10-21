"""Find (and optionally quarantine) uploaded files no paper references.

Orphaned uploads are created when a paper row is deleted or its PDF replaced,
or when an upload succeeds but the follow-up paper creation fails. They waste
disk and, because /uploads is served publicly, they stay readable forever.

Default behaviour is a dry run: nothing is moved. ``--quarantine DIR`` moves the
files out of the served directory into DIR (reversible, unlike deletion).

Usage (from the backend directory):

    python -m scripts.sweep_orphans
    python -m scripts.sweep_orphans --quarantine ../backup/quarantine
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

BACKEND_DIR = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report or quarantine unreferenced uploads.")
    parser.add_argument(
        "--quarantine",
        type=Path,
        default=None,
        help="Move orphans into this directory instead of only listing them.",
    )
    parser.add_argument("--upload-dir", type=Path, default=BACKEND_DIR / "uploads")
    parser.add_argument("--env-file", type=Path, default=BACKEND_DIR / ".env")
    args = parser.parse_args(argv)

    load_dotenv(args.env_file)
    connection = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE"),
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=10,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT file_url FROM papers WHERE file_url IS NOT NULL")
            rows = cursor.fetchall()
    finally:
        connection.close()

    sys.path.insert(0, str(BACKEND_DIR))
    from app.file_storage import find_orphaned_uploads, referenced_upload_filenames

    referenced = referenced_upload_filenames(row["file_url"] for row in rows)
    orphans = find_orphaned_uploads(args.upload_dir, referenced)

    print(f"upload dir   : {args.upload_dir}")
    print(f"referenced   : {len(referenced)} file(s)")
    print(f"orphaned     : {len(orphans)} file(s)")
    for path in orphans:
        size = path.stat().st_size if path.exists() else 0
        print(f"  {size:>10,} bytes  {path.name}")

    if not orphans:
        return 0
    if args.quarantine is None:
        print("\nDry run: nothing moved. Re-run with --quarantine <dir> to move these files.")
        return 0

    target = args.quarantine / datetime.now().strftime("%Y%m%d_%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    for path in orphans:
        shutil.move(str(path), str(target / path.name))
    print(f"\nMoved {len(orphans)} file(s) to {target}")
    print("Review them before deleting: they may contain personal data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
