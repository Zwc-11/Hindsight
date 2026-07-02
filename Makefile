.PHONY: install lint typecheck test coverage run benchmark smoke demo gates

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest tests/hindsight -q

coverage:
	python -m coverage run -m pytest tests/hindsight -q
	python -m coverage report

lint:
	python -m ruff check .

typecheck:
	python -m mypy

run:
	python -m hindsight.cli run

benchmark:
	python -m hindsight.cli benchmark

smoke: run benchmark demo

demo:
	python -m hindsight.cli demo

gates: lint typecheck coverage demo
