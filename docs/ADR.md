# ADR

Короткие решения. Подробности в файлах.

| ADR | Решение |
|-----|---------|
| [001](adr/001-modular-monolith.md) | Один FastAPI-монолит |
| [002](adr/002-postgresql-source-of-truth.md) | PostgreSQL — SoT |
| [003](adr/003-rest-primary.md) | REST на MVP |
| [004](adr/004-company-one-account.md) | Одна клиентская учётка на компанию |
| [005](adr/005-htmx-jinja-bootstrap.md) | Jinja2 + HTMX + CSS (custom design system) |
| [006](adr/006-worker-arq.md) | Arq + Redis, expiration счетов |
| [007](adr/007-object-storage.md) | MinIO |
| [008](adr/008-registration-approval.md) | Registration ≠ Approval ≠ Catalog access |
| [009](adr/009-billing-entity.md) | Юрлицо поставщика отдельно от компании-клиента |

Новый ADR: `docs/adr/00N-slug.md`. GraphQL/gRPC — только после стабилизации MVP.
