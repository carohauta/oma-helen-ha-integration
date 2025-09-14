# Helen Energy Integration Tests

This directory contains comprehensive unit tests for the Helen Energy Home Assistant integration.

## Test Structure

- `conftest.py` - Common fixtures and test utilities
- `test_sensor.py` - Tests for the sensor platform and data coordinator (13 tests)
- `test_config_flow.py` - Tests for the configuration flow (4 tests) 
- `test_migration.py` - Tests for entity migration utilities (8 tests)
- `test_init.py` - Tests for integration initialization and schema validation (9 tests)

**Total: 35 tests, all passing**

## Running Tests

### Prerequisites

Install test dependencies:
```bash
pip install -r test-requirements.txt
```

### Quick Commands (using Makefile)

```bash
# Run all tests
make test

# Run tests with coverage report
make test-cov

# Run a specific test file
make test-file FILE=tests/test_sensor.py

# Run tests in debug mode (verbose output)
make test-debug
```

### Running All Tests
```bash
pytest
```

### Running Specific Test Files
```bash
pytest tests/test_sensor.py
pytest tests/test_config_flow.py
```

### Running with Coverage
```bash
pytest --cov=custom_components.helen_energy --cov-report=html
```

### Running Only Unit Tests
```bash
pytest -m "not integration"
```

### Manual pytest Commands

```bash
# Run all tests
pytest tests/ -v

# Run specific test files
pytest tests/test_sensor.py -v
pytest tests/test_config_flow.py -v

# Run with coverage report
pytest tests/ --cov=custom_components.helen_energy --cov-report=html
pytest tests/ --cov=custom_components.helen_energy --cov-report=xml

# Run tests matching a pattern
pytest tests/ -k "sensor" -v
pytest tests/ -k "config_flow" -v
```

## Test Categories

### Unit Tests (Current Focus)
- Test individual functions and classes in isolation
- Use mocks for external dependencies (Helen API, Home Assistant core)
- Fast execution (~0.3-0.4 seconds for all 35 tests)
- High coverage of edge cases and error conditions
- No async tests (removed due to Home Assistant fixture complexity)

### Integration Tests (Future)
- Placeholder exists for future end-to-end testing
- Would test complete workflows with real Home Assistant instances
- Currently not implemented to keep test suite simple and fast

## Mocking Strategy

The tests use extensive mocking to isolate components:

- **HelenApiClient** - Mocked to avoid real API calls to Helen's servers
- **HelenPriceClient** - Mocked for price data retrieval
- **Home Assistant Core** - Mocked using `unittest.mock.Mock` for HA functionality
- **ConfigEntry** - Properly constructed for Home Assistant 2025.1.4 compatibility
- **Entity Registry** - Mocked for entity migration tests

## Test Data

Test fixtures in `conftest.py` provide realistic data:
- **Consumption data**: 150.5 kWh current month, 145.2 kWh last month
- **Price data**: 8.5 c/kWh unit price, 5.0 EUR base price
- **Contract types**: "PERUS" (basic), with support for fixed/variable pricing
- **Configuration entries**: Both basic and transfer cost configurations
- **Coordinator data**: Complete mock data structure for all sensor types

## Continuous Integration

Tests run automatically via GitHub Actions on:
- Push to `main`/`develop`/`feature/*` branches
- Pull requests to `main`/`develop`
- Multiple Python versions (3.11, 3.12)
- Ubuntu latest environment
- Home Assistant 2025.1.4 (pinned for consistency)

## Current Status

- ✅ **35 tests passing** (100% success rate)
- ✅ **0 failing tests**
- ✅ **0 skipped tests** (async tests removed for simplicity)
- ✅ **Fast execution** (~0.3-0.4 seconds locally)
- ✅ **CI/CD working** with consistent Home Assistant versions

## Writing New Tests

When adding new features:
1. **Add unit tests** for new functions/classes in the appropriate test file
2. **Update fixtures** in `conftest.py` if new mock data is needed
3. **Follow existing patterns**: Use the same mocking and assertion style
4. **Test error cases**: Include tests for invalid inputs and edge cases  
5. **Keep tests simple**: Avoid async tests, prefer synchronous unit tests
6. **Update this README** if adding new test files or significant changes

### Test File Guidelines
- `test_sensor.py` - Sensor platform, coordinator, and entity tests
- `test_config_flow.py` - Configuration flow and validation tests
- `test_migration.py` - Entity migration and legacy compatibility tests
- `test_init.py` - Integration setup and schema validation tests
