# Project status - 2026-07-08

This file is the cleanup checkpoint before starting the next research branch.
It is intentionally conservative: preserve collected data, mark failed ideas as
frozen, and avoid deleting strategy code until the dashboard/orchestrator wiring
is untangled.

## Safety rule

- No production/live trading unless explicitly requested and confirmed again.
- Default mode is dry run / paper / research.
- Historical data and reports under `data/` should be preserved.

## Active research fronts

1. External fair value / multi-market system
   - Best current strategic direction.
   - Priority markets: Fed/rates first, WTI second.
   - Rationale: less direct microstructure competition than BTC 5m, more room to
     combine external curves, cross-market structure, and systematic scanning.
   - Current report: `data/external_fair_value/report.json`.

2. Maker-only / maker rewards with honest fill model
   - Interesting only if reward accounting is modeled honestly.
   - Current paper result shows negative mark-to-mid PnL before rewards, with a
     reward proxy that might offset it.
   - Needs official reward model approximation before any real trading.
   - Current report: `data/market_making_paper_final_2104/report.json`.

3. BTC 15m microstructure
   - Less hostile than BTC 5m in the latest capture, but not positive as a
     standalone strategy.
   - Keep as a supporting research input, not as the main bet.
   - Current report: `data/btc_window_microstructure_compare_final_2104/report.json`.

## Background only

- Structural arbitrage
  - Latest scan found no actionable opportunities.
  - Keep as low-priority periodic scanner, not a main effort.
  - Current report: `data/structural_arbitrage/report.json`.

## Frozen / rejected unless new evidence appears

- Martingale / martingale maker
  - Rejected due streak risk, fees, and unfavorable tail behavior.

- Cheap reversal / giro_probe cheap model
  - Latest dry run showed the model overestimated reversal probability.
  - Approx observed result after activation: 122 entries, 21W/101L, around
    -48 USD paper PnL.

- BTC 5m fair_value / momentum / late continuation / cheap reversal as standalone
  - Current evidence is not enough to justify continuing as the main path.
  - Keep historical data for analysis; do not tune parameters by hand.

## Data to preserve

- `data/market_features*`
- `data/market_making*`
- `data/external_fair_value`
- `data/btc_window_microstructure_compare_final_2104`
- `data/market_making_paper_final_2104`
- `data/structural_arbitrage`
- `data/log_archive`

## Cleanup performed

- Stopped old scanners/recorders/schedulers before this checkpoint.
- Archived root `*.log` files into `data/log_archive/`.
- Removed generated `__pycache__` directories from project code folders.
- Did not delete old strategy code yet because martingale/giro/late/fair_value
  references are still mixed into `src/orchestrator.py`, `src/main.py`, and the
  dashboard template.

## Next cleanup pass

1. Untangle strategy registration/config from `src/orchestrator.py`.
2. Move frozen strategies behind an explicit `deprecated` or `legacy` boundary.
3. Remove dashboard controls for strategies we no longer run.
4. Keep tests and historical reports for reproducibility.
5. Only after that, delete truly unreachable modules.

## Pending infrastructure notes

- Convert reward/fair-value collection into a durable continuous research radar:
  periodic reward-market scan, external fair-value classification, paper fill
  simulation, combined shadow-action evaluator, heartbeat, and CSV/log rotation.
- Keep the radar dry/read-only by default. It should discover and classify new
  markets continuously, but only family-specific adapters with measurable
  external data should graduate from `watch_only` to paper quote candidates.
