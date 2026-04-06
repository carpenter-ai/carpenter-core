.PHONY: test test-fast test-sequential install dev clean

# Recommended: Fast parallel test execution (1.5 minutes)
test: test-fast

# Fast parallel tests (recommended)
test-fast:
	@echo "Running tests in parallel (fast mode)..."
	pytest tests/ -n auto -q

# Single-threaded tests (slower, ~5 minutes)
test-sequential:
	@echo "Running tests sequentially (slow mode)..."
	pytest tests/ -q

# Verbose parallel tests
test-verbose:
	@echo "Running tests in parallel with verbose output..."
	pytest tests/ -n auto -v

# Install package in development mode
install:
	pip install -e .

# Install with dev dependencies
dev:
	pip install -e ".[dev]"

# Clean up temporary files and caches
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache build dist

help:
	@echo "Carpenter Makefile Commands:"
	@echo ""
	@echo "  make test            - Run tests in parallel (fast, recommended)"
	@echo "  make test-fast       - Same as 'make test'"
	@echo "  make test-sequential - Run tests single-threaded (slow)"
	@echo "  make test-verbose    - Run tests in parallel with verbose output"
	@echo "  make install         - Install package in development mode"
	@echo "  make dev             - Install with dev dependencies"
	@echo "  make clean           - Remove temporary files and caches"
	@echo "  make help            - Show this help message"
