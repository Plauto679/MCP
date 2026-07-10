from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.reward_fair_value_evaluator import (  # noqa: E402
    RewardFairValueEvaluatorConfig,
    run_periodic_reward_fair_value_evaluator,
    run_reward_fair_value_evaluator,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry research-only evaluator combining maker rewards, rebates, and external fair value."
    )
    parser.add_argument(
        "--maker-history-csv",
        default=str(ROOT / "data" / "market_making" / "candidate_history.csv"),
    )
    parser.add_argument(
        "--external-history-csv",
        default=str(ROOT / "data" / "external_fair_value" / "candidate_history.csv"),
    )
    parser.add_argument(
        "--maker-paper-events-csv",
        default=str(ROOT / "data" / "market_making_paper_final_2104" / "maker_paper_events.csv"),
    )
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "reward_fair_value_evaluator"))
    parser.add_argument("--interval-seconds", type=float, default=600.0)
    parser.add_argument("--quote-size-override", type=float, default=0.0)
    parser.add_argument("--default-fill-rate", type=float, default=0.02)
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument(
        "--max-snapshot-age-hours",
        type=float,
        default=12.0,
        help="Ignore latest historical rows older than this. Use 0 to disable.",
    )
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--loop-interval-seconds", type=float, default=900.0)
    parser.add_argument("--skip-wti-volatility-fetch", action="store_true")
    parser.add_argument("--wti-annual-volatility-fallback", type=float, default=0.35)
    parser.add_argument("--skip-fed-rates-context-fetch", action="store_true")
    parser.add_argument("--fed-current-effr-fallback", type=float, default=3.625)
    parser.add_argument(
        "--fed-manual-probabilities-csv",
        default=str(ROOT / "data" / "external_sources" / "fedwatch_probabilities.csv"),
    )
    parser.add_argument("--min-external-edge-abs", type=float, default=0.08)
    parser.add_argument("--min-maker-fair-value-edge-abs", type=float, default=0.03)
    parser.add_argument("--min-maker-pessimistic-ev-usd", type=float, default=0.25)
    parser.add_argument("--min-maker-history-observations", type=int, default=30)
    parser.add_argument("--max-maker-fill-rate", type=float, default=0.25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = RewardFairValueEvaluatorConfig(
        maker_history_csv=Path(args.maker_history_csv),
        external_history_csv=Path(args.external_history_csv),
        maker_paper_events_csv=Path(args.maker_paper_events_csv),
        output_dir=Path(args.output_dir),
        interval_seconds=float(args.interval_seconds),
        quote_size_override=float(args.quote_size_override),
        default_fill_rate=float(args.default_fill_rate),
        max_rows=int(args.max_rows),
        max_snapshot_age_hours=float(args.max_snapshot_age_hours),
        fetch_wti_volatility=not bool(args.skip_wti_volatility_fetch),
        wti_annual_volatility_fallback=float(args.wti_annual_volatility_fallback),
        fetch_fed_rates_context=not bool(args.skip_fed_rates_context_fetch),
        fed_current_effr_fallback=float(args.fed_current_effr_fallback),
        fed_manual_probabilities_csv=(
            Path(args.fed_manual_probabilities_csv)
            if str(args.fed_manual_probabilities_csv or "").strip()
            else None
        ),
        min_external_edge_abs=float(args.min_external_edge_abs),
        min_maker_fair_value_edge_abs=float(args.min_maker_fair_value_edge_abs),
        min_maker_pessimistic_ev_usd=float(args.min_maker_pessimistic_ev_usd),
        min_maker_history_observations=int(args.min_maker_history_observations),
        max_maker_fill_rate=float(args.max_maker_fill_rate),
    )
    if int(args.iterations) <= 1:
        print(json.dumps(run_reward_fair_value_evaluator(config), indent=2))
    else:
        run_periodic_reward_fair_value_evaluator(
            config,
            iterations=int(args.iterations),
            interval_seconds=float(args.loop_interval_seconds),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
