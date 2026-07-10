from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.structural_arbitrage import (  # noqa: E402
    StructuralArbitrageConfig,
    run_periodic_structural_arbitrage,
    scan_structural_arbitrage,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Polymarket structural arbitrage scanner. No auth, no orders."
    )
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "structural_arbitrage"))
    parser.add_argument("--market-limit", type=int, default=180)
    parser.add_argument("--event-limit", type=int, default=20)
    parser.add_argument("--max-book-tokens", type=int, default=800)
    parser.add_argument("--min-gross-edge", type=float, default=0.001)
    parser.add_argument("--min-net-edge", type=float, default=0.0)
    parser.add_argument("--min-top-shares", type=float, default=5.0)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = StructuralArbitrageConfig(
        output_dir=Path(args.output_dir),
        market_limit=int(args.market_limit),
        event_limit=int(args.event_limit),
        max_book_tokens=int(args.max_book_tokens),
        min_gross_edge=float(args.min_gross_edge),
        min_net_edge=float(args.min_net_edge),
        min_top_shares=float(args.min_top_shares),
    )
    if int(args.iterations) <= 1:
        print(json.dumps(scan_structural_arbitrage(config), indent=2))
    else:
        run_periodic_structural_arbitrage(config, int(args.iterations), float(args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
