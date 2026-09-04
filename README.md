# B2B Commerce

B2B-опт спортивной экипировки: один логин на компанию-клиента, каталог, корзина, счета, склад и финансы в одном сервисе.

Публичный demo-проект: FastAPI, Jinja2 + HTMX, async SQLAlchemy, PostgreSQL, Redis, ARQ, MinIO. Оригинальный репозиторий разработки — приватный.

## Архитектура

Браузер ходит в FastAPI за HTML (Jinja + HTMX) и JSON под `/api`. Домен живёт в сервисах, доступ к БД — в репозиториях. Слой handlers не держит бизнес-правила: **router → service → repository**.

Фоновая работа — ARQ и Redis: истечение неоплаченных счетов и снятие резервов, синхронизация курса Rapira, пересчёт цен. Фото каталога — в MinIO (S3-совместимое хранилище).

```text
Браузер (HTML / HTMX)
        ↓
     FastAPI
   ├── PostgreSQL  (SQLAlchemy 2, Alembic)
   ├── Redis       (rate limit, очередь ARQ)
   ├── MinIO       (фото каталога)
   └── ARQ worker  (expire invoices, Rapira, reprice)
```

Модули: `auth`, `catalog`, `inventory`, `cart`, `invoices`, `companies`, `finance`, `notifications`, `support`, `rapira`, `audit`, `payments`, плюс `infra/` (БД, Redis, S3/MinIO, security).

## Основные возможности

### Компании и доступ
- Саморегистрация компании (`pending`), approve/reject у админа
- Один аккаунт на компанию; cookie-сессия + CSRF
- Роли admin и customer

### Каталог и склад
- Поиск, фильтры, категории, пагинация
- Остатки и резерв под счёт
- Импорт прайса из Excel (лист `Прайс`) с картинками
- Фото товаров в MinIO

### Корзина и счета
- Корзина с проверкой остатков
- Счёт из корзины; создание резервирует сток
- Жизненный цикл: `awaiting_payment` → `paid` → `shipped` (также `expired`, `canceled`)
- TTL неоплаченного счёта в рабочих днях; воркер снимает резерв
- Экспорт PDF и XLSX; реквизиты поставщика фиксируются в документе

### Финансы и курс
- Финансовый дашборд
- Курс USD/RUB (Rapira): действие админа + cron ARQ

### Операционка
- Аудит-лог, уведомления, тикеты поддержки
- Rate limit на login/register; в prod старт отклоняет дефолтные секреты

## Технологический стек

### Backend
- **Python 3.12**, **FastAPI**, **Uvicorn**
- **Jinja2**, **HTMX**
- **SQLAlchemy 2** (async) + **asyncpg**
- **Alembic**, **Pydantic Settings**
- **Argon2**, **openpyxl**, **fpdf2**

### Данные и очереди
- **PostgreSQL**, **Redis**, **ARQ**
- **MinIO** (S3)

### Инфраструктура и качество
- **Docker**, **Docker Compose**, **uv**
- **pytest**, **Ruff**
- **GitHub Actions** (pytest + Alembic; release — сборка/push образа в GHCR)

## Структура проекта

```text
.
├── src/b2b_commerce/
│   ├── main.py            FastAPI app, роутеры, /api/health
│   ├── worker.py          ARQ: expire invoices, Rapira, reprice
│   ├── bootstrap.py       создание первого админа
│   ├── config.py          настройки из .env
│   ├── db.py              async engine / sessions
│   ├── http.py            HTML-хелперы, сессия в шаблонах
│   ├── enums.py           статусы и доменные enum
│   ├── tables.py          общие декларации таблиц
│   ├── auth/              логин, регистрация, cookie-сессия, CSRF
│   ├── admin/             админ-панель (HTML/API)
│   ├── companies/         компании-клиенты, approve/reject
│   ├── catalog/           товары, категории, импорт XLSX
│   ├── inventory/         остатки и резервы
│   ├── cart/              корзина
│   ├── invoices/          счета, PDF/XLSX, жизненный цикл
│   ├── finance/           финансовый дашборд
│   ├── payments/          платёжные сущности (модели)
│   ├── rapira/            курс USD/RUB
│   ├── notifications/     исходящие уведомления
│   ├── support/           тикеты поддержки
│   ├── audit/             аудит-лог
│   ├── legal/             privacy / terms и т.п.
│   ├── infra/             health, security, MinIO/S3
│   ├── templates/         Jinja2 HTML
│   └── static/            CSS, JS, шрифты, media
├── alembic/               миграции
├── scripts/               seed, deploy
├── tests/                 pytest + fixtures (demo XLSX)
├── docs/                  ADR, CHANGELOG
├── make/                  dev / test / lint / prod цели
├── docker-compose.yml
├── docker-compose.prod.yml
└── Makefile
```

## Быстрый старт

Нужны Docker и **uv**. Для seed в `.env` должен быть `APP_ENV=dev`.

### 1. Окружение

```bash
cp .env.example .env
```

В примере уже плейсхолдеры (`admin123`, `b2b-commerce-secret`, demo-юрлицо) — не боевые секреты. В prod старт с такими значениями упадёт (`validate_prod_settings`).

### 2. Зависимости и инфраструктура

```bash
make install                  # uv sync --extra dev
make up-deps                  # postgres, redis, minio
make migrate
make create-admin             # ADMIN_LOGIN / ADMIN_PASSWORD из .env
make dev-seed                 # demo-каталог + клиент (админа не создаёт)
make dev                      # http://127.0.0.1:8000
```

Опционально: `make worker` для ARQ. Весь стек в Docker: `make up`. Health: `GET /api/health`.

Полезные цели: `make down`, `make logs`, `make test`, `make lint`.

## Основные эндпоинты

| Путь | Назначение |
| --- | --- |
| `GET /api/health` | liveness |
| `GET /api/ready` | readiness |
| `/login`, `/register` | вход и регистрация компании |
| `/catalog`, `/cart`, `/invoices` | витрина клиента (HTML) |
| `/admin/*` | админка (HTML) |
| `/api/auth/*`, `/api/catalog/*`, `/api/cart/*`, `/api/invoices/*` | те же потоки JSON |
| `/privacy`, `/terms` и др. legal | страницы из шаблонов |

## Ограничения public demo

- В репозитории нет боевых credentials и дампа продакшен-данных.
- Rapira — публичный endpoint курса; для живых вызовов нужна сеть.
- Без MinIO seed поднимает каталог без фото.
- Telegram/SMTP в коде есть; живых ключей в снимке нет.
- Release workflow — только CI + push образа в GHCR (`github.repository`); SSH-деплоя из Actions нет.
- Опциональные `scripts/deploy*.sh` читают локальный `scripts/deploy.env` (из `deploy.env.example`, в git не входит).

Снимок для чтения архитектуры и локального запуска, не как готовый прод-деплой.

## Ссылки

- [Changelog](docs/CHANGELOG.md)
- [ADR](docs/ADR.md)
