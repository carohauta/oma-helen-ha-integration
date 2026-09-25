# Backfill range is bounded by metering history, not by the contract

A backfill is bounded by how far back the **delivery site**'s metering history goes — which is the earliest start across every contract ever held at that GSRN, not the start of the contract we happen to have selected. `get_contract_energy_unit_price`'s sibling `get_contract_start_date()` returns the *newest active* contract's start (`_get_all_active_contracts` drops contracts with a past `end_date`, `_get_latest_contract` sorts `start_date` descending), so after a **contract renewal** it is the renewal date. Clamping the requested range to it silently truncates years of retrievable history, which is exactly the bug `d84507c` ("handle contract renewals gracefully") fixed.

So: the backfill service imposes **no lower bound and no upper bound** on the requested range. It issues a single request for the whole range and lets Helen decide what it has. A range whose start predates the contract is a *partial* overlap, which the measurements endpoint serves from the start of available data onwards; only a request that lies *wholly* before it 403s. Omitting `start_date` reaches back `DEFAULT_BACKFILL_DAYS` (30) — a default, not a cap.

## Considered options

- **Clamp the start to `get_contract_start_date()`.** Rejected: silent truncation on renewal, as above. It would need a *delivery-site* history accessor in helen-python to be correct, and none exists.
- **Split the range into yearly chunks** to be gentle on Helen's API. Rejected, and this is the load-bearing part: chunking is what *created* the 403. Slicing a partially-overlapping range turns the pre-contract stretch into whole requests that lie entirely before the contract, so they 403 — and since all chunks shared one `try` block, one 403 aborted the whole backfill. The clamp only existed to paper over a problem the chunking introduced. Ten years in one request has been verified to work.
- **Tolerate 403s on leading chunks** and carry on. Rejected: it makes a real authorisation failure indistinguishable from "before your time" and hides it.
- **Keep the 365-day maximum.** Rejected: there is no technical reason for it, and it blocks the main reason anyone backfills — getting their whole history into the Energy dashboard at once.
