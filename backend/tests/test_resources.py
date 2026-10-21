from io import BytesIO
from pathlib import Path
import asyncio

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from starlette.responses import FileResponse

try:
    from app import main
    from app.schemas import ResourceReviewUpdate
except ModuleNotFoundError:
    from backend.app import main
    from backend.app.schemas import ResourceReviewUpdate


class RecordingDatabase:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def pdf_upload():
    return UploadFile(
        file=BytesIO(b"%PDF-resource"),
        filename="study-resource.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )


@pytest.mark.parametrize(
    ("role", "expected_status"),
    [("user", "pending"), ("super_admin", "approved")],
)
def test_readers_submit_for_review_and_admin_uploads_publish_immediately(
    role, expected_status, tmp_path, monkeypatch
):
    monkeypatch.setattr(main, "RESOURCE_UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        main,
        "query_one",
        lambda _db, sql, _params=(): (
            {"id": "dept-1"} if "FROM departments" in sql else {
                "id": "resource-1",
                "title": "Lecture notes",
                "resource_type": "notes",
                "department_id": "dept-1",
                "department_name": "Computer Science",
                "original_file_name": "study-resource.pdf",
                "status": expected_status,
            }
        ),
    )
    writes = []
    activities = []
    monkeypatch.setattr(main, "execute", lambda _db, sql, params=(): writes.append((sql, params)) or 1)
    monkeypatch.setattr(main, "record_activity", lambda *_args: activities.append(True))
    database = RecordingDatabase()

    result = asyncio.run(
        main.create_resource(
            file=pdf_upload(),
            title="Lecture notes",
            resource_type="notes",
            department_id="dept-1",
            subject="Biology",
            semester=2,
            academic_year="2026-27",
            description="Class notes",
            db=database,
            user={"id": "uploader-1", "name": "Uploader", "role": role},
        )
    )

    assert result["status"] == expected_status
    assert result["file_url"] == "/api/resources/resource-1/file"
    assert writes[0][1][12] == expected_status
    assert activities == [True]
    assert database.commits == 1
    assert list(tmp_path.glob("*.pdf"))


def test_resource_list_hides_pending_uploads_from_other_readers(monkeypatch):
    captured = {}

    def query(_db, sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(main, "query_all", query)
    result = main.list_resources(db=object(), user={"id": "reader-1", "role": "user"})

    assert result == []
    assert "r.status = 'approved' OR r.uploaded_by = %s" in captured["sql"]
    assert captured["params"] == ("reader-1",)


def test_reader_cannot_approve_resources():
    with pytest.raises(HTTPException) as error:
        main.review_resource(
            "resource-1",
            ResourceReviewUpdate(status="approved"),
            db=object(),
            user={"id": "reader-1", "role": "user"},
        )

    assert error.value.status_code == 403


def test_resource_file_is_removed_when_database_insert_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RESOURCE_UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        main,
        "query_one",
        lambda *_args: {"id": "dept-1"},
    )

    def fail_insert(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "execute", fail_insert)
    database = RecordingDatabase()

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(
            main.create_resource(
                file=pdf_upload(),
                title="Lecture notes",
                resource_type="notes",
                department_id="dept-1",
                subject=None,
                semester=None,
                academic_year=None,
                description=None,
                db=database,
                user={"id": "reader-1", "name": "Reader", "role": "user"},
            )
        )

    assert database.rollbacks == 1
    assert list(tmp_path.iterdir()) == []


def test_admin_can_review_reader_resource(monkeypatch):
    database = RecordingDatabase()
    updates = []
    monkeypatch.setattr(main, "query_one", lambda *_args: {"title": "Lecture notes", "resource_type": "notes"})
    monkeypatch.setattr(main, "execute", lambda _db, _sql, params=(): updates.append(params) or 1)
    monkeypatch.setattr(main, "record_activity", lambda *_args: None)

    response = main.review_resource(
        "resource-1",
        ResourceReviewUpdate(status="approved"),
        db=database,
        user={"id": "admin-1", "name": "Admin", "role": "super_admin"},
    )

    assert response == {"id": "resource-1", "status": "approved"}
    assert updates == [("approved", "admin-1", "resource-1")]
    assert database.commits == 1


def test_pending_resource_file_is_visible_only_to_uploader_or_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RESOURCE_UPLOAD_DIR", tmp_path)
    (tmp_path / "private.pdf").write_bytes(b"%PDF-private")
    resource = {
        "id": "resource-1",
        "file_path": "private.pdf",
        "original_file_name": "private.pdf",
        "uploaded_by": "reader-1",
        "status": "pending",
    }
    monkeypatch.setattr(main, "query_one", lambda *_args: resource)

    with pytest.raises(HTTPException) as error:
        main.get_resource_file(
            "resource-1",
            db=object(),
            user={"id": "reader-2", "role": "user"},
        )
    assert error.value.status_code == 404

    response = main.get_resource_file(
        "resource-1",
        db=object(),
        user={"id": "reader-1", "role": "user"},
    )
    assert isinstance(response, FileResponse)
    assert Path(response.path) == tmp_path / "private.pdf"
