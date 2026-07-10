# Research Findings

This file records research conclusions that should not be lost between coding
sessions. Treat these as hypotheses until they survive wider historical ranges
and Polymarket price validation.

## 2026-07-01: BTC 1-second January 2024 Study

### Data

- Source: Binance public data daily ZIP files.
- Symbol: `BTCUSDT`.
- Interval: `1s`.
- Range: `2024-01-01` to `2024-01-31` exclusive.
- Rows: `2,592,000`.
- Usable 5-minute windows: `8,630`.
- Decision samples: `2,550,748`.

Generated files:

- `data/research/binance/BTCUSDT_1s_2024-01-01_2024-01-31.csv`
- `data/research/reversal_study_2024-01-01_2024-01-31/reversal_summary.csv`
- `data/research/reversal_study_2024-01-01_2024-01-31/candidate_zones.csv`
- `data/research/rule_simulator_2024-01-01_2024-01-31_high_prices/strategy_simulation_summary.csv`

### Reversal vs Continuation

Across all non-flat decision samples:

- Reversal rate: `21.65%`.
- Continuation rate: `78.35%`.

With a minimum of `2,000` samples per bucket, no reversal zone exceeded 50%.
That is important: the simple "bet on the turn" idea does not show a robust
historical edge in this January 2024 sample.

### Strongest Continuation Zones

Late-window continuation was very strong when BTC had already moved away from
the opening price:

- `285-300s`, `20-35 bps`, up/down: continuation was `100%` in this sample.
- `285-300s`, `10-20 bps`, up/down: continuation was about `99.9%`.
- `260-285s`, `20-35 bps`: continuation was about `99.9%`.
- `285-300s`, `5-10 bps`: continuation was about `99.5%`.

This does not mean the strategy is automatically profitable, because Polymarket
will usually price these positions very high. The key metric is the maximum
price we can pay while keeping positive expected value.

### Rule Simulator

Simulator setup:

- Train/test split: first `70%` of windows trains bucket probabilities; last
  `30%` tests strategy rules.
- Fee assumption: `4%` of stake for taker-like entries.
- Stake: `$5`.
- Minimum bucket samples: `2,000`.
- Minimum entries per reported rule: `500`.

Best robust continuation rule:

- Rule: `continuation|final_260_300|large_10_20bps`.
- Test entries: `910`.
- Win rate: `99.12%`.
- Break-even price with 4% fee: `0.9531`.
- ROI if entry price is `0.90`: `+6.13%`.
- ROI if entry price is `0.95`: `+0.34%`.
- ROI if entry price is `0.98`: `-2.86%`.

Broader but weaker continuation rule:

- Rule: `continuation|final_260_300|any|p>=0.75`.
- Test entries: `2,585`.
- Win rate: `94.27%`.
- Break-even price with 4% fee: `0.9065`.
- ROI if entry price is `0.90`: `+0.75%`.
- ROI if entry price is `0.95`: `-4.76%`.

### Interpretation

The research is currently pointing away from "predict lots of reversals" and
toward "only enter when continuation is extremely likely, and only if the market
price is below our break-even threshold."

This suggests a possible future strategy shape:

1. Wait until late in the 5-minute window.
2. Check BTC distance from the opening price.
3. Estimate continuation probability from historical buckets/model.
4. Compare Polymarket ask price against break-even price minus a safety margin.
5. Prefer maker if feasible; otherwise taker only when price is clearly below
   break-even after fees.

This still needs:

- Wider history: at least 90 days, ideally 12 months.
- Walk-forward tests over multiple periods.
- Polymarket price integration, because BTC-only backtests do not know actual
  entry prices, spread, or fill probability.

## 2026-07-02: Core/Maker/Giro and Late Continuation Update

### Code Added

- Added a new Fair Value tactic: `late_continuation`.
- It activates late in the window, default `260-300s`.
- It requires:
  - minimum absolute BTC move from window open, default `10 bps`;
  - minimum historical continuation probability, default `90%`;
  - positive EV after estimated taker fee;
  - entry price below a calculated max acceptable price.
- New entry/signal/outcome fields record:
  - `break_even_price`;
  - `max_acceptable_price`;
  - `price_margin`;
  - `continuation_probability`;
  - `abs_delta_bps`.

### Core/Maker Analyzer

Generated:

- `research/core_maker_research.py`
- `data/research/core_maker_latest/core_maker_report.json`
- `data/research/core_maker_latest/tactic_summary.csv`
- `data/research/core_maker_latest/route_summary.csv`
- `data/research/core_maker_latest/price_bucket_summary.csv`
- `data/research/core_maker_latest/direction_summary.csv`
- `data/research/core_maker_latest/maker_event_summary.csv`

Current mixed historical bot records show:

- Closed Fair Value trades analyzed: `1,238`.
- Overall historical PnL in those mixed dry/live records: `-$653.49`.
- Overall win rate: `39.26%`.
- Dry-run `core_edge`: `945` entries, `36.83%` win rate, `-$618.39`.
- Dry-run `momentum`: `151` entries, `49.01%` win rate, `-$31.85`.
- Live `core_edge`: `21` entries, `52.38%` win rate, `-$3.82`.
- Live `maker_live`: `5` entries, `+$7.54`.
- Live `taker` within core: `16` entries, `-$11.36`.

Interpretation: maker/live fills helped, but the old core model was not
reliable enough, especially in dry-run contrarian/giro mode.

### Giro/Reversal Recheck

The previous simulator required at least 50% predicted reversal probability,
which was too strict for the "cheap share" idea. A new run allowed reversal
probability thresholds down to `25%`.

Generated:

- `data/research/rule_simulator_2024-01-01_2024-01-31_low_prob_reversal_fee4/`

Key reversal finding:

- Best sizable reversal/giro buckets were around `38%-43%` realized win rate.
- They can be positive if entries are around `0.35-0.40`.
- They deteriorate quickly when entry price is closer to `0.45-0.50`.

Practical implication: giro should not be discarded, but it needs a strict price
cap. The strategy cannot simply buy any contrarian signal; it must demand cheap
shares and positive EV.

### Late Continuation Recheck

With broader price assumptions and 4% fee:

- `continuation|final_260_300|huge_20plus_bps` had `375/375` wins in the
  test split of January 2024.
- Break-even price with 4% fee was `0.9615`.
- ROI assumptions for that rule:
  - entry `0.80`: `+21.0%`;
  - entry `0.90`: `+7.11%`;
  - entry `0.95`: `+1.26%`.

This is exactly why `late_continuation` now computes max acceptable price before
entering. Paying too close to `0.98-0.99` destroys the apparent edge.

## 2026-07-03: Kalman Walk-Forward Update

Generated:

- `research/fair_value_walkforward_kalman.py`
- `data/research/fair_value_walkforward_kalman_latest/`

BTC 1m walk-forward range:

- `2025-07-01` to `2026-06-29`
- `104,504` usable 5-minute windows
- `412,853` non-leaky decision samples

Model result:

- Base BTC features: AUC about `0.690`, Brier about `0.1719`.
- Base + Kalman features: AUC about `0.695`, Brier about `0.1710`.
- Interpretation: Kalman adds a small but real historical signal.

Live-entry retrain result:

- Candidate model with Kalman was not promoted.
- Temporal holdout: candidate AUC `0.615`, current AUC `0.626`.
- Temporal holdout Brier: candidate `0.240`, current `0.236`.
- Interpretation: keep recording Kalman fields, but do not trust the live entry
  guard with Kalman until more real/dry Polymarket samples exist.

Strategy implications:

- `core_reversal` is only attractive when entry price is cheap. In the BTC-only
  walk-forward, reversal remained positive around assumed prices `0.35-0.40`,
  but deteriorated quickly near `0.45-0.50`.
- Real dry-run core entries since July had average price around `0.47` and
  negative PnL. This supports tightening core price caps.
- `momentum` remained much stronger historically and in dry-run records.
- `late` continuation remained strong historically, but only if price remains
  below its break-even cap after fees.
