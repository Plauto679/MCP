import argparse
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUTCOMES_CSV = ROOT / "data" / "fair_value_outcomes.csv"


def money(value: float) -> str:
    return f"${value:,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare fixed Fair Value stake PnL with shadow dynamic-stake PnL."
    )
    parser.add_argument("--since", help="Optional ISO timestamp lower bound, e.g. 2026-07-04T22:00")
    parser.add_argument("--csv", default=str(OUTCOMES_CSV), help="Path to fair_value_outcomes.csv")
    args = parser.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"Missing outcomes file: {path}")
        return 1

    df = pd.read_csv(path)
    if df.empty:
        print("No outcomes yet.")
        return 0

    if "event_ts_utc" in df.columns:
        df["event_ts_utc"] = pd.to_datetime(df["event_ts_utc"], errors="coerce", utc=True)
        if args.since:
            since = pd.to_datetime(args.since, errors="coerce", utc=True)
            if pd.notna(since):
                df = df[df["event_ts_utc"] >= since]

    df = df[df.get("result", "").isin(["WIN", "LOSS"])].copy()
    if df.empty:
        print("No settled WIN/LOSS outcomes in the selected range.")
        return 0

    for col in ("shadow_stake_usd", "shadow_pnl_usd"):
        if col not in df.columns:
            df[col] = pd.NA

    for col in ("stake_usd", "pnl_usd", "shadow_stake_usd", "shadow_pnl_usd"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    fixed_pnl = float(df["pnl_usd"].fillna(0).sum())
    shadow_pnl = float(df.get("shadow_pnl_usd", pd.Series(dtype=float)).fillna(0).sum())
    fixed_staked = float(df["stake_usd"].fillna(0).sum())
    shadow_staked = float(df.get("shadow_stake_usd", pd.Series(dtype=float)).fillna(0).sum())
    wins = int((df["result"] == "WIN").sum())
    losses = int((df["result"] == "LOSS").sum())

    print("Fair Value Fixed vs Shadow Dynamic Stake")
    print(f"Rows: {len(df)} | W/L: {wins}/{losses}")
    print(f"Fixed stake total:  {money(fixed_staked)} | PnL: {money(fixed_pnl)}")
    print(f"Shadow stake total: {money(shadow_staked)} | PnL: {money(shadow_pnl)}")
    print(f"Shadow - fixed PnL: {money(shadow_pnl - fixed_pnl)}")

    if "tactic" in df.columns:
        grouped = df.groupby("tactic", dropna=False).agg(
            entries=("result", "count"),
            wins=("result", lambda s: int((s == "WIN").sum())),
            fixed_pnl=("pnl_usd", "sum"),
            shadow_pnl=("shadow_pnl_usd", "sum"),
            fixed_staked=("stake_usd", "sum"),
            shadow_staked=("shadow_stake_usd", "sum"),
        )
        grouped["shadow_minus_fixed"] = grouped["shadow_pnl"] - grouped["fixed_pnl"]
        print("\nBy tactic")
        print(grouped.round(4).to_string())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
