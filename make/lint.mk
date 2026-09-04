UV ?= uv

.PHONY: lint

lint:
	$(UV) run ruff check src tests scripts
