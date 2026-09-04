# ADR-005: Jinja2 + server HTML + HTMX

Status: Accepted (styling later superseded)  
Supersedes: Vite/React/shadcn (отклонено)

## Context

Нужны admin и кабинет клиента. React/SPA — лишняя сложность для этого MVP.

## Decision (original)

Серверный HTML: Jinja2 + HTMX + CSS framework. Точечный JS по месту. Playwright не используем.

Первоначально в ADR фигурировал Bootstrap 5; это исторический выбор стека UI, не текущая реализация.

## Current implementation

Шаблоны Jinja2 + **custom CSS** / design system (feature 011) + HTMX + Lucide. Bootstrap в проекте не используется.

## Consequences

Шаблоны живут в backend. Отдельного frontend-бандлера нет. Один процесс отдаёт и HTML, и JSON.
