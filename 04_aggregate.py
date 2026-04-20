#!/usr/bin/env python3
"""
04_aggregate.py
Aggregate LOC and dependency measurements into paper-ready tables and figures.

Usage: python3 04_aggregate.py

Reads:
    results/loc_summary.csv
    results/deps_summary.csv

Produces:
    results/table_by_layer.csv         — per-layer aggregation for the paper
    results/table_by_layer.tex         — LaTeX table ready for inclusion
    results/language_breakdown.csv     — LOC by language across the full stack
    results/language_breakdown.png     — bar chart of language distribution
    results/cross_layer_summary.json   — shared dependencies across layers

Requirements:
    pip install pandas matplotlib pyyaml
"""

import csv
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"
CONFIG_FILE = SCRIPT_DIR / "stack_config.yaml"

LAYER_DISPLAY_NAMES = {
    "data_pipelines": "Data Pipelines",
    "training": "Training",
    "integration_serving": "Integration \\& Serving",
    "cross_cutting": "Cross-Cutting Infrastructure",
}


def load_csv(path: Path) -> list[dict]:
    """Load a CSV file into a list of dicts."""
    if not path.exists():
        print(f"WARN: {path} not found. Run measurement scripts first.")
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def aggregate_loc(loc_rows: list[dict]) -> dict:
    """Aggregate LOC data by layer."""
    layers = {}
    for row in loc_rows:
        layer = row["layer"]
        if layer not in layers:
            layers[layer] = {
                "projects": 0,
                "total_code_lines": 0,
                "total_files": 0,
                "languages": set(),
            }
        layers[layer]["projects"] += 1
        layers[layer]["total_code_lines"] += int(row.get("code_lines", 0))
        layers[layer]["total_files"] += int(row.get("files", 0))
        for lang in row.get("languages", "").strip('"').split(";"):
            if lang.strip():
                layers[layer]["languages"].add(lang.strip())
    return layers


def aggregate_deps(dep_rows: list[dict]) -> dict:
    """Aggregate dependency counts by layer."""
    layers = {}
    for row in dep_rows:
        layer = row["layer"]
        if layer not in layers:
            layers[layer] = {
                "ecosystems": set(),
                "total_direct": 0,
                "total_transitive": 0,
            }
        ecos = row.get("ecosystems", "")
        for eco in ecos.strip('"').split(","):
            eco = eco.strip()
            if eco:
                layers[layer]["ecosystems"].add(eco)
        direct = int(row.get("direct_deps", 0))
        transitive = int(row.get("transitive_deps", 0))
        if direct > 0:
            layers[layer]["total_direct"] += direct
        if transitive > 0:
            layers[layer]["total_transitive"] += transitive
    return layers

def main():
    print("=== Aggregating supply chain measurements ===\n")

    # Compute per-ecosystem LOC/package medians from measured data
    import importlib.util, pathlib
    _spec = importlib.util.spec_from_file_location(
        "eco_medians",
        pathlib.Path(__file__).parent / "06_ecosystem_loc_medians.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.main()
    print()

    loc_rows = load_csv(RESULTS_DIR / "loc_summary.csv")
    dep_rows = load_csv(RESULTS_DIR / "deps_summary.csv")

    if not loc_rows and not dep_rows:
        print("No data found. Run 02_measure_loc.sh and 03_count_deps.sh first.")
        sys.exit(1)

    loc_agg = aggregate_loc(loc_rows)
    dep_agg = aggregate_deps(dep_rows)

    # Per-layer summary CSV
    summary_path = RESULTS_DIR / "table_by_layer.csv"
    with open(summary_path, "w") as f:
        writer = csv.writer(f)
        writer.writerow([
            "layer", "projects", "total_loc", "num_languages",
            "direct_deps", "num_ecosystems",
        ])
        for layer_key in LAYER_DISPLAY_NAMES:
            loc = loc_agg.get(layer_key, {})
            dep = dep_agg.get(layer_key, {})
            writer.writerow([
                layer_key,
                loc.get("projects", 0),
                loc.get("total_code_lines", 0),
                len(loc.get("languages", set())),
                dep.get("total_direct", 0),
                len(dep.get("ecosystems", set())),
            ])
    print(f"  Summary CSV: {summary_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
