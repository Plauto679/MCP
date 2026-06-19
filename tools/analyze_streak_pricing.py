import argparse
import csv
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


ENTRY_RE = re.compile(
    r"^(?P<ts>\S+) .*?ENTRY \| slug=(?P<slug>btc-updown-5m-(?P<market_ts>\d+)) "
    r"\| direction=(?P<direction>Yes|No) \| usd=\$(?P<usd>[0-9.]+) "
    r"\| px=(?P<px>[0-9.]+).*?loss_bank=\$(?P<loss_bank>[0-9.]+).*?"
    r"streak=(?P<streak>\d+) \| elapsed=(?P<elapsed>[0-9.]+)s"
)
SETTLEMENT_RE = re.compile(
    r"^(?P<ts>\S+) .*?(?P<result>WIN|LOSS) settled for "
    r"(?P<slug>btc-updown-5m-(?P<market_ts>\d+))"
)
SKIP_RE = re.compile(
    r"^(?P<ts>\S+) .*?Skipping (?P<slug>btc-updown-5m-(?P<market_ts>\d+)): (?P<reason>.*)"
)
PRICE_SKIP_DIRECTION_RE = re.compile(r"(?P<direction>Yes|No) price (?P<price>[0-9.]+) outside")
DUAL_SKIP_RE = re.compile(r"Yes=(?P<yes>[0-9.]+), No=(?P<no>[0-9.]+)")


def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_dt(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ask_for(row, direction: str) -> float:
    key = "yes_best_ask" if direction == "Yes" else "no_best_ask"
    return parse_float(row.get(key))


def bid_for(row, direction: str) -> float:
    key = "yes_best_bid" if direction == "Yes" else "no_best_bid"
    return parse_float(row.get(key))


def load_runtime(path: Path, since: datetime | None):
    entries = []
    settlements = {}
    skips = []
    if not path.exists():
        return entries, settlements, skips

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = ENTRY_RE.search(line)
        if match:
            data = match.groupdict()
            ts = parse_dt(data["ts"])
            if since and ts and ts < since:
                continue
            entries.append(
                {
                    "ts": ts,
                    "slug": data["slug"],
                    "market_ts": int(data["market_ts"]),
                    "direction": data["direction"],
                    "usd": parse_float(data["usd"]),
                    "px": parse_float(data["px"]),
                    "loss_bank": parse_float(data["loss_bank"]),
                    "streak": int(data["streak"]),
                    "elapsed": parse_float(data["elapsed"]),
                }
            )
            continue

        match = SETTLEMENT_RE.search(line)
        if match:
            data = match.groupdict()
            ts = parse_dt(data["ts"])
            if since and ts and ts < since:
                continue
            settlements[data["slug"]] = {"ts": ts, "result": data["result"]}
            continue

        match = SKIP_RE.search(line)
        if match:
            data = match.groupdict()
            ts = parse_dt(data["ts"])
            if since and ts and ts < since:
                continue
            direction = None
            price = None
            price_match = PRICE_SKIP_DIRECTION_RE.search(data["reason"])
            if price_match:
                direction = price_match.group("direction")
                price = parse_float(price_match.group("price"))
            skips.append(
                {
                    "ts": ts,
                    "slug": data["slug"],
                    "market_ts": int(data["market_ts"]),
                    "reason": data["reason"],
                    "direction": direction,
                    "price": price,
                }
            )
    return entries, settlements, skips


def load_samples(path: Path, since: datetime | None):
    samples = defaultdict(list)
    if not path.exists():
        return samples

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ts = parse_dt(row.get("sample_ts_utc", ""))
            if since and ts and ts < since:
                continue
            slug = row.get("slug")
            if not slug:
                continue
            row["_ts"] = ts
            row["_elapsed"] = parse_float(row.get("elapsed_s"))
            samples[slug].append(row)
    for rows in samples.values():
        rows.sort(key=lambda item: item["_elapsed"])
    return samples


def first_in_range(rows, direction, low, high):
    for row in rows:
        ask = ask_for(row, direction)
        if ask and low <= ask <= high:
            return row["_elapsed"], ask
    return None


def sample_stats(rows, direction):
    if not rows:
        return {}
    first = next((row for row in rows if ask_for(row, direction) > 0), None)
    first_15 = [ask_for(row, direction) for row in rows if row["_elapsed"] <= 15 and ask_for(row, direction) > 0]
    first_30 = [ask_for(row, direction) for row in rows if row["_elapsed"] <= 30 and ask_for(row, direction) > 0]
    return {
        "first_elapsed": first["_elapsed"] if first else None,
        "first_ask": ask_for(first, direction) if first else None,
        "first_bid": bid_for(first, direction) if first else None,
        "avg_15": statistics.mean(first_15) if first_15 else None,
        "min_15": min(first_15) if first_15 else None,
        "max_15": max(first_15) if first_15 else None,
        "avg_30": statistics.mean(first_30) if first_30 else None,
        "first_in_40_60": first_in_range(rows, direction, 0.40, 0.60),
        "first_in_45_55": first_in_range(rows, direction, 0.45, 0.55),
        "sample_count": len(rows),
    }


def build_windows(entries, settlements, skips, samples):
    by_key = {}
    for entry in entries:
        key = (entry["slug"], entry["direction"])
        item = by_key.setdefault(
            key,
            {
                "slug": entry["slug"],
                "market_ts": entry["market_ts"],
                "direction": entry["direction"],
                "entries": [],
                "skips": [],
                "settlement": settlements.get(entry["slug"]),
            },
        )
        item["entries"].append(entry)

    for skip in skips:
        if skip["direction"]:
            key = (skip["slug"], skip["direction"])
            item = by_key.setdefault(
                key,
                {
                    "slug": skip["slug"],
                    "market_ts": skip["market_ts"],
                    "direction": skip["direction"],
                    "entries": [],
                    "skips": [],
                    "settlement": settlements.get(skip["slug"]),
                },
            )
            item["skips"].append(skip)
        else:
            prices = DUAL_SKIP_RE.search(skip["reason"])
            if prices:
                for direction in ("Yes", "No"):
                    key = (skip["slug"], direction)
                    item = by_key.setdefault(
                        key,
                        {
                            "slug": skip["slug"],
                            "market_ts": skip["market_ts"],
                            "direction": direction,
                            "entries": [],
                            "skips": [],
                            "settlement": settlements.get(skip["slug"]),
                        },
                    )
                    item["skips"].append(skip)

    windows = []
    for item in by_key.values():
        entries_for_window = item["entries"]
        item["max_usd"] = max((entry["usd"] for entry in entries_for_window), default=0.0)
        item["first_entry_elapsed"] = min((entry["elapsed"] for entry in entries_for_window), default=None)
        item["last_entry_px"] = entries_for_window[-1]["px"] if entries_for_window else None
        item["streak"] = max((entry["streak"] for entry in entries_for_window), default=0)
        item["loss_bank"] = max((entry["loss_bank"] for entry in entries_for_window), default=0.0)
        item["sample_stats"] = sample_stats(samples.get(item["slug"], []), item["direction"])
        windows.append(item)
    windows.sort(key=lambda item: item["market_ts"])
    return windows


def money(value):
    return f"${value:.2f}"


def price(value):
    return "n/a" if value is None else f"{value:.3f}"


def elapsed_pair(value):
    if not value:
        return "no"
    elapsed, ask = value
    return f"{ask:.2f} en {elapsed:.1f}s"


def render_report(windows, since):
    lines = []
    lines.append("# Streak Pricing Report")
    if since:
        lines.append(f"Periodo: desde {since.isoformat()}")
    lines.append("")

    if not windows:
        lines.append("No hay ventanas analizables en el periodo.")
        return "\n".join(lines)

    with_samples = [w for w in windows if w["sample_stats"].get("first_ask") is not None]
    recovery = [w for w in with_samples if w["streak"] >= 1 or w["loss_bank"] > 0]
    expensive = [w for w in with_samples if (w["sample_stats"].get("first_ask") or 0) > 0.60]
    cheap = [w for w in with_samples if 0 < (w["sample_stats"].get("first_ask") or 0) < 0.40]
    missed = [w for w in windows if w["skips"] and not w["entries"]]

    lines.append("## Resumen")
    lines.append(f"- Ventanas con datos: {len(windows)}")
    lines.append(f"- Ventanas con orderbook utilizable: {len(with_samples)}")
    lines.append(f"- Ventanas de recuperación: {len(recovery)}")
    lines.append(f"- Dirección objetivo cara al inicio (>0.60): {len(expensive)}")
    lines.append(f"- Dirección objetivo barata al inicio (<0.40): {len(cheap)}")
    lines.append(f"- Ventanas con skip y sin entrada detectada: {len(missed)}")
    lines.append("")

    lines.append("## Por streak")
    by_streak = defaultdict(list)
    for window in with_samples:
        by_streak[window["streak"]].append(window)
    for streak in sorted(by_streak):
        group = by_streak[streak]
        asks = [w["sample_stats"]["first_ask"] for w in group if w["sample_stats"].get("first_ask")]
        if not asks:
            continue
        out = [ask for ask in asks if ask < 0.40 or ask > 0.60]
        high = [ask for ask in asks if ask > 0.60]
        avg = statistics.mean(asks)
        lines.append(
            f"- streak={streak}: n={len(asks)}, ask inicial medio={avg:.3f}, "
            f"fuera 0.40-0.60={len(out)}, >0.60={len(high)}"
        )
    lines.append("")

    lines.append("## Casos críticos")
    critical = [
        w for w in with_samples
        if w["streak"] >= 2
        or w["loss_bank"] >= 5
        or w["max_usd"] >= 10
        or (w["sample_stats"].get("first_ask") or 0) > 0.60
        or w["skips"]
    ]
    for window in critical[-30:]:
        stats = window["sample_stats"]
        result = window["settlement"]["result"] if window.get("settlement") else "PENDING/UNKNOWN"
        lines.append(
            f"- {window['slug']} {window['direction']} | streak={window['streak']} | "
            f"loss_bank={money(window['loss_bank'])} | max_stake={money(window['max_usd'])} | "
            f"ask_ini={price(stats.get('first_ask'))} | avg15={price(stats.get('avg_15'))} | "
            f"rango40-60={elapsed_pair(stats.get('first_in_40_60'))} | "
            f"entrada={window['first_entry_elapsed']}s px={price(window['last_entry_px'])} | "
            f"resultado={result}"
        )
        for skip in window["skips"][:2]:
            lines.append(f"  skip: {skip['reason'][:180]}")
    lines.append("")

    lines.append("## Lectura")
    lines.append(
        "- Si una recuperación aparece con ask inicial >0.60, la dirección que necesitamos seguir ya abrió cara."
    )
    lines.append(
        "- Si `rango40-60=no`, esa ventana no volvió a una zona razonable mientras tuvimos muestras."
    )
    lines.append(
        "- Si vuelve al rango muy tarde, la entrada puede funcionar, pero la probabilidad ya está más condicionada."
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Analyze martingale target-side pricing after streaks.")
    parser.add_argument("--since-hours", type=float, default=None, help="Only include recent data.")
    parser.add_argument("--output", default=None, help="Optional markdown output path.")
    args = parser.parse_args()

    root = project_root()
    since = None
    if args.since_hours:
        since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)

    entries, settlements, skips = load_runtime(root / "data" / "runtime.log", since)
    samples = load_samples(root / "data" / "martingale_maker_orderbook_samples.csv", since)
    windows = build_windows(entries, settlements, skips, samples)
    report = render_report(windows, since)

    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = root / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(f"Wrote {output_path}")
    else:
        print(report)


if __name__ == "__main__":
    main()
