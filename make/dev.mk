PYTHON ?= python3
UV ?= uv
ENV_FILE ?= .env
export PYTHONPATH := src

# Подхватывает .env в shell перед локальным запуском.
RUN_WITH_ENV = set -a; [ -f $(ENV_FILE) ] && . ./$(ENV_FILE); set +a;

.PHONY: install up up-deps reload down logs migrate dev dev-seed dev-seed-qa dev-reset-data create-admin worker

install:
	$(UV) sync --extra dev

# Все сервисы в Docker (api + worker + инфраструктура).
up:
	docker compose up -d --build

# Только postgres/redis/minio — для локального make dev / make worker.
up-deps:
	docker compose up -d postgres redis minio

# Пересоздать контейнеры после правок .env или docker-compose.yml.
reload:
	docker compose up -d --build --force-recreate

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

migrate:
	@$(RUN_WITH_ENV) $(UV) run alembic upgrade head

# Локальный demo/test seed: каталог и связанные данные. Админ не создаётся — сначала make create-admin.
dev-seed:
	@$(RUN_WITH_ENV) $(UV) run python scripts/dev_seed.py
	@echo "Примечание: для фото нужен MinIO (make up-deps). Без MinIO — каталог без изображений."

# QA-данные Phase 2: 40 компаний, история счетов. Не импортирует каталог.
dev-seed-qa:
	@$(RUN_WITH_ENV) $(UV) run python scripts/dev_seed.py qa

# Wipe локальной БД + migrate + dev-seed. Требует APP_ENV=dev, allowlisted DATABASE_URL, CONFIRM=1.
dev-reset-data:
	@CONFIRM=1 $(RUN_WITH_ENV) bash scripts/dev_reset_data.sh

# Первый администратор из ADMIN_LOGIN / ADMIN_PASSWORD (.env). Не часть seed.
create-admin:
	@$(RUN_WITH_ENV) $(UV) run python -m b2b_commerce.bootstrap

dev:
	@$(RUN_WITH_ENV) $(UV) run uvicorn b2b_commerce.main:app --reload --app-dir src

worker:
	@$(RUN_WITH_ENV) $(UV) run arq b2b_commerce.worker.WorkerSettings
