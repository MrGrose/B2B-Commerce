# ADR-007: Object storage for product photos

Status: Accepted

## Context

Фото товаров нужны с первого каталога. S3 в проде, локально без AWS.

## Decision

Приложение пишет в object storage API. Локально MinIO в Compose. В prod — S3-совместимый бакет. В БД храним `storage_key`.

## Consequences

Смена бакета не трогает домен. Загрузка файлов валидируется (тип, размер).
