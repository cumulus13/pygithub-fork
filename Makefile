.PHONY: install test lint build publish clean

install:
	pip install -e ".[dev]"

test:
	pytest tests/ -v --tb=short --cov=pygithub_fork --cov-report=term-missing

lint:
	ruff check src/ tests/
	mypy src/pygithub_fork/

build: clean
	python -m build

publish: build
	twine check dist/*
	twine upload dist/*

# Upload to TestPyPI first to sanity-check before going live:
publish-test: build
	twine check dist/*
	twine upload --repository testpypi dist/*

clean:
	rm -rf dist/ build/ *.egg-info src/*.egg-info .pytest_cache .mypy_cache
