# External Strategy Review - 2026-07-06

Scope: Polymarket BTC/crypto 5m Up/Down strategies, with emphasis on ideas that can be tested in this repo without live trading.

Safety rule: do not enable production/live orders from this memo. Any implementation should start as offline replay or dry run.

## Local State Checked

- Latest saved config snapshot in `user_data.json`: `dry_run=True`, `strategy_mode=fair_value`, `poll_interval=1`, `fair_value_live_trading_enabled=False`, stake `$5`.
- `giro_probe` is enabled and now uses `data/cheap_reversal/cheap_reversal_model.json`.
- Cheap model file reports `model_version=cheap_reversal_v1`, `rows=27848`, `auc=0.678498`, `brier=0.178905`.
- Closed `cheap_reversal_v1` giro outcomes currently in CSV: `125` entries, `67` windows, `21W/104L`, `16.8%` win rate, about `-$63.99` PnL.
- Fair value normal outcomes since first cheap-model entry: `42` entries, `24W/18L`, `57.1%` win rate, about `-$35.78` PnL.
- Current implementation records REST `/book` snapshots through `fetch_order_book`; there is no Polymarket market websocket recorder for orderbook deltas yet. The existing websocket use is dashboard logs and Chainlink/BTC feed.

Interpretation: the cheap reversal model has decent offline discrimination but is badly miscalibrated or distribution-shifted in the current dry run. Do not keep hand-tuning it live without a replay layer.

## External Sources Read

- Polymarket CLOB fee docs: maker fees are `0`, taker fee formula for binary markets is `baseRate * min(price, 1-price)`; crypto 5m taker fee uses base rate `0.07`.
  - https://docs.polymarket.com/developers/CLOB/introduction
  - https://docs.polymarket.com/developers/CLOB/introduction#fees
- Polymarket 5-minute fees/rebates announcement: 5m crypto taker fees launched with maker rebates and higher minimum order sizes.
  - https://polymarket.com/learn/five-minute-crypto-fees
- Polymarket realtime docs: market websocket supports book and price-change messages; user websocket supports orders/trades.
  - https://docs.polymarket.com/developers/CLOB/websocket/market-channel
  - https://docs.polymarket.com/developers/CLOB/websocket/user-channel
- Polymarket official market-maker bot: reference architecture for quoting bands around midpoint, external fair value, inventory limits, tickers, and periodic quote refresh.
  - https://github.com/Polymarket/poly-market-maker
- `polymarket-terminal` maker strategy discussion: strategy looks for YES+NO bid sum below about `0.98`, earns maker rebates, merges complementary tokens, treats on-chain balances as source of truth, and warns about ghost fills/stale order state.
  - https://github.com/elian-bao-swe/polymarket-terminal
- `poly-maker`: generic maker-only bot using fair value, depth-weighted mid/microprice, EWMA orderflow, inventory skew, volatility toxicity, post-only orders, and fill/drawdown risk controls.
  - https://github.com/ibrahimelo2/poly-maker
- 5m bot repos and articles:
  - T-10 directional bot keyed off interval open/final BTC price: https://github.com/Archetapp/polymarket-crypto-trading-bot
  - Window-delta + micro-momentum + ATR-filtered 5m bot: https://github.com/jmazzini/polymarket-trading-bot
  - Commercial/blog taxonomy: resolution signal, collapse signal, conviction sizing, penny-buy fallback: https://blog.polysnipe.ai/posts/high-frequency-polymarket-trading-bot
- Public 5m orderbook dataset: 89.2k crypto up/down markets and 26.8M market ticks, 2025-06-26 to 2026-04-15.
  - https://huggingface.co/datasets/kachoio/polymarket-5-minute-crypto-up-down-markets
  - https://kacho.io/polymarket-5min-crypto-dataset
- PMXT research dataset and paper: emphasizes quote/trade reconstruction limitations, trade-direction inference, and lifecycle data gaps.
  - https://arxiv.org/abs/2605.11640
  - https://huggingface.co/datasets/PMXT/Polymarket-Trade-Dataset-v1
- Community warnings:
  - Reddit thread on 5m orderbook dataset: top-book only means queue/fill inference is limited.
    - https://www.reddit.com/r/algotrading/comments/1u8fsg7/free_dataset_polymarket_5min_crypto_updown_order/
  - Reddit thread on paper-vs-live failure: XGBoost paper alpha lost live once fees were included.
    - https://www.reddit.com/r/algotrading/comments/1kdo1vb/polymarket_bot_performs_well_on_paper_but_fails/
  - Reddit thread on short backtests overfitting and needing slippage/latency/quote-freshness instrumentation.
    - https://www.reddit.com/r/algotrading/comments/1iunt0f/polymarket_5m_15m_cryptobased_prediction_markets/
- Automation/competition context: Dune/MEXC discussion reports fast markets are highly automated; AI/bot flow was estimated around 55-62% of volume.
  - https://www.mexc.com/en-TR/news/polymarket-s-fast-markets-the-new-human-ai-decision-layer/269240

## Strategy Ideas Worth Testing

### 1. Maker-neutral paired quoting

Idea: quote/buy both YES and NO cheaply enough that the combined cost after rebates is below settlement value. This is closer to micro market making than prediction.

Why it fits the repo:
- Current code already has post-only maker order plumbing and maker event CSVs.
- Existing dry data suggests maker routing is less bad than taker routing, but the current dry maker simulation is optimistic unless queue/fill is modeled.

Required research before any live use:
- Market websocket recorder for book deltas, trades, order acknowledgements, cancels, and fills.
- Queue/fill simulator. A quote touch is not a guaranteed fill.
- Inventory/merge accounting for complementary tokens.
- Explicit stale-order and ghost-fill reconciliation using user websocket plus on-chain balances.

Initial offline rule:
- Attempt only if `best_bid_yes + best_bid_no <= 0.98` or tighter after explicit rebate/fee assumptions.
- Maker-only, no taker fallback.
- Cancel near close or when top-of-book moves against the quote.

Priority: high for research, not live.

### 2. Late continuation / resolution sniping

Idea: enter late when BTC has moved far enough from the window open and the market price is below a conservative break-even cap.

Why it fits the repo:
- Local BTC research already supports this direction.
- Existing `late_continuation` has the right shape: elapsed window, abs delta, continuation probability, break-even price, max acceptable price.

External confirmation:
- Public 5m bots and commercial writeups repeatedly use final-window clock signals and interval-open/current-price deltas.

Main risk:
- Everyone sees the same clock and BTC price. Edge depends on latency, quote freshness, and not overpaying.

Next test:
- Re-run edge/execution scanners with stricter buckets: final `260-300s`, abs delta buckets `5-10`, `10-20`, `20+`, and price caps under calculated break-even minus a buffer.
- Add quote-freshness and orderbook-update age as features before increasing confidence.

Priority: highest directional candidate.

### 3. Orderbook imbalance / microprice scalping

Idea: use top-of-book imbalance, depth slope, spread, price-change messages, and trade direction to predict very short Polymarket price moves, not necessarily final settlement.

Why it is not ready locally:
- The bot samples REST orderbooks at about 1s cadence. For imbalance scalping this is too coarse and can miss the event order.
- The current CSV has depth fields, but not market websocket deltas, queue position, or trade-aggressor labels.

Next test:
- Build a websocket recorder first.
- Study whether orderbook imbalance predicts (a) next Polymarket mid move, (b) fill probability, (c) final outcome.
- Separate "scalp-to-exit" PnL from "hold-to-settle" PnL. They are different strategies.

Priority: medium-high after recorder.

### 4. Cheap reversal / penny-buy fallback

Current verdict: pause the running idea as-is.

Offline data made some cheap buckets look good, especially very cheap early reversal buckets, but live/dry `cheap_reversal_v1` is underperforming badly. The most likely causes are:
- distribution shift between collected training snapshots and current windows;
- repeated/opportunity selection still overstating independence;
- fill/quote bias in cheap shares;
- model calibration error: it ranks setups somewhat, but probabilities are too high for trading thresholds.

Potential salvage only as a narrow research branch:
- Test extreme penny zones separately: `price <= 0.03`, `<=0.05`, `<=0.10`.
- Require external orderbook replay validation.
- Use realized calibration bins, not model probability directly.
- No current-threshold dry run until the replay agrees.

Priority: low until replay validates it.

### 5. Cross-market / timeframe arbitrage

Idea: compare 5m, 15m, hourly, and daily crypto binaries around shared underlying BTC moves; look for inconsistent prices and hedgeable combinations.

Why it is interesting:
- It is less dependent on predicting BTC direction if the book quotes imply inconsistent probabilities.

Why it is not quick:
- Needs multi-market discovery, synchronized orderbooks, and settlement/fee accounting across products.
- Inventory and exit logic are more complex than the current single-market bot.

Priority: medium as a separate scanner, not as a modification to `fair_value`.

## Recommended Next Experiments

1. Freeze cheap-reversal trading research in current form.
   - Do not production.
   - Keep historical rows, but evaluate new rules only offline.

2. Build or add a market websocket recorder.
   - Subscribe to Polymarket market channel for the active 5m tokens.
   - Record book snapshots/deltas, price changes, trades if available, and local receive timestamps.
   - Add quote-age/freshness to `fair_value_signals.csv`.

3. Create a replay scanner for the Kacho 5m dataset.
   - Validate `late_continuation`, `cheap_reversal`, and maker-neutral paired quoting on external out-of-sample data.
   - Add execution penalties: delay, spread crossing, stale quote rejection, minimum size, and fill probability.

4. Re-score current local data with calibration, not raw model probability.
   - For each model probability bucket, compute realized win rate, avg price, and PnL.
   - Use calibrated probability for EV gates.

5. Make maker research honest before live.
   - Replace dry maker "touch = fill" with a queue-aware fill model.
   - Track post-only rejects, cancels, partial fills, and ghost fills.
   - Reconcile user websocket and on-chain balances.

Short version: the most imitable path is not "find a better reversal threshold"; it is "record fresher market microstructure, replay execution honestly, then test late continuation and maker-neutral quoting."
