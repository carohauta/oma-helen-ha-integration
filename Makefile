.PHONY: test test-cov clean install-dev test-debug test-file help

# Default Python interpreter
PYTHON := python3

# Install development dependencies
install-dev:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r test-requirements.txt

# Run all tests
test:
	pytest tests/ -v

# Run tests with coverage report
test-cov:
	pytest tests/ -v --cov=custom_components.helen_energy --cov-report=term-missing --cov-report=html

# Generate coverage report and open in browser (macOS)
test-cov-open: test-cov
	@if command -v open >/dev/null 2>&1; then \
		open htmlcov/index.html; \
	else \
		echo "Coverage report generated in htmlcov/index.html"; \
	fi

# Clean up generated files
clean:
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

# Run specific test file
test-file:
	@if [ -z "$(FILE)" ]; then \
		echo "Usage: make test-file FILE=test_sensor.py"; \
		exit 1; \
	fi
	pytest tests/$(FILE) -v

# Debug mode - run tests with pdb on failure
test-debug:
	pytest tests/ -v --pdb

# Show help
help:
	@echo "Helen Energy Integration - Available Make Targets:"
	@echo ""
	@echo "  install-dev     Install development dependencies"
	@echo "  test            Run all tests"
	@echo "  test-cov        Run tests with coverage report"
	@echo "  test-cov-open   Generate coverage report and open in browser"
	@echo "  test-file       Run specific test file (make test-file FILE=test_sensor.py)"
	@echo "  test-debug      Run tests with debugger on failure"
	@echo "  clean           Remove generated files and cache"
	@echo "  help            Show this help message"
