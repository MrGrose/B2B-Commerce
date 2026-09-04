# ADR-003: REST as primary transport

Status: Accepted

## Context

Нужен основной API для web-клиента.

## Decision

REST + OpenAPI на MVP. GraphQL — эксперимент после стабилизации, без смены домена. gRPC не берём без конкретной нужды.

## Consequences

Домен не зависит от FastAPI-схем. Клиенты ходят в одни и те же application services.
