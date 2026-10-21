import json
import io
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

try:
    from app import ai_preparation, main
    from app.schemas import AIPreparationGenerateRequest
except ModuleNotFoundError:
    from backend.app import ai_preparation, main
    from backend.app.schemas import AIPreparationGenerateRequest


def make_paper(
    upload_dir: Path,
    paper_id: str,
    *,
    year: str = "2024",
    subject: str = "Algorithms",
    subject_code: str | None = "CS201",
    department_id: str = "dept-1",
    semester: int = 3,
    filename: str | None = None,
) -> dict:
    filename = filename or f"{paper_id}.pdf"
    (upload_dir / filename).write_bytes(b"%PDF-fake")
    return {
        "id": paper_id,
        "subject": subject,
        "subject_code": subject_code,
        "department_id": department_id,
        "department_name": "Computer Science",
        "semester": semester,
        "exam_type": "End Semester",
        "year": year,
        "file_url": f"https://example.test/uploads/{filename}",
    }


def test_subject_options_only_include_uploaded_papers_with_valid_years(tmp_path):
    first = make_paper(tmp_path, "paper-1", year="2022")
    second = make_paper(tmp_path, "paper-2", year="2024")
    other_subject = make_paper(
        tmp_path,
        "paper-3",
        year="2023",
        subject="Operating Systems",
        subject_code="CS301",
    )
    invalid_year = make_paper(tmp_path, "paper-4", year="202X")
    missing_file = {
        **make_paper(tmp_path, "paper-5"),
        "file_url": "https://example.test/uploads/not-present.pdf",
    }

    options = ai_preparation.build_subject_options(
        [first, second, other_subject, invalid_year, missing_file],
        tmp_path,
    )

    assert [option["subject"] for option in options] == ["Algorithms", "Operating Systems"]
    algorithms = options[0]
    assert algorithms["available_years"] == [2022, 2024]
    assert algorithms["papers_by_year"] == [
        {"year": 2022, "count": 1},
        {"year": 2024, "count": 1},
    ]
    assert algorithms["paper_count"] == 2


def test_year_filter_reports_missing_years_and_excludes_other_years(tmp_path):
    papers = [
        make_paper(tmp_path, "paper-2021", year="2021"),
        make_paper(tmp_path, "paper-2023", year="2023"),
        make_paper(tmp_path, "paper-2025", year="2025"),
    ]

    selected, missing = ai_preparation.select_papers_for_year_range(
        papers,
        2021,
        2024,
        tmp_path,
    )

    assert [paper["_year"] for paper in selected] == [2021, 2023]
    assert missing == [2022, 2024]


def test_stored_pdf_resolution_rejects_paths_outside_uploads(tmp_path):
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"%PDF")
    assert ai_preparation.stored_pdf_path(
        f"https://example.test/uploads/../{outside.name}",
        tmp_path,
    ) is None
    assert ai_preparation.stored_pdf_path(
        "https://other.example/remote.pdf",
        tmp_path,
    ) is None


def test_question_count_is_capped_and_duplicate_questions_are_removed(tmp_path):
    paper = make_paper(tmp_path, "paper-1")
    paper["_year"] = 2024
    questions = [
        {
            "question": "Explain binary search.",
            "type": "short_answer",
            "answer": "A supported answer.",
            "origin": "past_paper",
            "sources": [{"paper_id": "paper-1", "page": 1}],
        },
        {
            "question": "Explain binary-search!",
            "type": "short_answer",
            "answer": "A supported answer.",
            "origin": "past_paper",
            "sources": [{"paper_id": "paper-1", "page": 1}],
        },
        {
            "question": "What is a queue?",
            "type": "short_answer",
            "answer": "A supported answer.",
            "origin": "past_paper",
            "sources": [{"paper_id": "paper-1", "page": 1}],
        },
    ]

    deduplicated = ai_preparation.deduplicate_questions(questions)

    assert len(deduplicated) == 2
    assert deduplicated[0]["question"] == "Explain binary search."
    limited = ai_preparation.validate_and_enrich_questions(
        deduplicated,
        [paper],
        {"paper-1": ["text"]},
        feature="mock_test",
        question_count=1,
    )
    assert len(limited) == 1
    assert limited[0]["question"] == "Explain binary search."


def test_question_sources_must_be_from_selected_papers(tmp_path):
    paper = make_paper(tmp_path, "paper-1")
    paper["_year"] = 2024
    pages = {"paper-1": ["text"] * 2}
    question = {
        "question": "Explain the source topic.",
        "type": "short_answer",
        "answer": "A supported answer.",
        "origin": "past_paper",
        "sources": [{"paper_id": "not-selected", "page": 1}],
    }

    with pytest.raises(ai_preparation.AIProviderError, match="outside the selected source set"):
        ai_preparation.validate_and_enrich_questions(
            [question],
            [paper],
            pages,
            feature="mock_test",
            question_count=10,
        )


def test_question_page_citation_must_exist_in_source_pdf(tmp_path):
    paper = make_paper(tmp_path, "paper-1")
    paper["_year"] = 2024
    question = {
        "question": "Explain the source topic.",
        "type": "short_answer",
        "answer": "A supported answer.",
        "origin": "past_paper",
        "sources": [{"paper_id": "paper-1", "page": 3}],
    }

    with pytest.raises(ai_preparation.AIProviderError, match="outside the selected PDF"):
        ai_preparation.validate_and_enrich_questions(
            [question],
            [paper],
            {"paper-1": ["text", "text"]},
            feature="mock_test",
            question_count=10,
        )


def test_important_question_priority_is_limited_by_recurrence_evidence(tmp_path):
    paper = make_paper(tmp_path, "paper-1")
    paper["_year"] = 2024
    question = {
        "question": "Explain binary search trees.",
        "type": "long_answer",
        "answer": "A supported revision answer.",
        "origin": "past_paper",
        "sources": [{"paper_id": "paper-1", "page": 1}],
        "priority": "high",
        "priority_reason": "The model assigned a high priority.",
    }

    result = ai_preparation.validate_and_enrich_questions(
        [question],
        [paper],
        {"paper-1": ["text"]},
        feature="important_questions",
        question_count=10,
    )

    assert result[0]["priority"] == "low"
    assert "Cross-paper recurrence is not established" in result[0]["priority_reason"]


def test_excessive_model_question_response_is_rejected():
    with pytest.raises(ai_preparation.AIProviderError, match="too many questions"):
        ai_preparation.validate_and_enrich_questions(
            [{}] * 5,
            [],
            {},
            feature="mock_test",
            question_count=1,
        )


def test_generation_falls_back_only_after_explicit_consent(monkeypatch):
    monkeypatch.setattr(
        ai_preparation,
        "_call_ollama",
        lambda _prompt: (_ for _ in ()).throw(ai_preparation.AIProviderError("offline")),
    )
    monkeypatch.setattr(ai_preparation, "_hosted_provider_configured", lambda: True)
    monkeypatch.setattr(ai_preparation, "_call_hosted", lambda _prompt: '{"questions": []}')

    with pytest.raises(ai_preparation.AIUnavailableError, match="consent"):
        ai_preparation.generate_with_fallback("prompt", allow_hosted_fallback=False)

    response, provider = ai_preparation.generate_with_fallback(
        "prompt",
        allow_hosted_fallback=True,
    )
    assert json.loads(response) == {"questions": []}
    assert provider == "hosted"


def test_generation_reports_when_both_ai_providers_are_unavailable(monkeypatch):
    monkeypatch.setattr(
        ai_preparation,
        "_call_ollama",
        lambda _prompt: (_ for _ in ()).throw(ai_preparation.AIProviderError("offline")),
    )
    monkeypatch.setattr(ai_preparation, "_hosted_provider_configured", lambda: True)
    monkeypatch.setattr(
        ai_preparation,
        "_call_hosted",
        lambda _prompt: (_ for _ in ()).throw(ai_preparation.AIProviderError("offline")),
    )

    with pytest.raises(ai_preparation.AIUnavailableError, match="Local and hosted AI providers"):
        ai_preparation.generate_with_fallback("prompt", allow_hosted_fallback=True)


def test_generation_endpoint_rejects_count_above_feature_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    request = AIPreparationGenerateRequest(
        feature="important_questions",
        subject="Algorithms",
        subject_code="CS201",
        department_id="dept-1",
        semester=3,
        start_year=2022,
        end_year=2024,
        question_count=21,
    )

    with pytest.raises(HTTPException) as error:
        main.generate_ai_preparation(request, db=object(), user={"role": "user"})

    assert error.value.status_code == 422
    assert "at most 20" in error.value.detail


def test_generation_endpoint_reports_missing_source_papers(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(main, "query_all", lambda *_args: [])
    request = AIPreparationGenerateRequest(
        feature="mock_test",
        subject="Algorithms",
        subject_code="CS201",
        department_id="dept-1",
        semester=3,
        start_year=2022,
        end_year=2024,
        question_count=10,
    )

    with pytest.raises(HTTPException) as error:
        main.generate_ai_preparation(request, db=object(), user={"role": "user"})

    assert error.value.status_code == 404
    assert "No uploaded question-paper PDFs" in error.value.detail


def test_generation_endpoint_uses_selected_papers_and_reports_shortfall(monkeypatch, tmp_path):
    papers = [
        make_paper(tmp_path, "paper-2022", year="2022"),
        make_paper(tmp_path, "paper-2024", year="2024"),
    ]
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)

    def query_rows(_db, _sql, params):
        assert params == ("Algorithms", "dept-1", 3, "CS201")
        return papers

    monkeypatch.setattr(main, "query_all", query_rows)
    monkeypatch.setattr(main, "extract_pdf_pages", lambda _path: ["Source paper text."])
    monkeypatch.setattr(
        main,
        "generate_with_fallback",
        lambda _prompt, *, allow_hosted_fallback: (
            json.dumps({
                "questions": [{
                    "question": "Which algorithm uses a queue?",
                    "type": "multiple_choice",
                    "options": ["Breadth-first search", "Binary search"],
                    "answer": "Breadth-first search",
                    "origin": "past_paper",
                    "sources": [{"paper_id": "paper-2022", "page": 1, "question_number": "Q1"}],
                }]
            }),
            "local",
        ),
    )
    request = AIPreparationGenerateRequest(
        feature="mock_test",
        subject="Algorithms",
        subject_code="CS201",
        department_id="dept-1",
        semester=3,
        start_year=2022,
        end_year=2024,
        question_count=3,
    )

    result = main.generate_ai_preparation(
        request,
        db=object(),
        user={"role": "user"},
    )

    assert result["provider"] == "local"
    assert result["source_papers_found"] == 2
    assert result["missing_years"] == [2023]
    assert result["generated_count"] == 1
    assert "Only 1 distinct questions" in result["message"]
    assert result["questions"][0]["sources"][0]["year"] == 2022


def test_ai_preparation_options_require_reader_role(monkeypatch):
    query_calls = []
    monkeypatch.setattr(main, "query_all", lambda *_args: query_calls.append(True))

    with pytest.raises(HTTPException) as error:
        main.ai_preparation_options(
            db=object(),
            user={"role": "institute", "approval_status": "approved"},
        )

    assert error.value.status_code == 403
    assert query_calls == []


def test_digital_pdf_text_is_extracted_with_page_order(tmp_path):
    import fitz

    pdf_path = tmp_path / "digital-paper.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Explain the principles of a binary search tree and its traversal methods.",
    )
    document.save(pdf_path)
    document.close()

    pages = ai_preparation.extract_pdf_pages(pdf_path)

    assert len(pages) == 1
    assert "binary search tree" in pages[0]


def test_short_digital_pdf_does_not_require_ocr(tmp_path, monkeypatch):
    import fitz

    pdf_path = tmp_path / "short-digital-paper.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Q1: Solve x.")
    document.save(pdf_path)
    document.close()
    monkeypatch.setattr(
        ai_preparation,
        "_ocr_page",
        lambda *_args: (_ for _ in ()).throw(AssertionError("OCR should not run")),
    )

    pages = ai_preparation.extract_pdf_pages(pdf_path)

    assert "Solve x" in pages[0]


def test_scanned_pdf_page_uses_ocr_fallback(tmp_path, monkeypatch):
    import fitz

    pdf_path = tmp_path / "scanned-paper.pdf"
    image_buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(image_buffer, format="PNG")
    document = fitz.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=image_buffer.getvalue())
    document.save(pdf_path)
    document.close()
    ocr_calls = []
    monkeypatch.setattr(
        ai_preparation,
        "_ocr_page",
        lambda _page, filename: ocr_calls.append(filename) or "OCR extracted question text.",
    )

    pages = ai_preparation.extract_pdf_pages(pdf_path)

    assert pages == ["OCR extracted question text."]
    assert ocr_calls == ["scanned-paper.pdf"]
