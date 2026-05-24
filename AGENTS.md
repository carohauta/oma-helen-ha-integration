# AGENTS.md

This file provides detailed guidance for AI coding agents when working with code in this repository.

## Project Overview

Home Assistant custom integration for Helen Energy electricity service (Finland). Fetches electricity consumption, pricing, and costs from the Oma Helen API. Supports Exchange (spot), Market Price, and Fixed Price electricity contracts.

Key features:
- Config flow UI for setup (legacy YAML migration supported)
- Multiple contract types with automatic detection
- Statistics import for HA Energy Dashboard (72-hour backfill)
- Transfer costs tracking (optional)
- Entity ID migration for backward compatibility

## Development Commands

### Testing
```bash
# Run all tests with coverage (preferred - uses uv)
uv run pytest tests/

# Run all tests (fallback)
make test

# Run specific test file
make test-file FILE=test_statistics.py
# or directly:
uv run pytest tests/test_statistics.py -v

# Run with coverage report
make test-cov

# Open coverage HTML report (macOS)
make test-cov-open

# Debug mode (drops into pdb on failure)
make test-debug
```

### Linting & Validation
```bash
# GitHub Actions runs:
# - pytest with coverage
# - hassfest validation (HA integration validator)
# - HACS validation
```

### Clean Build Artifacts
```bash
make clean
```

## Architecture Overview

### Component Structure

**`__init__.py`** - Integration entry point
- Handles config entry setup/unload
- Creates `HelenDataCoordinator` with API clients
- Triggers entity migration for first entry only
- Supports legacy YAML import (deprecated)

**`config_flow.py`** - UI configuration flow
- User authentication via Helen API
- Contract type validation (automatic/fixed/market/exchange)
- Delivery site selection (optional)
- Generates unique IDs: `{username}_{delivery_site_id}` or `{username}_{timestamp}`

**`sensor.py`** - Main sensor platform
- **`HelenDataCoordinator`**: DataUpdateCoordinator that:
  - Updates every 3 hours (`SCAN_INTERVAL`)
  - Fetches consumption/pricing data from Helen API
  - Handles authentication errors (triggers reauth flow)
  - Optionally imports statistics via `HelenStatisticsManager`
- **Sensor entities** (contract-type specific):
  - `HelenExchangeElectricitySensor` - Exchange (spot) pricing
  - `HelenMarketPriceElectricitySensor` - Market price
  - `HelenFixedPriceElectricitySensor` - Fixed price
  - `HelenTransferCostsSensor` - Transfer/delivery costs (optional)
  - `HelenMonthlyConsumptionSensor` - Energy Dashboard integration

**`statistics.py`** - External statistics manager
- **`HelenStatisticsManager`**: Imports hourly statistics to HA database
  - Creates 2 statistic types for Energy Dashboard:
    - `helen_energy:hourly_energy_consumption` (cumulative kWh)
    - `helen_energy:hourly_cost` (cumulative EUR)
  - Fetches 15-minute resolution data from API (`RESOLUTION_QUARTER`)
  - Aggregates 15-min intervals to hourly for precise pricing
  - Backfills last 72 hours (hard-coded in `STATISTICS_BACKFILL_HOURS`)
  - Filters out future data (API returns predictions)
  - **Filters out already-imported data** by timestamp to prevent duplicates
  - Handles timezone conversion (Helsinki → UTC)
  - **Critical**: All timestamps normalized to UTC with microseconds stripped
  - Rounding: 2 decimals for consumption (kWh), 4 decimals for prices (EUR/kWh)

**`migration.py`** - Backward compatibility
- Migrates legacy YAML configs to config entries
- Preserves entity IDs for existing installations
- Supports multiple Helen Energy entries with unique suffixes
- Legacy entity ID mappings in `LEGACY_ENTITY_MAPPINGS`

**`const.py`** - Constants and configuration keys
- Domain: `helen_energy`
- Contract types: automatic/fixed/market/exchange
- Statistics backfill: 72 hours (not user-configurable)

### External Dependencies

**`oma-helen-cli==1.7.0`** (PyPI package `helenservice`)
- `HelenApiClient` - Authentication, consumption data, contract info
- `HelenPriceClient` - Spot/market/fixed pricing data
- API response models: `MeasurementsWithSpotPriceResponse`, `MeasurementsWithSpotPriceSeries`
- Resolution constants: `RESOLUTION_QUARTER` (15-min), `RESOLUTION_HOUR` (1-hour)
- Exceptions: `HelenAuthenticationException`, `InvalidDeliverySiteException`

### Data Flow

1. **Setup**: Config entry → Create API clients → Initialize coordinator
2. **Update cycle** (every 3 hours):
   - Fetch consumption data (current/last month)
   - Fetch pricing data (contract-type specific)
   - Update sensor states and attributes
   - Import statistics (if enabled)
3. **Statistics import**:
   - Fetch 15-minute intervals (72h backfill, `RESOLUTION_QUARTER`)
   - Aggregate to hourly: sum consumption, average spot prices
   - Query last imported timestamp from HA statistics
   - Skip already-imported data (timestamp <= last_timestamp)
   - Build cumulative consumption/cost starting from last known values
   - Write to HA statistics database via `async_add_external_statistics`

### Statistics Manager Implementation Details

**`HelenStatisticsManager` Key Methods**:

1. **`import_recent_statistics()`** - Main entry point
   - Fetches 15-minute data via `_fetch_interval_data()`
   - Queries last cumulative values and timestamps
   - Builds statistics with `_build_statistics_from_intervals()`
   - Imports via `_import_consumption_statistics()` and `_import_cost_statistics()`

2. **`_fetch_interval_data()`** - Data retrieval
   - Calculates date range from `STATISTICS_BACKFILL_HOURS` constant
   - Calls API with `RESOLUTION_QUARTER` for 15-min data
   - Aggregates to hourly via `_aggregate_to_hourly()`
   - Returns list of hourly `MeasurementsWithSpotPriceSeries`

3. **`_aggregate_to_hourly()`** - 15-min to hourly conversion
   - Parses timestamps and converts to UTC
   - Groups quarters by hour using `.replace(minute=0, second=0, microsecond=0)`
   - Sums consumption (4 quarters), rounds to 2 decimals
   - Averages spot prices (4 quarters), rounds to 4 decimals
   - Skips hours with != 4 quarters
   - **Critical**: Uses UTC for hour_key to prevent duplicate entries
   - Includes deduplication safety check

4. **`_build_statistics_from_intervals()`** - Cumulative calculation
   - Takes `last_timestamp` parameter to filter already-imported data
   - Skips intervals where `utc_time <= last_timestamp`
   - Normalizes timestamps: strips microseconds
   - Filters out future data (API predictions)
   - Builds cumulative consumption and cost from last known values
   - Returns `StatisticData` dicts with `start`, `state`, `sum` fields

5. **`_get_last_cumulative_total()`** - Query existing statistics
   - Queries HA's recorder for last statistic entry
   - Handles both Unix timestamp (float) and datetime objects
   - Returns tuple of `(cumulative_value, timestamp)`
   - Used to continue cumulative series and filter duplicates

### Testing Considerations

- Uses `pytest-homeassistant-custom-component==0.13.205`
- Async tests use `asyncio_mode = auto`
- Mocking: Mock `HelenApiClient` and `HelenPriceClient` responses
- Statistics tests: Mock `get_last_statistics` and `async_add_external_statistics`
- Config flow tests: Test unique ID generation, entry data building
- All tests must handle timezone conversions properly (Helsinki/UTC)
- **Test fixtures**:
  - `mock_measurement_series`: 15-minute intervals (12 quarters = 3 hours)
  - `mock_hourly_series`: Hourly intervals (for direct `_build_statistics_from_intervals` tests)
  - Tests calling aggregation use `mock_measurement_series`
  - Tests bypassing aggregation use `mock_hourly_series`

### Important Implementation Details

**Statistics Format**:
- Cumulative statistics (consumption, cost): Include both `state` and `sum` fields
- Both fields set to the SAME cumulative value for consistency
- Metadata `has_sum=True` for cumulative statistics

**15-Minute to Hourly Aggregation**:
- API fetched with `RESOLUTION_QUARTER` (15-minute intervals)
- Aggregation logic groups quarters by hour (UTC-normalized timestamps)
- Consumption: Sum of 4 quarters, rounded to 2 decimals
- Spot price: Average of 4 quarters, rounded to 4 decimals (cents/kWh → EUR/kWh)
- Hours with != 4 quarters are skipped (incomplete data)
- Rounding matches official Oma Helen app precision

**Preventing Duplicate Statistics** (CRITICAL):
- Timestamps MUST be normalized: `.replace(minute=0, second=0, microsecond=0)`
- Query last imported timestamp from existing statistics
- Skip intervals where `utc_time <= last_timestamp` to prevent re-imports
- Without this, cumulative values grow on every HA restart
- Hour keys during aggregation MUST use UTC to ensure consistency
- Example bug: Different timezone formats ("+03:00" vs "Z") create duplicate hour_keys

**Multiple Entries Support**:
- Each entry gets unique ID: `{username}_{delivery_site_id}` or `{username}_{timestamp}`
- Entities get numbered suffixes for 2nd+ entries: `_2`, `_3`, etc.
- Only first entry triggers entity migration

**Contract Type Detection**:
- Automatic mode validates against supported types: PERUS, KAYTTO, MARK, PORS, VALTTI
- Manual modes (fixed/market/exchange) skip validation
- Failure shows error with detected contract type for debugging

**Exception Handling**:
- `except (TypeError, ValueError):` - Python 3 syntax (NOT `except TypeError, ValueError:`)
- Authentication failures trigger HA reauth flow
- API errors logged with `exc_info=True` for debugging

### Common Pitfalls and Bugs to Avoid

**Duplicate Statistics on Restart**:
- **Symptom**: Cumulative values grow by ~89 kWh on every HA restart
- **Root cause**: Re-importing already-imported data without timestamp filtering
- **Fix**: Always pass `last_timestamp` and skip data where `utc_time <= last_timestamp`

**Inconsistent Timestamps**:
- **Symptom**: Multiple statistics entries for the same hour with different cumulative values
- **Root cause**: Timestamps not normalized (different microseconds, timezone formats)
- **Fix**: Always `.replace(minute=0, second=0, microsecond=0)` and convert to UTC

**Rounding Discrepancies**:
- **Symptom**: Energy Dashboard shows 1.43 kWh but official app shows 1.42 kWh
- **Root cause**: Summing 15-min intervals with full float precision
- **Fix**: Round aggregated hourly consumption to 2 decimals, prices to 4 decimals

**Incomplete Hour Aggregation**:
- **Symptom**: Hours with unusual high/low values (e.g., 89 kWh in one hour)
- **Root cause**: Summing != 4 quarters or duplicate quarters
- **Fix**: Skip hours where `len(quarters) != 4`, log warnings for > 4 quarters

**Timezone Confusion in hour_key**:
- **Symptom**: Duplicate hour entries with different timezone suffixes
- **Root cause**: Using local time `.isoformat()` instead of UTC
- **Fix**: Convert to UTC before creating hour_key during aggregation

### Home Assistant Version Compatibility

- Minimum HA Core: 2022.7.0
- Uses `StatisticMeanType` if available (HA 2026.11+), fallback to `has_mean`
- Unit class handling: EUR and EUR/kWh may break in future HA versions (noted in code)
