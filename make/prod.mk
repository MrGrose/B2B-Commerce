COMPOSE_PROD = docker compose -f docker-compose.prod.yml
PROD_ENV_FILE ?= .env

# Operational: B2B_COMMERCE_IMAGE_TAG из env или .deploy-tag. Deploy — только явный TAG.
define prod_with_tag
	set -a; [ -f $(PROD_ENV_FILE) ] && . ./$(PROD_ENV_FILE); set +a; \
	. ./scripts/prod-compose-env.sh; load_operational_image_tag "$(CURDIR)"
endef

.PHONY: prod-up prod-down prod-ps prod-logs prod-logs-worker prod-backup prod-migrate \
	prod-deploy prod-restart prod-health prod-edge-health prod-rollback prod-create-admin deploy-remote

# Первый запуск — явный B2B_COMMERCE_IMAGE_TAG (immutable SHA).
prod-up:
	@test -n "$${B2B_COMMERCE_IMAGE_TAG:-}" || (echo "B2B_COMMERCE_IMAGE_TAG is required (git SHA)" >&2; exit 1)
	B2B_COMMERCE_IMAGE_TAG=$${B2B_COMMERCE_IMAGE_TAG} $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) up -d

prod-down:
	@$(prod_with_tag); $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) down

prod-ps:
	@$(prod_with_tag); $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) ps

prod-logs:
	@$(prod_with_tag); $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) logs -f --tail=200

prod-logs-worker:
	@$(prod_with_tag); $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) logs -f --tail=200 worker

# Без B2B_COMMERCE_IMAGE_TAG — docker exec b2b-commerce-postgres.
prod-backup:
	@chmod +x scripts/backup-postgres.sh 2>/dev/null || true
	./scripts/backup-postgres.sh

prod-migrate:
	@$(prod_with_tag); $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) run --rm --no-deps api alembic upgrade head

# На сервере: B2B_COMMERCE_IMAGE_TAG=<sha> ./scripts/deploy.sh
prod-deploy:
	@test -n "$${B2B_COMMERCE_IMAGE_TAG:-}" || (echo "B2B_COMMERCE_IMAGE_TAG is required (git SHA)" >&2; exit 1)
	@chmod +x scripts/deploy.sh 2>/dev/null || true
	B2B_COMMERCE_IMAGE_TAG=$${B2B_COMMERCE_IMAGE_TAG} ./scripts/deploy.sh

prod-restart:
	@$(prod_with_tag); $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) up -d --force-recreate api worker

prod-health:
	@curl -fsS http://127.0.0.1:8000/api/health
	@curl -fsS http://127.0.0.1:8000/login | grep -q 'Вход'
	@$(prod_with_tag); $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) ps


# HTTPS edge через host Caddy (отдельно от deploy.sh).
prod-edge-health:
	@set -a; [ -f $(PROD_ENV_FILE) ] && . ./$(PROD_ENV_FILE); set +a; \
	test -n "$${DOMAIN:-}" || (echo "DOMAIN is required in $(PROD_ENV_FILE)" >&2; exit 1); \
	curl -fsS "https://$${DOMAIN}/api/health"; \
	curl -fsS "https://$${DOMAIN}/login" | grep -q 'Вход'

prod-rollback:
	@chmod +x scripts/rollback.sh 2>/dev/null || true
	./scripts/rollback.sh

prod-create-admin:
	@$(prod_with_tag); $(COMPOSE_PROD) --env-file $(PROD_ENV_FILE) run --rm --no-deps api python -m b2b_commerce.bootstrap

deploy-remote:
	@test -n "$${B2B_COMMERCE_IMAGE_TAG:-}" || (echo "B2B_COMMERCE_IMAGE_TAG is required (git SHA)" >&2; exit 1)
	@chmod +x scripts/deploy-remote.sh 2>/dev/null || true
	B2B_COMMERCE_IMAGE_TAG=$${B2B_COMMERCE_IMAGE_TAG} ./scripts/deploy-remote.sh
