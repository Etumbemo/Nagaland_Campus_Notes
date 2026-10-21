"""Storage helpers for public question papers and private documents.

Paper PDFs live in ``UPLOAD_DIR`` and are addressed by the public URL
``/uploads/<filename>``. Institute affiliation documents live in a sibling
directory and are never served statically. Moderated reader resources are
stored separately by the API and are not served from this directory.

The important rule this module enforces: **a file is only removed after the
database transaction that dropped its row has committed.** Removing first
would destroy data if the commit later failed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

UPLOAD_URL_PREFIX = "/uploads/"


def upload_filename(file_url: object) -> str | None:
    """Return the bare filename referenced by a stored paper URL.

    Accepts both the current relative form (``/uploads/x.pdf``) and the legacy
    absolute form (``http://host:8000/uploads/x.pdf``) so old rows keep working.

    Returns ``None`` for anything that is not a plain file inside the upload
    directory, which blocks traversal (``/uploads/../secrets``), nested paths,
    and non-upload URLs.
    """
    if not isinstance(file_url, str) or not file_url.strip():
        return None
    path = unquote(urlsplit(file_url.strip()).path)
    if not path.startswith(UPLOAD_URL_PREFIX):
        return None
    filename = path[len(UPLOAD_URL_PREFIX):]
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        return None
    return filename


def resolve_uploaded_file(file_url: object, upload_dir: Path) -> Path | None:
    """Resolve a stored URL to an existing file directly inside ``upload_dir``."""
    filename = upload_filename(file_url)
    if filename is None:
        return None
    root = Path(upload_dir).resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def delete_uploaded_file(file_url: object, upload_dir: Path) -> bool:
    """Delete the PDF referenced by ``file_url``. Never raises.

    Call this only after the owning row is committed. A missing file is not an
    error: it means an earlier cleanup already ran.
    """
    filename = upload_filename(file_url)
    if filename is None:
        return False
    root = Path(upload_dir).resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root:
        logger.warning("Refusing to delete a file outside the upload directory")
        return False
    try:
        candidate.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # A leftover file is better than a failed request.
        logger.warning("Could not delete uploaded file %s", candidate.name, exc_info=True)
        return False


def resolve_institute_document(stored_path: object, document_dir: Path) -> Path | None:
    """Resolve a stored private-document path, enforcing the directory boundary."""
    if not isinstance(stored_path, str) or not stored_path.strip():
        return None
    root = Path(document_dir).resolve()
    candidate = Path(stored_path).resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def delete_institute_document(stored_path: object, document_dir: Path) -> bool:
    """Delete a private affiliation document. Never raises."""
    candidate = resolve_institute_document(stored_path, document_dir)
    if candidate is None:
        return False
    try:
        candidate.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("Could not delete institute document", exc_info=True)
        return False


def referenced_upload_filenames(file_urls) -> set[str]:
    """Collect the set of filenames referenced by stored paper URLs."""
    names = set()
    for file_url in file_urls:
        filename = upload_filename(file_url)
        if filename:
            names.add(filename)
    return names


def find_orphaned_uploads(upload_dir: Path, referenced: set[str]) -> list[Path]:
    """Return files in ``upload_dir`` that no paper references."""
    root = Path(upload_dir)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir()
        if path.is_file() and path.name not in referenced
    )
