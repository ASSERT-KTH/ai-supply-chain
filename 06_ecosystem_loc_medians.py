#!/usr/bin/env python3
"""
06_ecosystem_loc_medians.py
Compute median LOC per package for each dependency ecosystem,
derived entirely from the measured data in this repository.

Method
------
For each project that has:
  - a measured LOC count (loc_summary.csv)
  - a resolved transitive dep count > 0 (deps_summary.csv)
  - a known primary resolver (first token of transitive_method)

We compute:  ratio = project_LOC / transitive_packages

Then for each ecosystem we take the median of all per-project ratios.
This gives a data-driven LOC/package estimate grounded in the actual
cloned repositories, not external literature values.

Note: the ratio conflates the project's own LOC with the average size
of its dependencies, so it is an approximation. It is however the best
estimate derivable from the available data without cloning the full
transitive closure.

Outputs
-------
  results/ecosystem_loc_medians.csv
    ecosystem, n_projects, loc_per_pkg_median, loc_per_pkg_mean,
    loc_per_pkg_min, loc_per_pkg_max, project_list

Usage
-----
  python3 06_ecosystem_loc_medians.py
  (also called by 04_aggregate.py)
"""

import csv
import statistics
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"

OUTPUT_CSV = RESULTS_DIR / "ecosystem_loc_medians.csv"


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        print(f"  WARN: {path} not found")
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def resolved_ecosystems(transitive_method: str) -> list[str]:
    """Return all ecosystems that actually produced a count in transitive_method.
    Skips resolvers that produced no result (none, manifest, CMakeLists.txt, configure.ac).
    e.g. 'python:uv_pip_compile;go:go_mod_graph;rust:cargo_metadata' -> ['python','go','rust']
    """
    UNRESOLVED = {"none", "manifest", "cmakelists.txt", "configure.ac", ""}
    method = transitive_method.strip().strip('"')
    if not method or method == "none":
        return []
    ecos = []
    for segment in method.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        parts = segment.split(":", 1)
        eco = parts[0].strip().lower()
        resolver = parts[1].strip().lower() if len(parts) > 1 else ""
        if resolver in UNRESOLVED or any(u and u in resolver for u in UNRESOLVED):
            continue
        if eco:
            ecos.append(eco)
    return ecos


def compute_medians(loc_rows: list[dict], dep_rows: list[dict]) -> dict:
    """Return {ecosystem: {ratios: [...], projects: []}}.

    For multi-ecosystem projects the merged transitive count is split equally
    among all resolved ecosystems — an approximation, but better than ignoring
    all but the primary resolver.
    """
    loc_by_proj = {}
    for r in loc_rows:
        proj = r["project"].strip()
        loc = int(r.get("code_lines", 0) or 0)
        if loc > 0:
            loc_by_proj[proj] = loc

    ecosystems: dict[str, dict] = {}

    for r in dep_rows:
        proj = r["project"].strip()
        trans_raw = r.get("transitive_deps", "").strip()
        try:
            trans = int(trans_raw)
        except ValueError:
            continue
        if trans <= 0:
            continue

        loc = loc_by_proj.get(proj)
        if not loc:
            continue

        method = r.get("transitive_method", "").strip().strip('"')
        ecos = resolved_ecosystems(method)
        if not ecos:
            continue

        share = trans / len(ecos)
        ratio = loc / share

        for eco in ecos:
            if eco not in ecosystems:
                ecosystems[eco] = {"ratios": [], "projects": []}
            ecosystems[eco]["ratios"].append(ratio)
            if proj not in ecosystems[eco]["projects"]:
                ecosystems[eco]["projects"].append(proj)

    return ecosystems


def write_csv(ecosystems: dict, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ecosystem",
            "n_projects",
            "loc_per_pkg_median",
            "loc_per_pkg_mean",
            "loc_per_pkg_min",
            "loc_per_pkg_max",
            "project_list",
        ])
        for eco in sorted(ecosystems):
            ratios   = ecosystems[eco]["ratios"]
            projects = ecosystems[eco]["projects"]
            writer.writerow([
                eco,
                len(ratios),
                round(statistics.median(ratios)),
                round(statistics.mean(ratios)),
                round(min(ratios)),
                round(max(ratios)),
                ";".join(projects),
            ])
    print(f"  Ecosystem medians: {output}")


def main():
    print("=== Computing per-ecosystem LOC/package medians ===\n")

    loc_rows = load_csv(RESULTS_DIR / "loc_summary.csv")
    dep_rows = load_csv(RESULTS_DIR / "deps_summary.csv")

    if not loc_rows or not dep_rows:
        print("  ERROR: need loc_summary.csv and deps_summary.csv. Run scripts 02 and 03 first.")
        return

    ecosystems = compute_medians(loc_rows, dep_rows)

    if not ecosystems:
        print("  WARN: no projects matched (need both LOC and resolved transitive deps).")
        return

    print(f"  {'Ecosystem':<12}  {'N':>3}  {'Median LOC/pkg':>15}  {'Min':>8}  {'Max':>8}  Projects")
    print(f"  {'─'*12}  {'─'*3}  {'─'*15}  {'─'*8}  {'─'*8}  ─────────────────────")
    for eco in sorted(ecosystems):
        ratios   = ecosystems[eco]["ratios"]
        projects = ecosystems[eco]["projects"]
        median   = round(statistics.median(ratios))
        lo       = round(min(ratios))
        hi       = round(max(ratios))
        print(f"  {eco:<12}  {len(ratios):>3}  {median:>15,}  {lo:>8,}  {hi:>8,}  {', '.join(projects)}")

    write_csv(ecosystems, OUTPUT_CSV)
    print("\nDone.")


if __name__ == "__main__":
    main()
