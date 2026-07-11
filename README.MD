# Polymarket Research Bot

Research-only Polymarket bot/scanner for market-making rewards, external fair value, and dry paper validation.

## AI Operator Start Here

If a user asks an AI assistant to run this project on a Mac mini, run **only** the dry research supervisor below. Do not start old BTC 5m strategies, martingale, production trading, wallet-authenticated code, or `src.main` unless the user explicitly asks for that separate dashboard/server.

Recommended Mac mini command:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
PUBLISH_EVERY_CYCLES=6 PUBLISH_GIT=1 PUBLISH_BRANCH=dry-run-telemetry \
bash scripts/run_reward_fair_value_dry_continuous.sh
```

This runs the current dry strategy stack:

- reward market scanner;
- external fair-value scanner;
- Fed rates fair-value proxy;
- filtered maker-only dry quotes;
- Fed-rate directional dry simulation;
- parallel active-exit and long-hold dry ledgers;
- taker diagnostic dry ledgers for model validation only;
- sanitized GitHub telemetry under `run_reports/`.

Hard constraints:

- Keep `mode` as `dry_research_only`.
- Do not place live orders.
- Do not use wallet/private-key credentials.
- Do not run martingale or legacy BTC 5m production paths.
- Do not auto-pull code while the 5-day experiment is running.

Current focus:

- Fed rates fair-value proxy using FRED/Yahoo futures or optional manual FedWatch probabilities.
- Maker-only dry quotes filtered to avoid fast sports/esports/unmodelled markets.
- Directional dry positions for Fed-rate edges.
- Active-exit vs long-hold comparison to test whether stops are helping or cutting reversals too early.
- Taker diagnostic ledgers to evaluate directional fair-value signals even when maker quotes do not fill.
- Long-running dry collection with heartbeat, logs, and CSV rotation.

## Safety

Default research scripts are dry-only:

- No wallet auth required.
- No live orders.
- No private key needed.

Do not copy production `.env` credentials into a remote dry-run machine.

## Windows Dry Run

```powershell
.\scripts\run_reward_fair_value_dry_continuous.ps1
```

## Mac/Linux Dry Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash scripts/run_reward_fair_value_dry_continuous.sh
```

For a bounded 5-day run:

```bash
STOP_AFTER_CYCLES=720 bash scripts/run_reward_fair_value_dry_continuous.sh
```

See `research/MAC_MINI_5D_DRY_RUNBOOK.md` for operational details.

Optional GitHub telemetry branch:

```bash
PUBLISH_EVERY_CYCLES=6 PUBLISH_GIT=1 PUBLISH_BRANCH=dry-run-telemetry \
bash scripts/run_reward_fair_value_dry_continuous.sh
```

This publishes small sanitized summaries under `run_reports/`; it does not publish `data/` or secrets.

## Key Outputs

- `data/reward_fair_value_dry_live/heartbeat.json`
- `data/reward_fair_value_dry_live/supervisor.log`
- `data/reward_fair_value_evaluator_live/report.json`
- `data/shadow_paper_trader_active_live/report.json`
- `data/shadow_paper_trader_active_live/paper_orders.csv`
- `data/shadow_paper_trader_hold_live/report.json`
- `data/shadow_paper_trader_hold_live/paper_orders.csv`
- `data/shadow_paper_trader_taker_active_live/report.json`
- `data/shadow_paper_trader_taker_hold_live/report.json`
- `run_reports/latest_summary.md` if telemetry publishing is enabled

The `data/` directory is ignored by git.
