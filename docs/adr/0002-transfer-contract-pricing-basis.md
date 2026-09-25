# Transfer contracts are priced from the transfer fee as fixed cost

A **transfer contract** has no energy product: Helen operates the grid connection but someone else sells the electricity. The API reports this as a `0.0` energy unit price with a `None` contract type, which previously left these users with consumption statistics and a permanently empty cost stream. We now fall back to the contract's **transfer fee** (`Siirtomaksu`) as the unit price, so their cost lands in the **fixed cost** stream.

This is a deliberate reinterpretation of what "cost" means for these users: it is the cost of *delivery* only, not the cost of the electricity, which Helen does not know. The alternative — pricing transfer hours at spot, as [PR #44](https://github.com/carohauta/oma-helen-ha-integration/pull/44) proposed via the contract-free spot-price chart endpoint — was rejected because a spot price is not what these users pay: their retailer's price is unknown to this integration, and showing a spot figure would look authoritative while being wrong. Anyone wanting market prices is better served by a Nord Pool or ENTSO-e integration alongside this one.

## Consequences

- A transfer contract's cost is understated relative to the user's real bill by whatever their retailer charges. This is the honest number available, not a complete one.
- The fee used is whatever `get_transfer_fee` returns; whether it includes sähkövero is Helen's choice, not ours, and has not been verified against a real transfer-contract response. No redacted fixture for one exists in the test suite yet.
- The docstring contract in `statistics.py` still holds: a missing spot price alone never zero-fills an hour. For transfer contracts that condition is permanent rather than transient, and the fixed-cost stream is what carries them.
