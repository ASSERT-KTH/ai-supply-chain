#!/usr/bin/env python3
"""
07_measure_dep_loc.py
Download source for each unique (name, version) dependency and measure LOC with scc.

Usage:
    python3 07_measure_dep_loc.py [--eco python go cargo maven gradle npm]

Reads:   results/transitive_merged.json
Writes:  results/dep_loc_measured.json
Scratch: results/dep_loc_work/  (never deleted between runs)
"""

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import urllib.request
import urllib.error

SCRIPT_DIR   = Path(__file__).parent
RESULTS_DIR  = SCRIPT_DIR / "results"
WORK_DIR     = RESULTS_DIR / "dep_loc_work"
INPUT_JSON   = RESULTS_DIR / "transitive_merged.json"
OUTPUT_JSON  = RESULTS_DIR / "dep_loc_measured.json"

WORKERS = 4

ALL_ECOSYSTEMS = ["python", "go", "cargo", "maven", "gradle", "npm"]

# ---------------------------------------------------------------------------
# scc helpers
# ---------------------------------------------------------------------------

def run_scc(path: str) -> dict:
    """Run scc --format json on a directory or file. Return aggregated counts.

    scc per-language JSON shape:
      {"Name": str, "Code": int, "Comment": int, "Blank": int, "Count": int, ...}
    "Count" is the number of files for that language entry.
    """
    result = subprocess.run(
        ["scc", "--format", "json", path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"scc failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    code = comment = blank = files = 0
    for entry in data:
        code    += entry.get("Code",    0)
        comment += entry.get("Comment", 0)
        blank   += entry.get("Blank",   0)
        files   += entry.get("Count",   0)
    return {"code": code, "comment": comment, "blank": blank, "files": files, "scc_ok": True}


def fail(reason: str) -> dict:
    return {"code": 0, "comment": 0, "blank": 0, "files": 0, "scc_ok": False, "error": reason}


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def measure_python(package_key: str) -> dict:
    """package_key: 'name==version'"""
    with tempfile.TemporaryDirectory(dir=WORK_DIR) as tmpdir:
        dl_dir  = os.path.join(tmpdir, "download")
        src_dir = os.path.join(tmpdir, "source")
        os.makedirs(dl_dir)
        os.makedirs(src_dir)

        r = subprocess.run(
            ["pip", "download", "--no-deps", "--dest", dl_dir, package_key],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return fail(f"pip download failed: {r.stderr.strip()[:200]}")

        downloaded = os.listdir(dl_dir)
        if not downloaded:
            return fail("pip download produced no files")

        # Prefer wheel, then sdist; skip .egg
        wheels  = [f for f in downloaded if f.endswith(".whl")]
        sdists  = [f for f in downloaded if f.endswith(".tar.gz")]
        zips    = [f for f in downloaded if f.endswith(".zip")]

        if wheels:
            archive = os.path.join(dl_dir, wheels[0])
            with zipfile.ZipFile(archive) as z:
                z.extractall(src_dir)
        elif sdists:
            archive = os.path.join(dl_dir, sdists[0])
            with tarfile.open(archive, "r:gz") as t:
                t.extractall(src_dir)
        elif zips:
            archive = os.path.join(dl_dir, zips[0])
            with zipfile.ZipFile(archive) as z:
                z.extractall(src_dir)
        else:
            return fail(f"no usable archive (got: {downloaded})")

        return run_scc(src_dir)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

def measure_go(package_key: str) -> dict:
    """package_key: 'module@version'"""
    with tempfile.TemporaryDirectory(dir=WORK_DIR) as gopath:
        env = os.environ.copy()
        env["GOPATH"]       = gopath
        env["GONOSUMCHECK"] = "*"
        env["GONOSUMDB"]    = "*"
        env["GONOPROXY"]    = "*"
        env["GOFLAGS"]      = ""

        r = subprocess.run(
            ["go", "mod", "download", "-json", package_key],
            capture_output=True, text=True, env=env
        )
        if r.returncode != 0:
            return fail(f"go mod download failed: {r.stderr.strip()[:200]}")

        # Output may contain multiple JSON objects; take the last complete one
        out = r.stdout.strip()
        if not out:
            return fail("go mod download produced no output")

        # Parse the last JSON object from potentially concatenated output
        depth = 0
        last_start = 0
        obj_json = None
        for i, ch in enumerate(out):
            if ch == "{":
                if depth == 0:
                    last_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj_json = out[last_start:i+1]

        if not obj_json:
            return fail("could not parse go mod download output")

        info = json.loads(obj_json)
        src_dir = info.get("Dir", "")
        if not src_dir or not os.path.isdir(src_dir):
            return fail(f"go mod download Dir not found: {src_dir!r}")

        return run_scc(src_dir)


# ---------------------------------------------------------------------------
# Cargo
# ---------------------------------------------------------------------------

def measure_cargo(package_key: str) -> dict:
    """package_key: 'name==version'"""
    name, _, version = package_key.partition("==")
    if not version:
        return fail(f"cannot parse cargo key: {package_key!r}")

    url = (
        f"https://static.crates.io/crates/{name}/{name}-{version}.crate"
    )
    with tempfile.TemporaryDirectory(dir=WORK_DIR) as tmpdir:
        crate_path = os.path.join(tmpdir, f"{name}-{version}.crate")
        try:
            urllib.request.urlretrieve(url, crate_path)
        except urllib.error.HTTPError as e:
            return fail(f"HTTP {e.code} fetching {url}")
        except Exception as e:
            return fail(f"download error: {e}")

        src_dir = os.path.join(tmpdir, "source")
        os.makedirs(src_dir)
        try:
            with tarfile.open(crate_path, "r:gz") as t:
                t.extractall(src_dir)
        except Exception as e:
            return fail(f"extract error: {e}")

        return run_scc(src_dir)


# ---------------------------------------------------------------------------
# Maven / Gradle (shared Maven Central logic)
# ---------------------------------------------------------------------------

def _maven_central_url(group: str, artifact: str, version: str, classifier: str = "") -> str:
    group_path = group.replace(".", "/")
    suffix = f"-{classifier}" if classifier else ""
    return (
        f"https://repo1.maven.org/maven2/"
        f"{group_path}/{artifact}/{version}/"
        f"{artifact}-{version}{suffix}.jar"
    )


def _download_url(url: str, dest: str) -> bool:
    try:
        urllib.request.urlretrieve(url, dest)
        return True
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def measure_maven_pkg(group: str, artifact: str, version: str) -> dict:
    with tempfile.TemporaryDirectory(dir=WORK_DIR) as tmpdir:
        jar_path = os.path.join(tmpdir, "pkg.jar")

        sources_url = _maven_central_url(group, artifact, version, "sources")
        regular_url = _maven_central_url(group, artifact, version)

        if not _download_url(sources_url, jar_path):
            if not _download_url(regular_url, jar_path):
                return fail("no sources jar and no regular jar found on Maven Central")

        src_dir = os.path.join(tmpdir, "source")
        os.makedirs(src_dir)
        try:
            with zipfile.ZipFile(jar_path) as z:
                z.extractall(src_dir)
        except Exception as e:
            return fail(f"unzip error: {e}")

        return run_scc(src_dir)


def measure_maven(package_key: str) -> dict:
    """package_key: 'group:artifact:type:version'"""
    parts = package_key.split(":")
    if len(parts) < 4:
        return fail(f"cannot parse maven key: {package_key!r}")
    group, artifact, _type, version = parts[0], parts[1], parts[2], parts[3]
    return measure_maven_pkg(group, artifact, version)


def measure_gradle(package_key: str) -> dict:
    """package_key: 'group:artifact:version'"""
    parts = package_key.split(":")
    if len(parts) < 3:
        return fail(f"cannot parse gradle key: {package_key!r}")
    group, artifact, version = parts[0], parts[1], parts[2]
    return measure_maven_pkg(group, artifact, version)


# ---------------------------------------------------------------------------
# npm (stub)
# ---------------------------------------------------------------------------

def measure_npm(package_key: str) -> dict:
    """package_key: 'name@version' — stub for future use."""
    name, _, version = package_key.rpartition("@")
    if not name:
        return fail(f"cannot parse npm key: {package_key!r}")
    # Future: fetch tarball from https://registry.npmjs.org/<name>/-/<name>-<version>.tgz
    return fail("npm measurement not yet implemented")


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

MEASURE_FN = {
    "python":  measure_python,
    "go":      measure_go,
    "cargo":   measure_cargo,
    "maven":   measure_maven,
    "gradle":  measure_gradle,
    "npm":     measure_npm,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_ecosystem(eco: str, packages: list, existing: dict) -> dict:
    results = dict(existing)

    pending = [p for p in packages if p not in results or not results[p].get("scc_ok")]
    total   = len(packages)
    already = total - len(pending)

    if already:
        print(f"[{eco}] {already}/{total} already measured, processing {len(pending)} remaining")

    if not pending:
        return results

    def worker(item):
        idx, pkg = item
        print(f"[{eco}] [{idx}/{total}] {pkg}")
        try:
            return pkg, MEASURE_FN[eco](pkg)
        except Exception as e:
            return pkg, fail(str(e))

    indexed = [(already + i + 1, pkg) for i, pkg in enumerate(pending)]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(worker, item): item for item in indexed}
        for fut in as_completed(futures):
            pkg, result = fut.result()
            results[pkg] = result

    ok    = sum(1 for v in results.values() if v.get("scc_ok"))
    total_code = sum(v.get("code", 0) for v in results.values())
    print(f"[{eco}] subtotal: {ok}/{len(results)} ok, {total_code:,} LOC measured\n")

    return results


def main():
    parser = argparse.ArgumentParser(description="Measure LOC for transitive dependencies")
    parser.add_argument(
        "--eco", nargs="+", choices=ALL_ECOSYSTEMS, default=ALL_ECOSYSTEMS,
        help="Ecosystems to process (default: all)"
    )
    args = parser.parse_args()

    if not INPUT_JSON.exists():
        print(f"ERROR: {INPUT_JSON} not found")
        sys.exit(1)

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    with open(INPUT_JSON) as f:
        merged = json.load(f)

    # Load existing results for resumption
    if OUTPUT_JSON.exists():
        with open(OUTPUT_JSON) as f:
            all_results = json.load(f)
    else:
        all_results = {eco: {} for eco in ALL_ECOSYSTEMS}

    for eco in ALL_ECOSYSTEMS:
        if eco not in all_results:
            all_results[eco] = {}

    for eco in args.eco:
        packages = merged.get(eco, [])
        if not packages:
            print(f"[{eco}] no packages, skipping")
            continue
        print(f"\n=== {eco}: {len(packages)} unique packages ===")
        all_results[eco] = process_ecosystem(eco, packages, all_results.get(eco, {}))

        # Save after each ecosystem so partial results survive crashes
        with open(OUTPUT_JSON, "w") as f:
            json.dump(all_results, f, indent=2)

    print(f"\nResults saved to {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
