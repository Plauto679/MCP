from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import math
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ELAPSED_BUCKETS = (
    ("0-60", 0.0, 60.0),
    ("60-180", 60.0, 180.0),
    ("180-240", 180.0, 240.0),
    ("240-285", 240.0, 285.0),
    ("285-305", 285.0, 305.0),
)


@dataclass(frozen=True)
class MarketFeatureAnalysisConfig:
    input_dir: Path
    output_dir: Path
    labels_csv: Path | None = None
    maker_thresholds: tuple[float, ...] = (0.98, 0.97, 0.95)
    taker_thresholds: tuple[float, ...] = (1.0, 0.995, 0.99)
    imbalance_thresholds: tuple[float, ...] = (0.5, 1.0, 1.5)
    future_horizons_s: tuple[float, ...] = (1.0, 3.0, 5.0, 10.0, 20.0)
    segment_gap_s: float = 1.25
    markov_horizon_s: float = 5.0
    markov_sample_step_s: float = 5.0
    markov_train_fraction: float = 0.70
    markov_min_train_windows: int = 5


@dataclass(frozen=True)
class LoadedFeatureRows:
    rows: list[dict[str, Any]]
    files_read: int
    files_skipped: int
    skipped_files: tuple[str, ...]
    total_size_bytes: int


def read_feature_rows(input_dir: Path) -> LoadedFeatureRows:
    paths = feature_paths(input_dir)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    files_read = 0
    skipped: list[str] = []
    total_size = 0

    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            skipped.append(str(path))
            continue
        total_size += size
        if size <= 0:
            skipped.append(str(path))
            continue
        try:
            with _open_feature_file(path) as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    skipped.append(str(path))
                    continue
                for row in reader:
                    slug = str(row.get("slug") or "")
                    sample_ts = str(row.get("sample_ts_utc") or "")
                    key = (slug, sample_ts)
                    if not slug or not sample_ts or key in seen:
                        continue
                    seen.add(key)
                    rows.append(dict(row))
            files_read += 1
        except (EOFError, OSError, gzip.BadGzipFile, UnicodeDecodeError, csv.Error):
            # The active recorder can leave the current gzip member temporarily
            # unreadable. Skipping it keeps analysis safe while collection runs.
            skipped.append(str(path))

    rows.sort(key=lambda row: (str(row.get("slug") or ""), str(row.get("sample_ts_utc") or "")))
    return LoadedFeatureRows(
        rows=rows,
        files_read=files_read,
        files_skipped=len(skipped),
        skipped_files=tuple(skipped),
        total_size_bytes=total_size,
    )


def feature_paths(input_dir: Path) -> list[Path]:
    if input_dir.is_file():
        return [input_dir]
    return sorted(
        [
            *input_dir.glob("*.features.csv.gz"),
            *input_dir.glob("*.features.csv"),
        ]
    )


def load_labels(path: Path | None) -> dict[str, int]:
    if not path or not path.exists():
        return {}
    labels: dict[str, int] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            slug = str(row.get("slug") or "")
            yes_won = parse_float(row.get("yes_won"))
            if slug and yes_won in (0.0, 1.0):
                labels[slug] = int(yes_won)
    return labels


def write_labels(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    fieldnames = [
        "slug",
        "window_start_ts",
        "open_time_ms",
        "close_time_ms",
        "opening_price",
        "closing_price",
        "price_delta",
        "yes_won",
        "label_source",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def analyze_market_features(
    loaded: LoadedFeatureRows,
    labels: dict[str, int],
    config: MarketFeatureAnalysisConfig,
) -> dict[str, Any]:
    rows = loaded.rows
    by_slug = group_by_slug(rows)
    health = health_report(loaded, by_slug, labels)
    paired_summary, paired_segments = paired_reports(rows, by_slug, config)
    taker_summary = taker_report(rows, config)
    imbalance_forward = imbalance_forward_report(by_slug, config)
    elapsed_summary = elapsed_maker_report(rows, config)
    late_continuation = late_continuation_report(by_slug, labels)
    imbalance_final = imbalance_final_report(by_slug, labels, config)
    markov_states, markov_transitions = markov_reports(by_slug, labels, config)
    markov_validation = markov_validation_report(by_slug, labels, config)

    return {
        "health": health,
        "paired_summary": paired_summary,
        "paired_segments": paired_segments,
        "taker_summary": taker_summary,
        "imbalance_forward": imbalance_forward,
        "elapsed_maker_summary": elapsed_summary,
        "late_continuation": late_continuation,
        "imbalance_final": imbalance_final,
        "markov_states": markov_states,
        "markov_transitions": markov_transitions,
        "markov_validation": markov_validation,
    }


def write_analysis_outputs(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "analysis_report.json", _json_report(report))
    _write_json(output_dir / "health_report.json", report["health"])
    _write_csv(output_dir / "paired_summary.csv", report["paired_summary"])
    _write_csv(output_dir / "paired_segments.csv", report["paired_segments"])
    _write_csv(output_dir / "taker_summary.csv", report["taker_summary"])
    _write_csv(output_dir / "imbalance_forward.csv", report["imbalance_forward"])
    _write_csv(output_dir / "elapsed_maker_summary.csv", report["elapsed_maker_summary"])
    _write_csv(output_dir / "late_continuation_summary.csv", report["late_continuation"])
    _write_csv(output_dir / "imbalance_final_summary.csv", report["imbalance_final"])
    _write_csv(output_dir / "markov_states.csv", report["markov_states"])
    _write_csv(output_dir / "markov_transitions.csv", report["markov_transitions"])
    _write_csv(output_dir / "markov_validation.csv", report["markov_validation"])


def health_report(
    loaded: LoadedFeatureRows,
    by_slug: dict[str, list[dict[str, Any]]],
    labels: dict[str, int],
) -> dict[str, Any]:
    rows = loaded.rows
    counts = sorted(len(group) for group in by_slug.values())
    first_ts = min((str(row.get("sample_ts_utc") or "") for row in rows), default="")
    last_ts = max((str(row.get("sample_ts_utc") or "") for row in rows), default="")
    captured_slugs = set(by_slug)
    labeled_slugs = captured_slugs.intersection(labels)
    return {
        "feature_files_read": loaded.files_read,
        "feature_files_skipped": loaded.files_skipped,
        "feature_size_mb": round(loaded.total_size_bytes / 1024 / 1024, 3),
        "rows": len(rows),
        "windows": len(by_slug),
        "labeled_windows": len(labeled_slugs),
        "first_ts_utc": first_ts,
        "last_ts_utc": last_ts,
        "rows_per_window_avg": round(mean(counts), 3) if counts else 0.0,
        "rows_per_window_median": round(median(counts), 3) if counts else 0.0,
        "rows_per_window_min": counts[0] if counts else 0,
        "rows_per_window_max": counts[-1] if counts else 0,
        "valid_yes_mid_pct": pct(sum(1 for row in rows if yes_mid(row) is not None), len(rows)),
        "valid_imbalance_pct": pct(
            sum(1 for row in rows if parse_float(row.get("directional_top_imbalance")) is not None),
            len(rows),
        ),
        "valid_maker_pair_pct": pct(
            sum(1 for row in rows if parse_float(row.get("maker_pair_bid_sum")) is not None),
            len(rows),
        ),
        "skipped_files": list(loaded.skipped_files[:20]),
    }


def paired_reports(
    rows: list[dict[str, Any]],
    by_slug: dict[str, list[dict[str, Any]]],
    config: MarketFeatureAnalysisConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    all_segments: list[dict[str, Any]] = []
    total_rows = len(rows)
    for threshold in config.maker_thresholds:
        samples: list[dict[str, Any]] = []
        segments: list[tuple[str, list[dict[str, Any]]]] = []
        for slug, group in by_slug.items():
            current: list[dict[str, Any]] = []
            previous_ts: float | None = None
            for row in group:
                value = parse_float(row.get("maker_pair_bid_sum"))
                row_time = timestamp(row)
                ok = value is not None and value <= threshold and row_time is not None
                if ok:
                    samples.append(row)
                    if current and previous_ts is not None and row_time - previous_ts <= config.segment_gap_s:
                        current.append(row)
                    else:
                        if current:
                            segments.append((slug, current))
                        current = [row]
                    previous_ts = row_time
                else:
                    if current:
                        segments.append((slug, current))
                    current = []
                    previous_ts = None
            if current:
                segments.append((slug, current))

        durations = []
        segment_rows: list[dict[str, Any]] = []
        for slug, segment in segments:
            start_ts = timestamp(segment[0])
            end_ts = timestamp(segment[-1])
            duration = max(float(end_ts or 0.0) - float(start_ts or 0.0), 0.0)
            durations.append(duration)
            maker_sums = compact_numbers(parse_float(row.get("maker_pair_bid_sum")) for row in segment)
            yes_sizes = compact_numbers(parse_float(row.get("yes_best_bid_size")) for row in segment)
            no_sizes = compact_numbers(parse_float(row.get("no_best_bid_size")) for row in segment)
            segment_rows.append({
                "threshold": threshold,
                "slug": slug,
                "start_ts_utc": segment[0].get("sample_ts_utc", ""),
                "end_ts_utc": segment[-1].get("sample_ts_utc", ""),
                "duration_s": round(duration, 3),
                "samples": len(segment),
                "min_sum": round(min(maker_sums), 6) if maker_sums else "",
                "max_edge": round(1.0 - min(maker_sums), 6) if maker_sums else "",
                "avg_yes_bid_size": round(mean(yes_sizes), 3) if yes_sizes else "",
                "avg_no_bid_size": round(mean(no_sizes), 3) if no_sizes else "",
            })
        all_segments.extend(segment_rows)

        edges = compact_numbers(
            1.0 - value
            for value in (parse_float(row.get("maker_pair_bid_sum")) for row in samples)
            if value is not None
        )
        yes_sizes = compact_numbers(parse_float(row.get("yes_best_bid_size")) for row in samples)
        no_sizes = compact_numbers(parse_float(row.get("no_best_bid_size")) for row in samples)
        summaries.append({
            "threshold": threshold,
            "samples": len(samples),
            "sample_pct": pct(len(samples), total_rows),
            "windows": len({row.get("slug") for row in samples}),
            "edge_avg": round(mean(edges), 6) if edges else "",
            "edge_median": round(median(edges), 6) if edges else "",
            "edge_max": round(max(edges), 6) if edges else "",
            "avg_yes_bid_size": round(mean(yes_sizes), 3) if yes_sizes else "",
            "avg_no_bid_size": round(mean(no_sizes), 3) if no_sizes else "",
            "segments": len(segments),
            "segment_median_s": round(median(durations), 3) if durations else "",
            "segment_p90_s": round(percentile(durations, 0.90), 3) if durations else "",
            "segment_max_s": round(max(durations), 3) if durations else "",
            "segments_ge_1s": sum(1 for duration in durations if duration >= 1.0),
            "segments_ge_3s": sum(1 for duration in durations if duration >= 3.0),
        })

    all_segments.sort(key=lambda row: (float(row["threshold"]), -float(row["duration_s"])))
    return summaries, all_segments


def taker_report(rows: list[dict[str, Any]], config: MarketFeatureAnalysisConfig) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for threshold in config.taker_thresholds:
        samples = [
            row
            for row in rows
            if (value := parse_float(row.get("taker_pair_ask_sum"))) is not None and value <= threshold
        ]
        report.append({
            "threshold": threshold,
            "samples": len(samples),
            "sample_pct": pct(len(samples), len(rows)),
            "windows": len({row.get("slug") for row in samples}),
        })
    return report


def imbalance_forward_report(
    by_slug: dict[str, list[dict[str, Any]]],
    config: MarketFeatureAnalysisConfig,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for threshold in config.imbalance_thresholds:
        for side in ("positive", "negative"):
            for horizon in config.future_horizons_s:
                moves: list[float] = []
                for group in by_slug.values():
                    times = [timestamp(row) for row in group]
                    mids = [yes_mid(row) for row in group]
                    imbalances = [parse_float(row.get("directional_top_imbalance")) for row in group]
                    usable_times = [time for time in times if time is not None]
                    if len(usable_times) != len(times):
                        continue
                    for index, (row_time, mid_now, imbalance) in enumerate(zip(usable_times, mids, imbalances)):
                        if mid_now is None or imbalance is None:
                            continue
                        if side == "positive" and imbalance < threshold:
                            continue
                        if side == "negative" and imbalance > -threshold:
                            continue
                        future_index = bisect_left(usable_times, row_time + horizon)
                        if future_index >= len(group):
                            continue
                        mid_future = mids[future_index]
                        if mid_future is None:
                            continue
                        move = mid_future - mid_now if side == "positive" else mid_now - mid_future
                        moves.append(move)
                if moves:
                    nonzero = [move for move in moves if abs(move) > 1e-9]
                    summaries.append({
                        "threshold_abs": threshold,
                        "side": side,
                        "horizon_s": horizon,
                        "n": len(moves),
                        "avg_mid_move_cents": round(mean(moves) * 100.0, 6),
                        "median_mid_move_cents": round(median(moves) * 100.0, 6),
                        "hit_rate_all": pct_ratio(sum(1 for move in moves if move > 0.0), len(moves)),
                        "unchanged_pct": pct(len(moves) - len(nonzero), len(moves)),
                        "nonzero_n": len(nonzero),
                        "hit_rate_nonzero": pct_ratio(sum(1 for move in nonzero if move > 0.0), len(nonzero)),
                        "avg_nonzero_move_cents": round(mean(nonzero) * 100.0, 6) if nonzero else "",
                    })
    return summaries


def elapsed_maker_report(
    rows: list[dict[str, Any]],
    config: MarketFeatureAnalysisConfig,
) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    main_threshold = config.maker_thresholds[0] if config.maker_thresholds else 0.98
    for label, start, end in ELAPSED_BUCKETS:
        bucket_rows = [
            row
            for row in rows
            if (elapsed := parse_float(row.get("elapsed_s"))) is not None and start <= elapsed < end
        ]
        paired = [
            row
            for row in bucket_rows
            if (value := parse_float(row.get("maker_pair_bid_sum"))) is not None and value <= main_threshold
        ]
        report.append({
            "elapsed_bucket": label,
            "rows": len(bucket_rows),
            "maker_threshold": main_threshold,
            "maker_sample_pct": pct(len(paired), len(bucket_rows)),
            "maker_windows": len({row.get("slug") for row in paired}),
        })
    return report


def late_continuation_report(
    by_slug: dict[str, list[dict[str, Any]]],
    labels: dict[str, int],
) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    min_confidences = (0.0, 0.05, 0.10, 0.20, 0.30, 0.40)
    for bucket_label, start, end in ELAPSED_BUCKETS:
        bucket_samples: list[tuple[dict[str, Any], str, float, bool, float]] = []
        for slug, group in by_slug.items():
            if slug not in labels:
                continue
            candidates = [
                row
                for row in group
                if (elapsed := parse_float(row.get("elapsed_s"))) is not None
                and start <= elapsed < end
                and yes_mid(row) is not None
            ]
            if not candidates:
                continue
            row = candidates[-1]
            side = "YES" if float(yes_mid(row) or 0.0) >= 0.5 else "NO"
            price = buy_ask(row, side)
            if price is None or not 0.0 < price <= 1.0:
                continue
            correct = (side == "YES" and labels[slug] == 1) or (side == "NO" and labels[slug] == 0)
            pnl = 1.0 - price if correct else -price
            bucket_samples.append((row, side, price, correct, pnl))
        for min_confidence in min_confidences:
            filtered = [
                sample
                for sample in bucket_samples
                if abs(float(yes_mid(sample[0]) or 0.5) - 0.5) >= min_confidence
            ]
            if not filtered:
                continue
            report.append({
                "elapsed_bucket": bucket_label,
                "min_abs_mid_edge": min_confidence,
                "n": len(filtered),
                "accuracy": pct_ratio(sum(1 for sample in filtered if sample[3]), len(filtered)),
                "avg_buy_price": round(mean(sample[2] for sample in filtered), 6),
                "rough_avg_pnl_per_share": round(mean(sample[4] for sample in filtered), 6),
                "rough_total_pnl_one_share_each": round(sum(sample[4] for sample in filtered), 6),
            })
    return report


def imbalance_final_report(
    by_slug: dict[str, list[dict[str, Any]]],
    labels: dict[str, int],
    config: MarketFeatureAnalysisConfig,
) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for bucket_label, start, end in ELAPSED_BUCKETS:
        samples: list[tuple[float, int]] = []
        for slug, group in by_slug.items():
            if slug not in labels:
                continue
            candidates = [
                row
                for row in group
                if (elapsed := parse_float(row.get("elapsed_s"))) is not None
                and start <= elapsed < end
                and parse_float(row.get("directional_top_imbalance")) is not None
            ]
            if candidates:
                samples.append((float(parse_float(candidates[-1].get("directional_top_imbalance")) or 0.0), labels[slug]))
        for threshold in config.imbalance_thresholds:
            filtered = [(imbalance, yes_won) for imbalance, yes_won in samples if abs(imbalance) >= threshold]
            if not filtered:
                continue
            correct = sum(
                1
                for imbalance, yes_won in filtered
                if (imbalance > 0.0 and yes_won == 1) or (imbalance < 0.0 and yes_won == 0)
            )
            report.append({
                "elapsed_bucket": bucket_label,
                "abs_imbalance_ge": threshold,
                "n": len(filtered),
                "final_direction_accuracy": pct_ratio(correct, len(filtered)),
            })
    return report


def markov_reports(
    by_slug: dict[str, list[dict[str, Any]]],
    labels: dict[str, int],
    config: MarketFeatureAnalysisConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    state_slug_rows: dict[tuple[str, str], dict[str, Any]] = {}
    transitions: dict[tuple[str, str], int] = defaultdict(int)
    transition_totals: dict[str, int] = defaultdict(int)

    for slug, group in by_slug.items():
        sampled = sample_rows(group, config.markov_sample_step_s)
        sampled_times = [timestamp(row) for row in sampled]
        state_keys = [state_key(row) for row in sampled]
        for row, key in zip(sampled, state_keys):
            if not key:
                continue
            state_slug_rows.setdefault((key, slug), row)
        usable_times = [time for time in sampled_times if time is not None]
        if len(usable_times) != len(sampled):
            continue
        for row_time, from_key in zip(usable_times, state_keys):
            if not from_key:
                continue
            future_index = bisect_left(usable_times, row_time + config.markov_horizon_s)
            if future_index >= len(sampled):
                continue
            to_key = state_keys[future_index]
            if not to_key:
                continue
            transitions[(from_key, to_key)] += 1
            transition_totals[from_key] += 1

    state_groups: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for (key, slug), row in state_slug_rows.items():
        if slug in labels:
            state_groups[key].append((slug, row))

    state_rows: list[dict[str, Any]] = []
    for key, items in state_groups.items():
        yes_wins = sum(1 for slug, _row in items if labels[slug] == 1)
        no_wins = len(items) - yes_wins
        mids = compact_numbers(yes_mid(row) for _slug, row in items)
        elapsed_values = compact_numbers(parse_float(row.get("elapsed_s")) for _slug, row in items)
        state_rows.append({
            **state_fields_from_key(key),
            "windows": len(items),
            "yes_wins": yes_wins,
            "no_wins": no_wins,
            "yes_rate": pct_ratio(yes_wins, len(items)),
            "yes_rate_laplace": round((yes_wins + 1.0) / (len(items) + 2.0), 6),
            "avg_yes_mid": round(mean(mids), 6) if mids else "",
            "avg_elapsed_s": round(mean(elapsed_values), 3) if elapsed_values else "",
        })

    transition_rows: list[dict[str, Any]] = []
    for (from_key, to_key), count in transitions.items():
        transition_rows.append({
            "horizon_s": config.markov_horizon_s,
            "from_state": from_key,
            "to_state": to_key,
            "count": count,
            "probability": round(count / transition_totals[from_key], 6) if transition_totals[from_key] else 0.0,
        })

    state_rows.sort(key=lambda row: (-int(row["windows"]), str(row["state"])))
    transition_rows.sort(key=lambda row: (str(row["from_state"]), -int(row["count"]), str(row["to_state"])))
    return state_rows, transition_rows


def markov_validation_report(
    by_slug: dict[str, list[dict[str, Any]]],
    labels: dict[str, int],
    config: MarketFeatureAnalysisConfig,
) -> list[dict[str, Any]]:
    labeled_slugs = sorted(
        [slug for slug in by_slug if slug in labels],
        key=lambda slug: window_start_ts(slug) or 0,
    )
    if len(labeled_slugs) < 4:
        return []
    split_index = int(len(labeled_slugs) * min(max(config.markov_train_fraction, 0.1), 0.9))
    train_slugs = set(labeled_slugs[:split_index])
    test_slugs = set(labeled_slugs[split_index:])

    state_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"n": 0, "yes": 0})
    for slug in train_slugs:
        for bucket_label, row in latest_rows_by_bucket(by_slug[slug]).items():
            key = state_key(row)
            if not key:
                continue
            state_counts[(bucket_label, key)]["n"] += 1
            state_counts[(bucket_label, key)]["yes"] += labels[slug]

    samples_by_bucket: dict[str, list[dict[str, float]]] = defaultdict(list)
    min_train_windows = max(int(config.markov_min_train_windows), 1)
    for slug in test_slugs:
        for bucket_label, row in latest_rows_by_bucket(by_slug[slug]).items():
            key = state_key(row)
            if not key:
                continue
            counts = state_counts.get((bucket_label, key))
            if not counts or counts["n"] < min_train_windows:
                continue
            market_p = yes_mid(row)
            if market_p is None:
                continue
            markov_p = (counts["yes"] + 1.0) / (counts["n"] + 2.0)
            actual = float(labels[slug])
            samples_by_bucket[bucket_label].append({
                "actual": actual,
                "market_p": market_p,
                "markov_p": markov_p,
                "train_n": float(counts["n"]),
            })

    report: list[dict[str, Any]] = []
    for bucket_label, samples in sorted(samples_by_bucket.items()):
        if not samples:
            continue
        markov_briers = [(sample["markov_p"] - sample["actual"]) ** 2 for sample in samples]
        market_briers = [(sample["market_p"] - sample["actual"]) ** 2 for sample in samples]
        markov_correct = sum((sample["markov_p"] >= 0.5) == bool(sample["actual"]) for sample in samples)
        market_correct = sum((sample["market_p"] >= 0.5) == bool(sample["actual"]) for sample in samples)
        report.append({
            "elapsed_bucket": bucket_label,
            "test_samples": len(samples),
            "avg_train_windows_per_state": round(mean(sample["train_n"] for sample in samples), 3),
            "markov_brier": round(mean(markov_briers), 6),
            "market_mid_brier": round(mean(market_briers), 6),
            "markov_accuracy": pct_ratio(markov_correct, len(samples)),
            "market_mid_accuracy": pct_ratio(market_correct, len(samples)),
            "avg_abs_markov_minus_mid": round(
                mean(abs(sample["markov_p"] - sample["market_p"]) for sample in samples),
                6,
            ),
        })
    return report


def latest_rows_by_bucket(group: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for bucket_label, start, end in ELAPSED_BUCKETS:
        candidates = [
            row
            for row in group
            if (elapsed := parse_float(row.get("elapsed_s"))) is not None
            and start <= elapsed < end
            and yes_mid(row) is not None
        ]
        if candidates:
            rows[bucket_label] = candidates[-1]
    return rows


def sample_rows(group: list[dict[str, Any]], step_s: float) -> list[dict[str, Any]]:
    sampled: list[dict[str, Any]] = []
    last_time: float | None = None
    for row in group:
        row_time = timestamp(row)
        if row_time is None:
            continue
        if last_time is None or row_time - last_time >= step_s:
            sampled.append(row)
            last_time = row_time
    return sampled


def state_key(row: dict[str, Any]) -> str:
    mid = yes_mid(row)
    elapsed = parse_float(row.get("elapsed_s"))
    imbalance = parse_float(row.get("directional_top_imbalance"))
    spread = parse_float(row.get("yes_spread"))
    if mid is None or elapsed is None or imbalance is None:
        return ""
    fields = {
        "elapsed": elapsed_bucket(elapsed),
        "mid": mid_bucket(mid),
        "imbalance": imbalance_bucket(imbalance),
        "spread": spread_bucket(spread),
    }
    if not fields["elapsed"]:
        return ""
    return (
        f"t={fields['elapsed']}|mid={fields['mid']}|"
        f"imb={fields['imbalance']}|spr={fields['spread']}"
    )


def state_fields_from_key(key: str) -> dict[str, Any]:
    pieces: dict[str, str] = {}
    for part in key.split("|"):
        if "=" in part:
            name, value = part.split("=", 1)
            pieces[name] = value
    return {
        "state": key,
        "elapsed_bucket": pieces.get("t", ""),
        "mid_bucket": pieces.get("mid", ""),
        "imbalance_bucket": pieces.get("imb", ""),
        "spread_bucket": pieces.get("spr", ""),
    }


def group_by_slug(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        slug = str(row.get("slug") or "")
        if slug:
            grouped[slug].append(row)
    for group in grouped.values():
        group.sort(key=lambda row: str(row.get("sample_ts_utc") or ""))
    return dict(grouped)


def elapsed_bucket(value: float) -> str:
    for label, start, end in ELAPSED_BUCKETS:
        if start <= value < end:
            return label
    if value >= 305.0:
        return "305+"
    return ""


def mid_bucket(value: float) -> str:
    clipped = min(max(value, 0.0), 0.999999)
    start = int(clipped * 10) * 10
    return f"{start:02d}-{start + 10:02d}"


def imbalance_bucket(value: float) -> str:
    if value <= -1.5:
        return "neg_hi"
    if value <= -1.0:
        return "neg_med"
    if value <= -0.5:
        return "neg_low"
    if value < 0.5:
        return "flat"
    if value < 1.0:
        return "pos_low"
    if value < 1.5:
        return "pos_med"
    return "pos_hi"


def spread_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value <= 0.01:
        return "<=1c"
    if value <= 0.03:
        return "<=3c"
    if value <= 0.05:
        return "<=5c"
    return ">5c"


def buy_ask(row: dict[str, Any], side: str) -> float | None:
    key = "yes_best_ask" if side == "YES" else "no_best_ask"
    return parse_float(row.get(key))


def yes_mid(row: dict[str, Any]) -> float | None:
    mid = parse_float(row.get("yes_mid"))
    if mid is not None and 0.0 < mid < 1.0:
        return mid
    bid = parse_float(row.get("yes_best_bid"))
    ask = parse_float(row.get("yes_best_ask"))
    if bid is not None and ask is not None and 0.0 < bid <= ask <= 1.0:
        return (bid + ask) / 2.0
    return None


def timestamp(row: dict[str, Any]) -> float | None:
    value = str(row.get("sample_ts_utc") or "")
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def window_start_ts(slug: str) -> int | None:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compact_numbers(values: Iterable[float | None]) -> list[float]:
    return [float(value) for value in values if value is not None and math.isfinite(float(value))]


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def median(values: Iterable[float]) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) / 2.0


def percentile(values: Iterable[float], q: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    index = int(round((len(values) - 1) * q))
    return values[max(0, min(index, len(values) - 1))]


def pct(count: int, total: int) -> float:
    return round((count / total) * 100.0, 6) if total else 0.0


def pct_ratio(count: int, total: int) -> float:
    return round(count / total, 6) if total else 0.0


def _open_feature_file(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return path.open("r", newline="", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _json_report(report: dict[str, Any]) -> dict[str, Any]:
    compact = dict(report)
    compact["paired_segments"] = report["paired_segments"][:50]
    compact["markov_transitions"] = report["markov_transitions"][:200]
    compact["markov_states"] = report["markov_states"][:200]
    compact["markov_validation"] = report["markov_validation"]
    return compact
