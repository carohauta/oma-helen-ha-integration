# AGENTS.md

This file provides detailed guidance for AI coding agents when working with code in this repository.

## Project Overview

Home Assistant custom integration for Helen Energy electricity service (Finland). Fetches electricity consumption, pricing, and costs from the Oma Helen API. Supports Exchange (spot), Market Price, Fixed Price, and Smart Guarantee (VALTTI) electricity contracts.

Key features:
- Config flow UI for setup (legacy YAML migration supported)
- Multiple contract types with automatic detection
- Multiple config entries (e.g. several delivery sites/contracts), each with isolated statistics
- Statistics import for HA Energy Dashboard (72-hour rolling backfill with gap detection)
- Ad-hoc `backfill_statistics` service for importing a custom historical date range
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
- Creates `HelenDataCoordinator` with API clients, stored as `hass.data[DOMAIN][entry_id]["coordinator"]`
- Triggers entity migration for first entry only
- Registers/unregisters the `backfill_statistics` service (on first entry / last entry)
- Schema migration handled by HA core via the module-level `async_migrate_entry` (see migration.py)
- Supports legacy YAML import (deprecated)

**`config_flow.py`** - UI configuration flow
- `VERSION = 2` (entries created at v2; older v1 entries auto-migrated by core)
- User authentication via Helen API
- Contract type validation (automatic/fixed/market/exchange); automatic mode that can't resolve shows `automatic_detection_failed` with the detected type
- Required `custom_name` for the entry title (helps distinguish multiple contracts)
- Delivery site selection (optional)
- Generates unique IDs: `{username}_{delivery_site_id}` or `{username}_{timestamp}`

**`services.py`** - Integration services
- Registers `helen_energy.backfill_statistics` (schema/UI in `services.yaml`)
- Backfills a custom date range (`start_date` → today, max `MAX_BACKFILL_DAYS=365`); optional `config_entry_id` targets one contract, otherwise all
- **Clears the affected statistic IDs first, then rebuilds** — intentional, so the cumulative `sum` chain stays consistent (inserting rows mid-chain would corrupt later sums)
- Delegates per-coordinator to `HelenStatisticsManager.backfill_statistics()`

**`coordinator.py`** - Data update coordinator
- **`HelenDataCoordinator`** (`DataUpdateCoordinator`):
  - Updates every 3 hours (`SCAN_INTERVAL`)
  - Fetches consumption/pricing data from Helen API
  - Handles authentication errors (triggers reauth flow)
  - Always imports statistics via `HelenStatisticsManager` after a successful fetch
  - Dynamically updates `_fixed_unit_price` from API if not user-configured (priority: user config > API contract price)
- Module-level async fetch helpers: `_login_helen_api_if_needed`, `_select_delivery_site`, `_get_total_consumption_*`, `_get_average_daily_consumption_for_current_month`, `_get_transfer_price_total_for_current_month`

**`sensor.py`** - Sensor platform
- `async_setup_entry` reads the coordinator from `hass.data` and builds the entity matching the contract type
- **Sensor entities** (contract-type specific):
  - `HelenExchangeElectricity` - Exchange (spot) pricing
  - `HelenMarketPriceElectricity` - Market price
  - `HelenFixedPriceElectricity` - Fixed price
  - `HelenSmartGuarantee` - Smart guarantee / VALTTI contract
  - `HelenTransferPrice` - Transfer/delivery costs (optional)
  - `HelenMonthlyConsumption` - Energy Dashboard integration

**`statistics.py`** - External statistics manager
- **`HelenStatisticsManager`**: Imports hourly statistics to HA database via gap detection
  - Creates up to 3 statistic streams for Energy Dashboard, **suffixed per config entry** to avoid collisions across multiple contracts (suffix = `config_entry_id` hyphen-stripped, lowercased, first 8 chars):
    - `helen_energy:hourly_energy_consumption_{suffix}` (cumulative kWh)
    - `helen_energy:hourly_cost_spot_{suffix}` (cumulative EUR, spot/exchange price)
    - `helen_energy:hourly_cost_fixed_{suffix}` (cumulative EUR, fixed unit price — only when `fixed_unit_price` is set)
  - Two entry points share the same gap-fill logic (`_fill_gaps`):
    - `import_recent_statistics()` — rolling 72h window, 15-min resolution aggregated to hourly (used by the coordinator)
    - `backfill_statistics(start_date, end_date)` — custom range, hourly resolution (used by the service)
  - Backfills last 72 hours (hard-coded in `STATISTICS_BACKFILL_HOURS`) on the rolling path
  - **Gap detection**: queries existing statistics in the window and only imports missing timestamps
  - **Pending data distinction**: missing API data (`electricity=None`) treated as pending, not as a fillable gap
  - Handles timezone conversion (Helsinki → UTC)
  - **Critical**: All timestamps normalized to UTC with microseconds stripped
  - Rounding: 2 decimals for consumption (kWh), 4 decimals for prices (EUR/kWh)

**`migration.py`** - Backward compatibility
- `async_migrate_entry`: schema-version transform only (v1 → v2; backfills `include_transfer_costs`, normalizes title). Called by HA core when a stored entry is older than `ConfigFlow.VERSION`
- `async_migrate_entities_for_compatibility`: re-points legacy YAML entity IDs onto the new config entry's unique IDs so history is preserved. Called from `async_setup_entry` (first entry only) — **not** from `async_migrate_entry`
- Legacy entity ID mappings in `LEGACY_ENTITY_MAPPINGS`

**`utils.py`** - Shared helpers
- `safe_round(value, decimals=2)` — used by both `sensor.py` and `statistics.py`

**`const.py`** - Constants and configuration keys
- Domain: `helen_energy`
- Contract types: automatic/fixed/market/exchange
- `SERVICE_BACKFILL_STATISTICS`, `CONF_CUSTOM_NAME`
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
   - Import statistics (always, after a successful fetch)
3. **Statistics import** (gap-detection approach, in `_fill_gaps`):
   - Fetch hourly series (rolling: 15-min `RESOLUTION_QUARTER` aggregated to hourly; backfill: `RESOLUTION_HOUR`)
   - Query existing records over the data window from HA statistics
   - Detect gaps: hourly timestamps present in API data but absent in HA
   - Skip entries with `electricity=None` (pending, not gaps)
   - For each gap: query cumulative sum just before it, build forward
   - Chain consecutive gaps without extra DB queries
   - Write the three streams via `_import_statistics` → `async_add_external_statistics`

4. **Ad-hoc backfill** (service): clear the contract's statistic IDs → `backfill_statistics(start_date, today)` → same `_fill_gaps` path rebuilds the chain from scratch.

### Statistics Manager Implementation Details

**`HelenStatisticsManager` Constructor** — `(hass, api_client, entity_id, config_entry_id, config_entry_title, fixed_unit_price=None)`:
- `config_entry_id` — derives the per-entry statistic_id suffix (prevents collisions across contracts)
- `config_entry_title` — used in statistic display names
- `fixed_unit_price: float | None` — fixed unit price in cents/kWh; enables the `hourly_cost_fixed` stream when set

**`HelenStatisticsManager` Key Methods**:

1. **`import_recent_statistics()`** - Coordinator entry point
   - Fetches 15-minute data via `_fetch_interval_data()` (aggregated to hourly), then delegates to `_fill_gaps()`

2. **`backfill_statistics(start_date, end_date)`** - Service entry point
   - Fetches the range at `RESOLUTION_HOUR`, then delegates to `_fill_gaps()`

3. **`_fill_gaps(series)`** - Shared gap-fill core (used by both entry points)
   - Queries existing records over the data window via `_get_existing_statistics_in_window()`
   - Finds missing timestamps via `_detect_gaps()`
   - Builds statistics for gaps only via `_build_statistics_for_gaps()`
   - Imports the three streams via `_import_statistics()` (consumption + spot; fixed only when `fixed_unit_price` set)

4. **`_fetch_interval_data()`** - Rolling-window data retrieval
   - Calculates date range from `STATISTICS_BACKFILL_HOURS` constant
   - Calls API with `RESOLUTION_QUARTER` for 15-min data, aggregates via `_aggregate_to_hourly()`

5. **`_aggregate_to_hourly()`** - 15-min to hourly conversion
   - Parses timestamps and converts to UTC
   - Groups quarters by hour using `.replace(minute=0, second=0, microsecond=0)`
   - Sums consumption (4 quarters, 2 decimals); averages spot prices (4 quarters, 4 decimals)
   - Skips hours with != 4 quarters
   - **Critical**: Uses UTC for hour_key to prevent duplicate entries

6. **`_get_existing_statistics_in_window()`** - Query existing records in a time window
   - Uses `statistics_during_period()`; returns `dict[datetime, float]` of normalized UTC timestamps → cumulative sums

7. **`_detect_gaps()`** - Find missing timestamps
   - Returns only entries where the timestamp is absent AND `electricity` is not None (`None` = pending, not a gap)

8. **`_build_statistics_for_gaps()`** - Cumulative calculation for gap filling
   - For each gap, queries the cumulative value just before it via `_get_cumulative_at_or_before_timestamp()`
   - Consecutive gaps chain from the previous entry without a DB query; non-consecutive gaps each query
   - Returns `(consumption_stats, cost_stats, fixed_cost_stats)`; `fixed_cost_stats` empty when `fixed_unit_price` is None

9. **`_get_cumulative_at_or_before_timestamp()`** - Point-in-time cumulative lookup
   - Queries `statistics_during_period` from epoch to the timestamp; returns `(sum, timestamp)`

10. **`_import_statistics(statistic_id, name, unit, unit_class, statistics)`** - Single import helper
    - Builds `StatisticMetaData` (with version-aware `mean_type`/`has_mean`) and calls `async_add_external_statistics`
    - One method for all three streams (consumption/spot/fixed)

### Testing Considerations

- Uses `pytest-homeassistant-custom-component==0.13.205`
- Async tests use `asyncio_mode = auto`
- Mocking: Mock `HelenApiClient` and `HelenPriceClient` responses
- Statistics tests: Mock `get_instance`, `statistics_during_period`, and `async_add_external_statistics`
- Config flow tests: Test unique ID generation, entry data building
- Migration: `tests/test_init.py::TestEntryMigration::test_v1_entry_migrates_to_v2` drives full setup of a v1 `MockConfigEntry` and asserts it ends at version 2
- All tests must handle timezone conversions properly (Helsinki/UTC)
- **Test fixtures** (`tests/test_statistics.py`):
  - `mock_measurement_series`: 15-minute intervals (12 quarters = 3 hours, 0.5 kWh each, 500 cents/kWh)
  - `mock_measurement_response`: Wraps `mock_measurement_series` in a mock API response object
- **Gap detection tests**: Mock `_get_cumulative_at_or_before_timestamp` to control cumulative starting values; verify call count to confirm consecutive vs non-consecutive chaining
- **End-to-end**: `test_fill_gaps_imports_all_three_streams` mocks `_get_existing_statistics_in_window` + `_get_cumulative_at_or_before_timestamp` and asserts all three streams import with correct per-stream metadata and cumulative sums
- `HelenStatisticsManager` constructor in tests takes the `config_entry_id` + `config_entry_title` args; statistic IDs are suffixed accordingly (e.g. `helen_energy:hourly_energy_consumption_test_ent`)

### Important Implementation Details

**Statistics Streams** (each statistic_id suffixed with the config entry's id for multi-entry isolation):
- `hourly_energy_consumption_{suffix}` — always present; cumulative kWh
- `hourly_cost_spot_{suffix}` — always present; cumulative EUR based on spot price
- `hourly_cost_fixed_{suffix}` — only created when `fixed_unit_price` is set on `HelenStatisticsManager`; cumulative EUR at fixed rate

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
- Each entry gets a unique config-entry ID: `{username}_{delivery_site_id}` or `{username}_{timestamp}`
- Optional `custom_name` sets the entry title to distinguish contracts
- Entities get numbered suffixes for 2nd+ entries: `_2`, `_3`, etc.
- Statistic IDs are suffixed per entry (see Statistics Streams) so multiple contracts don't collide on the Energy Dashboard
- Only first entry triggers entity migration

**Contract Type Detection**:
- Automatic mode validates against supported types: PERUS, KAYTTO, MARK, PORS, VALTTI
- Manual modes (fixed/market/exchange) skip validation
- Automatic mode that can't resolve a supported type shows `automatic_detection_failed` with the detected type (`{detected_type}`), prompting manual selection

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

- Tested against HA Core 2025.1 (pinned by `pytest-homeassistant-custom-component==0.13.205`); no minimum declared in `manifest.json`
- Config entry migration uses `async_update_entry(..., version=...)` (the supported API; direct `entry.version = x` assignment is rejected by modern HA)
- Uses `StatisticMeanType` if available (HA 2026.11+), fallback to `has_mean`
- Unit class handling: EUR and EUR/kWh may break in future HA versions (noted in code)
