from __future__ import annotations

import csv
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd


LOCAL_TZ = ZoneInfo("Europe/Madrid")
ROOT_DIR = Path(__file__).resolve().parents[1]
PERFORMANCE_FILE = ROOT_DIR / "historical_performance.xlsx"
RUNTIME_LOG_FILE = ROOT_DIR / "data" / "runtime.log"
LEDGER_FILE = ROOT_DIR / "data" / "capital_ledger.csv"


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _money(value: Any) -> float:
    number = _finite_float(value)
    return round(number or 0.0, 4)


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y"}


def _read_excel_snapshot(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()

    last_error: Optional[Exception] = None
    for _ in range(3):
        try:
            return pd.read_excel(path)
        except Exception as exc:  # The bot can be writing the file at the same time.
            last_error = exc
            time.sleep(0.08)
    raise RuntimeError(f"Could not read performance workbook: {last_error}")


def _prepare_trades(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    prepared = df.copy()
    for col in [
        "Market timestamp (UTC)",
        "Position opened timestamp (UTC)",
        "Position closed timestamp (UTC)",
    ]:
        if col in prepared.columns:
            prepared[col + "_dt"] = pd.to_datetime(prepared[col], errors="coerce", utc=True)

    if "Position opened timestamp (UTC)_dt" in prepared.columns:
        opened = prepared["Position opened timestamp (UTC)_dt"]
    else:
        opened = pd.Series(pd.NaT, index=prepared.index)
    prepared["_opened_local"] = opened.dt.tz_convert(LOCAL_TZ)
    prepared["_opened_date"] = prepared["_opened_local"].dt.date

    if "Market timestamp (UTC)_dt" in prepared.columns:
        market = prepared["Market timestamp (UTC)_dt"]
        prepared["_entry_latency_seconds"] = (opened - market).dt.total_seconds()
    else:
        prepared["_entry_latency_seconds"] = pd.NA

    for col in [
        "Entry Notional USD",
        "Payout USD (estimate)",
        "PnL USD (estimate)",
        "Entry Fee USD (estimate)",
        "Entry Price",
        "Shares",
        "Streak_Level",
    ]:
        if col in prepared.columns:
            prepared[col] = pd.to_numeric(prepared[col], errors="coerce")

    if "Dry_Run" in prepared.columns:
        prepared["_dry_run"] = prepared["Dry_Run"].map(_boolish)
    else:
        prepared["_dry_run"] = False

    if "Result" not in prepared.columns:
        prepared["Result"] = ""
    if "Entry Route" not in prepared.columns:
        prepared["Entry Route"] = ""

    return prepared


def _max_loss_streak(results: Iterable[Any]) -> int:
    longest = 0
    current = 0
    for raw in results:
        result = str(raw or "").upper()
        if result == "LOSS":
            current += 1
            longest = max(longest, current)
        elif result == "WIN":
            current = 0
    return longest


def _current_loss_streak(results: Iterable[Any]) -> int:
    current = 0
    for raw in reversed(list(results)):
        result = str(raw or "").upper()
        if result == "LOSS":
            current += 1
        elif result == "WIN":
            break
    return current


def _safe_percent(numerator: float, denominator: float) -> Optional[float]:
    if not denominator:
        return None
    return round((numerator / denominator) * 100.0, 2)


def _summarize_trades(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "pnl": 0.0,
            "notional": 0.0,
            "fees": 0.0,
            "payout": 0.0,
            "max_stake": 0.0,
            "max_loss_streak": 0,
            "current_loss_streak": 0,
            "avg_entry_price": None,
            "avg_entry_latency_seconds": None,
            "late_entries_15s": 0,
            "late_entries_60s": 0,
            "routes": {},
            "maker_trades": 0,
            "maker_notional": 0.0,
            "largest_trade": None,
        }

    closed = df[df["Result"].astype(str).str.upper().isin(["WIN", "LOSS"])].copy()
    if closed.empty:
        return _summarize_trades(pd.DataFrame())

    wins = int((closed["Result"].astype(str).str.upper() == "WIN").sum())
    losses = int((closed["Result"].astype(str).str.upper() == "LOSS").sum())
    routes = (
        closed["Entry Route"]
        .fillna("unknown")
        .replace("", "unknown")
        .astype(str)
        .value_counts()
        .to_dict()
    )
    maker_mask = closed["Entry Route"].fillna("").astype(str).str.contains("maker", case=False)

    largest_trade = None
    if "Entry Notional USD" in closed.columns and closed["Entry Notional USD"].notna().any():
        row = closed.loc[closed["Entry Notional USD"].idxmax()]
        largest_trade = {
            "market": str(row.get("Market", "")),
            "opened_local": str(row.get("Position opened timestamp (local)", "")),
            "direction": str(row.get("Direction", "")),
            "result": str(row.get("Result", "")),
            "stake": _money(row.get("Entry Notional USD")),
            "entry_price": _finite_float(row.get("Entry Price")),
            "pnl": _money(row.get("PnL USD (estimate)")),
            "route": str(row.get("Entry Route", "") or "unknown"),
            "streak": int(_finite_float(row.get("Streak_Level")) or 0),
        }

    latency = pd.to_numeric(closed.get("_entry_latency_seconds"), errors="coerce")
    return {
        "trades": int(len(closed)),
        "wins": wins,
        "losses": losses,
        "win_rate": _safe_percent(wins, len(closed)),
        "pnl": _money(closed.get("PnL USD (estimate)", pd.Series(dtype=float)).sum()),
        "notional": _money(closed.get("Entry Notional USD", pd.Series(dtype=float)).sum()),
        "fees": _money(closed.get("Entry Fee USD (estimate)", pd.Series(dtype=float)).sum()),
        "payout": _money(closed.get("Payout USD (estimate)", pd.Series(dtype=float)).sum()),
        "max_stake": _money(closed.get("Entry Notional USD", pd.Series(dtype=float)).max()),
        "max_loss_streak": _max_loss_streak(closed["Result"].tolist()),
        "current_loss_streak": _current_loss_streak(closed["Result"].tolist()),
        "avg_entry_price": _finite_float(closed.get("Entry Price", pd.Series(dtype=float)).mean()),
        "avg_entry_latency_seconds": _finite_float(latency.mean()),
        "late_entries_15s": int((latency > 15).sum()) if not latency.empty else 0,
        "late_entries_60s": int((latency > 60).sum()) if not latency.empty else 0,
        "routes": routes,
        "maker_trades": int(maker_mask.sum()),
        "maker_notional": _money(closed.loc[maker_mask, "Entry Notional USD"].sum()),
        "largest_trade": largest_trade,
    }


def _last_trades(df: pd.DataFrame, limit: int = 10) -> list[Dict[str, Any]]:
    if df.empty:
        return []
    rows = df.sort_values("Position opened timestamp (UTC)_dt").tail(limit)
    output = []
    for _, row in rows.iterrows():
        output.append(
            {
                "market_local": str(row.get("Market timestamp (local)", "")),
                "opened_local": str(row.get("Position opened timestamp (local)", "")),
                "direction": str(row.get("Direction", "")),
                "result": str(row.get("Result", "")),
                "stake": _money(row.get("Entry Notional USD")),
                "entry_price": _finite_float(row.get("Entry Price")),
                "shares": _finite_float(row.get("Shares")),
                "pnl": _money(row.get("PnL USD (estimate)")),
                "route": str(row.get("Entry Route", "") or "unknown"),
                "streak": int(_finite_float(row.get("Streak_Level")) or 0),
            }
        )
    return output


def _read_capital_ledger(path: Path = LEDGER_FILE) -> Dict[str, Any]:
    if not path.exists():
        return {
            "configured": False,
            "path": str(path),
            "net_contributions": None,
            "deposits": 0.0,
            "withdrawals": 0.0,
            "rows": [],
            "all_rows": [],
        }

    deposits = 0.0
    withdrawals = 0.0
    rows: list[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            amount = _money(raw.get("amount_usd"))
            kind = str(raw.get("type", "")).strip().lower()
            if kind in {"deposit", "contribution", "aportacion", "aporte"}:
                deposits += amount
                signed = amount
            elif kind in {"withdrawal", "withdraw", "retirada"}:
                withdrawals += amount
                signed = -amount
            else:
                signed = amount
            rows.append(
                {
                    "date": raw.get("date", ""),
                    "type": kind or "unknown",
                    "amount": amount,
                    "signed_amount": signed,
                    "note": raw.get("note", ""),
                }
            )

    return {
        "configured": True,
        "path": str(path),
        "net_contributions": _money(deposits - withdrawals),
        "deposits": _money(deposits),
        "withdrawals": _money(withdrawals),
        "rows": rows[-12:],
        "all_rows": rows,
    }


def _read_runtime_balance(path: Path = RUNTIME_LOG_FILE) -> Dict[str, Any]:
    if not path.exists():
        return {"last_balance": None, "last_floor": None, "last_timestamp": None}

    balance_re = re.compile(
        r"^(?P<ts>\S+).*?(?:Cycle balance anchor set at|Balance guard OK \| balance=) "
        r"\$(?P<balance>[0-9]+(?:\.[0-9]+)?)"
    )
    floor_re = re.compile(r"floor=\$(?P<floor>[0-9]+(?:\.[0-9]+)?)")
    last_balance = None
    last_floor = None
    last_timestamp = None

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            match = balance_re.search(line)
            if match:
                last_balance = _money(match.group("balance"))
                last_timestamp = match.group("ts")
                floor_match = floor_re.search(line)
                if floor_match:
                    last_floor = _money(floor_match.group("floor"))

    return {
        "last_balance": last_balance,
        "last_floor": last_floor,
        "last_timestamp": last_timestamp,
    }


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TZ)


def _parse_ledger_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    return _parse_iso_timestamp(text)


def _read_runtime_balance_series(path: Path = RUNTIME_LOG_FILE, limit: int = 500) -> list[Dict[str, Any]]:
    if not path.exists():
        return []

    balance_re = re.compile(
        r"^(?P<ts>\S+).*?(?:Cycle balance anchor set at|Balance guard OK \| balance=) "
        r"\$(?P<balance>[0-9]+(?:\.[0-9]+)?)"
    )
    points: list[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            match = balance_re.search(line)
            if not match:
                continue
            timestamp = _parse_iso_timestamp(match.group("ts"))
            if timestamp is None:
                continue
            points.append(
                {
                    "ts": timestamp.isoformat(),
                    "value": _money(match.group("balance")),
                }
            )
    return points[-limit:]


def _contribution_series_for_points(
    ledger: Dict[str, Any], portfolio_points: list[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    if not ledger.get("configured") or not portfolio_points:
        return []

    events = []
    for row in ledger.get("all_rows", []):
        timestamp = _parse_ledger_date(row.get("date"))
        if timestamp is None:
            continue
        events.append((timestamp, _money(row.get("signed_amount"))))
    events.sort(key=lambda item: item[0])

    series = []
    cumulative = 0.0
    index = 0
    for point in portfolio_points:
        timestamp = _parse_iso_timestamp(point.get("ts"))
        if timestamp is None:
            continue
        while index < len(events) and events[index][0] <= timestamp:
            cumulative += events[index][1]
            index += 1
        series.append({"ts": point["ts"], "value": _money(cumulative)})
    return series


def _active_state(trader: Any) -> Dict[str, Any]:
    app_state = getattr(trader, "app_state", {}) if trader else {}
    strategy_mode = getattr(trader, "strategy_mode", None) if trader else None
    if strategy_mode == "fair_value":
        state = app_state.get("fair_value_state", {})
        entries = state.get("entries") if isinstance(state.get("entries"), list) else []
        open_count = len(entries)
        current_amount = sum(_money(entry.get("stake_usd")) for entry in entries) if entries else _money(state.get("stake_usd"))
        latest = entries[-1] if entries else state
        return {
            "running": bool(getattr(trader, "running", False)) if trader else False,
            "strategy_mode": strategy_mode,
            "dry_run": bool(getattr(trader, "dry_run", False)) if trader else False,
            "active_slug": state.get("active_slug"),
            "entry_done": bool(entries or state.get("entry_done", False)),
            "open_entry_count": open_count,
            "direction": latest.get("direction"),
            "streak": 0,
            "current_amount": current_amount,
            "entry_price": _finite_float(latest.get("entry_price")),
            "entry_shares": _finite_float(latest.get("entry_shares")),
            "loss_bank": 0.0,
            "entry_route": latest.get("entry_route"),
            "tactic": latest.get("tactic"),
            "maker_price": None,
            "opened_at": latest.get("opened_at"),
            "fair_probability": _finite_float(latest.get("fair_probability")),
            "edge_probability": _finite_float(latest.get("edge_probability")),
            "ev_per_usd": _finite_float(latest.get("ev_per_usd")),
        }

    state = app_state.get("martingale_state", {}) if trader else {}
    return {
        "running": bool(getattr(trader, "running", False)) if trader else False,
        "strategy_mode": strategy_mode,
        "dry_run": bool(getattr(trader, "dry_run", False)) if trader else False,
        "active_slug": state.get("active_slug"),
        "entry_done": bool(state.get("entry_done", False)),
        "direction": state.get("direction"),
        "streak": int(_finite_float(state.get("streak")) or 0),
        "current_amount": _money(state.get("current_amount")),
        "entry_price": _finite_float(state.get("entry_price")),
        "entry_shares": _finite_float(state.get("entry_shares")),
        "loss_bank": _money(state.get("loss_bank")),
        "entry_route": state.get("entry_route"),
        "maker_price": _finite_float(state.get("maker_price")),
        "opened_at": state.get("opened_at"),
    }


def build_performance_snapshot(trader: Any = None) -> Dict[str, Any]:
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    workbook_error = None

    try:
        df = _prepare_trades(_read_excel_snapshot(PERFORMANCE_FILE))
    except Exception as exc:
        df = pd.DataFrame()
        workbook_error = str(exc)

    production = df[~df.get("_dry_run", pd.Series(False, index=df.index))].copy() if not df.empty else df
    today_df = production[production.get("_opened_date") == today].copy() if not production.empty else production

    runtime = _read_runtime_balance()
    ledger = _read_capital_ledger()
    portfolio_series = _read_runtime_balance_series()
    contribution_series = _contribution_series_for_points(ledger, portfolio_series)
    total_summary = _summarize_trades(production)
    today_summary = _summarize_trades(today_df)

    trading_profit_from_balance = None
    if ledger["configured"] and runtime["last_balance"] is not None:
        trading_profit_from_balance = _money(runtime["last_balance"] - ledger["net_contributions"])

    return {
        "generated_at": now.isoformat(),
        "workbook_error": workbook_error,
        "portfolio": {
            "last_reported_balance": runtime["last_balance"],
            "last_reported_balance_at": runtime["last_timestamp"],
            "guardrail_floor": runtime["last_floor"],
            "net_contributions": ledger["net_contributions"],
            "contributions_configured": ledger["configured"],
            "trading_profit_from_balance": trading_profit_from_balance,
            "closed_trading_pnl_total": total_summary["pnl"],
            "ledger_path": ledger["path"],
        },
        "today": today_summary,
        "total": total_summary,
        "active": _active_state(trader),
        "recent_trades": _last_trades(production, limit=12),
        "capital_ledger": ledger,
        "chart": {
            "portfolio": portfolio_series,
            "contributions": contribution_series,
        },
    }
