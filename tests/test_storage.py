from unittest.mock import MagicMock

import pytest

from b2b_commerce.config import Settings
from b2b_commerce.infra.storage import ObjectStorage


# Создаёт ObjectStorage с подменённым MinIO client.
def _storage_with_client(client: MagicMock) -> ObjectStorage:
    storage = ObjectStorage(Settings())
    storage._client = client
    return storage


@pytest.mark.asyncio
async def test_get_object_runs_sync_read_in_thread(monkeypatch):
    storage = ObjectStorage(Settings())
    calls: list[str] = []

    def fake_sync(key: str) -> tuple[bytes, str]:
        calls.append(key)
        return b"payload", "image/png"

    async def fake_to_thread(fn, key: str):
        return fn(key)

    monkeypatch.setattr("b2b_commerce.infra.storage.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr(storage, "_read_object_sync", fake_sync)

    data, content_type = await storage.get_object("products/demo/cover.png")

    assert data == b"payload"
    assert content_type == "image/png"
    assert calls == ["products/demo/cover.png"]


@pytest.mark.asyncio
async def test_read_object_sync_returns_bytes_and_closes_response():
    response = MagicMock()
    response.read.return_value = b"img"
    response.headers = {"Content-Type": "image/jpeg"}
    client = MagicMock()
    client.get_object.return_value = response
    storage = _storage_with_client(client)

    data, content_type = await storage.get_object("products/a/b.jpg")

    assert data == b"img"
    assert content_type == "image/jpeg"
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


@pytest.mark.asyncio
async def test_read_object_sync_maps_s3_error_to_file_not_found(monkeypatch):
    storage = ObjectStorage(Settings())

    def fake_sync(key: str) -> tuple[bytes, str]:
        raise FileNotFoundError(key)

    async def fake_to_thread(fn, key: str):
        return fn(key)

    monkeypatch.setattr("b2b_commerce.infra.storage.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr(storage, "_read_object_sync", fake_sync)

    with pytest.raises(FileNotFoundError):
        await storage.get_object("products/missing.jpg")


@pytest.mark.asyncio
async def test_read_object_sync_closes_response_when_read_fails():
    response = MagicMock()
    response.read.side_effect = OSError("read failed")
    client = MagicMock()
    client.get_object.return_value = response
    storage = _storage_with_client(client)

    with pytest.raises(OSError, match="read failed"):
        await storage.get_object("products/a/b.jpg")

    response.close.assert_called_once()
    response.release_conn.assert_called_once()
