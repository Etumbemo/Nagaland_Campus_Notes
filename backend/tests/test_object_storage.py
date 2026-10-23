import pytest

try:
    from app import object_storage
except ModuleNotFoundError:
    from backend.app import object_storage


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs

    def get_object(self, **kwargs):
        return self.objects[(kwargs["Bucket"], kwargs["Key"])]

    def delete_object(self, **kwargs):
        self.deleted.append((kwargs["Bucket"], kwargs["Key"]))


def configure_s3(monkeypatch):
    monkeypatch.setenv("RESOURCE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("RESOURCE_STORAGE_BUCKET", "private-resources")
    monkeypatch.setenv("RESOURCE_STORAGE_REGION", "ap-south-1")
    monkeypatch.delenv("RESOURCE_STORAGE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("RESOURCE_STORAGE_SECRET_ACCESS_KEY", raising=False)


def test_local_storage_remains_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("RESOURCE_STORAGE_BACKEND", raising=False)
    stored_path = object_storage.store_resource_file("note.pdf", b"%PDF-data", tmp_path)
    assert stored_path == "note.pdf"
    assert (tmp_path / "note.pdf").read_bytes() == b"%PDF-data"
    assert object_storage.delete_stored_resource_file(stored_path, tmp_path)


def test_s3_upload_download_and_delete_use_private_object_key(tmp_path, monkeypatch):
    configure_s3(monkeypatch)
    client = FakeS3Client()
    monkeypatch.setattr(object_storage, "_s3_client", lambda: client)

    stored_path = object_storage.store_resource_file("note.pdf", b"%PDF-data", tmp_path)

    assert stored_path == "s3:note.pdf"
    assert not (tmp_path / "note.pdf").exists()
    assert object_storage.get_resource_object(stored_path)["Body"] == b"%PDF-data"
    assert object_storage.delete_stored_resource_file(stored_path, tmp_path)
    assert client.deleted == [("private-resources", "note.pdf")]


def test_s3_configuration_requires_bucket_and_region(monkeypatch):
    monkeypatch.setenv("RESOURCE_STORAGE_BACKEND", "s3")
    monkeypatch.delenv("RESOURCE_STORAGE_BUCKET", raising=False)
    with pytest.raises(object_storage.ObjectStorageConfigurationError, match="BUCKET"):
        object_storage.validate_resource_storage_configuration()


def test_s3_credentials_must_be_configured_as_a_pair(monkeypatch):
    configure_s3(monkeypatch)
    monkeypatch.setenv("RESOURCE_STORAGE_ACCESS_KEY_ID", "access-key")
    with pytest.raises(object_storage.ObjectStorageConfigurationError, match="both"):
        object_storage.validate_resource_storage_configuration()


def test_object_storage_path_rejects_nested_keys(tmp_path, monkeypatch):
    configure_s3(monkeypatch)
    client = FakeS3Client()
    monkeypatch.setattr(object_storage, "_s3_client", lambda: client)
    assert object_storage.get_resource_object("s3:folder/note.pdf") is None
    assert not object_storage.delete_stored_resource_file("s3:folder/note.pdf", tmp_path)
