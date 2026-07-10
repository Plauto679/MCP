from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market_making import (  # noqa: E402
    MarketMakingScanConfig,
    run_periodic_market_making_scan,
    scan_market_making_rewards,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Polymarket maker-only rewards scanner. No auth, no orders."
    )
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "market_making"))
    parser.add_argument("--max-markets", type=int, default=250)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--request-timeout-s", type=float, default=30.0)
    parser.add_argument("--min-daily-rate", type=float, default=10.0)
    parser.add_argument("--min-volume-24hr", type=float, default=1000.0)
    parser.add_argument("--min-avg-spread", type=float, default=0.002)
    parser.add_argument("--max-mid-pair-deviation", type=float, default=0.04)
    parser.add_argument("--max-one-day-price-change", type=float, default=0.20)
    parser.add_argument("--min-hours-to-end", type=float, default=12.0)
    parser.add_argument("--allow-missing-end-date", action="store_true")
    parser.add_argument("--min-scoreable-sides", type=int, default=2)
    parser.add_argument("--default-quote-size", type=float, default=50.0)
    parser.add_argument("--max-quote-size", type=float, default=500.0)
    parser.add_argument("--max-history-rows-per-scan", type=int, default=50)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=600.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = MarketMakingScanConfig(
        output_dir=Path(args.output_dir),
        max_markets=int(args.max_markets),
        page_size=int(args.page_size),
        request_timeout_s=float(args.request_timeout_s),
        min_daily_rate=float(args.min_daily_rate),
        min_volume_24hr=float(args.min_volume_24hr),
        min_avg_spread=float(args.min_avg_spread),
        max_mid_pair_deviation=float(args.max_mid_pair_deviation),
        max_one_day_price_change=float(args.max_one_day_price_change),
        min_hours_to_end=float(args.min_hours_to_end),
        require_end_date=not bool(args.allow_missing_end_date),
        min_scoreable_sides=int(args.min_scoreable_sides),
        default_quote_size=float(args.default_quote_size),
        max_quote_size=float(args.max_quote_size),
        max_history_rows_per_scan=int(args.max_history_rows_per_scan),
    )
    if int(args.iterations) <= 1:
        print(json.dumps(scan_market_making_rewards(config), indent=2))
    else:
        run_periodic_market_making_scan(config, int(args.iterations), float(args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
