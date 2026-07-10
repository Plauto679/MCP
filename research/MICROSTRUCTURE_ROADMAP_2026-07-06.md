# Microstructure Roadmap - 2026-07-06

Purpose: stop tuning strategies blindly and build a measured path toward a
possibly tradable Polymarket BTC 5m strategy.

## Current Decision

The full websocket recorder is too heavy for continuous collection on this
machine.

Observed local sizes:

- wide summary CSV reached about `90 MB` for one partial window before gzip;
- compact event SQLite still reached about `155 MB` active, `21 MB` gzipped for
  a partial/high-traffic window;
- extrapolated to 288 windows/day, full lossless capture can become many GB/day.

Decision: do not run full lossless capture continuously. Use a two-tier data
plan.

## What We Are Trying To Learn

### H1: Maker-only paired YES/NO has real executable edge

Question:

- Do moments where `YES bid + NO bid <= 0.98` happen often enough?
- Do they last long enough to place/cancel maker orders?
- Is there enough size?
- Would we fill both legs, or get stuck with one exposed leg?

Pass criteria before dry strategy:

- At least `300` paired opportunities across `>= 100` windows.
- Median opportunity duration above `500 ms`.
- Enough displayed size for at least the minimum order.
- Replay fill model remains positive after queue and one-leg risk haircut.

Fail/park criteria:

- Opportunities are mostly sub-100ms flickers.
- Most edge is inside unreachable queue.
- One-leg exposure dominates gross spread.

### H2: Orderbook imbalance improves entries/exits

Question:

- Does top-of-book/depth imbalance predict the next 1s/3s/5s mid move?
- Does it predict maker fill probability?
- Does it help avoid bad `late_continuation` entries?

Pass criteria:

- Out-of-sample hit rate or expected value remains positive across multiple
  daily splits.
- Signal survives after quote age, spread, and latency filters.
- It improves an existing candidate strategy, not just a standalone chart.

Fail/park criteria:

- Imbalance only reflects current price movement and adds no forward signal.
- Signal flips by day or by volatility regime.

### H3: Late continuation is the first directional candidate

Question:

- Does late-window continuation remain positive when using fresh Polymarket
  quotes and realistic execution?

Pass criteria:

- Positive EV after taker fee or maker fill haircut.
- Works over several days, not one lucky market regime.
- Price cap remains strict; no paying 0.98 for obvious outcomes.

Fail/park criteria:

- Apparent edge disappears after quote freshness and delay.

## Data Collection Plan

### Tier A: Continuous Feature Recorder

This is the default medium/long-term dataset.

Store one compact row every `250-500 ms` per active market, not every websocket
delta:

- timestamp;
- YES/NO best bid/ask and sizes;
- spread/mid;
- top imbalance and depth imbalance;
- paired bid/ask sums;
- last trade price/side if available;
- event/update counters since previous sample;
- quote age;
- elapsed seconds in the 5m window.

Target size:

- `250 ms`: about `1,200 rows/window`, `345k rows/day`;
- `500 ms`: about `600 rows/window`, `173k rows/day`;
- compressed target: tens of MB/day, not GB/day.

This is enough to study most signal questions and avoids Excel limits.

### Tier B: Short Full Replay Bursts

Use full websocket event SQLite only for selected windows:

- a few high-volume windows per day;
- windows where Tier A detects many paired opportunities;
- explicit debug sessions.

Purpose:

- build the honest maker fill/queue model;
- inspect event order around opportunities;
- validate whether Tier A features lose important information.

Budget:

- max `30-60 min/day` full capture unless storage is reviewed.

### Tier C: Labels And Outcomes

For every recorded window, attach:

- BTC window open/close;
- final Yes/No result;
- realized volatility/range;
- Binance/Chainlink settlement source;
- optional existing fair-value signal/outcome rows.

No strategy research is accepted without labels.

## Collection Duration

Minimum useful dataset:

- `48 hours` for smoke and plumbing validation;
- `7 days` for first serious candidate filtering;
- `14-30 days` before considering any live-maker dry strategy.

For BTC 5m:

- `1 day` = 288 windows;
- `7 days` = 2,016 windows;
- `30 days` = 8,640 windows.

The first actionable research pass should happen after `48-72h`, not after a
month. We should not wait passively.

## What We Do While Collecting

1. Build Tier A continuous feature recorder with disk guard.
   - Stop writing full lossless data continuously.
   - Add max daily storage warning/stop.
   - Compress/rotate by day or window.

2. Build daily health report.
   - windows captured;
   - rows/window;
   - MB/day;
   - missing windows;
   - paired opportunity counts;
   - top imbalance distribution.

3. Build labels join.
   - Merge websocket feature windows with BTC outcome labels.
   - Reuse existing settlement/Binance tooling.

4. Build first scanners.
   - paired YES/NO opportunity duration and size;
   - imbalance forward mid-move;
   - late continuation with quote freshness.

5. Define gates before new dry strategy.
   - No strategy moves to dry run unless offline replay passes.
   - No production without explicit confirmation.

## What We Do When There Is Enough Data

After `48-72h`:

- run health report;
- run paired opportunity scanner;
- run imbalance forward-move scanner;
- compare late continuation entries with quote freshness.

After `7 days`:

- temporal train/test by day;
- estimate maker fill model from full replay bursts;
- rank candidate rules by EV and robustness;
- choose one dry-run shadow strategy or park the branch.

After `14-30 days`:

- walk-forward validation;
- stress test by volatility regime/time of day/liquidity;
- decide whether any maker-only dry strategy deserves implementation.

## Stop Conditions

Stop/park this research if:

- storage remains above practical budget after Tier A compression;
- paired opportunities are too fleeting or too small;
- imbalance signal is unstable out-of-sample;
- late continuation loses edge after quote freshness.

## Immediate Next Step

Replace the current full continuous recorder with Tier A continuous feature
recording plus a disk guard. Then resume collection.
