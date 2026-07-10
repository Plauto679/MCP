# BTC 5m Research Plan

This document is the persistent plan for moving the bot from threshold tuning to
data-driven research. Keep it updated when we add datasets, models, or strategy
rules.

## Goal

Build an offline research lab that answers, with historical data:

- When a 5-minute BTC window is already up/down, how often does it reverse?
- Which features warn that continuation or reversal is more likely?
- Which Polymarket-style entry rules survive out-of-sample backtests?
- Which rules are strong enough to run in dry run, then production?

The lab must not place orders and must not change live/dry-run configuration.

## Data Layers

1. Bot logs
   - Source: `data/fair_value_signals.csv`, `data/fair_value_entries.csv`,
     `data/fair_value_outcomes.csv`.
   - Strength: exact bot decisions and Polymarket orderbook snapshots while the
     bot was running.
   - Weakness: limited sample size.

2. External BTC history
   - Source: Binance public market data / REST klines.
   - Initial interval: `1s`, because Binance documents `1s` spot klines.
   - Strength: very large sample of BTC 5-minute windows.
   - Weakness: does not include Polymarket liquidity, spreads, or maker fill
     probability.

3. Polymarket market history
   - Source: CLOB prices-history when available, plus our own recorder going
     forward.
   - Strength: closer to actual trading prices.
   - Weakness: historical orderbook depth is not reliably available, so we need
     our recorder for deep maker/taker research.

## Research Questions

### R1: Reversal/Continuation Probability

For every 5-minute window and every sampled second:

- elapsed seconds
- remaining seconds
- BTC delta from opening price
- BTC delta in bps
- movement speed
- range so far
- number of open-price crossings so far
- current side: up/down
- final side: up/down
- label: reversal or continuation

Outputs:

- reversal rate by elapsed bucket and delta bucket
- continuation rate by elapsed bucket and delta bucket
- candidate zones where reversal/continuation is materially above 50%

### R2: Strategy Simulation

Use R1 outputs to simulate many rule sets:

- core contrarian entries
- momentum entries
- maker-only vs taker fallback assumptions
- max entries per window
- entry-price limits
- EV thresholds
- late-window rules

Outputs:

- win rate
- expected PnL under different price/fee assumptions
- max drawdown and longest losing streak
- sensitivity to entry price and fees

### R3: Model Training

Train models only after R1/R2 produce stable candidate features.

Candidate targets:

- final side: Yes/No
- reversal vs continuation
- entry side win probability
- expected PnL per entry

Validation:

- temporal split only
- walk-forward testing
- no training and testing on the same time period

## Safety Rules

- Research scripts write only under `data/research/` unless explicitly given a
  different output path.
- Research scripts never import or call order placement code.
- Production changes require a separate implementation step after backtest
  results are reviewed.

## Current Pipeline

1. Download/cache Binance 1-second BTC klines:

```powershell
.\.venv\Scripts\python.exe research\binance_klines.py --start 2024-01-01 --end 2024-01-02
```

2. Run reversal/continuation study:

```powershell
.\.venv\Scripts\python.exe research\reversal_window_study.py --klines data\research\binance\BTCUSDT_1s_2024-01-01_2024-01-02.csv
```

3. Review:

- `data/research/reversal_study/reversal_summary.csv`
- `data/research/reversal_study/candidate_zones.csv`
- `data/research/reversal_study/reversal_report.json`

4. Simulate rule grids:

```powershell
.\.venv\Scripts\python.exe research\btc_rule_simulator.py --klines data\research\binance\BTCUSDT_1s_2024-01-01_2024-01-31.csv --fee-per-usd 0.04 --price-assumptions 0.70,0.80,0.90,0.95,0.98
```

5. Current findings:

See `research/FINDINGS.md`.
