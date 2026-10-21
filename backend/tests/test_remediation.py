"""Regression tests for the audit remediation.

Each test names the audit finding it protects:
FILE-01 file lifecycle, API-01 stored URLs, VAL-01 year validation,
AI-03 citation grounding, AI-04 extraction cache, SEC-02 rate limiting,
DB-01 department delete impact, AUTHZ-01 institute delete scope.
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

try:
    from app import ai_preparation, main
    from app.file_storage import delete_uploaded_file, upload_filename
    from app.rate_limit import SlidingWindowLimiter
    from app.schemas import PaperCreate, PaperUpdate
except ModuleNotFoundError:
    from backend.app import ai_preparation, main
    from backend.app.file_storage import delete_uploaded_file, upload_filename
    from backend.app.rate_limit import SlidingWindowLimiter
    from backend.app.schemas import PaperCreate, PaperUpdate


# --------------------------------------------------------------------------
# FILE-01 + API-01: stored URLs and file lifecycle
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_url", "expected"),
    [
        ("/uploads/paper.pdf", "paper.pdf"),
        ("http://10.0.2.2:8000/uploads/paper.pdf", "paper.pdf"),
        ("https://example.test/uploads/a%20b.pdf", "a b.pdf"),
        ("/uploads/../secret.pdf", None),
        ("/uploads/nested/paper.pdf", None),
        ("https://example.test/other/paper.pdf", None),
        ("", None),
        (None, None),
    ],
)
def test_upload_filename_accepts_relative_and_absolute_only(file_url, expected):
    assert upload_filename(file_url) == expected


def test_delete_uploaded_file_removes_file_and_is_idempotent(tmp_path):
    stored = tmp_path / "paper.pdf"
    stored.write_bytes(b"%PDF-1.4")

    assert delete_uploaded_file("/uploads/paper.pdf", tmp_path) is True
    assert not stored.exists()

    # A second delete must not raise: an earlier cleanup may already have run.
    assert delete_uploaded_file("/uploads/paper.pdf", tmp_path) is False


def test_delete_uploaded_file_refuses_paths_outside_upload_dir(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4")

    assert delete_uploaded_file("/uploads/../secret.pdf", uploads) is False
    assert outside.exists(), "a traversal attempt must never delete outside uploads"


# --------------------------------------------------------------------------
# VAL-01: paper year must be a bare four-digit year
# --------------------------------------------------------------------------


@pytest.mark.parametrize("year", ["2024-25", "SPRING", "20 25", "202"])
def test_paper_create_rejects_ambiguous_years(year):
    with pytest.raises(ValidationError):
        PaperCreate(
            subject="Algorithms",
            department_id="dept-1",
            semester=3,
            exam_type="End Semester",
            year=year,
        )


def test_paper_create_accepts_four_digit_year():
    paper = PaperCreate(
        subject="Algorithms",
        department_id="dept-1",
        semester=3,
        exam_type="End Semester",
        year="2024",
    )

    assert paper.year == "2024"


def test_paper_update_rejects_ambiguous_years():
    with pytest.raises(ValidationError):
        PaperUpdate(year="2024-25")


# --------------------------------------------------------------------------
# AI-03: a past_paper claim must be supported by the cited page text
# --------------------------------------------------------------------------


def _paper(paper_id="paper-1", year=2024):
    return {
        "id": paper_id,
        "subject": "Algorithms",
        "subject_code": "CS201",
        "department_id": "dept-1",
        "department_name": "Computer Science",
        "semester": 3,
        "exam_type": "End Semester",
        "year": str(year),
        "_year": year,
    }


def _question(text, page=1, paper_id="paper-1"):
    return {
        "question": text,
        "type": "short_answer",
        "answer": "A supported answer.",
        "origin": "past_paper",
        "sources": [{"paper_id": paper_id, "page": page}],
    }


def test_cited_question_that_appears_on_the_page_stays_past_paper():
    pages = {"paper-1": ["Explain the complexity of binary search and its preconditions."]}

    result = ai_preparation.validate_and_enrich_questions(
        [_question("Explain the complexity of binary search")],
        [_paper()],
        pages,
        feature="mock_test",
        question_count=5,
    )

    assert result[0]["origin"] == "past_paper"
    assert result[0]["grounding"] == "verified"


def test_fabricated_question_citing_a_real_page_is_downgraded():
    # The paper and page exist, but the question wording is not on that page.
    pages = {"paper-1": ["Section A. Answer all questions. Total marks 70."]}

    result = ai_preparation.validate_and_enrich_questions(
        [_question("Derive the pumping lemma for regular languages")],
        [_paper()],
        pages,
        feature="mock_test",
        question_count=5,
    )

    assert result[0]["origin"] == "ai_generated"
    assert result[0]["grounding"] == "unverified"


def test_past_paper_claim_without_a_page_cannot_be_verified():
    question = _question("Explain the complexity of binary search")
    question["sources"] = [{"paper_id": "paper-1"}]

    result = ai_preparation.validate_and_enrich_questions(
        [question],
        [_paper()],
        {"paper-1": ["Explain the complexity of binary search."]},
        feature="mock_test",
        question_count=5,
    )

    assert result[0]["grounding"] == "unverified"


# --------------------------------------------------------------------------
# AI-04: extracted page text is cached
# --------------------------------------------------------------------------


def test_extract_pdf_pages_is_cached_between_calls(tmp_path, monkeypatch):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    calls = []

    def fake_extract(path):
        calls.append(path)
        return ["cached page text"]

    monkeypatch.setattr(ai_preparation, "_extract_pdf_pages_uncached", fake_extract)
    ai_preparation.clear_extraction_cache()

    first = ai_preparation.extract_pdf_pages(pdf_path)
    second = ai_preparation.extract_pdf_pages(pdf_path)

    assert first == second == ["cached page text"]
    assert len(calls) == 1, "the second call must be served from the cache"


def test_cache_is_invalidated_when_the_file_changes(tmp_path, monkeypatch):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    outputs = iter([["first version"], ["second version"]])
    monkeypatch.setattr(
        ai_preparation, "_extract_pdf_pages_uncached", lambda _path: next(outputs)
    )
    ai_preparation.clear_extraction_cache()

    assert ai_preparation.extract_pdf_pages(pdf_path) == ["first version"]

    pdf_path.write_bytes(b"%PDF-fake-replaced")
    assert ai_preparation.extract_pdf_pages(pdf_path) == ["second version"]


# --------------------------------------------------------------------------
# SEC-02 / AI-02: sliding-window throttling
# --------------------------------------------------------------------------


def test_sliding_window_allows_up_to_the_limit_then_blocks():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)

    assert limiter.check("user").allowed is True
    assert limiter.check("user").allowed is True

    blocked = limiter.check("user")

    assert blocked.allowed is False
    assert blocked.retry_after >= 1
    assert limiter.check("other-user").allowed is True


def test_sliding_window_reset_clears_state():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert limiter.check("user").allowed is True
    assert limiter.check("user").allowed is False

    limiter.reset("user")

    assert limiter.check("user").allowed is True


# --------------------------------------------------------------------------
# DB-01: deleting a department reports and gates its cascade
# --------------------------------------------------------------------------


class RecordingDatabase:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_delete_department_requires_force_when_papers_exist(monkeypatch):
    monkeypatch.setattr(
        main,
        "query_one",
        lambda _db, sql, _params=(): {"papers": 2, "favorites": 1, "department_admins": 1},
    )
    monkeypatch.setattr(main, "query_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main, "execute", lambda *_args, **_kwargs: 1)
    database = RecordingDatabase()

    with pytest.raises(HTTPException) as error:
        main.delete_department(
            "dept-1",
            force=False,
            db=database,
            user={"role": "super_admin"},
        )

    assert error.value.status_code == 409
    assert "2 question paper" in error.value.detail
    assert "force=true" in error.value.detail
    assert database.commits == 0, "nothing may be deleted before confirmation"


def test_delete_department_with_force_reports_what_was_removed(monkeypatch):
    monkeypatch.setattr(
        main,
        "query_one",
        lambda _db, sql, _params=(): {"papers": 2, "favorites": 1, "department_admins": 1},
    )
    monkeypatch.setattr(main, "query_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main, "execute", lambda *_args, **_kwargs: 1)
    database = RecordingDatabase()

    result = main.delete_department(
        "dept-1",
        force=True,
        db=database,
        user={"role": "super_admin"},
    )

    assert result["deleted"] == {"papers": 2, "favorites": 1, "department_admins": 1}
    assert database.commits == 1


# --------------------------------------------------------------------------
# AUTHZ-01: institute accounts may publish but not delete
# --------------------------------------------------------------------------


def test_institute_account_may_write_papers():
    main.can_manage_department(
        {"role": "institute", "approval_status": "approved"},
        "dept-any",
        action="write",
    )


def test_institute_account_may_not_delete_papers():
    with pytest.raises(HTTPException) as error:
        main.can_manage_department(
            {"role": "institute", "approval_status": "approved"},
            "dept-any",
            action="delete",
        )

    assert error.value.status_code == 403


def test_unapproved_institute_account_is_still_blocked():
    with pytest.raises(HTTPException) as error:
        main.can_manage_department(
            {"role": "institute", "approval_status": "pending"},
            "dept-any",
            action="write",
        )

    assert error.value.status_code == 403


# --------------------------------------------------------------------------
# AI-01: provider diagnostics never expose the hosted key
# --------------------------------------------------------------------------


def test_ai_provider_status_reports_configuration_without_secrets(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:latest")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("HOSTED_AI_API_KEY", "super-secret-key-value")
    monkeypatch.setenv("HOSTED_AI_BASE_URL", "https://hosted.example/v1")
    monkeypatch.setenv("HOSTED_AI_MODEL", "some-model")

    status = ai_preparation.ai_provider_status()
    serialized = repr(status)

    assert status["local_model"] == "qwen2.5-coder:latest"
    assert status["hosted_configured"] is True
    assert "super-secret-key-value" not in serialized
    assert "hosted.example" not in serialized
    assert "key" not in serialized.lower()
