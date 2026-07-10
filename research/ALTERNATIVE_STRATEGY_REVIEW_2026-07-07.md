# Alternative Strategy Review - 2026-07-07

Objective: prepare the next research track if BTC 5m late-continuation,
maker-pair, and imbalance do not survive stricter testing.

This is research only. Nothing here should be wired into live trading without a
separate dry-run build, data audit, and explicit confirmation.

## Bottom Line

The most interesting alternatives are not "another BTC 5m threshold". The
strongest paths are structural:

1. **Cross-market / combinatorial arbitrage on Polymarket**
2. **Polymarket-Kalshi cross-venue arbitrage**
3. **Market making + liquidity rewards with inventory/toxicity controls**
4. **Wallet intelligence / copy-counter trading as signal generation**
5. **Crypto interval basket research across BTC/ETH/SOL/XRP and longer windows**

The common lesson from external builders is brutal but useful: directional crypto
snipes and momentum bots often look good in paper mode and collapse once ask
price, fill, spread, fee, and adverse selection are modeled.

## Strategy 1 - Structural Arbitrage / Bregman / Neg-Risk

Priority: **highest**

Why it matters:

- This is market-neutral and mathematical, not "predict BTC".
- It scans price constraints that should hold across binary or multi-outcome
  markets.
- It naturally extends to Polymarket's negative-risk events.

External evidence:

- LayerX's research journey says their crypto 15m directional bot lost `37.81%`,
  CEX momentum failed because paper mode used the wrong executable prices, and
  their current focus became Bregman / Frank-Wolfe arbitrage.
- The IMDEA paper models single-condition and combinatorial arbitrage in
  prediction markets and formalizes dependency/complementarity relationships
  across markets.
- FlexiWay's repo claims a scanner for single-condition, neg-risk rebalancing,
  and whale tracking, referencing large historical arbitrage extraction.
- Polymarket's own negative-risk docs explain that one NO share can convert into
  YES shares in all other outcomes in a multi-outcome event.

Core idea:

- Binary: if `YES_ask + NO_ask + fees < 1`, buy both.
- Overpriced binary: if `YES_bid + NO_bid > 1`, split/merge or sell both when
  possible.
- Multi-outcome: if sum of executable outcome prices violates logical constraints,
  solve a constrained allocation.
- Related markets: build implication graphs, e.g. "candidate wins" implies
  "party wins", or a margin market implies an outright winner.

Mathematical tools:

- Linear programming / integer programming for logical constraints.
- Bregman projection and Frank-Wolfe for projecting market prices onto a valid
  probability polytope.
- Graph search over related events and markets.
- VWAP execution model over depth, not top-of-book fantasy.

What we can build:

1. A read-only scanner over Gamma/CLOB markets.
2. Constraint graph builder:
   - same event multi-outcome constraints;
   - neg-risk sets;
   - manually curated relation templates first;
   - later LLM-assisted relation proposals, but never LLM-only execution.
3. Execution simulator:
   - top-of-book and depth VWAP;
   - full fill vs partial fill;
   - fees/rebates;
   - settlement timing and capital lockup.
4. Ranking:
   - gross edge;
   - executable size;
   - capital lock duration;
   - operational complexity;
   - historical recurrence.

Kill criteria:

- Opportunities are too rare after executable VWAP.
- Edge disappears after partial fill/failed leg assumptions.
- Capital lockup makes annualized return unattractive.

## Strategy 2 - Polymarket-Kalshi Cross-Venue Arbitrage

Priority: **very high**, assuming data access is practical.

Why it matters:

- Multiple public repos independently target this.
- It avoids being purely directional if both sides are filled.
- Kalshi orderbooks are public enough for research, and official docs explain
  orderbook conventions.

External evidence:

- CarlosIbCu's repo monitors BTC 1-hour Polymarket/Kalshi opportunities and
  calculates opposing-position costs.
- realfishsam's repo describes synthetic arbitrage: YES on one venue plus NO on
  another, with simultaneous aggressive liquidity taking.
- Reddit r/algotrading discussion emphasizes the important nuance: holding to
  maturity can be close to risk-free if both legs fill, but actively trading
  convergence is not risk-free.
- GitHub topic pages show several Kalshi/Polymarket BTC 15m arbitrage projects.

Core idea:

- Match equivalent markets between venues.
- Convert orderbooks into comparable YES/NO executable asks.
- Detect `YES_A + NO_B < 1 - costs` or `NO_A + YES_B < 1 - costs`.

What we can build:

1. Public-data scanner first, no auth:
   - Kalshi market discovery/orderbook;
   - Polymarket event/market discovery/orderbook;
   - fuzzy + time-window matching.
2. Dry-run opportunity log:
   - both executable prices;
   - sizes;
   - edge;
   - capital duration;
   - settlement mismatch.
3. Later: paper fill simulator with simultaneous leg logic.

Risks:

- Venue access/regulatory constraints.
- Market definitions differ subtly.
- One leg fills and the other moves.
- Fees, withdrawal/custody, and settlement timing can dominate small edges.

Kill criteria:

- After matching and VWAP, fewer than a handful of real opportunities per day.
- Average net edge below operational/settlement risk.

## Strategy 3 - Market Making + Liquidity Rewards

Priority: **high**, but not on BTC 5m first.

Why it matters:

- It monetizes spread/rewards rather than forecasting.
- Polymarket docs explicitly describe maker rewards and inventory management.
- Existing repos implement band-based and maker-only market making.

External evidence:

- Polymarket docs: resting limit orders can earn liquidity rewards, with scoring
  based on two-sided participation, tightness, size cutoffs, and market-specific
  parameters.
- Polymarket inventory docs recommend skewing quotes when inventory becomes
  imbalanced and using merge/split mechanics.
- `poly-maker` is maker-only, uses live book, fair value + inventory skew,
  volatility/toxicity estimates, and warns that market making can lose money.
- `polymarket-marketmaking` shows band-based quoting with configurable margins
  and order sizes.
- Avellaneda-Stoikov gives a mature framework for inventory-aware quoting.

Core idea:

- Select markets with reward/rebate pool large enough relative to volatility.
- Quote both sides with inventory skew.
- Pull quotes when toxicity/news/volatility spikes.
- Treat fills as adverse-selection risk, not free spread.

Mathematical tools:

- Avellaneda-Stoikov reservation price and optimal spread.
- Survival/hazard model for fill probability.
- Toxicity model from order-flow imbalance and recent price jumps.
- Fractional Kelly or capped utility for position sizing.

What we can build:

1. Market scoring scanner:
   - reward parameters;
   - spread;
   - depth;
   - volatility;
   - volume;
   - event category.
2. Paper quoting engine:
   - maintain hypothetical bid/ask orders;
   - fill model using full websocket bursts;
   - inventory PnL and reward estimate.
3. Do this on slower/event markets, not BTC 5m first.

Kill criteria:

- Fill model shows fills mostly occur before adverse moves.
- Rewards are too small after realistic inventory drawdown.
- Required live order maintenance is too latency-sensitive for our setup.

## Strategy 4 - Wallet Intelligence / Copy-Counter Trading

Priority: **medium-high** as a signal layer, not blind copy trading.

Why it matters:

- Polymarket is transparent enough to track wallets and activity.
- Tools and blogs increasingly focus on copied wallets, suspicious wallets, and
  "insider" detection.
- This can produce leads in non-crypto markets where informational edge exists.

External evidence:

- Polymarket's own "COPYCAT" post says wallets are trackable but warns about
  hidden secondary accounts, iceberging, merging, and bots.
- Polymarket Data API exposes user activity types including trades, splits,
  merges, conversions, maker/taker rebates, deposits, and withdrawals.
- QuickNode and multiple GitHub repos implement copy-trading/wallet tracking.
- Insider-tracker repos monitor fresh wallets, unusual sizing, and niche-market
  entries.
- Tremor-like tools detect sudden market moves and then look for public news or
  lack of public news.

Core idea:

- Do not copy top leaderboard wallets blindly.
- Build wallet features:
  - category-specific PnL;
  - pre-move timing;
  - position concentration;
  - repeatable behavior;
  - new wallet / funding links;
  - exit behavior;
  - stale/zombie positions;
  - whether trades are copyable after lag.
- Convert wallet moves into alerts and backtestable signals.

What we can build:

1. Wallet activity collector using public Data API.
2. Wallet scoring model:
   - realized PnL adjusted for unresolved/zombie positions;
   - Sharpe-like consistency;
   - category specialization;
   - post-trade market movement after 1m/5m/30m/24h.
3. Signal scanner:
   - "sharp wallet bought early";
   - "cluster of fresh wallets";
   - "large trade with no public news";
   - "counter-trade consistently bad wallets".

Risks:

- Copying lag turns us into exit liquidity.
- Leaderboards are polluted by luck, unresolved losses, and uncopyable market
  makers.
- Ethical/legal caution around explicit insider trading.

Kill criteria:

- Signals are not profitable after entry lag and spread.
- Wallet edge is concentrated in non-copyable fills or private information.

## Strategy 5 - Broader Crypto Interval Basket

Priority: **medium**, only if structural tracks stall.

Why it matters:

- Public repos target BTC/ETH/SOL/XRP, 5m/15m/1h, not just BTC 5m.
- More assets/windows increase sample size and may expose less efficient niches.

External evidence:

- Trum3it repo targets BTC, ETH, SOL, XRP 5m late favorite entries at
  `0.97-0.99` and explicitly warns one reversal can wipe many wins.
- Aulekator repo proposes BTC 15m with spike detection, sentiment, price
  divergence, risk-first sizing, and signal fusion.
- LayerX's failed momentum section is a warning: paper profits vanished when
  execution used real ask prices.

Core idea:

- Extend our recorder/analyzer from BTC 5m to BTC/ETH/SOL/XRP and 15m/1h where
  available.
- Do not assume one set of parameters transfers.
- Learn per-asset, per-window, per-volatility-regime rules.

Mathematical tools:

- Hierarchical models: asset/window as random effects.
- Regime detection: volatility, CEX momentum, realized range, market spread.
- Multiple-testing control: nested walk-forward, not best bucket shopping.

Kill criteria:

- Edge only appears after slicing into tiny buckets.
- Fees/delay erase the edge across all assets.

## Strategy 6 - Microstructure / OFI / Hawkes

Priority: **medium**, useful as support layer.

Why it matters:

- Our current data already shows imbalance has short-horizon mid-price signal,
  but not necessarily settlement edge.
- Full websocket bursts can support richer event-level models.

External evidence:

- Order-book imbalance bots use depth ratios around the mid and exit when the
  book normalizes.
- Academic LOB work models mid-price movement with Markov/birth-death dynamics,
  OFI, log-OFI, and Hawkes/Neural Hawkes processes.
- Avellaneda-Stoikov and later LOB simulators emphasize fill probability and
  inventory, not just direction.

What to use it for:

- Fill model.
- Quote pull/keep decision.
- Late-continuation confirmation.
- Avoiding toxic fills.

What not to use it for:

- A standalone "imbalance says buy" bot without execution modeling.

## Strategy 7 - Event/News/Volatility Tremor

Priority: **medium-low initially**, higher later for non-crypto markets.

Why it matters:

- Prediction markets move on information shocks.
- Tremor-like tools monitor hundreds of markets and score sudden moves.

External evidence:

- Tremor monitors many active markets, scores significant price movement, and
  tries to distinguish public-news moves from unexplained moves.
- Insider/wallet tracker repos use suspicious-wallet heuristics.

What we can build:

- Market move detector:
  - price change z-score;
  - volume spike;
  - wallet cluster;
  - no obvious public-news tag.
- Do not auto-trade first; create alerts and label outcomes.

Risks:

- News latency and interpretation are hard.
- Insider-style markets may be ethically and legally sensitive.
- Some moves are manipulation or rumors.

## Replicable Research Roadmap

### Phase A - No-order scanners

1. Structural arbitrage scanner:
   - binary sum;
   - neg-risk sum;
   - same-event multi-outcome;
   - related-market manual templates.
2. Cross-venue scanner:
   - Kalshi public orderbooks;
   - Polymarket CLOB;
   - market matching;
   - VWAP net edge.
3. Wallet intelligence collector:
   - top wallets by category;
   - activity + post-trade drift;
   - copy lag backtest.

### Phase B - Execution realism

1. For every opportunity, compute:
   - top-of-book edge;
   - VWAP edge for $10/$50/$100;
   - partial-fill loss;
   - capital lockup;
   - settlement/fee assumptions.
2. Require opportunity logs before strategy code.

### Phase C - Mathematical layer

1. Bregman / Frank-Wolfe for constraint arbitrage.
2. Avellaneda-Stoikov for market making.
3. Hazard/survival model for fills.
4. OFI/log-OFI and Markov/Hawkes for microstructure.
5. Calibration tracking:
   - Brier;
   - reliability curves;
   - temporal split;
   - conformal intervals if sample supports it.
6. Risk:
   - fractional Kelly only after calibrated probabilities;
   - hard max stake;
   - drawdown caps;
   - no martingale.

## "Jim Simons" Translation For This Project

Do not copy the myth. Copy the discipline:

- collect more data than feels necessary;
- search for many small, weak, orthogonal signals;
- keep models boring until data demands complexity;
- strict out-of-sample tests;
- no narrative override when PnL disagrees;
- account for capacity, fees, slippage, and latency;
- prefer structural mispricing to directional guessing.

For us, this means a portfolio of scanners and falsification tests, not one
hero strategy.

## Recommended Next Build If BTC 5m Fails

Build `research/structural_arbitrage_scanner.py` before anything else.

Minimum viable version:

1. Pull active Polymarket events/markets.
2. Fetch CLOB orderbooks.
3. Detect:
   - binary taker pair `YES ask + NO ask < 1`;
   - maker pair `YES bid + NO bid < 1` as watchlist only;
   - neg-risk event sum under/over pricing;
   - top same-event multi-outcome sum violations.
4. Write:
   - `data/structural_arbitrage/opportunities.csv`;
   - `data/structural_arbitrage/report.json`.
5. No live execution.

Second build:

`research/kalshi_polymarket_scanner.py`

Minimum viable version:

1. Read Kalshi public market/orderbook endpoints.
2. Match BTC/ETH/SOL/XRP 15m/1h first, then broader events.
3. Compute synthetic arbitrage after executable prices.
4. Log only.

## Source Notes

- LayerX research journey:
  https://layerx.xyz/blog/polymarketbots
- IMDEA arbitrage paper:
  https://suarez-tangil.networks.imdea.org/papers/2025aft-arbitrage.pdf
- Polymarket negative risk:
  https://docs.polymarket.com/advanced/neg-risk
- Polymarket liquidity rewards:
  https://docs.polymarket.com/market-makers/liquidity-rewards
- Polymarket inventory management:
  https://docs.polymarket.com/market-makers/inventory
- Polymarket Data API user activity:
  https://docs.polymarket.com/api-reference/core/get-user-activity
- Kalshi orderbook docs:
  https://docs.kalshi.com/api-reference/market/get-market-orderbook
- Polymarket/Kalshi BTC arbitrage repo:
  https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot
- Synthetic arbitrage repo:
  https://github.com/realfishsam/prediction-market-arbitrage-bot
- poly-maker:
  https://github.com/warproxxx/poly-maker
- Polymarket marketmaking repo:
  https://github.com/elielieli909/polymarket-marketmaking
- Order-book imbalance bot:
  https://github.com/mooncitydev/polymarket-order-book-imbalance-bot
- Trum3it late-window crypto repo:
  https://github.com/Trum3it/polymarket-arbitrage-bot
- Aulekator BTC 15m repo:
  https://github.com/aulekator/Polymarket-BTC-15-Minute-Trading-Bot
- Polymarket COPYCAT:
  https://news.polymarket.com/p/copycat
- TREMOR:
  https://github.com/sculptdotfun/tremor
- Avellaneda-Stoikov:
  https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf
- Statistical arbitrage LOB project:
  https://web.stanford.edu/class/msande444/2009/2009Projects/2009-2/MSE444.pdf
- Prediction market structures / BMM:
  https://people.cs.vt.edu/~sanmay/papers/predmarkets.pdf
- OFI/log-OFI research summary:
  https://www.researchgate.net/publication/352360390_High-frequency_Statistical_Arbitrage_Strategy_Based_on_Stationarized_Order_Flow_Imbalance
- Kelly for prediction markets:
  https://arxiv.org/html/2412.14144v1
