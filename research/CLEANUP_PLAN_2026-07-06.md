# Cleanup Plan - 2026-07-06

Goal: remove failed or unused strategy code without deleting research records.

Keep:

- All CSV/JSON logs under `data/`, including martingale, fair value, maker, cheap reversal, and websocket records.
- Research scripts that read historical logs.
- Tests that protect settlement, fee, and order execution helpers until replacement code exists.

Do not remove yet:

- Martingale code inside `src/orchestrator.py`.
- Martingale dashboard controls.
- MCP order placement helpers in `src/server.py`.

Reason: martingale is not strategically useful now, but its code still shares
settlement, maker-order, fee, and CLOB execution plumbing with newer research.
Deleting it directly risks breaking dashboard/config paths and hiding useful
edge cases already covered by tests.

Recommended cleanup phases:

1. Extract shared plumbing.
   - Move orderbook parsing, settlement helpers, fee math, and maker-order
     lifecycle helpers into small modules.
   - Keep tests green after each extraction.

2. Freeze old strategies behind explicit legacy names.
   - Rename martingale dashboard/API labels to `legacy_martingale`.
   - Hide from default UI once fair-value/research flows no longer import it.

3. Remove dead strategy entrypoints.
   - Delete legacy strategy loops only after all shared helpers have moved out.
   - Preserve CSV readers and historical analyzers.

4. Add a compatibility migration.
   - If `user_data.json` contains old strategy state, ignore it cleanly instead
     of failing startup.

5. Delete legacy UI controls.
   - Remove controls only after backend no longer exposes those config keys.

Near-term safe cleanup:

- Stop extending martingale.
- Add new market microstructure work under `src/market_*` and `research/`.
- Treat old martingale results as archived data, not a live strategy candidate.
