# Backfill range is bounded by metering history, not by the contract

A backfill is bounded by how far back the **delivery site**'s metering history goes — which is the earliest start across every contract ever held at that GSRN, not the start of the contract we happen to have selected. `get_contract_energy_unit_price`'s sibling `get_contract_start_date()` returns the *newest active* contract's start (`_get_all_active_contracts` drops contracts with a past `end_date`, `_get_latest_contract` sorts `start_date` descending), so after a **contract renewal** it is the renewal date. Clamping the requested range to it silently truncates years of retrievable history, which is exactly the bug `d84507c` ("handle contract renewals gracefully") fixed.

So: the backfill service imposes **no lower bound and no upper bound** on the requested range. It issues a single request for the whole range and lets Helen decide what it has. A range whose start predates the contract is a *partial* overlap, which the measurements endpoint serves from the start of available data onwards; only a request that lies *wholly* before it 403s. Omitting `start_date` reaches back `DEFAULT_BACKFILL_DAYS` (30) — a default, not a cap.

## Considered options

- **Clamp the start to `get_contract_start_date()`.** Rejected: silent truncation on renewal, as above. It would need a *delivery-site* history accessor in helen-python to be correct, and none exists.
- **Split the range into yearly chunks** to be gentle on Helen's API. Rejected, and this is the load-bearing part: chunking is what *created* the 403. Slicing a partially-overlapping range turns the pre-contract stretch into whole requests that lie entirely before the contract, so they 403 — and since all chunks shared one `try` block, one 403 aborted the whole backfill. The clamp only existed to paper over a problem the chunking introduced. Ten years in one request has been verified to work on the energy channel — but see the update below, which partially reverses this.
- **Tolerate 403s on leading chunks** and carry on. Rejected: it makes a real authorisation failure indistinguishable from "before your time" and hides it.
- **Keep the 365-day maximum.** Rejected: there is no technical reason for it, and it blocks the main reason anyone backfills — getting their whole history into the Energy dashboard at once.

## Update 2026-09-25: the transfer channel has a ~4-year ceiling

Reported on [#42](https://github.com/carohauta/oma-helen-ha-integration/pull/42) and not known when the above was written. On the transfer channel (`osv`), a request spanning **1,461 days or fewer returns everything; at 1,462 days the response is HTTP 200 with `electricity: null` on every hour and `missing_series: ["electricity_transfer"]`.** The data is dropped silently — no error status, no partial result.

"One request, however long" is therefore safe only below that ceiling, so the rejection of chunking above is wrong, and chunking is reinstated. **The clamp stays gone**; the two were coupled in the original PR, which is what made them look like one idea, but only the clamp had the renewal problem.

For the record, the rejection above was not merely unlucky. #42 carried the reason in a code comment — *"The API silently returns an empty series (`missing_series`) for large requests, so we fetch in yearly chunks instead"* — and it was read as an API-politeness measure and removed. The lesson worth keeping: a contributor's stated reason for a defensive measure outranks an assumption about their motive, and removing one needs its premise disproved rather than doubted.

**Chunk size is `MAX_BACKFILL_CHUNK_DAYS = 365`, not 1,460.** The ceiling rests on a single measurement on a single channel, and the energy channel (`oh`) is unverified. A year leaves a wide margin, keeps responses to ~8,760 points, and costs ten requests for a decade of history — seconds, on a manually triggered action. It is also the size the contributor originally chose.

Chunking reintroduces the 403, since a leading chunk of a partially-overlapping range can lie wholly before the contract. Those chunks are skipped individually. If *every* chunk 403s, the range genuinely is outside the contract and that still raises, so a real authorisation failure — which fails all chunks — is not swallowed. Any error that isn't `no-relevant-contract` fails the backfill immediately.

The silent drop is also made visible. `backfill_statistics` previously bailed only on an *empty* series, and a dropped payload's series is full, just null; the `missing_series` warning then reported a count of channel names as though it were a count of hours ("1 missing hourly intervals"). `_is_dropped_payload()` now raises on that shape, because writing nothing while reporting success is the worst available outcome.
