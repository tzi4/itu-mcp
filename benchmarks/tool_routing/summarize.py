"""Aggregate ``results.jsonl`` from run_claude.py into a per-variant table."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def summarize(records: list[dict[str, Any]], baseline: str) -> list[dict[str, Any]]:
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_variant[record["variant"]].append(record)
    rows = []
    for variant, items in by_variant.items():
        ok = [item for item in items if not item["is_error"]]
        rows.append({
            "variant": variant,
            "runs": len(items),
            "errors": len(items) - len(ok),
            "route_hit_rate": round(sum(item["hit"] for item in ok) / len(ok), 3) if ok else 0.0,
            "median_input_tokens": _median([item["input_tokens_total"] for item in ok]),
            "total_input_tokens": sum(item["input_tokens_total"] for item in ok),
            "median_output_tokens": _median([item["output_tokens"] for item in ok]),
            "median_itu_calls": _median([item["itu_tool_calls"] for item in ok]),
            "median_turns": _median([item["num_turns"] or 0 for item in ok]),
            "median_seconds": _median([item["wall_seconds"] for item in ok]),
            "total_cost_usd": round(sum(item["cost_usd"] or 0 for item in ok), 4),
        })
    base = next((row for row in rows if row["variant"] == baseline), None)
    for row in rows:
        if base and base["total_input_tokens"]:
            row["input_tokens_vs_baseline"] = round(
                row["total_input_tokens"] / base["total_input_tokens"] - 1, 3)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="results.jsonl from run_claude.py")
    parser.add_argument("--baseline", default="full-nosearch")
    args = parser.parse_args(argv)
    records = [json.loads(line) for line in args.results.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = summarize(records, args.baseline)
    columns = list(rows[0]) if rows else []
    print("| " + " | ".join(columns) + " |")
    print("|" + "---|" * len(columns))
    for row in rows:
        print("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
