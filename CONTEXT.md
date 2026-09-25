# Helen Energy

A Home Assistant integration that pulls electricity data from Helen's Oma Helen service and writes it into Home Assistant's long-term statistics so it shows up in the Energy dashboard.

## Language

### Helen's side

**Delivery site**:
A metering point at a physical address, identified by its GSRN. Consumption is measured per delivery site, not per contract.
_Avoid_: metering point, address, GSRN (as a noun for the site itself)

**Energy contract**:
A contract under which Helen both delivers and sells the electricity. Carries a price for the energy itself — spot, fixed, or market.
_Avoid_: full contract, real contract

**Transfer contract**:
A contract under which Helen only operates the grid connection for a delivery site; the electricity itself is bought from another retailer. Carries a transfer fee per kWh but no energy price, so Helen never prices these hours at spot.
_Avoid_: electricity-transfer site, transfer-only contract, distribution contract

**Contract renewal**:
A new contract replacing an expired one at the same delivery site. The delivery site's metering history continues across the seam; the new contract's start date does not mark the beginning of available data.

**Perusmaksu / Energia / Siirtomaksu**:
The Finnish names Helen uses for the components of a contract's pricing: the standing monthly charge, the per-kWh energy price, and the per-kWh transfer fee.

### Our side

**Consumption**:
Electricity drawn at a delivery site during one hour, in kWh. The one quantity Helen measures; everything else is priced from it.

**Spot cost**:
The hourly cost of consumption priced at that hour's spot price. Only energy contracts on a spot product have one.

**Fixed cost**:
The hourly cost of consumption priced at a per-kWh rate that does not vary by hour — a fixed energy price, or a transfer contract's transfer fee.

**Backfill**:
A user-triggered import of a past date range, rewriting the statistics in that range and leaving everything outside it untouched.
_Avoid_: import, sync, reimport

**Pending hour**:
An hour whose price Helen already publishes but whose consumption has not landed yet. Helen prices day-ahead, so prices run ahead of measurements and never the other way round.

**Zero-filled hour**:
An hour written with 0 kWh because Helen returned no consumption for it. A placeholder that keeps the cumulative chain unbroken, not a measurement.
_Avoid_: gap, missing hour

**Repair**:
Rewriting a previously zero-filled hour once Helen starts returning real consumption for it, and correcting every later cumulative total by the difference.

**Extend mode**:
Writing statistics forward from the newest hour already stored, the normal behaviour of the hourly update.

**Rebuild mode**:
Overwriting a bounded date range wholesale, anchored to the last stored hour before that range. What a backfill does.
