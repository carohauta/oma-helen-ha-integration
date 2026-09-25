"""Constants for the Helen Energy integration."""

from typing import Final

DOMAIN: Final = "helen_energy"
CONF_VAT: Final = "vat"
CONF_FIXED_PRICE: Final = "is_fixed_price"
CONF_CONTRACT_TYPE: Final = "contract_type"
CONF_DEFAULT_UNIT_PRICE: Final = "default_unit_price"
CONF_DEFAULT_BASE_PRICE: Final = "default_base_price"
CONF_INCLUDE_TRANSFER_COSTS: Final = "include_transfer_costs"
CONF_DELIVERY_SITE_ID: Final = "delivery_site_id"
CONF_CUSTOM_NAME: Final = "custom_name"
CONF_CONTRACT_START_DATE: Final = "contract_start_date"

# Contract type options
CONTRACT_TYPE_AUTOMATIC: Final = "automatic"
CONTRACT_TYPE_FIXED: Final = "fixed"
CONTRACT_TYPE_MARKET: Final = "market"
CONTRACT_TYPE_EXCHANGE: Final = "exchange"

# Statistics configuration
# 7 days; hours outside this window are permanently zero-filled
STATISTICS_BACKFILL_HOURS: Final = 168

# Backfill service: how far back to reach when the caller omits start_date.
# Unrelated to STATISTICS_BACKFILL_HOURS, which is the automatic repair window.
DEFAULT_BACKFILL_DAYS: Final = 30

# Longest span sent to the API in one request. The transfer channel silently
# drops everything past 1461 days (HTTP 200, all-null electricity), so long
# backfills are split. 365 keeps a wide margin under the only measured
# boundary and holds even if other channels turn out to be stricter.
# See ADR-0001.
MAX_BACKFILL_CHUNK_DAYS: Final = 365

# Services
SERVICE_BACKFILL_STATISTICS: Final = "backfill_statistics"
