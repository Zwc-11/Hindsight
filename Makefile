.PHONY: install lint typecheck test coverage run benchmark smoke demo gates

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest tests/hindsight -q

coverage:
	$(PYTHON) -m coverage run -m pytest tests/hindsight -q
	$(PYTHON) -m coverage report

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy

run:
	$(PYTHON) -m hindsight.cli run

benchmark:
	$(PYTHON) -m hindsight.cli benchmark

smoke: run benchmark demo

demo:
	$(PYTHON) -m hindsight.cli demo

gates: lint typecheck coverage demo
