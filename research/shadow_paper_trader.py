from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shadow_paper_trader import (  # noqa: E402
    DEFAULT_ALLOWED_NEW_ORDER_FAMILIES,
    DEFAULT_BLOCKED_NEW_ORDER_TEXT_TERMS,
    ShadowPaperTraderConfig,
    run_shadow_paper_trader,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry ledger for selected reward/fair-value paper maker quotes. No auth, no orders."
    )
    parser.add_argument(
        "--shadow-actions-csv",
        default=str(ROOT / "data" / "reward_fair_value_evaluator_live" / "shadow_actions.csv"),
    )
    parser.add_argument(
        "--maker-history-csv",
        default=str(ROOT / "data" / "market_making" / "candidate_history.csv"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "data" / "shadow_paper_trader_live"),
    )
    parser.add_argument("--quote-size-override", type=float, default=0.0)
    parser.add_argument("--directional-size-override", type=float, default=50.0)
    parser.add_argument("--directional-entry-mode", choices=["maker", "taker"], default="maker")
    parser.add_argument("--min-reward-score", type=float, default=0.0)
    parser.add_argument("--include-asks", action="store_true")
    parser.add_argument("--max-new-markets-per-cycle", type=int, default=20)
    parser.add_argument("--disable-active-exit", action="store_true")
    parser.add_argument("--take-profit-per-share", type=float, default=0.03)
    parser.add_argument("--stop-loss-per-share", type=float, default=0.05)
    parser.add_argument("--max-position-cycles", type=int, default=72)
    parser.add_argument("--close-on-signal-loss", action="store_true")
    parser.add_argument(
        "--allowed-new-order-families",
        default=",".join(DEFAULT_ALLOWED_NEW_ORDER_FAMILIES),
        help="Comma-separated families allowed for new paper entries. Existing positions are still managed.",
    )
    parser.add_argument(
        "--blocked-new-order-text-terms",
        default=",".join(DEFAULT_BLOCKED_NEW_ORDER_TEXT_TERMS),
        help="Comma-separated text terms that block new paper entries.",
    )
    parser.add_argument("--min-new-order-hours-to-end", type=float, default=6.0)
    parser.add_argument("--allow-missing-new-order-end-date", action="store_true")
    parser.add_argument("--new-order-cooldown-hours", type=float, default=12.0)
    parser.add_argument("--max-new-directional-markets-per-cycle", type=int, default=3)
    parser.add_argument("--max-open-quote-wall-hours", type=float, default=2.0)
    parser.add_argument("--max-position-without-update-hours", type=float, default=24.0)
    parser.add_argument("--stale-end-grace-hours", type=float, default=2.0)
    parser.add_argument("--disable-stale-open-quote-cancel", action="store_true")
    parser.add_argument("--disable-stale-position-close", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = ShadowPaperTraderConfig(
        shadow_actions_csv=Path(args.shadow_actions_csv),
        maker_history_csv=Path(args.maker_history_csv),
        output_dir=Path(args.output_dir),
        quote_size_override=float(args.quote_size_override),
        directional_size_override=float(args.directional_size_override),
        directional_entry_mode=str(args.directional_entry_mode),
        min_reward_score=float(args.min_reward_score),
        include_asks=bool(args.include_asks),
        max_new_markets_per_cycle=int(args.max_new_markets_per_cycle),
        active_exit_enabled=not bool(args.disable_active_exit),
        take_profit_per_share=float(args.take_profit_per_share),
        stop_loss_per_share=float(args.stop_loss_per_share),
        max_position_cycles=int(args.max_position_cycles),
        close_on_signal_loss=bool(args.close_on_signal_loss),
        allowed_new_order_families=str(args.allowed_new_order_families),
        blocked_new_order_text_terms=str(args.blocked_new_order_text_terms),
        min_new_order_hours_to_end=float(args.min_new_order_hours_to_end),
        require_new_order_hours_to_end=not bool(args.allow_missing_new_order_end_date),
        new_order_cooldown_hours=float(args.new_order_cooldown_hours),
        max_new_directional_markets_per_cycle=int(args.max_new_directional_markets_per_cycle),
        cancel_stale_open_quotes=not bool(args.disable_stale_open_quote_cancel),
        max_open_quote_wall_hours=float(args.max_open_quote_wall_hours),
        close_stale_positions=not bool(args.disable_stale_position_close),
        max_position_without_update_hours=float(args.max_position_without_update_hours),
        stale_end_grace_hours=float(args.stale_end_grace_hours),
    )
    print(json.dumps(run_shadow_paper_trader(config), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
