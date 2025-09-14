.PHONY: test test-unit test-integration test-cov lint type-check clean install-dev

# Default Python interpreter
PYTHON := python3

# Install development dependencies
install-dev:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r test-requirements.txt

# Run all tests
test:
	pytest tests/ -v

# Run only unit tests (exclude integration tests)
test-unit:
	pytest tests/ -v -m "not integration"

# Run only integration tests
test-integration:
	pytest tests/ -v -m integration

# Run tests with coverage report
test-cov:
	pytest tests/ -v --cov=custom_components.helen_energy --cov-report=term-missing --cov-report=html

# Run linting
lint:
	flake8 custom_components tests --max-line-length=127 --exclude=__pycache__
	black --check custom_components tests --line-length=127

# Run type checking
type-check:
	mypy custom_components/helen_energy --ignore-missing-imports --no-strict-optional

# Format code
format:
	black custom_components tests --line-length=127
	isort custom_components tests

# Run all quality checks
check: lint type-check test

# Clean up generated files
clean:
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Run tests in watch mode (requires pytest-xdist)
test-watch:
	pytest tests/ -v -f

# Generate coverage report and open in browser
test-cov-open: test-cov
	open htmlcov/index.html

# Run specific test file
test-file:
	@echo "Usage: make test-file FILE=test_sensor.py"
	pytest tests/$(FILE) -v

# Debug mode - run tests with pdb on failure
test-debug:
	pytest tests/ -v --pdb

# Run tests in parallel (requires pytest-xdist)
test-parallel:
	pytest tests/ -v -n auto
