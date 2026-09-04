UV ?= uv
export PYTHONPATH := src

.PHONY: test-dbs test

test-dbs:
	@docker compose exec -T postgres psql -U b2b_commerce -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='b2b_commerce_test'" | grep -q 1 || docker compose exec -T postgres psql -U b2b_commerce -d postgres -c "CREATE DATABASE b2b_commerce_test;"
	@docker compose exec -T postgres psql -U b2b_commerce -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='b2b_commerce_migtest'" | grep -q 1 || docker compose exec -T postgres psql -U b2b_commerce -d postgres -c "CREATE DATABASE b2b_commerce_migtest;"

test: test-dbs
	TEST_DATABASE_URL=$${TEST_DATABASE_URL:-postgresql+asyncpg://b2b_commerce:b2b_commerce@127.0.0.1:5432/b2b_commerce_test} $(UV) run pytest
