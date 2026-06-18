# AGENTS.md

This file provides detailed guidance for AI coding agents when working with code in this repository.

## Project Overview

Home Assistant custom integration for Helen Energy electricity service (Finland). Fetches electricity consumption, pricing, and costs from the Oma Helen API. Supports Exchange (spot), Market Price, Fixed Price, and Smart Guarantee (VALTTI) electricity contracts.

Key features:
- Config flow UI for setup (legacy YAML migration supported)
- Multiple contract types with automatic detection
- Multiple config entries (e.g. several delivery sites/contracts), each with isolated statistics
- Statistics import for HA Energy Dashboard (168-hour / 7-day rolling window, consecutive chain walk)
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
- Registers `_async_reload_entry` as an `add_update_listener`, so option changes (via the OptionsFlow below) trigger an entry reload
- Schema migration handled by HA core via the module-level `async_migrate_entry` (see migration.py)
- Supports legacy YAML import (deprecated)

**`config_flow.py`** - UI configuration flow
- `VERSION = 2` (entries created at v2; older v1 entries auto-migrated by core)
- User authentication via Helen API
- Contract type validation (automatic/fixed/market/exchange); automatic mode that can't resolve shows `automatic_detection_failed` with the detected type
- Required `custom_name` for the entry title (helps distinguish multiple contracts)
- Delivery site selection (optional)
- Generates unique IDs: `{username}_{delivery_site_id}` or `{username}_{timestamp}`
- **`HelenOptionsFlow`** (wired via `async_get_options_flow`) lets users reconfigure VAT, contract type, transfer-costs, and default unit/base prices after setup; saved options are read via `conf()` and an option change triggers reload through the listener in `__init__.py`

**`services.py`** - Integration services
- Registers `helen_energy.backfill_statistics` (schema/UI in `services.yaml`)
- Backfills a custom date range (`start_date` → today, max `MAX_BACKFILL_DAYS=365`); optional `config_entry_id` targets one contract, otherwise all
- Fetches the requested range first; only writes to the DB on success — a failed API call leaves existing statistics untouched
- Uses rebuild mode: anchors on the last DB record before the range, overwrites the range via upsert; data outside the range is never touched
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
- `_assign_identity(entity, coordinator, sensor_type)` centralizes per-entry `unique_id` + display-name logic (first entry keeps the legacy name; later entries get a delivery-site-or-index suffix); all seven entity classes use it
- **Sensor entities** (contract-type specific):
  - `HelenExchangeElectricity` - Exchange (spot) pricing
  - `HelenMarketPriceElectricity` - Market price
  - `HelenFixedPriceElectricity` - Fixed price
  - `HelenSmartGuarantee` - Smart guarantee / VALTTI contract
  - `HelenTransferPrice` - Transfer/delivery costs (optional)
  - `HelenMonthlyConsumption` - Energy Dashboard integration

**`statistics.py`** - External statistics manager
- **`HelenStatisticsManager`**: Imports hourly statistics to HA database via a consecutive cumulative chain walk
  - Creates up to 3 statistic streams for Energy Dashboard, **suffixed per config entry** to avoid collisions across multiple contracts (suffix = `config_entry_id` hyphen-stripped, lowercased, first 8 chars):
    - `helen_energy:hourly_energy_consumption_{suffix}` (cumulative kWh)
    - `helen_energy:hourly_cost_spot_{suffix}` (cumulative EUR, spot/exchange price)
    - `helen_energy:hourly_cost_fixed_{suffix}` (cumulative EUR, fixed unit price — only when `fixed_unit_price` is set)
  - Two entry points share `_write_statistics_chain()` with different modes:
    - `import_recent_statistics()` — extend mode, 168h (7-day) rolling window (used by the coordinator)
    - `backfill_statistics(start_date, end_date)` — rebuild mode, custom range (used by the service)
  - **Extend mode**: walks consecutively from `last_db_hour + 1h`; stops at missing recent hours, zero-fills hours older than `STATISTICS_MAX_GAP_WAIT_HOURS` (5 days) to unblock the chain
  - **Rebuild mode**: anchors at the last DB record before the range (30-day lookback), overwrites the full range via upsert; data outside the range is never touched
  - **Pending data**: API hours with `electricity=None` halt the walk (extend) or are zero-filled if stale (both modes)
  - Handles timezone conversion (Helsinki → UTC)
  - **Critical**: All timestamps normalized to UTC with microseconds stripped
  - Rounding: 2 decimals for consumption (kWh), 4 decimals for prices (EUR/kWh)

**`migration.py`** - Backward compatibility
- `async_migrate_entry`: schema-version transform only (v1 → v2; backfills `include_transfer_costs`, normalizes title). Called by HA core when a stored entry is older than `ConfigFlow.VERSION`
- `async_migrate_entities_for_compatibility`: re-points legacy YAML entity IDs onto the new config entry's unique IDs so history is preserved. Called from `async_setup_entry` (first entry only) — **not** from `async_migrate_entry`
- Legacy entity ID mappings in `LEGACY_ENTITY_MAPPINGS`

**`utils.py`** - Shared helpers
- `safe_round(value, decimals=2)` — round-or-zero, used across the integration
- `conf(config_entry, key, default=None)` — read a setting, preferring `entry.options` (reconfigurable via OptionsFlow) over `entry.data`, falling back to `default`
- `get_entry_position(hass, config_entry)` — returns `(is_first_entry, zero-based index)` among Helen entries; drives per-entry naming in the coordinator and `_assign_identity`

**`const.py`** - Constants and configuration keys
- Domain: `helen_energy`
- Contract types: automatic/fixed/market/exchange
- `SERVICE_BACKFILL_STATISTICS`, `CONF_CUSTOM_NAME`
- Statistics rolling window: 168 hours / 7 days (not user-configurable, set by `STATISTICS_BACKFILL_HOURS`)

### External Dependencies

**`oma-helen-cli==1.7.0`** (PyPI package `helenservice`)
- `HelenApiClient` - Authentication, consumption data, contract info
- `HelenPriceClient` - Spot/market/fixed pricing data
- API response models: `MeasurementsWithSpotPriceResponse`, `MeasurementsWithSpotPriceSeries`
- Resolution constant: `RESOLUTION_HOUR` (1-hour) — both the rolling window and the ad-hoc backfill service use this
- Exceptions: `HelenAuthenticationException`, `InvalidDeliverySiteException`

### Data Flow

1. **Setup**: Config entry → Create API clients → Initialize coordinator
2. **Update cycle** (every 3 hours):
   - Fetch consumption data (current/last month)
   - Fetch pricing data (contract-type specific)
   - Update sensor states and attributes
   - Update `_fixed_unit_price` from API if not user-configured
   - Import statistics (always, after a successful fetch)
3. **Statistics import** (consecutive chain walk, in `_write_statistics_chain`):
   - Fetch hourly series with `RESOLUTION_HOUR` (same call for both paths)
   - **Extend mode** (incremental): query existing stats in the 7-day window; start walk from `last_db_hour + 1h`; stop at any missing recent hour; zero-fill hours older than 5 days
   - **Rebuild mode** (backfill): query 30-day lookback before range for cumulative anchor; walk and upsert the full range

4. **Ad-hoc backfill** (service): `backfill_statistics(start_date, today)` → `_write_statistics_chain(rebuild=True)` — no statistics are cleared beforehand; API failure leaves DB untouched.

### Statistics Manager Implementation Details

**`HelenStatisticsManager` Constructor** — `(hass, api_client, entity_id, config_entry_id, config_entry_title, fixed_unit_price=None)`:
- `config_entry_id` — derives the per-entry statistic_id suffix (prevents collisions across contracts)
- `config_entry_title` — used in statistic display names
- `fixed_unit_price: float | None` — fixed unit price in cents/kWh; enables the `hourly_cost_fixed` stream when set

**`HelenStatisticsManager` Key Methods**:

1. **`import_recent_statistics()`** - Coordinator entry point
   - Fetches hourly data via `_fetch_interval_data()`, then calls `_write_statistics_chain(series)`
   - Extend mode: anchors at the last DB record in the window, appends new hours only

3. **`backfill_statistics(start_date, end_date)`** - Custom-range rebuild
   - Fetches the range at `RESOLUTION_HOUR`, then calls `_write_statistics_chain(series, rebuild=True)`
   - Rebuild mode: anchors at the last DB record *before* the range (30-day lookback), upserts the full range

4. **`_write_statistics_chain(series, rebuild=False)`** - Core chain writer
   - Builds `api_entries` dict (UTC hour → series entry)
   - Queries existing stats to find the anchor cumulative (window query in extend, lookback query in rebuild)
   - Walks hour-by-hour: missing recent hour → stop (extend) or zero-fill if older than `STATISTICS_MAX_GAP_WAIT_HOURS`
   - Imports all three streams via `_import_statistics()`

5. **`_get_existing_statistics_in_window()`** - DB window query
   - Returns `{UTC datetime: cumulative_sum}` for a statistic ID in a time range

6. **`_import_statistics()`** - HA import wrapper
   - Calls `async_add_external_statistics` with correct `StatisticMetaData`

   - Queries `statistics_during_period` from epoch to the timestamp; returns `(sum, timestamp)`

9. **`_import_statistics(statistic_id, name, unit, unit_class, statistics)`** - Single import helper
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
- **Chain extension tests**: mock `_get_existing_statistics_in_window` to seed DB state; assert cumulative sums start from the correct anchor
- **End-to-end**: `test_write_statistics_chain_imports_all_three_streams` mocks `_get_existing_statistics_in_window` and asserts all three streams import with correct metadata and cumulative sums
- **Rebuild mode tests**: verify anchor is taken from the lookback window (before the range), not from records inside the range

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

**Preventing Duplicate Statistics** (CRITICAL — via consecutive chain walk):

- Extend mode starts from `last_db_hour + 1h`, so already-imported hours are never written again
- Rebuild mode uses upserts (`async_add_external_statistics` replaces existing records for the same timestamp), so re-running backfill for the same range is safe
- API hours with `electricity=None` halt the walk — they are never written

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
- **Root cause**: cumulative chain jumping over missing hours, producing wrong base for subsequent records
- **Fix**: consecutive walk from `last_db_hour + 1h` — the chain never jumps, and the anchor is always the last verified DB record

**Inconsistent Timestamps**:
- **Symptom**: Multiple statistics entries for the same hour with different cumulative values
- **Root cause**: Timestamps not normalized (different microseconds, timezone formats)
- **Fix**: Always `.replace(minute=0, second=0, microsecond=0)` and convert to UTC

### Home Assistant Version Compatibility

- Tested against HA Core 2025.1 (pinned by `pytest-homeassistant-custom-component==0.13.205`); no minimum declared in `manifest.json`
- Config entry migration uses `async_update_entry(..., version=...)` (the supported API; direct `entry.version = x` assignment is rejected by modern HA)
- Uses `StatisticMeanType` if available (HA 2026.11+), fallback to `has_mean`
- Unit class handling: EUR and EUR/kWh may break in future HA versions (noted in code)
