"""Private local and S3-compatible storage for reader resource PDFs."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

OBJECT_KEY_PREFIX = "s3:"


class ObjectStorageConfigurationError(RuntimeError):
    """Raised when object storage is enabled without valid settings."""


def resource_storage_backend() -> str:
    backend = os.getenv("RESOURCE_STORAGE_BACKEND", "local").strip().lower()
    if backend not in {"local", "s3"}:
        raise ObjectStorageConfigurationError(
            "RESOURCE_STORAGE_BACKEND must be either 'local' or 's3'."
        )
    return backend


def _s3_configuration() -> dict[str, str | None]:
    bucket = os.getenv("RESOURCE_STORAGE_BUCKET", "").strip()
    region = os.getenv("RESOURCE_STORAGE_REGION", "").strip()
    endpoint = os.getenv("RESOURCE_STORAGE_ENDPOINT_URL", "").strip() or None
    access_key = os.getenv("RESOURCE_STORAGE_ACCESS_KEY_ID", "").strip() or None
    secret_key = os.getenv("RESOURCE_STORAGE_SECRET_ACCESS_KEY", "").strip() or None
    if not bucket:
        raise ObjectStorageConfigurationError(
            "RESOURCE_STORAGE_BUCKET is required when RESOURCE_STORAGE_BACKEND=s3."
        )
    if not region:
        raise ObjectStorageConfigurationError(
            "RESOURCE_STORAGE_REGION is required when RESOURCE_STORAGE_BACKEND=s3."
        )
    if bool(access_key) != bool(secret_key):
        raise ObjectStorageConfigurationError(
            "Set both RESOURCE_STORAGE_ACCESS_KEY_ID and "
            "RESOURCE_STORAGE_SECRET_ACCESS_KEY, or neither."
        )
    if endpoint and not endpoint.startswith(("https://", "http://")):
        raise ObjectStorageConfigurationError(
            "RESOURCE_STORAGE_ENDPOINT_URL must start with http:// or https://."
        )
    return {
        "bucket": bucket,
        "region": region,
        "endpoint": endpoint,
        "access_key": access_key,
        "secret_key": secret_key,
    }


def validate_resource_storage_configuration() -> None:
    """Fail during application startup for invalid storage configuration."""
    if resource_storage_backend() == "s3":
        _s3_configuration()


def _s3_client():
    import boto3
    from botocore.config import Config

    configuration = _s3_configuration()
    options = {
        "region_name": configuration["region"],
        "endpoint_url": configuration["endpoint"],
        "aws_access_key_id": configuration["access_key"],
        "aws_secret_access_key": configuration["secret_key"],
    }
    if configuration["endpoint"]:
        options["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **options)


def is_object_storage_path(stored_path: object) -> bool:
    return isinstance(stored_path, str) and stored_path.startswith(OBJECT_KEY_PREFIX)


def _safe_object_key(stored_path: object) -> str | None:
    if not is_object_storage_path(stored_path):
        return None
    key = stored_path[len(OBJECT_KEY_PREFIX):]
    if (
        not key
        or "/" in key
        or "\\" in key
        or Path(key).name != key
        or key in {".", ".."}
    ):
        return None
    return key


def store_resource_file(
    file_name: str,
    contents: bytes,
    local_directory: Path,
) -> str:
    """Persist a resource PDF and return its database storage reference."""
    if Path(file_name).name != file_name or file_name in {".", ".."}:
        raise ValueError("Resource filename must be a plain filename.")
    if resource_storage_backend() == "local":
        (local_directory / file_name).write_bytes(contents)
        return file_name

    configuration = _s3_configuration()
    _s3_client().put_object(
        Bucket=configuration["bucket"],
        Key=file_name,
        Body=contents,
        ContentType="application/pdf",
    )
    return f"{OBJECT_KEY_PREFIX}{file_name}"


def get_resource_object(stored_path: object) -> dict | None:
    """Fetch a private object using its validated database storage reference."""
    key = _safe_object_key(stored_path)
    if key is None:
        return None
    configuration = _s3_configuration()
    return _s3_client().get_object(Bucket=configuration["bucket"], Key=key)


def delete_stored_resource_file(stored_path: object, local_directory: Path) -> bool:
    """Delete either a local resource file or an S3 object; never raises."""
    key = _safe_object_key(stored_path)
    if key is not None:
        try:
            configuration = _s3_configuration()
            _s3_client().delete_object(Bucket=configuration["bucket"], Key=key)
            return True
        except Exception:
            logger.warning("Could not delete private object-storage resource", exc_info=True)
            return False

    if not isinstance(stored_path, str) or not stored_path.strip():
        return False
    if Path(stored_path).name != stored_path:
        logger.warning("Refusing to delete a resource outside the local storage directory")
        return False
    root = Path(local_directory).resolve()
    candidate = (root / stored_path).resolve()
    if candidate.parent != root:
        logger.warning("Refusing to delete a resource outside the local storage directory")
        return False
    try:
        candidate.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("Could not delete private resource file", exc_info=True)
        return False
