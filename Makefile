.PHONY: demo test evals serve install install-pip prod-smoke

install:  ## Install the locked set with uv (primary workflow)
	uv sync --extra dev

install-pip:  ## Fallback install: pip with the pinned/frozen set
	# The frozen set first, then the package itself with --no-deps:
	# pip refuses an editable install under a hashed constraints
	# file, so the two never mix.
	pip install -r constraints.txt
	pip install --no-deps -e .

demo:  ## Traced demo: sample application, intake to extraction (offline)
	uv run commercial-credit-demo

test:  ## Run the pytest suite
	uv run pytest -q

evals:  ## Run the eval gate (provenance completeness = 100% required)
	uv run python evals/run_evals.py

prod-smoke:  ## Production smoke on Postgres (R16): needs DATABASE_URL set
	uv run python scripts/prod_smoke.py

serve:  ## Serve the API on http://localhost:8000
	uv run uvicorn commercial_credit.api:app --port 8000
