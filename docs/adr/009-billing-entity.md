# ADR-009: Billing entity is not the client company

Status: Accepted

## Context

Счёт выставляется от ИП/ООО поставщика, а не от компании-клиента. Реквизиты продавца сейчас в env (`SUPPLIER_*`). Клиентов может обслуживать несколько юрлиц.

## Decision

- Справочник `billing_entities` (название, ИНН, КПП, адрес, банк).
- У `companies` — nullable `billing_entity_id`. Админ закрепляет юрлицо.
- Seed создаёт одну запись из `SUPPLIER_*`.
- Snapshot реквизитов на Invoice — реализован (Phase 5); PDF/XLSX читают поля счёта, не live env.

## Consequences

Компания без юрлица существует (заявка, офлайн-create). Выставить счёт без billing entity нельзя будет после Phase 5.
