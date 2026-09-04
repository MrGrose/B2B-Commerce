# ADR-004: One company account on MVP

Status: Accepted

## Context

Нужна изоляция данных компании без ролей внутри клиента.

## Decision

`Company` → ровно один `CompanyAccount` на MVP. Админ системы — таблица `admin_users`, не сотрудник компании.

## Consequences

Авторизация: субъект сессии, не `company_id` из body. Несколько учеток на компанию — после MVP.
