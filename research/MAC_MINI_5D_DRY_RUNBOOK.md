# Mac Mini 5-Day Dry Runbook

This run is research-only. It uses no Polymarket private key, no auth, and sends no orders.

## AI Operator Instructions

If you are an AI assistant operating this repo for the Mac mini owner, your task is to run the current dry research supervisor only:

```bash
bash scripts/run_reward_fair_value_dry_continuous.sh
```

Do not run:

- martingale;
- old BTC 5m strategies;
- live/production trading;
- wallet-authenticated order placement;
- unrelated dashboard/server commands unless explicitly requested.

The active experiment is the reward/fair-value dry stack focused on Fed rates, filtered maker-only quotes, Fed-rate directional dry simulation, active-exit versus long-hold ledger comparison, and taker diagnostic dry ledgers for model validation only.

## Setup

```bash
git clone <repo-url> polymarket-mcp
cd polymarket-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Do not copy a production `.env` to the Mac mini for this run. The dry scanners do not need wallet credentials.

## Start

Use `tmux` or `screen` so the process survives SSH disconnects.

```bash
tmux new -s poly-dry
source .venv/bin/activate
bash scripts/run_reward_fair_value_dry_continuous.sh
```

Optional 5-day bounded run:

```bash
STOP_AFTER_CYCLES=720 bash scripts/run_reward_fair_value_dry_continuous.sh
```

At 10 minutes per cycle, 720 cycles is about 5 days.

## Remote Monitoring Via GitHub

The runner can publish small sanitized snapshots to `run_reports/`. It does not publish `data/`, wallet files, `.env`, or full CSV histories.

Manual snapshot:

```bash
source .venv/bin/activate
python research/publish_dry_run_snapshot.py
```

This writes:

- `run_reports/latest_summary.md`
- `run_reports/latest_summary.json`
- `run_reports/history.jsonl`

To auto-publish every 6 cycles, about once per hour:

```bash
PUBLISH_EVERY_CYCLES=6 \
PUBLISH_GIT=1 \
PUBLISH_REMOTE=origin \
PUBLISH_BRANCH=dry-run-telemetry \
bash scripts/run_reward_fair_value_dry_continuous.sh
```

Requirements for GitHub publishing:

- The Mac mini clone must have push access, preferably via SSH deploy key or normal GitHub SSH auth.
- Use a separate branch such as `dry-run-telemetry`, not `main`.
- Keep the clone clean before starting the run.

From another computer, follow:

```bash
git fetch origin dry-run-telemetry
git show origin/dry-run-telemetry:run_reports/latest_summary.md
```

Or just open `run_reports/latest_summary.md` in GitHub on the `dry-run-telemetry` branch.

Code changes are intentionally not auto-pulled during the run. If the strategy code needs changes, stop the run, `git pull`, run tests, and restart. This avoids changing the experiment halfway without noticing.

## Health Checks

```bash
cat data/reward_fair_value_dry_live/heartbeat.json
tail -n 50 data/reward_fair_value_dry_live/supervisor.log
ls -lh data/market_making data/external_fair_value data/shadow_paper_trader_live
```

Expected:

- `mode` is `dry_research_only`.
- All exit codes are `0` most cycles.
- `shadow_paper_trader_live/report.json` updates every cycle.
- Data files stay modest; large histories rotate automatically around 128 MB.

## What To Review

Primary files:

- `run_reports/latest_summary.md` if GitHub publishing is enabled
- `run_reports/latest_summary.json` if GitHub publishing is enabled
- `data/shadow_paper_trader_active_live/report.json`
- `data/shadow_paper_trader_active_live/paper_orders.csv`
- `data/shadow_paper_trader_hold_live/report.json`
- `data/shadow_paper_trader_hold_live/paper_orders.csv`
- `data/shadow_paper_trader_taker_active_live/report.json`
- `data/shadow_paper_trader_taker_hold_live/report.json`
- `data/reward_fair_value_evaluator_live/latest_external_fair_value.csv`
- `data/reward_fair_value_evaluator_live/latest_maker_economics.csv`
- `data/shadow_action_analysis_live/shadow_action_summary.csv`

Key metrics:

- Active realized PnL and unrealized PnL, excluding reward proxy.
- Active-exit PnL versus long-hold mark-to-mid PnL.
- Taker diagnostic PnL to evaluate fair-value direction independent of maker fill rate.
- Take-profit vs stop-loss vs stale/max-hold exits.
- Fed rates directional dry positions versus maker-only Fed quotes.
- Conservative reward proxy needed to break even.
- Whether fills keep showing adverse selection.

## Fed Rates Inputs

By default the Fed adapter uses:

- FRED DFF effective fed funds rate when reachable.
- Yahoo 30-Day Fed Funds futures (`ZQ*.CBT`) as a proxy.

This is not official CME FedWatch. If official/manual probabilities are available, create:

```bash
mkdir -p data/external_sources
cp research/fedwatch_probabilities.example.csv data/external_sources/fedwatch_probabilities.csv
```

Then edit probabilities. The evaluator will prefer that CSV when `market_slug` or `condition_id` matches.

## Stop

Inside the tmux session, press `Ctrl+C`.

Or from another shell:

```bash
kill "$(cat data/reward_fair_value_dry_live/supervisor.pid)"
```

## Production Gate

Do not switch to live trading from this run alone. The minimum gate after 5 days is:

- Positive active PnL before rewards, or very small adverse-selection loss versus conservative reward estimate.
- Fed rates signals outperform watch-only baselines.
- No unexplained stale exposure.
- Clean logs and reproducible reports.
- Manual confirmation before any live order path is enabled.
