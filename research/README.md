# Fair Value Research

This folder contains offline research tooling for the Fair Value strategy. It is
read-only with respect to trading: scripts read bot CSV logs and write derived
datasets/reports under `data/`.

## Persistent plan

See `research/RESEARCH_PLAN.md`. That file is the working memory for the
research program: questions, datasets, safety rules, and pipeline stages.

## External BTC reversal study

Download/cache Binance BTCUSDT 1-second klines:

```powershell
.\.venv\Scripts\python.exe research\binance_klines.py --start 2024-01-01 --end 2024-01-02
```

Run the 5-minute reversal/continuation study:

```powershell
.\.venv\Scripts\python.exe research\reversal_window_study.py --klines data\research\binance\BTCUSDT_1s_2024-01-01_2024-01-02.csv
```

Outputs:

- `data/research/reversal_study/reversal_summary.csv`
- `data/research/reversal_study/candidate_zones.csv`
- `data/research/reversal_study/reversal_report.json`

This is the first layer for answering: "if BTC is up/down by X after Y seconds,
how often does the 5-minute window reverse?"

## Offline rule simulator

Run a temporal train/test simulation over BTC history:

```powershell
.\.venv\Scripts\python.exe research\btc_rule_simulator.py --klines data\research\binance\BTCUSDT_1s_2024-01-01_2024-01-02.csv
```

The simulator learns reversal/continuation probabilities from the first part of
the historical range, tests rule grids on the later part, and reports:

- entry count and win rate;
- predicted probability vs realized win rate;
- break-even entry price;
- hypothetical PnL/ROI under configurable entry-price assumptions.

Outputs:

- `data/research/rule_simulator/strategy_simulation_summary.csv`
- `data/research/rule_simulator/probability_table.csv`
- `data/research/rule_simulator/strategy_simulation_report.json`

## Build datasets

```powershell
.\.venv\Scripts\python.exe research\fair_value_dataset.py
```

Outputs:

- `data/fair_value_window_labels.csv`
- `data/fair_value_training_signals.csv`
- `data/fair_value_training_entries.csv`

`fair_value_training_signals.csv` is the main training dataset. It labels each
recorded signal with the final 5-minute BTC outcome.

## Train/evaluate candidate model

```powershell
.\.venv\Scripts\python.exe research\fair_value_model_research.py
```

Outputs:

- `data/fair_value_model_candidate.json`
- `data/fair_value_model_report.json`

The model script uses a temporal split by market window. It does not modify the
running bot and does not enable live trading.

## Edge scanner

The edge scanner is the new strategy-agnostic layer. It reads recorded
`fair_value_signals.csv` plus settlement labels and asks: "in which buckets did
the market pay the risk badly?"

It creates hypothetical Yes/No, maker/taker candidates for every recorded signal,
labels them with realized PnL per dollar, and reports:

- profitability by archetype (`momentum`, `late_continuation`,
  `core_reversal`, `cheap_reversal`, etc.);
- profitability by route and side relation;
- top buckets by elapsed time, price, and BTC delta;
- a simple temporal walk-forward bucket scan.

```powershell
.\.venv\Scripts\python.exe research\edge_scanner.py
```

Useful recent-window run:

```powershell
.\.venv\Scripts\python.exe research\edge_scanner.py --start "2026-07-05 10:06"
```

Outputs are written under `data\edge_scanner\`. This script is read-only with
respect to trading and does not delete any collected data.

## Execution Scanner

The execution scanner is the next layer after the edge scanner. It asks whether
an apparent edge was actually capturable once execution delays are included:

- taker entry after 0, 1, 2, 3, and 5 seconds;
- maker-only attempts at the current bid with 1, 2, 3, and 5 second waits;
- maker-then-taker fallback after the maker wait expires.

```powershell
.\.venv\Scripts\python.exe research\execution_scanner.py
```

Useful strict run:

```powershell
.\.venv\Scripts\python.exe research\execution_scanner.py --min-bucket-attempts 120 --min-bucket-windows 20
```

Outputs are written under `data\execution_scanner\`. This is also read-only
with respect to trading.

## Market Websocket Recorder

There are two market websocket recorders:

- lightweight feature recorder: default for medium/long-term collection;
- full event recorder: short Tier B replay bursts only.

Neither authenticates or places orders.

### Lightweight Feature Recorder

This is the recorder to leave running for hours. It listens to every websocket
message but only persists compact sampled features.

Run for 9 hours, one gzip CSV per 5-minute window:

```powershell
.\.venv\Scripts\python.exe research\record_market_features.py --max-runtime-seconds 32400 --sample-interval-ms 500 --max-output-mb 300
```

Run the parallel BTC 15-minute microstructure recorder:

```powershell
.\.venv\Scripts\python.exe research\record_market_features.py --output-dir data\market_features_btc_15m --slug-prefix btc-updown-15m --window-seconds 900 --max-runtime-seconds 32400 --sample-interval-ms 500 --max-output-mb 300
```

Outputs go under `data\market_features\`:

- `*.features.csv.gz`: sampled top-of-book, paired YES/NO sums, imbalance,
  quote age, and event counters.

The disk guard stops the recorder if the output directory exceeds
`--max-output-mb`.

Analyze the collected lightweight feature files:

```powershell
.\.venv\Scripts\python.exe research\analyze_market_features.py --fetch-binance-labels
```

Outputs are written under `data\market_features_analysis\`:

- `health_report.json`: coverage, skipped active files, label coverage;
- `paired_summary.csv` and `paired_segments.csv`: maker-only YES+NO bid
  opportunities and how long they persisted;
- `imbalance_forward.csv`: short-horizon mid-price movement after imbalance;
- `late_continuation_summary.csv`: rough hold-to-settlement continuation read
  using fetched Binance 5m labels;
- `markov_states.csv` and `markov_transitions.csv`: first offline Markov-style
  state table. States bucket elapsed time, YES mid, imbalance, and spread. This
  is research-only and must be compared against simpler baselines before any
  strategy logic uses it;
- `markov_validation.csv`: temporal split check comparing state probabilities
  against the market mid baseline on later windows.

Run the late-continuation execution scanner after labels/features are updated:

```powershell
.\.venv\Scripts\python.exe research\late_continuation_execution_scanner.py
```

Outputs are written under `data\late_continuation_execution\`:

- `split_summary.csv`: train/test/all execution summaries for each rule grid;
- `train_test.csv`: train and test metrics joined by scenario;
- `selected_train.csv`: scenarios that were positive in train and had enough
  test attempts. These are candidates for review, not trading rules.

The scanner models taker delay, limit price caps, spread filters, imbalance
confirmation, taker fee, and one attempted trade per window/scenario. Optional
strict-fill flags can require executable top-of-book size, uncrossed YES/NO
books, and fresh selected-side quotes:

```powershell
.\.venv\Scripts\python.exe research\late_continuation_execution_scanner.py --min-top-shares 5 --require-uncrossed-books --max-quote-age-ms 2000
```

### Frozen t270 Validator

Once a late-continuation family is selected, validate it without moving the
grid. This avoids turning every new capture into another parameter search.

```powershell
.\.venv\Scripts\python.exe research\t270_frozen_validator.py --fetch-binance-labels
```

Outputs are written under `data\t270_frozen_validation\`:

- `analysis\health_report.json`: current feature/label coverage;
- `execution\selected_train.csv`: frozen t270 family train/test rows;
- `holdout_summary.csv`: scenarios evaluated only on labels absent from the
  baseline seed, optionally constrained with `--holdout-start-ts`;
- `report.json` and `run_log.jsonl`: latest and historical validation runs.

This is research-only and does not authenticate or place orders.

Strict-fill validation can be run in a separate output directory while reading
the same labels as the normal watcher:

```powershell
.\.venv\Scripts\python.exe research\t270_frozen_validator.py --output-dir data\t270_frozen_validation_strict_fill --labels-csv data\t270_frozen_validation\binance_5m_labels.csv --holdout-start-ts 1783407300 --min-top-shares 5 --require-uncrossed-books --max-quote-age-ms 2000
```

### Structural Arbitrage Scanner

Read-only scanner for broad Polymarket structural opportunities. It fetches
active Gamma markets/events and public CLOB orderbooks, then checks:

- binary complete-set buy opportunities: buy both outcomes below `1`;
- binary split/sell opportunities: sell both outcomes above `1`;
- neg-risk YES basket opportunities inside multi-outcome events.

```powershell
.\.venv\Scripts\python.exe research\structural_arbitrage_scanner.py
```

Outputs are written under `data\structural_arbitrage\`:

- `latest_snapshot.csv`: latest scan metrics for the markets/baskets checked;
- `latest_near_misses.csv`: closest non-opportunities by net/gross edge;
- `opportunities.csv`: appended rows only when edge survives fee estimates;
- `report.json` and `scan_log.jsonl`: latest and historical scan reports.

This is also research-only: no wallet, no auth, no orders.

### Market Making Rewards Scanner

Read-only scanner for maker-only market making candidates. It fetches active
Polymarket reward markets and public CLOB orderbooks, then ranks markets by:

- daily reward rate;
- scoreable quote sides near the reward midpoint;
- spread, volume, recent price movement, and YES/NO midpoint consistency;
- minimum reward size versus a configured quote-size cap;
- time left until the market end, so near-expiry books are filtered out.

```powershell
.\.venv\Scripts\python.exe research\market_making_scanner.py
```

Longer read-only watch, suitable for overnight candidate discovery:

```powershell
.\.venv\Scripts\python.exe research\market_making_scanner.py --iterations 54 --interval-seconds 600
```

Outputs are written under `data\market_making\`:

- `latest_snapshot.csv`: latest ranked reward-market snapshot;
- `latest_candidates.csv`: markets passing the conservative filters;
- `candidate_history.csv`: compact append-only top candidates by scan;
- `report.json` and `scan_log.jsonl`: latest and historical scan reports.

This does not estimate live PnL yet. It selects markets worth deeper maker-only
paper simulation with websocket fill/adverse-selection modeling.

Run the first paper layer over accumulated `candidate_history.csv` snapshots:

```powershell
.\.venv\Scripts\python.exe research\market_making_paper_simulator.py
```

Outputs are written under `data\market_making_paper\`:

- `maker_paper_events.csv`: per quote-side event, next-snapshot touch/fill flag,
  mark-to-mid PnL, and reward proxies;
- `maker_paper_side_summary.csv`: YES/NO bid/ask aggregate behavior;
- `maker_paper_market_summary.csv`: per-market aggregate behavior;
- `report.json`: top and worst markets plus explicit caveats.

This simulator is intentionally conservative: with 10-minute scanner snapshots,
it only counts a fill when the next sampled orderbook touches or crosses the
paper quote. Reward numbers are proxies, not official Polymarket accounting.

### External Polymarket Crypto 5m Dataset Probe

Inventory and cautious downloader for external historical crypto 5m datasets.
By default it can inspect Hugging Face metadata and samples without pulling
large tick files.

```powershell
.\.venv\Scripts\python.exe research\external_polymarket_dataset_probe.py --coin btc --download-markets --sample-rows 100
```

Outputs are written under `data\external_polymarket\kachoio_5m\`:

- `dataset_inventory.csv`: available parquet files and sizes;
- `btc_markets.parquet`: small downloaded BTC market table when requested;
- `btc_ticks.parquet`: large BTC tick table only when `--download-ticks` and a
  high enough `--max-download-mb` are explicitly passed;
- `report.json`: source inventory, samples, and next actions.

### External Crypto 5m Strategy Probe

Offline backtest harness for downloaded external Polymarket crypto 5m parquet
files. It is research-only and checks late-continuation/t270 plus paired YES/NO
complete-set opportunities.

```powershell
.\.venv\Scripts\python.exe research\external_crypto_5m_strategy_probe.py --entry-elapsed-s 270 --taker-delays-s 0,1,2,3,5 --max-prices 0.94,0.95 --min-mid-edges 0,0.05 --min-depth-imbalances 0 --max-spreads 0.01,0.02,0.03,0.05 --min-top-shares 5 --output-dir data\external_polymarket\kachoio_5m\strategy_probe_t270_frozen_uncrossed
```

The probe drops null outcomes, crossed books, and top-of-book rows smaller than
`--min-top-shares`. Current notes are in
`research\EXTERNAL_POLYMARKET_DATASET_PROBE_2026-07-07.md`. It also writes
`stability_summary.csv`, grouped by external dataset week, to check whether an
edge is stable across time.

### Dry Research Watch

Launches the frozen t270 validator, structural scanner, and a queued lightweight
feature recorder that waits for any active feature recorder before starting.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_dry_research_watch.ps1
```

Useful strict-forward run after freezing t270:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_dry_research_watch.ps1 -RuntimeSeconds 32400 -HoldoutStartTs 1783407300 -SkipFeatureRecorder
```

Supervisor logs are written under `data\dry_research_watch\`. Use
`-SkipFeatureRecorder` when a feature-recorder supervisor is already queued.

### Full Event Recorder

Use this only for short replay/debug bursts, because it captures far more data.

Record one active 5-minute window:

```powershell
.\.venv\Scripts\python.exe research\record_market_ws.py --duration-seconds 300
```

Useful short smoke run:

```powershell
.\.venv\Scripts\python.exe research\record_market_ws.py --duration-seconds 20 --no-raw
```

Continuous medium/long-term recorder, one compact SQLite file per 5-minute
window:

```powershell
.\.venv\Scripts\python.exe research\record_market_ws.py --continuous --no-raw
```

For a bounded test of continuous rotation:

```powershell
.\.venv\Scripts\python.exe research\record_market_ws.py --continuous --no-raw --max-windows 2
```

Outputs are written under `data\market_ws\` by default:

- `*.market.sqlite.gz`: compact compressed replayable event/delta storage. This
  is the default for continuous recording and avoids Excel row limits;
- `*.market.sqlite`: transient active-window SQLite file while recording;
- `*.raw.jsonl`: optional raw websocket messages with local receive timestamps;
- `*.summary.csv` or `*.summary.csv.gz`: optional normalized YES/NO top-of-book,
  paired YES/NO cost, and imbalance feature export.

Scan a recorded SQLite window or summary CSV:

```powershell
.\.venv\Scripts\python.exe research\market_microstructure_scanner.py data\market_ws\<window-file>.market.sqlite
```

Compressed SQLite files can be scanned directly too:

```powershell
.\.venv\Scripts\python.exe research\market_microstructure_scanner.py data\market_ws\<window-file>.market.sqlite.gz
```

Outputs are written under `data\market_microstructure\`:

- `paired_candidates.csv`
- `paired_segments.csv`
- `imbalance_events.csv`
- `imbalance_summary.csv`
- `report.json`

This is the first layer for testing maker-only paired YES/NO ideas and
orderbook imbalance without assuming that a touched maker quote would fill.

## Cheap Reversal Research

Cheap reversal is modelled separately from `core`, `momentum`, and `late`.
It builds one opportunity per market/side/bucket instead of treating every
recorded snapshot as an independent trade.

```powershell
.\.venv\Scripts\python.exe research\cheap_reversal_research.py
```

Outputs are written under `data\cheap_reversal\`:

- `cheap_reversal_opportunities.csv`
- `cheap_reversal_walk_forward_scored.csv`
- `cheap_reversal_rule_summary.csv`
- `cheap_reversal_model.json`
- `cheap_reversal_report.json`

The running bot currently records related dry-run paper entries through
`fair_value_giro_probe_entries.csv` and `fair_value_giro_probe_outcomes.csv`.
