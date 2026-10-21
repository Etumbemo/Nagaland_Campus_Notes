import io
import json
import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

logger = logging.getLogger(__name__)

MAX_SOURCE_PAPERS = 10
MAX_SOURCE_PAGES = 100
MAX_TOTAL_SOURCE_PAGES = 200
MAX_SOURCE_TEXT_CHARACTERS = 120_000
MAX_MODEL_QUESTION_MULTIPLIER = 4
MAX_MOCK_QUESTIONS = 30
MAX_IMPORTANT_QUESTIONS = 20


class AIPreparationError(Exception):
    pass


class PDFProcessingError(AIPreparationError):
    pass


class AIProviderError(AIPreparationError):
    pass


class AIUnavailableError(AIPreparationError):
    pass


def parse_exam_year(value: object) -> int | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}", value.strip()) is None:
        return None
    year = int(value)
    if year < 1900 or year > 2100:
        return None
    return year


def stored_pdf_path(file_url: object, upload_dir: Path) -> Path | None:
    if not isinstance(file_url, str) or not file_url.strip():
        return None
    file_path = unquote(urlsplit(file_url.strip()).path)
    prefix = "/uploads/"
    if not file_path.startswith(prefix):
        return None
    filename = file_path[len(prefix):]
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        return None
    root = upload_dir.resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def build_subject_options(rows: list[dict], upload_dir: Path) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for paper in rows:
        year = parse_exam_year(paper.get("year"))
        if year is None or stored_pdf_path(paper.get("file_url"), upload_dir) is None:
            continue
        subject = str(paper.get("subject") or "").strip()
        department_id = paper.get("department_id")
        if not subject or not department_id:
            continue
        subject_code = paper.get("subject_code")
        semester = paper.get("semester")
        key = (subject.casefold(), (subject_code or "").casefold(), department_id, semester)
        option = groups.setdefault(key, {
            "subject": subject,
            "subject_code": subject_code,
            "department_id": department_id,
            "department_name": paper.get("department_name"),
            "semester": semester,
            "papers_by_year": {},
        })
        option["papers_by_year"][year] = option["papers_by_year"].get(year, 0) + 1

    options = []
    for option in groups.values():
        papers_by_year = option.pop("papers_by_year")
        option["available_years"] = sorted(papers_by_year)
        option["papers_by_year"] = [
            {"year": year, "count": papers_by_year[year]}
            for year in sorted(papers_by_year)
        ]
        option["paper_count"] = sum(papers_by_year.values())
        options.append(option)
    return sorted(
        options,
        key=lambda item: (
            item["subject"].casefold(),
            (item["subject_code"] or "").casefold(),
            str(item["department_name"] or "").casefold(),
            item["semester"] or 0,
        ),
    )


def select_papers_for_year_range(
    rows: list[dict],
    start_year: int,
    end_year: int,
    upload_dir: Path,
) -> tuple[list[dict], list[int]]:
    selected = []
    for paper in rows:
        year = parse_exam_year(paper.get("year"))
        if year is None or not start_year <= year <= end_year:
            continue
        path = stored_pdf_path(paper.get("file_url"), upload_dir)
        if path is None:
            continue
        selected.append({**paper, "_year": year, "_path": path})
    selected.sort(key=lambda item: (item["_year"], item["id"]))
    available = {paper["_year"] for paper in selected}
    missing = [
        year for year in range(start_year, end_year + 1)
        if year not in available
    ]
    return selected, missing


def extract_pdf_pages(file_path: Path) -> list[str]:
    try:
        import fitz
    except ImportError as error:
        raise PDFProcessingError(
            "PDF text extraction is not installed on the server. Install the backend dependencies."
        ) from error

    try:
        document = fitz.open(file_path)
    except (fitz.FileDataError, RuntimeError, ValueError) as error:
        raise PDFProcessingError(
            f"Could not read uploaded PDF '{file_path.name}'. It may be damaged or encrypted."
        ) from error
    try:
        if document.page_count == 0:
            raise PDFProcessingError(f"The PDF '{file_path.name}' has no pages.")
        if document.page_count > MAX_SOURCE_PAGES:
            raise PDFProcessingError(
                f"The PDF '{file_path.name}' has more than {MAX_SOURCE_PAGES} pages; "
                "choose a narrower source set."
            )

        pages = []
        for page in document:
            text = page.get_text("text").strip()
            if not text and page.get_images(full=True):
                text = _ocr_page(page, file_path.name)
            pages.append(text.strip())
        if not any(pages):
            raise PDFProcessingError(
                f"No readable text was found in '{file_path.name}'. Check the PDF or OCR setup."
            )
        return pages
    except (fitz.FileDataError, OSError, RuntimeError, ValueError) as error:
        raise PDFProcessingError(
            f"Could not read uploaded PDF '{file_path.name}'. It may be damaged or encrypted."
        ) from error
    finally:
        document.close()


def _ocr_page(page, filename: str) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as error:
        raise PDFProcessingError(
            f"Scanned PDF '{filename}' requires OCR. Install the backend OCR dependencies."
        ) from error

    tesseract_command = os.getenv("TESSERACT_CMD", "").strip()
    if tesseract_command:
        pytesseract.pytesseract.tesseract_cmd = tesseract_command
    try:
        image = Image.open(io.BytesIO(page.get_pixmap(dpi=180).tobytes("png")))
        return pytesseract.image_to_string(image, lang="eng", timeout=30)
    except pytesseract.TesseractNotFoundError as error:
        raise PDFProcessingError(
            f"Scanned PDF '{filename}' needs OCR, but the Tesseract OCR executable "
            "is not installed or TESSERACT_CMD is not configured."
        ) from error
    except (OSError, pytesseract.TesseractError, RuntimeError) as error:
        raise PDFProcessingError(
            f"OCR failed while reading '{filename}'. Check the server's OCR installation."
        ) from error


def build_ai_prompt(
    feature: str,
    papers: list[dict],
    pages_by_paper: dict[str, list[str]],
    question_count: int,
) -> str:
    source_parts = []
    total_characters = 0
    for paper in papers:
        for page_number, text in enumerate(pages_by_paper[str(paper["id"])], start=1):
            if not text:
                continue
            total_characters += len(text)
            if total_characters > MAX_SOURCE_TEXT_CHARACTERS:
                raise PDFProcessingError(
                    "The selected PDFs contain too much text to analyze at once. "
                    "Narrow the year range or choose fewer source papers."
                )
            source_parts.append(
                f"[PAPER id={paper['id']} year={paper['_year']} "
                f"subject={paper['subject']} exam={paper.get('exam_type') or ''} "
                f"page={page_number}]\n{text}"
            )

    feature_instructions = (
        "Create a balanced mock test from the supplied past-paper text. Prefer exact "
        "past-paper questions when clearly present; otherwise create a new variation "
        "and mark it ai_generated. Include multiple_choice, short_answer, and "
        "long_answer types where supported by the sources. Do not repeat questions."
        if feature == "mock_test"
        else "Create a prioritized revision list. Detect repeated questions and similar "
        "questions testing the same concept across papers, then keep one representative "
        "question for each concept. Prioritize recurrence and marks only when evidenced "
        "in the text. Assign high, medium, or low priority and explain the evidence. "
        "Never promise the questions will appear in a future exam."
    )
    return f"""You analyze only the supplied previous-year question-paper excerpts.
Do not invent source papers, years, pages, question numbers, recurrence, or marks.
Return one valid JSON object and no markdown, using this shape:
{{
  "questions": [
    {{
      "question": "question text",
      "type": "multiple_choice|short_answer|long_answer",
      "options": ["choice 1", "choice 2"],
      "answer": "exact correct choice for multiple choice; otherwise a concise answer",
      "explanation": "brief explanation",
      "marks": null,
      "origin": "past_paper|ai_generated",
      "sources": [{{"paper_id": "exact supplied id", "page": 1, "question_number": "if visible"}}],
      "priority": "high|medium|low",
      "priority_reason": "evidence-based reason"
    }}
  ]
}}
For a multiple-choice question, answer must exactly equal one option. For past_paper
origin, cite at least one exact source. For AI-generated variations, cite the source
paper(s) that informed them but mark origin ai_generated. Keep to the requested count
or return fewer if the source material cannot support more distinct questions.

Feature: {feature}
Requested number of questions: {question_count}
Task: {feature_instructions}

SOURCE TEXT:
{chr(10).join(source_parts)}
"""


def _provider_timeout() -> float:
    try:
        timeout = float(os.getenv("AI_PROVIDER_TIMEOUT_SECONDS", "45"))
    except ValueError as error:
        raise AIProviderError("AI_PROVIDER_TIMEOUT_SECONDS must be a number.") from error
    if not 1 <= timeout <= 180:
        raise AIProviderError("AI_PROVIDER_TIMEOUT_SECONDS must be between 1 and 180.")
    return timeout


def _call_ollama(prompt: str) -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "llama3.2:3b").strip()
    if not model:
        raise AIProviderError("OLLAMA_MODEL is not configured.")
    try:
        response = httpx.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "format": "json",
                "stream": False,
            },
            timeout=_provider_timeout(),
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise AIProviderError("Local Ollama is unavailable or returned an invalid response.") from error
    message = body.get("message") if isinstance(body, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise AIProviderError("Local Ollama returned an empty response.")
    return content


def _hosted_provider_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("HOSTED_AI_BASE_URL", "HOSTED_AI_MODEL", "HOSTED_AI_API_KEY")
    )


def _call_hosted(prompt: str) -> str:
    base_url = os.getenv("HOSTED_AI_BASE_URL", "").strip().rstrip("/")
    model = os.getenv("HOSTED_AI_MODEL", "").strip()
    api_key = os.getenv("HOSTED_AI_API_KEY", "").strip()
    if not all((base_url, model, api_key)):
        raise AIProviderError("Hosted AI fallback is not fully configured.")
    try:
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=_provider_timeout(),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
        raise AIProviderError("Hosted AI provider is unavailable or returned an invalid response.") from error
    if not isinstance(content, str) or not content.strip():
        raise AIProviderError("Hosted AI provider returned an empty response.")
    return content


def generate_with_fallback(prompt: str, *, allow_hosted_fallback: bool) -> tuple[str, str]:
    try:
        return _call_ollama(prompt), "local"
    except AIProviderError as local_error:
        logger.warning("Local AI provider failed: %s", local_error)
    if not _hosted_provider_configured():
        raise AIUnavailableError(
            "Local AI is unavailable and hosted AI fallback is not configured. "
            "Try again later or contact the administrator."
        )
    if not allow_hosted_fallback:
        raise AIUnavailableError(
            "Local AI is unavailable. To use the configured hosted fallback, consent "
            "to sending the selected PDF text to the hosted provider."
        )
    try:
        return _call_hosted(prompt), "hosted"
    except AIProviderError as hosted_error:
        logger.warning("Hosted AI fallback failed: %s", hosted_error)
        raise AIUnavailableError(
            "Local and hosted AI providers are unavailable. Please retry later."
        ) from hosted_error


def parse_ai_questions(response_text: str) -> list[dict]:
    content = response_text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise AIProviderError("The AI provider returned unreadable question data.") from error
    questions = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(questions, list):
        raise AIProviderError("The AI provider returned no question list.")
    return questions


def normalize_question_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def deduplicate_questions(questions: list[dict]) -> list[dict]:
    unique = []
    normalized = []
    for question in questions:
        text = normalize_question_text(question["question"])
        if not text:
            continue
        if any(
            text == prior or SequenceMatcher(None, text, prior).ratio() >= 0.92
            for prior in normalized
        ):
            continue
        unique.append(question)
        normalized.append(text)
    return unique


def validate_and_enrich_questions(
    questions: list[dict],
    papers: list[dict],
    pages_by_paper: dict[str, list[str]],
    *,
    feature: str,
    question_count: int,
) -> list[dict]:
    if len(questions) > question_count * MAX_MODEL_QUESTION_MULTIPLIER:
        raise AIProviderError("The AI provider returned too many questions to validate safely.")
    source_by_id = {str(paper["id"]): paper for paper in papers}
    enriched = []
    for raw in questions:
        if not isinstance(raw, dict):
            raise AIProviderError("The AI provider returned an invalid question.")
        question_text = raw.get("question")
        question_type = raw.get("type")
        if not isinstance(question_text, str) or not question_text.strip():
            raise AIProviderError("The AI provider returned a question with no text.")
        if question_type not in {"multiple_choice", "short_answer", "long_answer"}:
            raise AIProviderError("The AI provider returned an unsupported question type.")

        options = raw.get("options") or []
        if question_type == "multiple_choice":
            if (
                not isinstance(options, list)
                or len(options) < 2
                or not all(isinstance(option, str) and option.strip() for option in options)
            ):
                raise AIProviderError("The AI provider returned invalid multiple-choice options.")
        elif not isinstance(options, list):
            raise AIProviderError("The AI provider returned invalid question options.")

        answer = raw.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise AIProviderError("The AI provider returned a question without an answer.")
        if question_type == "multiple_choice" and answer not in options:
            raise AIProviderError("A multiple-choice answer did not match any provided option.")

        raw_sources = raw.get("sources") or []
        if not isinstance(raw_sources, list):
            raise AIProviderError("The AI provider returned invalid source references.")
        sources = []
        for source in raw_sources:
            if not isinstance(source, dict) or str(source.get("paper_id", "")) not in source_by_id:
                raise AIProviderError("The AI provider cited a paper outside the selected source set.")
            paper_id = str(source["paper_id"])
            paper = source_by_id[paper_id]
            page = source.get("page")
            if page is not None and (
                isinstance(page, bool)
                or not isinstance(page, int)
                or page < 1
                or page > len(pages_by_paper[paper_id])
            ):
                raise AIProviderError("The AI provider cited a page outside the selected PDF.")
            question_number = source.get("question_number")
            if question_number is not None and (
                isinstance(question_number, bool)
                or not isinstance(question_number, (str, int))
                or len(str(question_number)) > 32
            ):
                raise AIProviderError("The AI provider returned an invalid source question number.")
            sources.append({
                "paper_id": paper_id,
                "subject": paper["subject"],
                "year": paper["_year"],
                "exam_type": paper.get("exam_type"),
                "page": page,
                "question_number": source.get("question_number"),
            })
        origin = raw.get("origin")
        if origin not in {"past_paper", "ai_generated"}:
            raise AIProviderError("The AI provider returned an unsupported question origin.")
        if not sources:
            raise AIProviderError("A generated question was returned without a source citation.")

        item = {
            "question": question_text.strip(),
            "type": question_type,
            "options": options,
            "answer": answer.strip(),
            "explanation": str(raw.get("explanation") or "").strip(),
            "marks": raw.get("marks") if isinstance(raw.get("marks"), int) else None,
            "origin": origin,
            "sources": sources,
        }
        if feature == "important_questions":
            priority = str(raw.get("priority") or "").lower()
            if priority not in {"high", "medium", "low"}:
                raise AIProviderError("The AI provider returned an invalid priority.")
            priority_reason = raw.get("priority_reason")
            if not isinstance(priority_reason, str) or not priority_reason.strip():
                raise AIProviderError("The AI provider returned no priority evidence.")
            distinct_source_papers = {source["paper_id"] for source in sources}
            if len(distinct_source_papers) < 2:
                priority = "low"
                priority_reason = (
                    "Cross-paper recurrence is not established by the cited sources. "
                    + priority_reason.strip()
                )
            item["priority"] = priority
            item["priority_reason"] = priority_reason.strip()
        enriched.append(item)

    unique = deduplicate_questions(enriched)
    return unique[:question_count]
