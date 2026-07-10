from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.external_fair_value import (  # noqa: E402
    ExternalFairValueConfig,
    run_periodic_external_fair_value_scan,
    scan_external_fair_value,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only external fair value candidate scanner. No auth, no orders."
    )
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "external_fair_value"))
    parser.add_argument("--max-markets", type=int, default=500)
    parser.add_argument("--market-page-size", type=int, default=100)
    parser.add_argument("--request-timeout-s", type=float, default=30.0)
    parser.add_argument("--max-book-tokens", type=int, default=900)
    parser.add_argument("--min-modelability-score", type=int, default=3)
    parser.add_argument("--min-volume-24hr", type=float, default=250.0)
    parser.add_argument("--max-yes-spread", type=float, default=0.12)
    parser.add_argument("--min-hours-to-end", type=float, default=0.5)
    parser.add_argument("--include-crypto", action="store_true")
    parser.add_argument("--skip-external-prices", action="store_true")
    parser.add_argument("--max-history-rows-per-scan", type=int, default=80)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=600.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = ExternalFairValueConfig(
        output_dir=Path(args.output_dir),
        max_markets=int(args.max_markets),
        market_page_size=int(args.market_page_size),
        request_timeout_s=float(args.request_timeout_s),
        max_book_tokens=int(args.max_book_tokens),
        min_modelability_score=int(args.min_modelability_score),
        min_volume_24hr=float(args.min_volume_24hr),
        max_yes_spread=float(args.max_yes_spread),
        min_hours_to_end=float(args.min_hours_to_end),
        include_crypto=bool(args.include_crypto),
        fetch_external_prices=not bool(args.skip_external_prices),
        max_history_rows_per_scan=int(args.max_history_rows_per_scan),
    )
    if int(args.iterations) <= 1:
        print(json.dumps(scan_external_fair_value(config), indent=2))
    else:
        run_periodic_external_fair_value_scan(config, int(args.iterations), float(args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
