from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.late_continuation_execution import (  # noqa: E402
    LateContinuationExecutionConfig,
    run_late_continuation_execution_scanner,
)


def _float_tuple(text: str) -> tuple[float, ...]:
    return tuple(float(item.strip()) for item in text.split(",") if item.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execution scanner for BTC 5m late-continuation hypotheses from market feature captures."
    )
    parser.add_argument("--feature-dir", default=str(ROOT / "data" / "market_features"))
    parser.add_argument(
        "--labels-csv",
        default=str(ROOT / "data" / "market_features_analysis" / "binance_5m_labels_20260706_capture.csv"),
    )
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "late_continuation_execution"))
    parser.add_argument("--entry-elapsed-s", default="210,225,240,255,270")
    parser.add_argument("--taker-delays-s", default="0,1,2,3,5")
    parser.add_argument("--max-prices", default="0.94,0.95,0.96,0.97,0.98,0.99")
    parser.add_argument("--min-mid-edges", default="0,0.05,0.10,0.20,0.30")
    parser.add_argument("--min-confirm-imbalances", default="0,1.0,1.5")
    parser.add_argument("--max-spreads", default="0.01,0.02,0.03,0.05")
    parser.add_argument("--min-top-shares", type=float, default=0.0)
    parser.add_argument("--require-uncrossed-books", action="store_true")
    parser.add_argument("--max-quote-age-ms", type=float, default=0.0)
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--min-train-attempts", type=int, default=25)
    parser.add_argument("--min-test-attempts", type=int, default=10)
    parser.add_argument("--min-train-windows", type=int, default=20)
    parser.add_argument("--write-candidates", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = LateContinuationExecutionConfig(
        feature_dir=Path(args.feature_dir),
        labels_csv=Path(args.labels_csv),
        output_dir=Path(args.output_dir),
        entry_elapsed_s=_float_tuple(args.entry_elapsed_s),
        taker_delays_s=_float_tuple(args.taker_delays_s),
        max_prices=_float_tuple(args.max_prices),
        min_mid_edges=_float_tuple(args.min_mid_edges),
        min_confirm_imbalances=_float_tuple(args.min_confirm_imbalances),
        max_spreads=_float_tuple(args.max_spreads),
        min_top_shares=max(float(args.min_top_shares), 0.0),
        require_uncrossed_books=bool(args.require_uncrossed_books),
        max_quote_age_ms=max(float(args.max_quote_age_ms), 0.0),
        fee_rate=float(args.fee_rate),
        train_fraction=float(args.train_fraction),
        min_train_attempts=int(args.min_train_attempts),
        min_test_attempts=int(args.min_test_attempts),
        min_train_windows=int(args.min_train_windows),
        write_candidates=bool(args.write_candidates),
    )
    report = run_late_continuation_execution_scanner(config)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
