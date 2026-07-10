from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market_making import MakerPaperSimConfig, simulate_market_making_history  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry research-only maker rewards paper simulator. No auth, no orders."
    )
    parser.add_argument(
        "--history-csv",
        default=str(ROOT / "data" / "market_making" / "candidate_history.csv"),
        help="candidate_history.csv produced by market_making_scanner.py",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "data" / "market_making_paper"),
    )
    parser.add_argument("--horizon-scans", type=int, default=1)
    parser.add_argument("--min-quote-score", type=float, default=0.0)
    parser.add_argument(
        "--quote-size-override",
        type=float,
        default=0.0,
        help="Optional fixed share size. Defaults to each candidate row quote_size.",
    )
    parser.add_argument("--max-events", type=int, default=100_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = MakerPaperSimConfig(
        history_csv=Path(args.history_csv),
        output_dir=Path(args.output_dir),
        horizon_scans=int(args.horizon_scans),
        min_quote_score=float(args.min_quote_score),
        quote_size_override=float(args.quote_size_override),
        max_events=int(args.max_events),
    )
    print(json.dumps(simulate_market_making_history(config), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
