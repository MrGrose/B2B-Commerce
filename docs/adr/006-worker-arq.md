# ADR-006: Arq worker for invoice expiration

Status: Accepted

## Context

Счёт живёт 5 дней, потом резерв надо снять. Нельзя полагаться на запрос клиента.

## Decision

Redis + Arq. Джоба `expire_invoices` идемпотентна. Celery не берём на MVP.

## Consequences

Compose поднимает worker рядом с API. Повторы джобы не создают второй `released`.
