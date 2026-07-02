.PHONY: install test run compare benchmark smoke demo

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest tests/hindsight -q

run:
	python -m hindsight.cli run

compare:
	python -m hindsight.cli compare

benchmark:
	python -m hindsight.cli benchmark

smoke: run compare benchmark

demo:
	python -m hindsight.cli demo
