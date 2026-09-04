# Скрипты обслуживания (Linux, prod-сервер)

Запускать из **корня репозитория** на машине, где поднят Docker Compose (`docker-compose.prod.yml`) и лежит `.env`.

Подробный runbook: [`docs/DEPLOY.md`](../docs/DEPLOY.md).

**На prod через Makefile:**

```bash
make prod-deploy          # → scripts/deploy.sh (явный B2B_COMMERCE_IMAGE_TAG)
make prod-backup          # → scripts/backup-postgres.sh (TAG не нужен)
make prod-rollback        # → scripts/rollback.sh
make prod-health          # curl /api/health + /login + compose ps (loopback)
make prod-edge-health     # HTTPS через host Caddy (DOMAIN из .env)
make deploy-remote        # → scripts/deploy-remote.sh (с ноутбука по SSH)
```

Operational (`prod-ps`, `prod-logs`, `prod-down`, `prod-migrate`, …) подхватывают `B2B_COMMERCE_IMAGE_TAG` из `.deploy-tag` после первого успешного deploy. Deploy и remote-deploy — **только явный SHA**.

| Скрипт | Назначение |
|--------|------------|
| `vps-bootstrap.sh` | Первичная настройка VPS: Docker, UFW, clone, шаблон `.env`, пользователь `deploy` |
| `deploy.sh` | Deploy: git pull (config) → backup → pull GHCR → stop worker → migrate → worker → api → health |
| `deploy-remote.sh` | **С локального ПК:** SSH на VPS и вызов `deploy.sh` |
| `rollback.sh` | Откат **только образа** api/worker на `.deploy-tag.prev` (без alembic downgrade / restore БД) |
| `backup-postgres.sh` | Снимок PostgreSQL в `/var/backups/b2b_commerce/*.sql.gz`; retention 7 дней |
| `disk-cleanup.sh` | При диске ≥ 80% — старые бэкапы, `docker image prune` |
| `prod-compose-env.sh` | Внутренний loader: `B2B_COMMERCE_IMAGE_TAG` из `.deploy-tag` (через Makefile, не вызывать вручную) |
| `dev_seed.py` | Локальный demo/QA seed (`APP_ENV=dev` only) |
| `dev_reset_data.sh` | Wipe локальной БД + migrate + dev-seed |

Конфиг remote deploy: [`deploy.env.example`](deploy.env.example) → `deploy.env` (в `.gitignore`).

Права на выполнение: `chmod +x scripts/*.sh`

---

## vps-bootstrap.sh

**Что делает:** на чистом VPS (от root) ставит Docker, UFW (22/80/443), клонирует репозиторий в `/opt/b2b-commerce`, создаёт пользователя `deploy`, копирует `.env.prod.example` → `.env`, готовит каталог бэкапов.

**Нужно:** Ubuntu/Debian, доступ root, `GIT_REPO` / `REPO_DIR` при необходимости.

**После bootstrap:** заполнить `.env`, `docker login ghcr.io`, первый deploy с явным `B2B_COMMERCE_IMAGE_TAG`.

**Ручной запуск:**

```bash
REPO_DIR=/opt/b2b-commerce bash scripts/vps-bootstrap.sh
```

---

## deploy.sh

**Что делает:** production deploy на сервере (api/worker; **не** управляет host Caddy). Порядок: проверка диска → `git pull --ff-only` (compose/scripts) → backup Postgres → `docker pull` GHCR → stop worker → `alembic upgrade head` → worker → api → health. При failed health — откат образа на `.deploy-tag.prev` (без restore БД).

**Обязательно:** `B2B_COMMERCE_IMAGE_TAG=<git-sha>` в окружении. Скрипт **не** читает `.deploy-tag` — только явный immutable tag.

**Переменные:** `SKIP_GIT_PULL=1`, `B2B_COMMERCE_PULL=0` (аварийный `docker compose build` вместо pull), `COMPOSE_FILE`, `B2B_COMMERCE_IMAGE_REPO`, `HEALTH_TIMEOUT_SEC`.

**Ручной запуск:**

```bash
cd /opt/b2b-commerce
B2B_COMMERCE_IMAGE_TAG=abc123def456 ./scripts/deploy.sh
```

**Аварийный build без GHCR:**

```bash
B2B_COMMERCE_PULL=0 B2B_COMMERCE_IMAGE_TAG=abc123def456 ./scripts/deploy.sh
```

---

## deploy-remote.sh

**Что делает:** с локальной машины по SSH вызывает тот же `deploy.sh` на VPS (контракт идентичен будущему GitHub Actions).

**Нужно:** `scripts/deploy.env` (из `deploy.env.example`), код в git на сервере, явный `B2B_COMMERCE_IMAGE_TAG`.

**Ручной запуск:**

```bash
cp scripts/deploy.env.example scripts/deploy.env
# B2B_COMMERCE_SSH=deploy@host, B2B_COMMERCE_REMOTE_DIR=/opt/b2b-commerce
B2B_COMMERCE_IMAGE_TAG=abc123def456 ./scripts/deploy-remote.sh
```

---

## rollback.sh

**Что делает:** откатывает api/worker на образ из `.deploy-tag.prev`. Миграции не откатывает, PostgreSQL не восстанавливает.

**Когда:** после неудачного deploy вручную или если auto-rollback в `deploy.sh` не помог.

**Ручной запуск:**

```bash
cd /opt/b2b-commerce
./scripts/rollback.sh
# или
make prod-rollback
```

---

## backup-postgres.sh

**Что делает:** `pg_isready` + `pg_dump` из контейнера `b2b-commerce-postgres` → gzip в `/var/backups/b2b_commerce/b2b_commerce_YYYYMMDD_HHMMSS.sql.gz`. Повторные попытки при временной недоступности БД. Удаляет файлы старше `BACKUP_RETENTION_DAYS` (по умолчанию 7).

**Не использует** `docker compose` и **не требует** `B2B_COMMERCE_IMAGE_TAG`.

**Нужно:** работающий контейнер `b2b-commerce-postgres`, в `.env` заданы `POSTGRES_USER`, `POSTGRES_DB`.

**Переменные:** `POSTGRES_CONTAINER`, `ENV_FILE`, `BACKUP_DIR`, `BACKUP_RETENTION_DAYS`, `BACKUP_MAX_RETRIES`, `BACKUP_RETRY_DELAY_SEC`.

**Ручной запуск:**

```bash
cd /opt/b2b-commerce
./scripts/backup-postgres.sh
# или
make prod-backup
```

---

## disk-cleanup.sh

**Что делает:** смотрит заполнение `/`. Пока **< 80%** — выходит без изменений. При **≥ 80%** удаляет старые бэкапы и dangling Docker images.

**Не трогает:** тома PostgreSQL, Redis, MinIO.

**Переменные:** `DISK_MOUNT`, `DISK_THRESHOLD_PCT`, `BACKUP_DIR`, `BACKUP_RETENTION_DAYS`.

**Ручной запуск:**

```bash
cd /opt/b2b-commerce
./scripts/disk-cleanup.sh
```

---

## prod-compose-env.sh

**Что делает:** bash-функция `load_operational_image_tag` — если `B2B_COMMERCE_IMAGE_TAG` не задан, читает `.deploy-tag`.

**Кто вызывает:** `make/prod.mk` (`prod-ps`, `prod-logs`, `prod-down`, …). **Не** используется в `deploy.sh`.

**Вручную не нужен.**

---

## dev_seed.py

**Что делает:** локальный demo-каталог и опционально QA-данные. Требует `APP_ENV=dev` (`require_dev_env`).

**На production не запускать.**

**Запуск:**

```bash
make dev-seed
make dev-seed-qa
```

---

## dev_reset_data.sh

**Что делает:** wipe локальной БД, migrate, dev-seed. Только `APP_ENV=dev`, allowlisted `DATABASE_URL`, `CONFIRM=1`.

**На production не запускать.**

**Запуск:**

```bash
CONFIRM=1 make dev-reset-data
```

---

## Cron на сервере (Linux)

```bash
crontab -e
```

Подставить путь `/opt/b2b-commerce`. **`B2B_COMMERCE_IMAGE_TAG` в cron не нужен** для backup.

```cron
# Бэкап БД — каждый день в 02:55
55 2 * * * cd /opt/b2b-commerce && ./scripts/backup-postgres.sh >> /var/log/b2b-commerce-backup.log 2>&1

# Очистка диска — каждый час (работает только при ≥ 80%)
0 * * * * cd /opt/b2b-commerce && ./scripts/disk-cleanup.sh >> /var/log/b2b-commerce-disk.log 2>&1
```

---

## deploy.env.example

**Что делает:** шаблон для `scripts/deploy.env` (SSH host и путь на VPS). Не содержит секретов приложения.

```bash
B2B_COMMERCE_SSH=deploy@your-server.example
B2B_COMMERCE_REMOTE_DIR=/opt/b2b-commerce
```
