# Market Features Status - 2026-07-06

## Running collection

Lightweight websocket feature recorder is running in research/dry-data mode only.
It does not authenticate and does not place orders.

Started at local time about 19:44:

```powershell
.\.venv\Scripts\python.exe research\record_market_features.py --max-runtime-seconds 21600 --sample-interval-ms 500 --max-output-mb 300
```

Logs:

- `data\market_features\record_features_6h_20260706_194408.out.log`
- `data\market_features\record_features_6h_20260706_194408.err.log`

The error log was empty at the last check. Expected stop is about 01:44 local
time unless the disk guard stops first.

## Analysis pipeline added

New repeatable command:

```powershell
.\.venv\Scripts\python.exe research\analyze_market_features.py --fetch-binance-labels --labels-csv data\market_features_analysis\binance_5m_labels_20260706_capture.csv
```

New files:

- `src\market_feature_analysis.py`
- `research\analyze_market_features.py`
- `tests\test_market_feature_analysis.py`

Outputs:

- `data\market_features_analysis\analysis_report.json`
- `data\market_features_analysis\health_report.json`
- `data\market_features_analysis\paired_summary.csv`
- `data\market_features_analysis\paired_segments.csv`
- `data\market_features_analysis\imbalance_forward.csv`
- `data\market_features_analysis\late_continuation_summary.csv`
- `data\market_features_analysis\imbalance_final_summary.csv`
- `data\market_features_analysis\markov_states.csv`
- `data\market_features_analysis\markov_transitions.csv`
- `data\market_features_analysis\markov_validation.csv`

## Latest analysis snapshot

Last generated output included:

- `64,854` feature rows;
- `112` windows observed;
- `111` windows labeled with Binance 5m proxy labels;
- `145` completed feature files read;
- `1` active gzip skipped safely;
- about `4.606 MB` feature data.

Maker-only paired YES/NO:

- `YES bid + NO bid <= 0.98`: `4,843` samples, `110` windows, `7.50%`.
- `<= 0.97`: `2,409` samples, `108` windows, `3.73%`.
- `<= 0.95`: `674` samples, `94` windows, `1.04%`.

Late continuation rough read:

- `240-285s`, no confidence filter: `110` samples, `95.45%` accuracy,
  rough `+0.0059` PnL/share before fees/slippage.
- `240-285s`, `abs(mid-0.5) >= 0.10`: `109` samples, `96.33%` accuracy,
  rough `+0.0115` PnL/share before fees/slippage.
- `285-305s`: accuracy is very high, but average buy price is about `0.999`,
  so the rough PnL is negative. Too late.

Markov first read:

- Markov state tables now exist, but they are not proof of edge.
- Temporal validation says late states are easy to classify, but the market mid
  baseline is already extremely strong near close and has better Brier in those
  buckets.
- Current use: state map and diagnostics. Not strategy logic yet.

## Verification

Full test suite passed after the changes:

```text
76 passed
```

## Next when back

1. Let the current 6h recorder continue or stop it if you want to inspect.
2. Re-run `research\analyze_market_features.py` to include newly completed
   windows.
3. Decide whether to leave an overnight 8h recorder.
4. Do not move to production. Next research target is fill modeling for
   maker-only paired YES/NO and a stricter late-continuation backtest.

## Late continuation execution scanner

Added on 2026-07-07:

- `src\late_continuation_execution.py`
- `research\late_continuation_execution_scanner.py`
- `tests\test_late_continuation_execution.py`

Default command:

```powershell
.\.venv\Scripts\python.exe research\late_continuation_execution_scanner.py
```

Strict command:

```powershell
.\.venv\Scripts\python.exe research\late_continuation_execution_scanner.py --output-dir data\late_continuation_execution_strict --min-train-attempts 50 --min-test-attempts 20 --min-train-windows 40
```

Outputs:

- `data\late_continuation_execution\split_summary.csv`
- `data\late_continuation_execution\train_test.csv`
- `data\late_continuation_execution\selected_train.csv`
- `data\late_continuation_execution_strict\split_summary.csv`
- `data\late_continuation_execution_strict\train_test.csv`
- `data\late_continuation_execution_strict\selected_train.csv`

Current strict read:

- Most high-accuracy late entries are not profitable after taker fee and price
  paid.
- The only stricter train/test positives so far are small:
  - `t240_d2_px0p94_edge0_imb0_spr0p03`: train `+0.0060`,
    test `+0.0115` PnL per $ attempted; `51` train attempts, `28` test attempts.
  - `t240_d5_px0p94_edge0_imb0_spr0p03`: train `+0.0041`,
    test `+0.0083`; `51` train attempts, `28` test attempts.
- This is a research lead, not enough sample for a trading rule.

## Overnight 2026-07-07

Supervisor launched at local time about `00:15`.

Script:

```powershell
scripts\run_market_features_overnight.ps1
```

Behavior:

1. Wait for any existing `record_market_features.py` process to finish.
2. Start a new 8h lightweight feature recorder.
3. Refresh market feature analysis.
4. Run default and strict late-continuation execution scanners.

Logs:

- `data\market_features\overnight_supervisor_20260707_001550.log`
- `data\market_features\supervisor_launcher_20260707_001550.out.log`
- `data\market_features\supervisor_launcher_20260707_001550.err.log`

At launch, the supervisor was waiting for the still-active 6h recorder rather
than starting a duplicate recorder.

## Parallel full-websocket bursts

Added on 2026-07-07:

- `scripts\run_market_ws_bursts_overnight.ps1`

Purpose:

- The lightweight feature recorder is enough for late continuation, simple
  imbalance, and paired YES/NO top-of-book frequency.
- It is not enough for an honest maker fill model.
- Full websocket SQLite bursts collect replayable orderbook/trade events needed
  for optimistic/base/pessimistic maker-fill simulations, without running a huge
  full recorder all night.

Launched at local time about `00:23`:

```powershell
scripts\run_market_ws_bursts_overnight.ps1 -Bursts 6 -IntervalSeconds 3600 -DurationSeconds 300 -MaxOutputMb 500
```

Initial conservative plan was 6 bursts, 1 hour apart. After the first burst
compressed to only about `3.5 MB`, the old supervisor was stopped and replaced
with a denser plan:

```powershell
scripts\run_market_ws_bursts_overnight.ps1 -Bursts 24 -IntervalSeconds 900 -MaxOutputMb 500
```

Final behavior:

- up to 24 bursts;
- one burst about every 15 minutes;
- each burst records one active BTC 5m window with `--continuous --max-windows 1`,
  so websocket reconnects retry within the window;
- public websocket only;
- no auth and no orders;
- output guard stops before a burst if `data\market_ws` reaches 500 MB.

Logs:

- `data\market_ws\ws_bursts_supervisor_20260707_002338.log`
- `data\market_ws\ws_bursts_launcher_20260707_002337.out.log`
- `data\market_ws\ws_bursts_launcher_20260707_002337.err.log`

Final dense supervisor:

- `data\market_ws\ws_bursts_supervisor_20260707_002758.log`
- `data\market_ws\ws_bursts_dense_launcher_20260707_002757.out.log`
- `data\market_ws\ws_bursts_dense_launcher_20260707_002757.err.log`

At dense launch, burst 1 had started with `record_market_ws.py --continuous
--max-windows 1 --window-grace-seconds 3 --no-raw --storage sqlite`.
