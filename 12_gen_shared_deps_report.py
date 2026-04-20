#!/usr/bin/env python3
"""
12_gen_shared_deps_report.py
Generate cross-layer shared dependencies report.

Usage: python3 12_gen_shared_deps_report.py

Reads:
    results/deps_per_project/*.json

Produces:
    results/shared_deps_report.csv — packages appearing in 2+ layers

This identifies supply chain risk: packages that must be maintained
across multiple infrastructure layers.
"""

import csv
import json
from pathlib import Path
from collections import defaultdict

SCRIPT_DIR = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"
DEPS_DIR = RESULTS_DIR / "deps_per_project"

def main():
    print("=== Generating cross-layer shared dependencies report ===\n")
    
    if not DEPS_DIR.exists():
        print(f"ERROR: {DEPS_DIR} not found.")
        return
    
    # Aggregate direct dependencies across all projects
    dep_sets = defaultdict(lambda: {"layers": set(), "projects": [], "count": 0})
    
    json_files = sorted(DEPS_DIR.glob("*.json"))
    print(f"Processing {len(json_files)} project dependency files...")
    
    for json_file in json_files:
        with open(json_file) as f:
            data = json.load(f)
        
        layer = data.get("layer", "").strip()
        repo = data.get("repo", "").strip()
        direct_deps = data.get("direct_deps", [])
        
        if not direct_deps or not layer:
            continue
        
        for dep in direct_deps:
            dep_norm = str(dep).strip().lower()
            if not dep_norm:
                continue
            
            dep_sets[dep_norm]["layers"].add(layer)
            dep_sets[dep_norm]["projects"].append(repo)
            dep_sets[dep_norm]["count"] += 1
    
    # Filter to 2+ layers and sort
    shared = []
    for dep, info in dep_sets.items():
        if len(info["layers"]) >= 2:
            shared.append({
                "dependency": dep,
                "n_layers": len(info["layers"]),
                "layers": ";".join(sorted(info["layers"])),
                "num_projects": info["count"],
            })
    
    # Sort by layers desc, then count desc
    shared.sort(key=lambda x: (-x["n_layers"], -x["num_projects"]))
    
    print(f"Found {len(shared)} cross-layer shared dependencies\n")
    
    # Write CSV
    output_path = RESULTS_DIR / "shared_deps_report.csv"
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["dependency", "n_layers", "layers", "num_projects"])
        writer.writeheader()
        writer.writerows(shared)
    
    print(f"Report: {output_path}")
    print(f"\nTop 20 most-shared packages across layers:")
    for i, s in enumerate(shared[:20], 1):
        print(f"  {i:2d}. {s['dependency']:<40} {s['n_layers']} layers, {s['num_projects']:3d} projects")

if __name__ == "__main__":
    main()
