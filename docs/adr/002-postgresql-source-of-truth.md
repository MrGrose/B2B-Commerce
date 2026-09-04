# ADR-002: PostgreSQL is the source of truth

Status: Accepted

## Context

Остатки, резервы и финансы нельзя размазать по файлам и кэшу.

## Decision

PostgreSQL — система записи. Redis — очередь и кэш, не остатки. Снимки цен на счёте, не текущий `products.sale_price`.

## Consequences

Все инварианты склада — constraints + транзакции. Отчёты finance читают invoice snapshots.
