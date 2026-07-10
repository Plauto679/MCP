# Away Runbook 2026-07-08

Research-only / dry-run state. No auth, no wallet, no live orders.

## Running thesis

- `t270` / late continuation is now low priority. The strict holdout reached 293 windows with 0 positive scenarios, so it is effectively rejected unless a later offline rerun reverses hard.
- BTC 15m maker-only is the main active question. Early comparison suggests fewer adverse touches than BTC 5m.
- Maker rewards generalist remains a separate candidate: useful if rewards compensate adverse selection, but reward accounting is still only a proxy.
- Structural arbitrage remains background only; latest scans found no real opportunities.
- Option C is now active: external fair value / modelable non-crypto markets. It is a read-only scanner, not a trading bot. First scan found Fed/rates, WTI, weather, sports, and politics candidates.

## Captures left running

- BTC 5m feature recorder continues in `data/market_features`.
- BTC 15m feature recorder continues in `data/market_features_btc_15m`.
- A queued BTC 5m recorder waits for PID `8544` and then runs another 10 hours.
- A queued BTC 15m recorder waits for PID `12884` and then runs another 6 hours.
- Maker rewards scanner extended for 60 scans, 10-minute interval, plus a queued continuation to run until roughly 21:06.
- Structural arbitrage scanner extended for 60 scans, 10-minute interval, plus a queued continuation to run until roughly 21:06.
- External fair value scanner runs in `data/external_fair_value` for 54 scans, 10-minute interval, plus a queued continuation to run until roughly 21:03. It excludes crypto by default and fetches a WTI proxy price from Yahoo Finance when available.
- Post-away analysis is scheduled with the original 9-hour delay and a second run at roughly 21:10, after the extended scanners finish.

## Stopped intentionally

- The strict `t270_frozen_validator.py` watcher was stopped to free resources. It only analyzed existing data; it did not record new market data.
- The older midnight maker-rewards and structural scanners were stopped to avoid duplicate writes to the same output files. The newer extended scanners remain running.

## On return, check these first

```powershell
Get-ChildItem data\btc_window_microstructure_compare_after_away
Get-Content data\btc_window_microstructure_compare_after_away\report.json -Raw
Get-Content data\market_making_paper_after_away\report.json -Raw
Get-Content data\external_fair_value\report.json -Raw
```

Useful quick status:

```powershell
Get-Process python,powershell | Select-Object Id,ProcessName,StartTime,CPU,WorkingSet64
Get-ChildItem data\market_features -Filter *.features.csv.gz | Measure-Object -Property Length -Sum
Get-ChildItem data\market_features_btc_15m -Filter *.features.csv.gz | Measure-Object -Property Length -Sum
Import-Csv data\external_fair_value\latest_modelable.csv | Group-Object family | Sort-Object Count -Descending
```

## Decision rule

- If BTC 15m keeps lower touch rate and less negative PnL per touched quote than BTC 5m, build the next layer: a real paper maker simulator for BTC 15m with quote lifetime, cancel/reprice rules, and fair-value distance.
- If BTC 15m converges to 5m behavior, deprioritize crypto maker-only and focus on maker rewards generalist.
- If external fair value shows stable liquid families, prioritize the smallest source-integrable family first: Fed/rates via FedWatch/SOFR futures style probabilities, or WTI via CL futures/options and threshold-distance dynamics. Sports/politics are broader backups, not first build targets.
- Do not move anything to production.
