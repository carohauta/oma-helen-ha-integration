# Transfer contracts are priced from the transfer fee as fixed cost

A **transfer contract** has no energy product: Helen operates the grid connection but someone else sells the electricity. The API reports this as a `0.0` energy unit price with a `None` contract type, which previously left these users with consumption statistics and a permanently empty cost stream. We now fall back to the contract's **transfer fee** (`Siirtomaksu`) as the unit price, so their cost lands in the **fixed cost** stream.

This is a deliberate reinterpretation of what "cost" means for these users: it is the cost of *delivery* only, not the cost of the electricity, which Helen does not know. The alternative — pricing transfer hours at spot, as [PR #44](https://github.com/carohauta/oma-helen-ha-integration/pull/44) proposed via the contract-free spot-price chart endpoint — was rejected because a spot price is not what these users pay: their retailer's price is unknown to this integration, and showing a spot figure would look authoritative while being wrong. Anyone wanting market prices is better served by a Nord Pool or ENTSO-e integration alongside this one.

## Consequences

- A transfer contract's cost is understated relative to the user's real bill by whatever their retailer charges. This is the honest number available, not a complete one.
- The fee used is whatever `get_transfer_fee` returns, and it **includes sähkövero**. Verified 2026-09-25 against a real transfer contract ([#42](https://github.com/carohauta/oma-helen-ha-integration/pull/42)): the product carries `siirtomaksu` at 4.44 snt/kWh and `Sähkövero` at 2.91788 c/kWh as separate components, and `get_transfer_fee()` returns their sum, 7.35788. A redacted fixture lives in helen-python at `tests/resources/transfer_contracts_response.json`.
- The transfer fee has its own price history — the same fixture shows it moving 4.07 → 0.00 (the 2024 transfer-fee holiday) → 4.07 → 4.12 → 4.44. Backfill reprices the whole requested range at the *current* fee, so historical transfer costs are approximate. This is the same caveat that already applies to fixed energy prices.
- The docstring contract in `statistics.py` still holds: a missing spot price alone never zero-fills an hour. For transfer contracts that condition is permanent rather than transient, and the fixed-cost stream is what carries them.
