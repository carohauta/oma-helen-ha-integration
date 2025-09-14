# Helen Energy Integration Tests

This directory contains comprehensive unit and integration tests for the Helen Energy Home Assistant integration.

## Test Structure

- `conftest.py` - Common fixtures and test utilities
- `test_sensor.py` - Tests for the sensor platform and data coordinator
- `test_config_flow.py` - Tests for the configuration flow
- `test_migration.py` - Tests for entity migration utilities
- `test_init.py` - Tests for integration initialization
- `test_integration.py` - End-to-end integration tests

## Running Tests

### Prerequisites

Install test dependencies:
```bash
pip install -r test-requirements.txt
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

### Running Only Integration Tests
```bash
pytest -m integration
```

## Test Categories

### Unit Tests
- Test individual functions and classes in isolation
- Use mocks for external dependencies
- Fast execution
- High coverage of edge cases

### Integration Tests
- Test complete workflows end-to-end
- Test interaction between components
- Simulate real-world scenarios
- Test error handling and recovery

## Mocking Strategy

The tests use extensive mocking to isolate components:

- **HelenApiClient** - Mocked to avoid real API calls
- **HelenPriceClient** - Mocked for price data
- **Home Assistant Core** - Mocked for HA-specific functionality
- **Entity Registry** - Mocked for migration tests

## Test Data

Test fixtures provide realistic data:
- Consumption data (kWh values)
- Price data (EUR values)
- Contract types (PERUS, MARK, PORS, VALTTI)
- Configuration entries
- Entity states

## Continuous Integration

Tests run automatically on:
- Push to main/develop branches
- Pull requests
- Multiple Python versions (3.11, 3.12)

## Coverage Goals

Target coverage: >90%
- All core functionality covered
- Error paths tested
- Edge cases handled
- Migration scenarios verified

## Writing New Tests

When adding new features:
1. Add unit tests for new functions/classes
2. Add integration tests for new workflows
3. Update fixtures if needed
4. Maintain existing test patterns
5. Ensure good coverage of error cases
