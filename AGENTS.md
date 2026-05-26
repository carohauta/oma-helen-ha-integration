# AGENTS.md

This file provides detailed guidance for AI coding agents when working with code in this repository.

## Project Overview

Home Assistant custom integration for Helen Energy electricity service (Finland). Fetches electricity consumption, pricing, and costs from the Oma Helen API. Supports Exchange (spot), Market Price, Fixed Price, and Smart Guarantee (VALTTI) electricity contracts.

Key features:
- Config flow UI for setup (legacy YAML migration supported)
- Multiple contract types with automatic detection
- Statistics import for HA Energy Dashboard (72-hour backfill with gap detection)
- Transfer costs tracking (optional)
- Entity ID migration for backward compatibility

## Development Commands

### Setup
```bash
uv sync
```

### Testing
```bash
# Run all tests (coverage flags live in pytest.ini)
uv run pytest tests/

# Run a specific test file
uv run pytest tests/test_statistics.py -v

# Debug mode (drops into pdb on failure)
uv run pytest tests/ -v --pdb
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
rm -rf .pytest_cache htmlcov .coverage coverage.xml
find . -type d -name __pycache__ -exec rm -rf {} +
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
  - Dynamically updates `_fixed_unit_price` from API if not user-configured (priority: user config > API contract price)
- **Sensor entities** (contract-type specific):
  - `HelenExchangeElectricity` - Exchange (spot) pricing
  - `HelenMarketPriceElectricity` - Market price
  - `HelenFixedPriceElectricity` - Fixed price
  - `HelenSmartGuarantee` - Smart guarantee / VALTTI contract
  - `HelenTransferPrice` - Transfer/delivery costs (optional)
  - `HelenMonthlyConsumption` - Energy Dashboard integration

**`statistics.py`** - External statistics manager
- **`HelenStatisticsManager`**: Imports hourly statistics to HA database via gap detection
  - Creates up to 3 statistic streams for Energy Dashboard:
    - `helen_energy:hourly_energy_consumption` (cumulative kWh)
    - `helen_energy:hourly_cost_spot` (cumulative EUR, spot/exchange price)
    - `helen_energy:hourly_cost_fixed` (cumulative EUR, fixed unit price — only when `fixed_unit_price` is set)
  - Fetches 15-minute resolution data from API (`RESOLUTION_QUARTER`)
  - Aggregates 15-min intervals to hourly for precise pricing
  - Backfills last 72 hours (hard-coded in `STATISTICS_BACKFILL_HOURS`)
  - **Gap detection**: queries existing statistics in the window and only imports missing timestamps
  - **Pending data distinction**: missing API data (`electricity=None`) treated as pending, not as a fillable gap
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
   - Update `_fixed_unit_price` from API if not user-configured
   - Import statistics (if enabled)
3. **Statistics import** (gap-detection approach):
   - Fetch 15-minute intervals (72h backfill, `RESOLUTION_QUARTER`)
   - Aggregate to hourly: sum consumption, average spot prices
   - Query all existing records in the backfill window from HA statistics
   - Detect gaps: hourly timestamps present in API data but absent in HA
   - Skip entries with `electricity=None` (pending, not gaps)
   - For each gap: query cumulative sum just before it, build forward
   - Chain consecutive gaps without extra DB queries
   - Write gap-filling data via `async_add_external_statistics`

### Statistics Manager Implementation Details

**`HelenStatisticsManager` Constructor**:
- `fixed_unit_price: float | None` — fixed unit price in cents/kWh; enables `hourly_cost_fixed` stream when set

**`HelenStatisticsManager` Key Methods**:

1. **`import_recent_statistics()`** - Main entry point (gap-detection based)
   - Fetches 15-minute data via `_fetch_interval_data()`
   - Queries all existing records in the backfill window via `_get_existing_statistics_in_window()`
   - Finds missing timestamps via `_detect_gaps()`
   - Builds statistics for gaps only via `_build_statistics_for_gaps()`
   - Imports via `_import_consumption_statistics()`, `_import_cost_statistics()`, and optionally `_import_fixed_cost_statistics()`

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

4. **`_get_existing_statistics_in_window()`** - Query existing records in a time window
   - Uses `statistics_during_period()` to fetch all records between two timestamps
   - Returns `dict[datetime, float]` mapping normalized UTC timestamps to cumulative sums
   - Used by gap detection to know which hours are already populated

5. **`_detect_gaps()`** - Find missing timestamps
   - Compares API hourly series against existing statistics timestamps
   - Returns only entries where the timestamp is absent AND `electricity` is not None
   - Entries with `electricity=None` are treated as pending (not yet available), not gaps

6. **`_build_statistics_for_gaps()`** - Cumulative calculation for gap filling
   - For each gap entry, queries the cumulative value just before it via `_get_cumulative_at_or_before_timestamp()`
   - Consecutive gaps (hourly sequence) chain from the previous entry without a DB query
   - Non-consecutive gaps each trigger a fresh DB query
   - Returns tuple of `(consumption_stats, cost_stats, fixed_cost_stats)`
   - `fixed_cost_stats` is empty list when `fixed_unit_price` is None

7. **`_get_cumulative_at_or_before_timestamp()`** - Point-in-time cumulative lookup
   - Queries `statistics_during_period` from epoch to the given timestamp
   - Returns the last record's sum and timestamp as `(float, datetime | None)`
   - Used when a gap is non-consecutive (can't chain from previous gap's cumulative)

8. **`_get_last_cumulative_total()`** - Legacy helper (still used by tests)
   - Queries HA's recorder for the most recent statistic entry
   - Handles both Unix timestamp (float) and datetime objects
   - Returns tuple of `(cumulative_value, timestamp)`

9. **`_build_statistics_from_intervals()`** - Legacy method (kept but not called by `import_recent_statistics`)
   - Takes `last_timestamp` to skip already-imported data
   - Still used in some tests that bypass the new gap-detection flow

### Testing Considerations

- Uses `pytest-homeassistant-custom-component==0.13.205`
- Async tests use `asyncio_mode = auto`
- Mocking: Mock `HelenApiClient` and `HelenPriceClient` responses
- Statistics tests: Mock `get_instance`, `statistics_during_period`, `get_last_statistics`, and `async_add_external_statistics`
- Config flow tests: Test unique ID generation, entry data building
- All tests must handle timezone conversions properly (Helsinki/UTC)
- **Test fixtures** (`tests/test_statistics.py`):
  - `mock_measurement_series`: 15-minute intervals (12 quarters = 3 hours, 0.5 kWh each, 500 cents/kWh)
  - `mock_hourly_series`: Hourly intervals (3 hours, 2.0 kWh each, 500 cents/kWh) for direct `_build_statistics_from_intervals` tests
  - `mock_measurement_response`: Wraps `mock_measurement_series` in a mock API response object
  - Tests calling aggregation use `mock_measurement_series`
  - Tests bypassing aggregation use `mock_hourly_series`
- **Gap detection tests**: Mock `_get_cumulative_at_or_before_timestamp` to control cumulative starting values; verify call count to confirm consecutive vs non-consecutive chaining

### Important Implementation Details

**Statistics Streams**:
- `hourly_energy_consumption` — always present; cumulative kWh
- `hourly_cost_spot` — always present; cumulative EUR based on spot price
- `hourly_cost_fixed` — only created when `fixed_unit_price` is set on `HelenStatisticsManager`; cumulative EUR at fixed rate

**Statistics Format**:
- Cumulative statistics: Include both `state` and `sum` fields set to the SAME cumulative value
- Metadata `has_sum=True` for all three streams
- Cost unit: `"EUR"` with `unit_class=None` (may need revisiting in HA 2026.11+)

**15-Minute to Hourly Aggregation**:
- API fetched with `RESOLUTION_QUARTER` (15-minute intervals)
- Aggregation logic groups quarters by hour (UTC-normalized timestamps)
- Consumption: Sum of 4 quarters, rounded to 2 decimals
- Spot price: Average of 4 quarters, rounded to 4 decimals (cents/kWh → EUR/kWh)
- Hours with != 4 quarters are skipped (incomplete data)
- Rounding matches official Oma Helen app precision

**Preventing Duplicate Statistics** (CRITICAL — now via gap detection):
- Timestamps MUST be normalized: `.replace(minute=0, second=0, microsecond=0)`
- Query existing statistics across the full 72-hour window using `statistics_during_period`
- Only write records for timestamps missing from existing statistics
- Gap detection replaces the old "skip timestamp <= last_known" approach
- Hour keys during aggregation MUST use UTC to ensure consistency
- Example bug: Different timezone formats ("+03:00" vs "Z") create duplicate hour_keys

**Pending vs Gap distinction**:
- API can return entries with `electricity=None` for recent hours (data not yet available)
- `_detect_gaps()` treats `electricity=None` as pending, not as a fillable gap
- Only timestamps with actual electricity data are counted as missing/gaps

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
- **Root cause**: Re-importing already-imported data without gap detection
- **Fix**: Use `_get_existing_statistics_in_window()` + `_detect_gaps()` to only import missing timestamps

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
