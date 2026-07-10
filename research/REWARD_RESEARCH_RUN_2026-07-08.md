# Reward research run - 2026-07-08

Started at local time around 21:43.

## Mode

- Dry/read-only research.
- No auth.
- No orders.
- No production/live trading.

## Running collectors

- Maker rewards scanner:
  - PID: `20176`
  - Child PID observed: `23800`
  - Output: `data/reward_research_run_20260708_214327/maker_scanner.out.log`
  - Error log: `data/reward_research_run_20260708_214327/maker_scanner.err.log`

- External fair value scanner:
  - PID: `14136`
  - Child PID observed: `11304`
  - Output: `data/reward_research_run_20260708_214327/external_scanner.out.log`
  - Error log: `data/reward_research_run_20260708_214327/external_scanner.err.log`

- Reward/fair-value evaluator loop:
  - Original PID `9532` was restarted after fixing combined ranking so external
    fair-value signals are not hidden when the same market also has maker data.
  - Later restarts added WTI barrier probability, volatility sensitivity, and
    shadow action history.
  - Current PID: `13348`
  - Child PID observed: `20648`
  - Output: `data/reward_research_run_20260708_214327/evaluator_loop_action_history.out.log`
  - Error log: `data/reward_research_run_20260708_214327/evaluator_loop_action_history.err.log`
  - Live report: `data/reward_fair_value_evaluator_live/report.json`
  - Latest dry actions: `data/reward_fair_value_evaluator_live/shadow_actions.csv`
  - Dry action history: `data/reward_fair_value_evaluator_live/shadow_action_history.csv`

- Final supervisor:
  - Original PID `10784` was restarted to wait on the new evaluator PID.
  - Current PID: `3472`
  - Waits for PIDs: `20176,14136,13348`
  - Log: `data/reward_research_run_20260708_214327/overnight_10h_with_analysis_20260708_235949.supervisor.log`
  - Final step also runs shadow action stability analysis over the live action
    history.

## Schedule

- Iterations: 60
- Interval: 600 seconds
- Approx duration: 10 hours

After collectors exit, the supervisor will:

1. Re-run the maker paper simulator with the updated maker history.
2. Run the combined reward/fair-value evaluator using the updated paper events.
3. Write final outputs under:
   - `data/market_making_paper_overnight_10h_restarted_*`
   - `data/reward_fair_value_evaluator_overnight_10h_restarted_*`

## First live signal

The initial live evaluator report already ranks:

- WTI July high threshold as an external fair-value watch item. After adding
  volatility sensitivity, it is currently marked as `watch_wti_vol_sensitive`
  rather than a directional dry trade if the sign flips across volatility
  scenarios.
- Fed/rates markets as the main external adapter target.
- Some maker reward markets with positive base proxy, but these still need more
  honest adverse-selection validation before any trading consideration.

## Morning checklist

1. Confirm all collector processes exited or are still intentionally running.
2. Inspect final supervisor log for exit codes.
3. Read the latest final evaluator `report.json`.
4. Inspect `shadow_actions.csv` and `shadow_action_history.csv` for stable dry
   actions.
5. Inspect the final `data/shadow_action_analysis_overnight_10h_with_analysis_*`
   report for stability-ranked actions.
6. Compare `net_ev_pessimistic_usd`, `net_ev_base_usd`, `history_fill_rate`,
   and `expected_adverse_selection_usd` before trusting any maker-reward candidate.
7. Decide whether the next implementation target is:
   - Fed/rates external curve adapter,
   - WTI family-specific probability model,
   - or tighter maker fill/adverse-selection model.
