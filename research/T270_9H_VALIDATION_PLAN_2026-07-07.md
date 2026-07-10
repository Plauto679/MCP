# T270 9h Validation Plan - 2026-07-07

Research only. No auth, no wallet, no live orders.

## Questions To Answer

1. Does frozen `t270` still look positive on the local forward holdout after more
   current-regime windows?
2. Does it still look positive under a stricter fill model?
3. Is the external Mar-May 2026 signal stable across time, or concentrated in a
   few lucky periods?

## Running While Away

Already running before this plan:

- Lightweight feature recorder:
  - `research\record_market_features.py --max-runtime-seconds 14400 --sample-interval-ms 500 --max-output-mb 300`
  - Started 2026-07-07 09:46 Europe/Madrid, expected to end around 13:46.
- Normal frozen t270 watcher:
  - output: `data\t270_frozen_validation`
  - started 09:28, 36 iterations, 15-minute interval.
- Structural scanner:
  - output: `data\structural_arbitrage`
  - started 09:28, 108 iterations, 5-minute interval.

Added for this 9h window:

- Strict-fill frozen t270 watcher:
  - output: `data\t270_frozen_validation_strict_fill`
  - reads labels from `data\t270_frozen_validation\binance_5m_labels.csv`
  - fill model: `min_top_shares=5`, `require_uncrossed_books=true`,
    `max_quote_age_ms=2000`.
- Queued feature-recorder extension:
  - `scripts\run_market_features_overnight.ps1 -RuntimeSeconds 21600`
  - waits for the active recorder to finish, then captures another 6 hours.

## New Code Added

- `LateContinuationExecutionConfig.min_top_shares`
- `LateContinuationExecutionConfig.require_uncrossed_books`
- `LateContinuationExecutionConfig.max_quote_age_ms`
- CLI flags in `research\t270_frozen_validator.py`
- CLI flags in `research\late_continuation_execution_scanner.py`
- External weekly stability output:
  - `data\external_polymarket\kachoio_5m\strategy_probe_t270_frozen_uncrossed\stability_summary.csv`

## Current Baseline Before Leaving

Normal local validator:

- feature windows: 285
- labeled windows: 283
- holdout labeled windows: 16
- holdout candidate rows: 240
- positive scenarios: 80

Strict-fill local validator:

- feature windows: 286
- labeled windows: 283
- holdout labeled windows: 16
- holdout candidate rows: 240
- positive scenarios: 80
- top holdout rows are still based on only 3 windows, so this is not enough yet.

External stability:

- Paired YES/NO after uncrossed-book filter: 0 positive ticks.
- `t270_d0_px0.94_edge0.05_spread0.05` is positive across most weekly periods.
- Weak spot: Down side in 2026-05-04/2026-05-10 is roughly flat/slightly negative;
  the final partial week has too few samples to judge.

## Decision Criteria

Promote t270 to a tiny dedicated dry-run candidate only if all are true:

- Normal local holdout reaches at least 40-50 holdout windows.
- Strict-fill holdout has enough executed attempts, ideally 15+ independent
  windows for the exact scenario family.
- Normal and strict-fill both remain positive on the same scenario family.
- External weekly stability does not show one isolated period explaining the
  entire edge.

Kill or downgrade t270 if any are true:

- Strict-fill holdout flips negative while normal stays positive.
- Most positive local rows come from fewer than 10 independent windows.
- Quote-age/top-size filters remove most executable opportunities.
- The next 9h current-regime windows are materially worse than the old local
  train/test view.

## Files To Check On Return

- `data\t270_frozen_validation\report.json`
- `data\t270_frozen_validation\holdout_summary.csv`
- `data\t270_frozen_validation_strict_fill\report.json`
- `data\t270_frozen_validation_strict_fill\holdout_summary.csv`
- `data\external_polymarket\kachoio_5m\strategy_probe_t270_frozen_uncrossed\stability_summary.csv`
- `data\structural_arbitrage\report.json`
- latest files under `data\market_features\`
