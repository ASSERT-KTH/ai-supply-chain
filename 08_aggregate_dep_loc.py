#!/usr/bin/env python3
"""
08_aggregate_dep_loc.py
Aggregate measured LOC from dep_loc_measured.json and compare against
the earlier median-based estimates.

Usage:
    python3 08_aggregate_dep_loc.py

Reads:
    results/dep_loc_measured.json
    results/transitive_merged.json

Writes:
    results/dep_loc_summary.csv
"""

import csv
import json
import statistics
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"

INPUT_MEASURED = RESULTS_DIR / "dep_loc_measured.json"
INPUT_MERGED   = RESULTS_DIR / "transitive_merged.json"
OUTPUT_CSV     = RESULTS_DIR / "dep_loc_summary.csv"


def load_json(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: {path} not found")
        raise SystemExit(1)
    with open(path) as f:
        return json.load(f)


def aggregate_ecosystem(eco: str, pkg_results: dict) -> dict:
    """Return per-ecosystem stats, using median of measured as fallback for failures."""
    measured_loc   = []
    failed_keys    = []

    for key, v in pkg_results.items():
        if v.get("scc_ok"):
            measured_loc.append(v["code"])
        else:
            failed_keys.append(key)

    measured_ok  = len(measured_loc)
    unique_pairs = len(pkg_results)

    fallback_per_pkg = int(statistics.median(measured_loc)) if measured_loc else 0
    fallback_loc     = fallback_per_pkg * len(failed_keys)
    total_measured   = sum(measured_loc)
    total_estimate   = total_measured + fallback_loc

    return {
        "unique_pairs":     unique_pairs,
        "measured_ok":      measured_ok,
        "measured_loc":     total_measured,
        "fallback_loc":     fallback_loc,
        "fallback_per_pkg": fallback_per_pkg,
        "failed":           len(failed_keys),
        "total_loc_estimate": total_estimate,
    }


def print_summary_table(rows: list[dict]):
    header = (
        f"{'Ecosystem':<10}  {'Unique':>7}  {'Meas.OK':>7}  "
        f"{'Meas. LOC':>13}  {'Fallback LOC':>13}  {'Total est.':>13}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    grand_total = 0
    for r in rows:
        eco  = r["ecosystem"]
        print(
            f"{eco:<10}  {r['unique_pairs']:>7,}  {r['measured_ok']:>7,}  "
            f"{r['measured_loc']:>13,}  {r['fallback_loc']:>13,}  "
            f"{r['total_loc_estimate']:>13,}"
        )
        grand_total += r["total_loc_estimate"]
    print(sep)
    print(f"{'TOTAL':<10}  {'':>7}  {'':>7}  {'':>13}  {'':>13}  {grand_total:>13,}")
    print()


def main():
    print("=== Aggregating dep LOC measurements ===\n")

    measured = load_json(INPUT_MEASURED)
    merged   = load_json(INPUT_MERGED)

    # Build rows only for ecosystems present in the measured file
    rows = []
    for eco in ["python", "go", "cargo", "maven", "gradle", "npm"]:
        pkg_results = measured.get(eco, {})
        if not pkg_results:
            # Still report the ecosystem with zeros so the CSV is complete
            rows.append({
                "ecosystem":         eco,
                "unique_pairs":      len(merged.get(eco, [])),
                "measured_ok":       0,
                "measured_loc":      0,
                "fallback_loc":      0,
                "total_loc_estimate": 0,
            })
            continue

        stats = aggregate_ecosystem(eco, pkg_results)
        rows.append({"ecosystem": eco, **stats})

    print_summary_table(rows)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "ecosystem", "unique_pairs", "measured_ok",
            "measured_loc", "fallback_loc", "total_loc_estimate",
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in writer.fieldnames})

    print(f"CSV saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
