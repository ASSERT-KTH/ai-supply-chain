#!/usr/bin/env python3
"""
09_retry_failed_loc.py
Retry LOC measurement for packages that previously failed (scc_ok: false).
Reports what changed and updates dep_loc_measured.json in place.

Usage:
    python3 09_retry_failed_loc.py [--eco python go cargo maven gradle npm] [--dry-run]

Reads/writes: results/dep_loc_measured.json
Scratch:      results/dep_loc_work/
"""

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.error
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"
WORK_DIR    = RESULTS_DIR / "dep_loc_work"
INPUT_JSON  = RESULTS_DIR / "dep_loc_measured.json"

ALL_ECOSYSTEMS = ["python", "go", "cargo", "maven", "gradle", "npm"]

# Import measure functions and helpers from 07
import importlib.util

spec = importlib.util.spec_from_file_location(
    "measure_dep_loc",
    SCRIPT_DIR / "07_measure_dep_loc.py"
)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)

fail    = _mod.fail
WORKERS = _mod.WORKERS

# ---------------------------------------------------------------------------
# Improved Python measurement with PyPI JSON API fallback.
# Used only here for retries — does not modify 07's results for ok packages.
# ---------------------------------------------------------------------------

def _pypi_direct_download(name: str, version: str, dest_dir: str) -> str | None:
    """Fetch sdist or wheel directly from PyPI JSON API. Returns local path or None."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            meta = json.loads(resp.read())
    except Exception:
        return None

    urls = meta.get("urls", [])
    for pkg_type in ("sdist", "bdist_wheel"):
        for entry in urls:
            if entry.get("packagetype") == pkg_type:
                dest = os.path.join(dest_dir, entry["filename"])
                try:
                    urllib.request.urlretrieve(entry["url"], dest)
                    return dest
                except Exception:
                    continue
    return None


def measure_python_with_fallback(package_key: str) -> dict:
    """Like 07's measure_python but falls back to PyPI JSON API on pip failure."""
    name, _, version = package_key.partition("==")
    if not version:
        return fail(f"cannot parse python key: {package_key!r}")

    with tempfile.TemporaryDirectory(dir=WORK_DIR) as tmpdir:
        dl_dir  = os.path.join(tmpdir, "download")
        src_dir = os.path.join(tmpdir, "source")
        os.makedirs(dl_dir)
        os.makedirs(src_dir)

        # Primary: pip download
        r = subprocess.run(
            ["pip", "download", "--no-deps", "--dest", dl_dir, package_key],
            capture_output=True, text=True
        )
        downloaded = os.listdir(dl_dir) if r.returncode == 0 else []

        # Fallback: PyPI JSON API (avoids metadata-build failures for old sdists)
        if not downloaded:
            archive = _pypi_direct_download(name, version, dl_dir)
            if archive:
                downloaded = [os.path.basename(archive)]
            else:
                return fail(
                    f"pip download failed and PyPI direct fetch also failed: "
                    f"{r.stderr.strip()[:200]}"
                )

        wheels = [f for f in downloaded if f.endswith(".whl")]
        sdists = [f for f in downloaded if f.endswith(".tar.gz") or f.endswith(".tgz")]
        zips   = [f for f in downloaded if f.endswith(".zip")]

        archive_name = (wheels or sdists or zips or [None])[0]
        if not archive_name:
            return fail(f"no usable archive (got: {downloaded})")

        archive_path = os.path.join(dl_dir, archive_name)
        try:
            if archive_path.endswith(".whl") or archive_path.endswith(".zip"):
                with zipfile.ZipFile(archive_path) as z:
                    z.extractall(src_dir)
            else:
                with tarfile.open(archive_path, "r:gz") as t:
                    t.extractall(src_dir)
        except Exception as e:
            return fail(f"extract error: {e}")

        return _mod.run_scc(src_dir)


# Override only the python entry; all other ecosystems use 07's functions unchanged
MEASURE_FN = dict(_mod.MEASURE_FN)
MEASURE_FN["python"] = measure_python_with_fallback


def retry_ecosystem(eco: str, failed_pkgs: list, all_results: dict) -> dict:
    """Retry failed packages and return (pkg, old_result, new_result) triples."""
    retried = []

    def worker(pkg):
        try:
            return pkg, MEASURE_FN[eco](pkg)
        except Exception as e:
            return pkg, fail(str(e))

    total = len(failed_pkgs)
    print(f"\n[{eco}] retrying {total} failed package(s)")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(worker, pkg): pkg for pkg in failed_pkgs}
        for i, fut in enumerate(as_completed(futures), 1):
            pkg, new_result = fut.result()
            old_result = all_results[eco][pkg]
            retried.append((pkg, old_result, new_result))
            status = "OK" if new_result.get("scc_ok") else "FAIL"
            print(f"  [{i}/{total}] {status}  {pkg}")
            if not new_result.get("scc_ok"):
                old_err = old_result.get("error", "")
                new_err = new_result.get("error", "")
                if old_err != new_err:
                    print(f"           old error: {old_err}")
                    print(f"           new error: {new_err}")
                else:
                    print(f"           error: {new_err}")

    return retried


def print_report(eco_reports: dict):
    print("\n" + "=" * 60)
    print("RETRY REPORT")
    print("=" * 60)

    grand_newly_ok = 0
    grand_still_fail = 0
    error_buckets = defaultdict(list)  # error prefix -> [pkg, ...]

    for eco, retried in eco_reports.items():
        if not retried:
            continue
        newly_ok    = [(p, o, n) for p, o, n in retried if n.get("scc_ok")]
        still_fail  = [(p, o, n) for p, o, n in retried if not n.get("scc_ok")]
        grand_newly_ok   += len(newly_ok)
        grand_still_fail += len(still_fail)

        print(f"\n[{eco}]  {len(newly_ok)} newly OK / {len(still_fail)} still failing")

        if newly_ok:
            print("  Newly measured:")
            for pkg, _, n in newly_ok:
                print(f"    {pkg}  →  {n['code']:,} LOC")

        if still_fail:
            print("  Still failing:")
            for pkg, o, n in still_fail:
                err = n.get("error", "unknown")
                # Bucket by first ~60 chars of error for pattern analysis
                bucket = err[:60].rstrip()
                error_buckets[bucket].append(f"{eco}/{pkg}")
                print(f"    {pkg}")
                print(f"      {err}")

    print("\n" + "-" * 60)
    print(f"Total: {grand_newly_ok} newly measured, {grand_still_fail} still failing")

    if error_buckets:
        print("\nError pattern summary (for pipeline diagnosis):")
        for pattern, pkgs in sorted(error_buckets.items(), key=lambda x: -len(x[1])):
            print(f"  [{len(pkgs):3d}x]  {pattern!r}")
            for p in pkgs[:5]:
                print(f"          {p}")
            if len(pkgs) > 5:
                print(f"          ... and {len(pkgs) - 5} more")

    print()


def main():
    parser = argparse.ArgumentParser(description="Retry failed LOC measurements")
    parser.add_argument(
        "--eco", nargs="+", choices=ALL_ECOSYSTEMS, default=ALL_ECOSYSTEMS,
        help="Ecosystems to retry (default: all)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report failures without retrying; just show what would be retried"
    )
    args = parser.parse_args()

    if not INPUT_JSON.exists():
        print(f"ERROR: {INPUT_JSON} not found — run 07_measure_dep_loc.py first")
        sys.exit(1)

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    with open(INPUT_JSON) as f:
        all_results = json.load(f)

    eco_reports = {}

    for eco in args.eco:
        eco_data = all_results.get(eco, {})
        failed_pkgs = [pkg for pkg, v in eco_data.items() if not v.get("scc_ok")]

        if not failed_pkgs:
            print(f"[{eco}] no failures, skipping")
            continue

        print(f"[{eco}] {len(failed_pkgs)} failed package(s) out of {len(eco_data)}")

        if args.dry_run:
            for pkg in failed_pkgs:
                err = eco_data[pkg].get("error", "unknown")
                print(f"  {pkg}")
                print(f"    {err}")
            eco_reports[eco] = []
            continue

        retried = retry_ecosystem(eco, failed_pkgs, all_results)
        eco_reports[eco] = retried

        # Update results in place for newly OK packages
        for pkg, _, new_result in retried:
            if new_result.get("scc_ok"):
                all_results[eco][pkg] = new_result

        # Save after each ecosystem
        with open(INPUT_JSON, "w") as f:
            json.dump(all_results, f, indent=2)

    if not args.dry_run:
        print_report(eco_reports)
        print(f"Updated results saved to {INPUT_JSON}")
        print("Re-run 08_aggregate_dep_loc.py to refresh dep_loc_summary.csv")
    else:
        total_failed = sum(
            len([p for p, v in all_results.get(eco, {}).items() if not v.get("scc_ok")])
            for eco in args.eco
        )
        print(f"\nDry run: {total_failed} total failures across selected ecosystems")


if __name__ == "__main__":
    main()
