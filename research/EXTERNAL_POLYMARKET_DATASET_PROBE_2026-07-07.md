# External Polymarket Crypto 5m Probe - 2026-07-07

Research only. No auth, no wallet, no orders.

## Why This Exists

The local forward holdout is still too small, so we opened an external-history
track to test whether the current late-continuation/t270 idea survives on a much
larger Polymarket BTC 5m sample.

## Sources

- Kacho HF dataset: https://huggingface.co/datasets/kachoio/polymarket-5-minute-crypto-up-down-markets
- Kacho write-up: https://kacho.io/polymarket-5min-crypto-dataset
- Aliplayer HF dataset: https://huggingface.co/datasets/aliplayer1/polymarket-crypto-updown
- BrockMisner HF dataset: https://huggingface.co/datasets/BrockMisner/polymarket-crypto-5m-15m
- Polymarket market maker reference: https://github.com/Polymarket/poly-market-maker

## Data Pulled

Script:

```powershell
.\.venv\Scripts\python.exe research\external_polymarket_dataset_probe.py --coin btc --download-markets --sample-rows 100 --max-download-mb 25
.\.venv\Scripts\python.exe research\external_polymarket_dataset_probe.py --coin btc --download-ticks --max-download-mb 200 --sample-rows 100
```

Local files:

- `data/external_polymarket/kachoio_5m/btc_markets.parquet` - 3.9 MB
- `data/external_polymarket/kachoio_5m/btc_ticks.parquet` - 182.5 MB
- `data/external_polymarket/kachoio_5m/dataset_inventory.csv`
- `data/external_polymarket/kachoio_5m/report.json`

BTC dataset facts:

- 15,682 BTC 5m markets, 2026-03-24 22:10 UTC to 2026-05-18 10:35 UTC.
- 14,226 have usable `Up`/`Down` labels; 1,456 are null.
- 4,704,518 BTC ticks in the raw dataset.
- The dataset labels are inferred from final book state, not on-chain resolution.

## External Strategy Probe

Script:

```powershell
.\.venv\Scripts\python.exe research\external_crypto_5m_strategy_probe.py --entry-elapsed-s 270 --taker-delays-s 0,1,2,3,5 --max-prices 0.94,0.95 --min-mid-edges 0,0.05 --min-depth-imbalances 0 --max-spreads 0.01,0.02,0.03,0.05 --min-top-shares 5 --output-dir data\external_polymarket\kachoio_5m\strategy_probe_t270_frozen_uncrossed
```

Important filters:

- Drops null outcome markets.
- Drops crossed books (`bid > ask`) before evaluating entries or paired YES/NO.
- Requires at least 5 shares at the execution top-of-book.
- Uses taker fee model consistent with the local late-continuation scanner.

## Findings

### 1. Paired YES/NO

Raw paired YES/NO showed a few huge apparent opportunities, but inspection found
they came from crossed/stale snapshots. After dropping crossed books:

- buy complete set positive ticks: 0
- split/sell positive ticks: 0
- min buy sum: 1.0
- max sell sum: 1.0
- best net edge after fees: -0.00107

Conclusion: paired YES/NO is not dead forever, but the simple top-of-book version
does not show real external edge in this dataset. The scanner must keep the
uncrossed-book guard.

### 2. Frozen t270 Late Continuation

The external BTC dataset supports the same broad t270 phenomenon, even after
uncrossed-book and top-size filters.

Representative all-split rows:

- `px0.94 edge0.05 spread0.05`, Up side: 2,135 attempts, 85.48% accuracy, avg execution price 0.8077, avg PnL per attempted USD +0.0490.
- `px0.94 edge0.05 spread0.05`, Down side: 2,171 attempts, 84.75% accuracy, avg execution price 0.8116, avg PnL per attempted USD +0.0322.
- `px0.95 edge0.05 spread0.05`, Up side: 2,351 attempts, 86.30% accuracy, avg execution price 0.8208, avg PnL per attempted USD +0.0437.
- `px0.95 edge0.05 spread0.05`, Down side: 2,390 attempts, 85.69% accuracy, avg execution price 0.8243, avg PnL per attempted USD +0.0289.

Conclusion: this is the first external evidence that t270 is not just a tiny
local overfit. It is still not production evidence.

## Cautions

- The external `outcome` is best-effort and inferred from final book state.
- This is historical Mar-May 2026 data; current July regime can differ.
- The fill model assumes the sampled top ask and size are executable. It does
  not model our latency, taker race conditions, failed requests, or book changes
  between sample and order placement.
- Positive t270 here is not independent of Polymarket prices; it is a test of
  market-implied late continuation, not a pure Binance signal.

## Next Steps

1. Keep the current local dry holdout running; it is the strictest current-regime
   test.
2. Add weekly/monthly external splits to see whether t270 is stable or localized
   to one period.
3. Add a stricter fill model: require ask size above intended stake and penalize
   one-tick adverse movement.
4. Fetch or reconstruct Binance spot labels for the same external windows to
   compare final book labels against exchange truth.
5. Only if local forward holdout and external split stability agree, consider a
   very small dry execution layer. No live trading without explicit confirmation.
