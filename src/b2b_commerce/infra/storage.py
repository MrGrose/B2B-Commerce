import asyncio
from io import BytesIO

from minio import Minio
from minio.error import S3Error

from b2b_commerce.config import Settings


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.minio_bucket
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    # Создаёт бакет, если его ещё нет.
    def ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    # Синхронно сохраняет объект в бакет.
    def _put_object_sync(self, key: str, data: bytes, content_type: str) -> None:
        self.ensure_bucket()
        self._client.put_object(
            self._bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    # Сохраняет объект в бакет (sync API для совместимости).
    def put_object(self, key: str, data: bytes, content_type: str) -> None:
        self._put_object_sync(key, data, content_type)

    # Сохраняет объект в бакет без блокировки event loop.
    async def put_object_async(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._put_object_sync, key, data, content_type)

    # Синхронно читает объект из бакета (вызывается из worker thread).
    def _read_object_sync(self, key: str) -> tuple[bytes, str]:
        response = None
        try:
            try:
                response = self._client.get_object(self._bucket, key)
            except S3Error:
                raise FileNotFoundError(key) from None
            # RAM read: streaming — отдельная оптимизация при росте нагрузки.
            data = response.read()
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            return data, content_type
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    # Читает объект из бакета без блокировки event loop.
    async def get_object(self, key: str) -> tuple[bytes, str]:
        return await asyncio.to_thread(self._read_object_sync, key)
