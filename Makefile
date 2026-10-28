.PHONY: help test test-agent demo demo-agent lint clean install screenshots

PY ?= python3
export PYTHONPATH := $(PWD)/src

help:
	@echo "make test        - run the full test suite (no network)"
	@echo "make test-agent  - run agent tests too (requires openminion)"
	@echo "make demo        - offline gate: mock baseline vs degraded candidate"
	@echo "make demo-agent  - offline gate + OpenMinion agent tool loop"
	@echo "make lint        - compile all modules and check imports"
	@echo "make install     - editable install into the active virtualenv"
	@echo "make screenshots - regenerate README screenshots (needs node + playwright)"
	@echo "make clean       - remove caches and generated artifacts"

test:
	$(PY) -m unittest discover -s tests -v

test-agent:
	CANARY_MOCK_KEY=mock $(PY) -m unittest discover -s tests -v

demo:
	rm -rf artifacts/demo-run
	./canary demo --workspace artifacts/demo-run

demo-agent:
	CANARY_MOCK_KEY=mock ./canary demo --workspace artifacts/demo-run --agent

lint:
	$(PY) -m compileall -q src tests
	$(PY) -c "import sys; sys.path.insert(0, 'src'); import cortensor_canary, cortensor_canary.cli, cortensor_canary.agent, cortensor_canary.report; print('imports ok')"

install:
	$(PY) -m pip install -e .

screenshots:
	node scripts/screenshots.mjs

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf build dist *.egg-info artifacts/demo-run .pytest_cache
