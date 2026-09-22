.PHONY: sync lint fmt type test check run migrate revision prompts keygen preflight eval eval-judge eval-proactive clean

UV ?= uv

sync:            ## Install/refresh the virtualenv from uv.lock
	$(UV) sync

lint:            ## Ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:             ## Auto-fix lint and format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

type:            ## mypy --strict over src
	$(UV) run mypy --strict src

test:            ## pytest
	$(UV) run pytest -q

check: lint type test prompts-check  ## Everything CI runs

run:             ## Run the bot locally (reads .env)
	$(UV) run python -m strikt

migrate:         ## Apply migrations to DATABASE_URL
	$(UV) run alembic upgrade head

revision:        ## Autogenerate a migration: make revision m="add x"
	$(UV) run alembic revision --autogenerate -m "$(m)"

prompts:         ## Rebuild PROMPTS.md from src/strikt/agent/prompts/*.md
	$(UV) run python scripts/build_prompts_md.py

prompts-check:
	$(UV) run python scripts/build_prompts_md.py --check

keygen:          ## Print a fresh TOKEN_ENCRYPTION_KEY
	$(UV) run python -c "from strikt.db.crypto import generate_key; print(generate_key())"

preflight:       ## One real Anthropic call with the coach's tools (ANTHROPIC_API_KEY=...)
	$(UV) run python scripts/preflight.py

eval:            ## Run the turn eval: 21 real turns, graded on the database (ANTHROPIC_API_KEY=...)
	$(UV) run python -m evals.run

eval-judge:      ## The same, plus one rubric question per case answered by Haiku
	$(UV) run python -m evals.run --judge

eval-proactive:  ## Grade the check-ins the bot sends first (evals/proactive_cases.json)
	$(UV) run python -m evals.run --flow proactive

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-14s %s\n", $$1, $$2}'
