from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market_ws_recorder import MarketWsRecorder, RecorderConfig, fetch_market_tokens


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record Polymarket market websocket events for the active BTC 5m market. Research only; places no orders."
    )
    parser.add_argument("--slug", default="", help="Market slug. Defaults to current btc-updown-5m-{window_start}.")
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=300.0,
        help="Fixed recording duration for one run. Ignored with --continuous.",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Continuously rotate through active BTC 5m windows until stopped.",
    )
    parser.add_argument(
        "--max-windows",
        type=int,
        default=0,
        help="With --continuous, stop after this many windows. 0 means run until stopped.",
    )
    parser.add_argument(
        "--window-grace-seconds",
        type=float,
        default=3.0,
        help="With --continuous, keep recording a few seconds after the 5m boundary.",
    )
    parser.add_argument(
        "--retry-seconds",
        type=float,
        default=5.0,
        help="With --continuous, wait this long after a fetch/connect failure.",
    )
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "market_ws"))
    parser.add_argument("--summary-csv", default="", help="Optional fixed summary CSV path.")
    parser.add_argument("--raw-jsonl", default="", help="Optional fixed raw JSONL path.")
    parser.add_argument("--no-raw", action="store_true", help="Write only normalized summary CSV.")
    parser.add_argument(
        "--storage",
        choices=("sqlite", "csv", "both"),
        default="sqlite",
        help="Primary storage format. sqlite is compact and replayable; csv is a wide summary export.",
    )
    parser.add_argument("--sqlite-path", default="", help="Optional fixed SQLite path for a single non-continuous run.")
    parser.add_argument(
        "--summary-interval-ms",
        type=float,
        default=250.0,
        help="Minimum interval between persisted summary rows for fast CSV book updates. Use 0 for every update.",
    )
    parser.add_argument(
        "--no-gzip-summary",
        action="store_true",
        help="Write plain .summary.csv instead of compressed .summary.csv.gz for auto-named CSV summaries.",
    )
    parser.add_argument(
        "--sqlite-store-raw-json",
        action="store_true",
        help="Also store raw event JSON in SQLite. Larger, but useful for parser debugging.",
    )
    parser.add_argument(
        "--no-gzip-sqlite",
        action="store_true",
        help="Keep closed SQLite windows uncompressed. By default they are stored as .market.sqlite.gz.",
    )
    args = parser.parse_args()

    if args.continuous and args.slug:
        parser.error("--continuous records rotating active BTC 5m windows; do not pass --slug.")
    if args.continuous and (args.summary_csv or args.raw_jsonl):
        parser.error("--continuous creates one file per window; do not pass --summary-csv or --raw-jsonl.")
    if args.continuous and args.sqlite_path:
        parser.error("--continuous creates one SQLite file per window; do not pass --sqlite-path.")

    async def _run_once() -> dict:
        tokens = await fetch_market_tokens(args.slug or None)
        config = RecorderConfig(
            tokens=tokens,
            output_dir=Path(args.output_dir),
            duration_seconds=args.duration_seconds,
            raw_jsonl=Path(args.raw_jsonl) if args.raw_jsonl else None,
            summary_csv=Path(args.summary_csv) if args.summary_csv else None,
            write_raw=not args.no_raw,
            gzip_summary=not args.no_gzip_summary,
            summary_interval_ms=args.summary_interval_ms,
            storage=args.storage,
            sqlite_path=Path(args.sqlite_path) if args.sqlite_path else None,
            sqlite_store_raw_json=args.sqlite_store_raw_json,
            gzip_sqlite=not args.no_gzip_sqlite,
        )
        recorder = MarketWsRecorder(config)
        return await recorder.run()

    async def _run_continuous() -> list[dict]:
        reports: list[dict] = []
        completed = 0
        while args.max_windows <= 0 or completed < args.max_windows:
            try:
                tokens = await fetch_market_tokens(None)
                now_ts = time.time()
                window_end_ts = float(tokens.window_start_ts or now_ts) + 300.0 + max(float(args.window_grace_seconds), 0.0)
                duration = max(window_end_ts - now_ts, 1.0)
                config = RecorderConfig(
                    tokens=tokens,
                    output_dir=Path(args.output_dir),
                    duration_seconds=duration,
                    write_raw=not args.no_raw,
                    gzip_summary=not args.no_gzip_summary,
                    summary_interval_ms=args.summary_interval_ms,
                    storage=args.storage,
                    sqlite_store_raw_json=args.sqlite_store_raw_json,
                    gzip_sqlite=not args.no_gzip_sqlite,
                )
                print(
                    json.dumps({
                        "event": "window_start",
                        "slug": tokens.slug,
                        "duration_seconds": round(duration, 3),
                        "token_ids": tokens.token_ids,
                        "write_raw": not args.no_raw,
                        "storage": args.storage,
                        "gzip_summary": not args.no_gzip_summary,
                        "summary_interval_ms": float(args.summary_interval_ms),
                        "gzip_sqlite": not args.no_gzip_sqlite,
                    }),
                    flush=True,
                )
                report = await MarketWsRecorder(config).run()
                reports.append(report)
                completed += 1
                print(json.dumps({"event": "window_complete", **report}), flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    json.dumps({
                        "event": "recorder_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }),
                    flush=True,
                )
                await asyncio.sleep(max(float(args.retry_seconds), 1.0))
        return reports

    report = asyncio.run(_run_continuous() if args.continuous else _run_once())
    if not args.continuous:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
