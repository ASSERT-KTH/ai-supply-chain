#!/usr/bin/env bash
# =============================================================================
# 03_count_deps.sh — Count direct and transitive dependencies per repo
# =============================================================================
# Usage: ./03_count_deps.sh [--all]
#   --all  include "alternative" repos
#
# Prerequisites: run 01_clone_repos.sh first.
# Requirements:  python3 + PyYAML, jq
#   Optional (per ecosystem):
#     go       — transitive Go module resolution
#     cargo    — transitive Rust crate resolution
#     npm/node — transitive JS/TS resolution
#
# What this script measures:
#   - Direct deps:     packages explicitly declared in manifest files
#   - Transitive deps: full closure (where tooling supports it)
#
# Python / CUDA packages (pytorch, deepspeed, etc.) cannot be installed
# without a CUDA environment, so Python transitive counts are manifest-only
# (direct deps only). This is clearly flagged in the output.
# The LOC estimate section uses per-ecosystem median LOC/package to convert
# package counts into an order-of-magnitude LOC estimate for the paper.
#
# Per-ecosystem average LOC/package used for estimates (order-of-magnitude):
#   PyPI    ~5,000   (Python Software Foundation / PyPI size studies)
#   Go mod  ~8,000   (Go module registry distribution)
#   npm     ~1,500   (npm package size surveys)
#   Maven   ~15,000  (Maven Central artifact studies)
#   Cargo   ~5,000   (crates.io registry distribution)
#
# Outputs:
#   results/deps_per_project/     per-project JSON cache files
#   results/deps_summary.csv      one row per repo
#   results/deps_report.tsv       append-log across runs
#   results/deps_summary.txt      human-readable summary + LOC estimate
# =============================================================================

set -uo pipefail   # no -e: failures handled per-repo

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/stack_config.yaml"
REPOS_DIR="$SCRIPT_DIR/repos"
RESULTS_DIR="$SCRIPT_DIR/results"
DEPS_DIR="$RESULTS_DIR/deps_per_project"
MEASURE_ALL=false
START_TS=$(date +%s)

[[ "${1:-}" == "--all" ]] && MEASURE_ALL=true

# ── dependency check ──────────────────────────────────────────────────────────
for cmd in python3 jq; do
    command -v "$cmd" &>/dev/null || { echo "ERROR: $cmd not found" >&2; exit 1; }
done
python3 -c "import yaml" 2>/dev/null || {
    echo "ERROR: PyYAML not installed (pip install pyyaml)" >&2; exit 1
}

# Optional tools — note which are available
HAS_GO=false;    command -v go    &>/dev/null && HAS_GO=true
HAS_CARGO=false; command -v cargo &>/dev/null && HAS_CARGO=true
HAS_NPM=false;   command -v npm   &>/dev/null && HAS_NPM=true
HAS_MVN=false;   command -v mvn   &>/dev/null && HAS_MVN=true
# Gradle: prefer repo's ./gradlew over system gradle.
# gradlew uses the version declared in gradle-wrapper.properties, which is correct
# for that repo. System gradle may be too old (e.g. 4.x vs required 9.x).
# HAS_GRADLE signals java is available; the actual executable is chosen per-repo.
HAS_GRADLE=false
if command -v java &>/dev/null; then
    HAS_GRADLE=true
    # GRADLE_CMD is a system-gradle fallback; gradlew takes priority in count_gradle
    command -v gradle &>/dev/null && GRADLE_CMD="gradle" || GRADLE_CMD=""
fi
# uv pip compile: resolves Python transitive deps from PyPI metadata without installing.
# CUDA is required to *run* ML packages, not to *resolve* their dependency graph.
# uv may live in ~/.local/bin (installed via pip install uv) rather than /usr/bin.
UV_CMD=""
for _uv_candidate in uv ~/.local/bin/uv; do
    if command -v "$_uv_candidate" &>/dev/null 2>&1; then
        UV_CMD="$_uv_candidate"; break
    fi
done
HAS_UV=false; [[ -n "$UV_CMD" ]] && HAS_UV=true
unset _uv_candidate
# pip --dry-run --report: fallback for transitive Python resolution (pip >= 22.2)
HAS_PIP_REPORT=false
if command -v pip &>/dev/null; then
    _pip_ver=$(pip --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo "0.0")
    _pip_major=$(echo "$_pip_ver" | cut -d. -f1)
    _pip_minor=$(echo "$_pip_ver" | cut -d. -f2)
    if [[ "$_pip_major" -gt 22 ]] || \
       [[ "$_pip_major" -eq 22 && "$_pip_minor" -ge 2 ]]; then
        HAS_PIP_REPORT=true
    fi
    unset _pip_ver _pip_major _pip_minor
fi

mkdir -p "$RESULTS_DIR" "$DEPS_DIR"

# ── parse YAML once ───────────────────────────────────────────────────────────
MANIFEST=$(STACK_CONFIG="$CONFIG" python3 -c "
import os, yaml
layers = ['data_pipelines', 'training', 'integration_serving', 'cross_cutting']
cfg = yaml.safe_load(open(os.environ['STACK_CONFIG']))
for layer in layers:
    for p in cfg.get(layer, {}).get('projects', []):
        role = p.get('role', '').replace('\t', ' ').replace('\n', ' ')
        print(f\"{layer}\t{p.get('repo','')}\t{p.get('status','')}\t{role}\")
")

# ── output files ──────────────────────────────────────────────────────────────
CSV="$RESULTS_DIR/deps_summary.csv"
REPORT_TSV="$RESULTS_DIR/deps_report.tsv"
HLINE="════════════════════════════════════════════════════════════"

printf "layer,project,repo,status,ecosystems,direct_deps,transitive_deps,transitive_method\n" > "$CSV"
if [[ ! -f "$REPORT_TSV" ]]; then
    printf "timestamp\tstatus\tlayer\trepo\tecosystems\tdirect\ttransitive\tmethod\n" > "$REPORT_TSV"
fi

# ── counters ──────────────────────────────────────────────────────────────────
measured_count=0
skip_count=0
fail_count=0
current=0
LAYERS=("data_pipelines" "training" "integration_serving" "cross_cutting")

# Accumulate for LOC estimate
declare -A eco_direct   # ecosystem -> total direct packages seen
declare -A eco_transitive
for eco in python go npm maven cargo gradle cpp; do
    eco_direct[$eco]=0
    eco_transitive[$eco]=0
done


# ── pre-count ─────────────────────────────────────────────────────────────────
total=0
while IFS=$'\t' read -r _l _r status _role; do
    [[ "$MEASURE_ALL" == false && "$status" == "alternative" ]] && continue
    total=$((total+1))
done <<< "$MANIFEST"

# ── graceful interrupt ────────────────────────────────────────────────────────
INTERRUPTED=false
trap 'INTERRUPTED=true; echo ""' INT TERM

# ── helpers ───────────────────────────────────────────────────────────────────
ts()  { date "+%H:%M:%S"; }
pad() { printf "%0${#total}d" "$1"; }
fmt() { python3 -c "print(f'{int(\"$1\"):,}')" 2>/dev/null || echo "$1"; }
SEP="  ────────────────────────────────────────────────────────────"

log_row() {
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "$4" "$5" "$6" "$7" \
        >> "$REPORT_TSV"
}

python_setup_py_direct_count() {
    local dir="$1"
    python3 - <<'PY' 2>/dev/null
import ast, pathlib, sys
path = pathlib.Path(sys.argv[1]) / 'setup.py'
if not path.is_file():
    print(0)
    sys.exit(0)
text = path.read_text(encoding='utf-8', errors='ignore')
try:
    tree = ast.parse(text, filename=str(path))
except Exception:
    print(0)
    sys.exit(0)
assigns = {}

def eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        items = []
        for elt in node.elts:
            v = eval_node(elt)
            if isinstance(v, str):
                items.append(v)
            else:
                return None
        return items
    if isinstance(node, ast.Name):
        return assigns.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('fetch_requirements', 'parse_requirements', 'load_requirements'):
        if len(node.args) == 1:
            arg = eval_node(node.args[0])
            if isinstance(arg, str):
                req = (path.parent / arg).resolve()
                if req.is_file():
                    lines = [l.strip() for l in req.read_text(encoding='utf-8', errors='ignore').splitlines() if l.strip() and not l.strip().startswith('#')]
                    return lines
    return None

class Visitor(ast.NodeVisitor):
    def visit_Assign(self, node):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            val = eval_node(node.value)
            if val is not None:
                assigns[node.targets[0].id] = val
        self.generic_visit(node)

    def visit_Call(self, node):
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name == 'setup':
            for kw in node.keywords:
                if kw.arg == 'install_requires':
                    val = eval_node(kw.value)
                    if isinstance(val, list):
                        print(len(val))
                        sys.exit(0)
        self.generic_visit(node)

Visitor().visit(tree)
# Regex fallback: some setup.py files (e.g. ray) store install_requires in an
# intermediate object (setup_spec.install_requires) rather than a direct literal
# inside setup(). As a last resort, find any module-level assignment whose LHS
# name contains 'install_requires' or 'requirements' and whose RHS is a list of
# quoted strings, and return the largest such count found.
import re
best = 0
for m in re.finditer(
        r'(?:install_requires|requirements)\s*=\s*\[([\s\S]*?)\]',
        text, re.IGNORECASE):
    items = re.findall(r'["\']([^"\']+)["\']', m.group(1))
    if len(items) > best:
        best = len(items)
print(best)
PY
}

python_parse_python_requirements() {
    local file="$1"
    _PY_FILE="$file" python3 - <<'PY' 2>/dev/null
import re, os, sys
from pathlib import Path
path = Path(os.environ["_PY_FILE"])
if not path.is_file():
    sys.exit(0)
for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
    line = line.strip()
    if not line or line.startswith('#') or line.startswith('-'):
        continue
    if line.startswith('-r') or line.startswith('--'):  # include file or option directives
        continue
    line = re.split(r'[;#]', line, 1)[0].strip()
    if not line:
        continue
    if line.startswith('git+') or line.startswith('http'):
        continue
    match = re.match(r'^([A-Za-z0-9_.+-]+)', line)
    if match:
        print(match.group(1).lower())
PY
}

python_parse_setup_cfg_deps() {
    local file="$1"
    _PY_FILE="$file" python3 - <<'PY' 2>/dev/null
import configparser, re, os, sys
from pathlib import Path
path = Path(os.environ["_PY_FILE"])
if not path.is_file():
    sys.exit(0)
parser = configparser.ConfigParser()
parser.read(path)
reqs = parser.get('options', 'install_requires', fallback='')
for line in reqs.splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    line = re.split(r'[;#]', line, 1)[0].strip()
    if not line:
        continue
    match = re.match(r'^([A-Za-z0-9_.+-]+)', line)
    if match:
        print(match.group(1).lower())
PY
}

python_parse_pyproject_deps() {
    local file="$1"
    _PY_FILE="$file" python3 - <<'PY' 2>/dev/null
import re, os, sys
from pathlib import Path
try:
    import tomllib
except ImportError:
    import tomli as tomllib
path = Path(os.environ["_PY_FILE"])
if not path.is_file():
    sys.exit(0)
text = path.read_bytes()
data = tomllib.loads(text.decode('utf-8', errors='ignore'))
seen = set()

def normalize(item):
    if not item:
        return None
    if isinstance(item, dict):
        item = item.get('version', '')
    if not isinstance(item, str):
        return None
    item = item.split(';', 1)[0].strip()
    item = item.split('[', 1)[0].strip()
    item = re.split(r'[<>=!~]', item, 1)[0].strip()
    if not item or item.startswith('git+') or item.startswith('http'):
        return None
    return item.lower()

for dep in data.get('project', {}).get('dependencies', []):
    name = normalize(dep)
    if name:
        seen.add(name)

for key, val in data.get('tool', {}).get('poetry', {}).get('dependencies', {}).items():
    if key.lower() != 'python':
        seen.add(key.lower())

for key, val in data.get('tool', {}).get('poetry', {}).get('dev-dependencies', {}).items():
    if key.lower() != 'python':
        seen.add(key.lower())

for item in sorted(seen):
    print(item)
PY
}

python_setup_py_direct_deps() {
    local dir="$1"
    _PY_DIR="$dir" python3 - <<'PY' 2>/dev/null
import re, os, sys
from pathlib import Path
path = Path(os.environ["_PY_DIR"]) / 'setup.py'
if not path.is_file():
    sys.exit(0)
text = path.read_text(encoding='utf-8', errors='ignore')
seen = set()
for m in re.finditer(r'(?:install_requires|requirements)\s*=\s*\[([^\]]*)\]', text, re.IGNORECASE | re.DOTALL):
    block = m.group(1)
    for item in re.findall(r'["\']([^"\']+)["\']', block):
        item = re.split(r'[;#]', item, 1)[0].strip()
        item = re.split(r'[\[<>=!~]', item, 1)[0].strip()
        if item and not item.startswith('-') and not item.startswith('git+') and not item.startswith('http'):
            seen.add(item.lower())
for item in sorted(seen):
    print(item)
PY
}

python_parse_go_mod_deps() {
    local file="$1"
    GO_MOD_FILE="$file" python3 - <<'PY' 2>/dev/null
import os, re
from pathlib import Path
path = Path(os.environ["GO_MOD_FILE"])
if not path.is_file():
    raise SystemExit(0)
text = path.read_text(encoding='utf-8', errors='ignore')
seen = set()
for block in re.findall(r'(?m)^\s*require\s*\((.*?)^\s*\)', text, re.DOTALL | re.MULTILINE):
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] != 'require':
            seen.add(parts[0])
for line in text.splitlines():
    line = line.strip()
    if line.startswith('require ') and '(' not in line:
        parts = line.split()
        if len(parts) >= 2:
            seen.add(parts[1])
for item in sorted(seen):
    print(item)
PY
}

python_parse_cargo_deps() {
    local file="$1"
    _PY_FILE="$file" python3 - <<'PY' 2>/dev/null
import re, os, sys
from pathlib import Path
try:
    import tomllib
except ImportError:
    import tomli as tomllib
path = Path(os.environ["_PY_FILE"])
if not path.is_file():
    sys.exit(0)
text = path.read_bytes()
data = tomllib.loads(text.decode('utf-8', errors='ignore'))
seen = set()
for section in ['dependencies', 'dev-dependencies', 'build-dependencies']:
    items = data.get(section, {})
    if isinstance(items, dict):
        for key in items.keys():
            seen.add(key.lower())
for item in sorted(seen):
    print(item)
PY
}

python_parse_package_json_deps() {
    local file="$1"
    jq -r '(.dependencies // {} | keys[]) , (.devDependencies // {} | keys[]) | select(. != null)' "$file" 2>/dev/null | sort -u | tr '[:upper:]' '[:lower:]'
}

python_parse_pom_deps() {
    local file="$1"
    _PY_FILE="$file" python3 - <<'PY' 2>/dev/null
import xml.etree.ElementTree as ET, os, sys
from pathlib import Path
path = Path(os.environ["_PY_FILE"])
if not path.is_file():
    sys.exit(0)
try:
    tree = ET.parse(path)
except Exception:
    sys.exit(0)
M = 'http://maven.apache.org/POM/4.0.0'
# Exclude dependencies declared only in dependencyManagement or profiles.
excl = set()
for xpath in [
    f'.//{{{M}}}dependencyManagement/{{{M}}}dependencies/{{{M}}}dependency',
    './/dependencyManagement/dependencies/dependency',
    f'.//{{{M}}}profiles//{{{M}}}dependencies/{{{M}}}dependency',
    './/profiles//dependencies/dependency',
]:
    for d in tree.findall(xpath):
        excl.add(id(d))
all_deps = tree.findall(f'.//{{{M}}}dependencies/{{{M}}}dependency')
if not all_deps:
    all_deps = tree.findall('.//dependencies/dependency')
seen = set()
for dep in all_deps:
    if id(dep) in excl:
        continue
    group = dep.find(f'{{{M}}}groupId') or dep.find('groupId')
    artifact = dep.find(f'{{{M}}}artifactId') or dep.find('artifactId')
    if group is not None and artifact is not None:
        seen.add(f"{group.text.strip().lower()}:{artifact.text.strip().lower()}")
for item in sorted(seen):
    print(item)
PY
}

python_parse_gradle_deps() {
    local dir="$1"
    _PY_DIR="$dir" python3 - <<'PY' 2>/dev/null
import re, os
from pathlib import Path
try:
    import tomllib
except ImportError:
    import tomli as tomllib
path = Path(os.environ["_PY_DIR"])
seen = set()
for candidate in path.rglob('libs.versions.toml'):
    try:
        data = tomllib.loads(candidate.read_bytes().decode('utf-8', errors='ignore'))
        libs = data.get('libraries', {})
        for val in libs.values():
            if isinstance(val, str):
                seen.add(val.strip().lower())
            elif isinstance(val, dict):
                mod = val.get('module') or val.get('group')
                if mod:
                    seen.add(str(mod).strip().lower())
    except Exception:
        pass
for candidate in path.rglob('dependencies.gradle'):
    text = candidate.read_text(encoding='utf-8', errors='ignore')
    for match in re.findall(r'"([^"]+:[^"]+:[^"]+)"', text):
        seen.add(match.strip().lower())
for candidate in path.rglob('build.gradle*'):
    text = candidate.read_text(encoding='utf-8', errors='ignore')
    for match in re.findall(r'([a-zA-Z0-9_.-]+:[a-zA-Z0-9_.-]+:[^"\s\)]+)', text):
        seen.add(match.strip().lower())
for item in sorted(seen):
    print(item)
PY
}

count_python() {
    local dir="$1"
    DIRECT=0; TRANSITIVE=-1; METHOD="none"
    DIRECT_DEPS=""
    TRANSITIVE_DEPS=""
    local found=false

    # ── direct deps: parse manifest files ────────────────────────────────────
    for f in "$dir"/requirements*.txt; do
        if [[ -f "$f" ]]; then
            [[ "$f" =~ requirements-(dev|test|build|docs|lint|ci|typing) ]] && continue
            DIRECT_DEPS="$DIRECT_DEPS"$'\n'"$(python_parse_python_requirements "$f" || true)"
        fi
    done
    for f in "$dir/requirements/common.txt" \
             "$dir/requirements/base.txt" \
             "$dir/requirements/core.txt"; do
        if [[ -f "$f" ]]; then
            DIRECT_DEPS="$DIRECT_DEPS"$'\n'"$(python_parse_python_requirements "$f" || true)"
        fi
    done
    if [[ -f "$dir/setup.cfg" ]]; then
        DIRECT_DEPS="$DIRECT_DEPS"$'\n'"$(python_parse_setup_cfg_deps "$dir/setup.cfg" || true)"
    fi
    if [[ -f "$dir/pyproject.toml" ]]; then
        DIRECT_DEPS="$DIRECT_DEPS"$'\n'"$(python_parse_pyproject_deps "$dir/pyproject.toml" || true)"
    fi
    if [[ -f "$dir/setup.py" ]]; then
        DIRECT_DEPS="$DIRECT_DEPS"$'\n'"$(python_setup_py_direct_deps "$dir" || true)"
    fi

    DIRECT_DEPS="$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | sort -u)"
    if [[ -n "$DIRECT_DEPS" ]]; then
        DIRECT=$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | wc -l | tr -d ' ')
        found=true
    fi

    if [[ "$DIRECT" -eq 0 ]]; then
        local d=0
        if [[ -f "$dir/setup.cfg" ]]; then
            d=$((d + $(python3 -c "
import configparser
c = configparser.ConfigParser()
c.read('$dir/setup.cfg')
reqs = c.get('options', 'install_requires', fallback='')
print(len([l for l in reqs.splitlines() if l.strip() and not l.strip().startswith('#')]))
" 2>/dev/null || echo 0)))
        fi
        if [[ -f "$dir/pyproject.toml" ]]; then
            d=$((d + $(python3 -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
import sys
with open('$dir/pyproject.toml', 'rb') as f:
    t = tomllib.load(f)
seen = set()
for d in t.get('project', {}).get('dependencies', []):
    import re as _re
    name = _re.split(r'[\[<>=!~;]', d, 1)[0].strip().lower()
    if name and name != 'python':
        seen.add(name)
for k in t.get('tool', {}).get('poetry', {}).get('dependencies', {}).keys():
    if k.lower() != 'python':
        seen.add(k.lower())
print(len(seen))
" 2>/dev/null || echo 0)))
        fi
        if [[ -f "$dir/setup.py" && "$d" -eq 0 ]]; then
            local setup_py_count
            setup_py_count=$(python_setup_py_direct_count "$dir")
            setup_py_count=${setup_py_count:-0}
            d=$((d + setup_py_count))
            [[ "$setup_py_count" -gt 0 ]] && found=true
        fi
        if [[ "$d" -eq 0 ]]; then
            while IFS= read -r candidate; do
                local relpath="${candidate#$dir/}"
                echo "$relpath" | grep -qiE '/(test|tests|dev|docs|example|examples|contrib|bench|sample|build)/' && continue
                d=$((d + $(python3 -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
try:
    with open('$candidate', 'rb') as f:
        t = tomllib.load(f)
    deps = t.get('project', {}).get('dependencies', [])
    print(len([x for x in deps if not x.lower().startswith('python')]))
except: print(0)
" 2>/dev/null || echo 0)))
            done < <(find "$dir" -maxdepth 4 -name "pyproject.toml" \
                         -not -path "*/.git/*" 2>/dev/null)
            while IFS= read -r sub_setup; do
                [[ -f "$(dirname "$sub_setup")/pyproject.toml" ]] && continue
                local sub_relpath="${sub_setup#$dir/}"
                echo "$sub_relpath" | grep -qiE '/(test|tests|dev|docs|example|examples|contrib|bench|sample|build)/' && continue
                local sub_count
                sub_count=$(python_setup_py_direct_count "$(dirname "$sub_setup")")
                sub_count=${sub_count:-0}
                d=$((d + sub_count))
            done < <(find "$dir" -mindepth 2 -maxdepth 3 -name "setup.py" \
                         -not -path "*/.git/*" 2>/dev/null)
        fi
        [[ $d -gt 0 ]] && found=true
        DIRECT=$d
    fi

    [[ $DIRECT -gt 0 ]] && METHOD="manifest"

    # ── transitive deps: resolve from PyPI metadata (no install, no CUDA) ────
    # CUDA is required to *run* these packages, not to *resolve* their dep graph.
    # uv/pip resolve by fetching PyPI JSON metadata only — no binaries downloaded.
    #
    # Spec file selection:
    #   1. Root pyproject.toml > setup.cfg > requirements.txt (plain packages)
    #   2. Monorepo fallback: scan depth ≤ 3 for pyproject.toml files that
    #      declare a [project] section, excluding test/dev/docs/contrib subtrees.
    #      Among candidates, prefer the directory whose name best matches the
    #      repo name; otherwise pick the shallowest path.
    # Excluded: requirements-dev/test/build files (list dev tools, not runtime deps)
    local spec=""
    if [[ -f "$dir/pyproject.toml" ]]; then
        spec="$dir/pyproject.toml"
    elif [[ -f "$dir/setup.cfg" ]]; then
        spec="$dir/setup.cfg"
    elif [[ -f "$dir/setup.py" ]]; then
        spec="$dir/setup.py"
    else
        for f in "$dir/requirements.txt" \
                 "$dir/requirements/common.txt" \
                 "$dir/requirements/base.txt" \
                 "$dir/requirements/core.txt"; do
            [[ -f "$f" ]] && { spec="$f"; break; }
        done
    fi

    # Monorepo fallback: no root manifest found — collect ALL sub-packages,
    # resolve each with uv, and deduplicate by package name across them.
    # This is correct for repos like cuda-python that ship multiple co-installed
    # packages: their combined transitive surface is the union, not any single pkg.
    local monorepo_specs=()
    if [[ -z "$spec" ]]; then
        while IFS= read -r candidate; do
            local relpath="${candidate#$dir/}"
            echo "$relpath" | grep -qiE '/(test|tests|dev|docs|example|examples|contrib|bench|sample|build)/' && continue
            if [[ "$candidate" == */pyproject.toml ]]; then
                # Accept both PEP 517 [project] tables and Poetry [tool.poetry] tables
                grep -qE '^\[(project|tool\.poetry)\]' "$candidate" 2>/dev/null || continue
            fi
            # Skip setup.py when a sibling pyproject.toml exists: uv pip compile
            # rejects a bare directory with --output-file, and the pyproject.toml
            # will be processed on its own iteration.
            if [[ "$candidate" == */setup.py ]]; then
                [[ -f "$(dirname "$candidate")/pyproject.toml" ]] && continue
            fi
            monorepo_specs+=("$candidate")
        done < <(find "$dir" \( -name "pyproject.toml" -o -name "setup.cfg" -o -name "setup.py" -o -name "requirements.txt" \) -maxdepth 4 2>/dev/null)
        # If exactly one candidate, treat it as the plain spec (simpler path)
        [[ "${#monorepo_specs[@]}" -eq 1 ]] && spec="${monorepo_specs[0]}" && monorepo_specs=()
    fi

    if [[ -n "$spec" ]] || [[ "${#monorepo_specs[@]}" -gt 0 ]]; then
        local tmp_out
        tmp_out=$(mktemp)

        if [[ "$HAS_UV" == true ]]; then
            # --python-version 3.11: many ML packages do not yet declare Python 3.13
            # support; 3.11 is the current production baseline for ML workloads.
            if [[ -n "$spec" ]]; then
                local compile_target="$spec"
                if [[ "$spec" == */setup.py ]]; then
                    # Prefer sibling pyproject.toml; uv pip compile rejects a bare
                    # directory when --output-file is used.
                    local sibling_pep517="$(dirname "$spec")/pyproject.toml"
                    if [[ -f "$sibling_pep517" ]]; then
                        compile_target="$sibling_pep517"
                    else
                        compile_target="$(dirname "$spec")"
                    fi
                fi
                $UV_CMD pip compile --quiet --python-version 3.11 \
                    "$compile_target" --output-file "$tmp_out" 2>/dev/null
            else
                # Monorepo: resolve each sub-package, deduplicate by package name
                local tmp_merged
                tmp_merged=$(mktemp)
                for candidate in "${monorepo_specs[@]}"; do
                    local tmp_pkg
                    tmp_pkg=$(mktemp)
                    local compile_target="$candidate"
                    if [[ "$candidate" == */setup.py ]]; then
                        compile_target="$(dirname "$candidate")"
                    fi
                    $UV_CMD pip compile --quiet --python-version 3.11 \
                        "$compile_target" --output-file "$tmp_pkg" 2>/dev/null
                    # Extract bare package names (before ==) for deduplication
                    grep -oE '^[a-zA-Z0-9][a-zA-Z0-9._-]+' "$tmp_pkg" >> "$tmp_merged"
                    rm -f "$tmp_pkg"
                done
                sort -u "$tmp_merged" > "$tmp_out"
                rm -f "$tmp_merged"
            fi
            local t
            t=$(grep -cE '^[a-zA-Z0-9]' "$tmp_out" 2>/dev/null || true)
            t=${t:-0}
            if [[ "$t" -gt 0 ]]; then
                TRANSITIVE=$t
                TRANSITIVE_DEPS="$(grep -E '^[a-zA-Z0-9]' "$tmp_out" 2>/dev/null | grep -oE '^[a-zA-Z0-9][a-zA-Z0-9._-]+==[^ ]+' || true)"
                METHOD="uv_pip_compile"
                found=true
            fi

            # ── Fallback A: primary spec failed — try requirements files ─────────
            # Triggered when the root pyproject.toml/setup.cfg/setup.py could not
            # be compiled by uv (e.g. requires CUDA_HOME, missing build artefacts,
            # malformed metadata). We look for a plain requirements.txt or a
            # requirements/common.txt which uv can always compile without building.
            if [[ "$t" -eq 0 ]] && [[ -n "$spec" ]] && [[ "$spec" != *.txt ]]; then
                local req_fallback=""
                for _rf in "$dir/requirements/common.txt" \
                            "$dir/requirements/base.txt" \
                            "$dir/requirements/core.txt" \
                            "$dir/requirements.txt"; do
                    [[ -f "$_rf" ]] && { req_fallback="$_rf"; break; }
                done
                if [[ -n "$req_fallback" ]]; then
                    > "$tmp_out"
                    $UV_CMD pip compile --quiet --python-version 3.11 \
                        "$req_fallback" --output-file "$tmp_out" 2>/dev/null
                    t=$(grep -cE '^[a-zA-Z0-9]' "$tmp_out" 2>/dev/null || true)
                    t=${t:-0}
                    if [[ "$t" -gt 0 ]]; then
                        TRANSITIVE=$t
                        TRANSITIVE_DEPS="$(grep -E '^[a-zA-Z0-9]' "$tmp_out" 2>/dev/null | grep -oE '^[a-zA-Z0-9][a-zA-Z0-9._-]+==[^ ]+' || true)"
                        METHOD="uv_pip_compile"
                        found=true
                    fi
                fi
            fi

            # ── Fallback B: primary spec failed — try monorepo sub-packages ──────
            # Triggered when the root manifest (pyproject.toml / setup.cfg) is a
            # monorepo stub that uv cannot compile (e.g. promptflow root setup.cfg
            # has no install_requires; the real packages live under src/).
            if [[ "$t" -eq 0 ]] && [[ -n "$spec" ]] && [[ "$spec" != *.txt ]]; then
                local fb_specs=()
                while IFS= read -r candidate; do
                    local relpath="${candidate#$dir/}"
                    echo "$relpath" | grep -qiE '/(test|tests|dev|docs|example|examples|contrib|bench|sample|build)/' && continue
                    if [[ "$candidate" == */pyproject.toml ]]; then
                        # Accept both PEP 517 [project] tables and Poetry [tool.poetry] tables
                        grep -qE '^\[(project|tool\.poetry)\]' "$candidate" 2>/dev/null || continue
                    fi
                    if [[ "$candidate" == */setup.py ]]; then
                        [[ -f "$(dirname "$candidate")/pyproject.toml" ]] && continue
                    fi
                    fb_specs+=("$candidate")
                done < <(find "$dir" -mindepth 2 \( -name "pyproject.toml" -o -name "setup.cfg" -o -name "setup.py" \) -maxdepth 4 2>/dev/null)
                if [[ "${#fb_specs[@]}" -gt 0 ]]; then
                    local tmp_merged_fb
                    tmp_merged_fb=$(mktemp)
                    for candidate in "${fb_specs[@]}"; do
                        local tmp_pkg_fb
                        tmp_pkg_fb=$(mktemp)
                        local compile_target="$candidate"
                        if [[ "$candidate" == */setup.py ]]; then
                            compile_target="$(dirname "$candidate")"
                        fi
                        $UV_CMD pip compile --quiet --python-version 3.11 \
                            "$compile_target" --output-file "$tmp_pkg_fb" 2>/dev/null
                        grep -oE '^[a-zA-Z0-9][a-zA-Z0-9._-]+' "$tmp_pkg_fb" >> "$tmp_merged_fb"
                        rm -f "$tmp_pkg_fb"
                    done
                    sort -u "$tmp_merged_fb" > "$tmp_out"
                    rm -f "$tmp_merged_fb"
                    t=$(grep -cE '^[a-zA-Z0-9]' "$tmp_out" 2>/dev/null || true)
                    t=${t:-0}
                    if [[ "$t" -gt 0 ]]; then
                        TRANSITIVE=$t
                        TRANSITIVE_DEPS="$(grep -E '^[a-zA-Z0-9]' "$tmp_out" 2>/dev/null | grep -oE '^[a-zA-Z0-9][a-zA-Z0-9._-]+==[^ ]+' || true)"
                        METHOD="uv_pip_compile"
                        found=true
                    fi
                fi
            fi

        elif [[ "$HAS_PIP_REPORT" == true ]]; then
            local tmp_report
            tmp_report=$(mktemp --suffix=.json)
            # requirements files use -r; pyproject.toml/setup.cfg install from dir
            if [[ "$spec" == *.txt ]]; then
                pip install --dry-run --ignore-installed --quiet \
                    --report "$tmp_report" -r "$spec" 2>/dev/null
            else
                pip install --dry-run --ignore-installed --quiet \
                    --report "$tmp_report" "$(dirname "$spec")" 2>/dev/null
            fi
            local t
            t=$(jq '.install | length' "$tmp_report" 2>/dev/null || echo 0)
            if [[ "$t" -gt 0 ]]; then
                TRANSITIVE=$t
                TRANSITIVE_DEPS="$(jq -r '.install[] | "\(.metadata.name)==\(.metadata.version)"' "$tmp_report" 2>/dev/null || true)"
                METHOD="pip_dry_run"
                found=true
            fi
            rm -f "$tmp_report"
        fi

        rm -f "$tmp_out"
    fi

    $found && return 0 || return 1
}

count_go() {
    local dir="$1"
    DIRECT=0; TRANSITIVE=-1; METHOD="none"
    DIRECT_DEPS=""
    TRANSITIVE_DEPS=""
    [[ ! -f "$dir/go.mod" ]] && return 1

    DIRECT_DEPS="$(python_parse_go_mod_deps "$dir/go.mod" || true)"
    DIRECT_DEPS="$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | sort -u)"
    if [[ -n "$DIRECT_DEPS" ]]; then
        DIRECT=$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | wc -l | tr -d ' ')
    else
        DIRECT=$(grep -E '^\s+\S' "$dir/go.mod" 2>/dev/null \
            | grep -v '// indirect' \
            | grep -vE '^\s*(module|go|toolchain)\s' \
            | grep -cE '\S' || true)
        DIRECT=${DIRECT:-0}
    fi
    METHOD="go.mod"

    if [[ "$HAS_GO" == true ]]; then
        local _go_list _go_method
        # Prefer go list -m all (MVS-selected modules only — exactly what is
        # compiled). Retry with -mod=readonly for vendored repos that reject
        # the default -mod=vendor for this command.
        _go_list=$(cd "$dir" && GOFLAGS="" GONOSUMCHECK="*" GONOSUMDB="*" \
            go list -m all 2>/dev/null \
            | awk 'NF>=2 {print $1 "@" $2}' | sort -u || true)
        _go_method="go_list_m_all"
        if [[ -z "$_go_list" ]]; then
            _go_list=$(cd "$dir" && GOFLAGS="" GONOSUMCHECK="*" GONOSUMDB="*" \
                go list -mod=readonly -m all 2>/dev/null \
                | awk 'NF>=2 {print $1 "@" $2}' | sort -u || true)
            _go_method="go_list_m_all_readonly"
        fi
        if [[ -n "$_go_list" ]]; then
            local t
            t=$(printf '%s\n' "$_go_list" | grep -c . || echo -1)
            TRANSITIVE=$t
            TRANSITIVE_DEPS="$_go_list"
            METHOD="$_go_method"
        fi
    fi
    return 0
}

count_rust() {
    local dir="$1"
    DIRECT=0; TRANSITIVE=-1; METHOD="none"
    DIRECT_DEPS=""
    TRANSITIVE_DEPS=""
    [[ ! -f "$dir/Cargo.toml" ]] && return 1

    DIRECT_DEPS="$(python_parse_cargo_deps "$dir/Cargo.toml" || true)"
    DIRECT_DEPS="$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | sort -u)"
    if [[ -n "$DIRECT_DEPS" ]]; then
        DIRECT=$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | wc -l | tr -d ' ')
    else
        DIRECT=$(python3 -c "
import re
text = open('$dir/Cargo.toml').read()
blocks = re.findall(r'^\[dependencies\](.*?)(?=^\[|\Z)', text, re.MULTILINE | re.DOTALL)
count = 0
for b in blocks:
    for line in b.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            count += 1
print(count)
" 2>/dev/null || echo 0)
    fi
    METHOD="Cargo.toml"

    if [[ "$HAS_CARGO" == true ]]; then
        local _cargo_json
        _cargo_json=$(cd "$dir" && cargo metadata --format-version 1 --quiet 2>/dev/null || true)
        local t
        t=$(printf '%s' "$_cargo_json" | jq '.packages | length' 2>/dev/null || echo -1)
        TRANSITIVE=$t
        TRANSITIVE_DEPS="$(printf '%s' "$_cargo_json" | jq -r '.packages[] | "\(.name)==\(.version)"' 2>/dev/null | sort -u || true)"
        METHOD="cargo_metadata"
    fi
    return 0
}

count_npm() {
    local dir="$1"
    DIRECT=0; TRANSITIVE=-1; METHOD="none"
    DIRECT_DEPS=""
    TRANSITIVE_DEPS=""
    [[ ! -f "$dir/package.json" ]] && return 1

    DIRECT_DEPS="$(python_parse_package_json_deps "$dir/package.json" || true)"
    DIRECT_DEPS="$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | sort -u)"
    if [[ -n "$DIRECT_DEPS" ]]; then
        DIRECT=$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | wc -l | tr -d ' ')
    else
        DIRECT=$(jq '(.dependencies // {} | length) + (.devDependencies // {} | length)' \
            "$dir/package.json" 2>/dev/null || echo 0)
    fi
    METHOD="package.json"

    if [[ "$HAS_NPM" == true ]]; then
        local _npm_json
        _npm_json=$(cd "$dir" && npm ls --all --json 2>/dev/null || true)
        local t
        t=$(printf '%s' "$_npm_json" | jq '[.. | .version? // empty] | length' 2>/dev/null || echo -1)
        TRANSITIVE=$t
        TRANSITIVE_DEPS="$(printf '%s' "$_npm_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
seen = set()
def walk(node):
    for name, child in node.get('dependencies', {}).items():
        ver = child.get('version')
        if ver:
            seen.add(f'{name}@{ver}')
        walk(child)
walk(data)
print('\n'.join(sorted(seen)))
" 2>/dev/null || true)"
        METHOD="npm_ls"
    fi
    return 0
}

count_maven() {
    local dir="$1"
    DIRECT=0; TRANSITIVE=-1; METHOD="none"
    DIRECT_DEPS=""
    TRANSITIVE_DEPS=""
    [[ ! -f "$dir/pom.xml" ]] && return 1

    DIRECT_DEPS="$(python_parse_pom_deps "$dir/pom.xml" || true)"
    DIRECT_DEPS="$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | sort -u)"
    if [[ -n "$DIRECT_DEPS" ]]; then
        DIRECT=$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | wc -l | tr -d ' ')
    else
        DIRECT=$(python3 -c "
import xml.etree.ElementTree as ET
try:
    tree = ET.parse('$dir/pom.xml')
    root = tree.getroot()
    M = 'http://maven.apache.org/POM/4.0.0'
    # Collect exclusion sets from dependencyManagement and profiles using element identity.
    # We build them from both namespaced and non-namespaced paths to cover all POM variants.
    excl = set()
    for xpath in [
        f'.//{{{M}}}dependencyManagement/{{{M}}}dependencies/{{{M}}}dependency',
        './/dependencyManagement/dependencies/dependency',
        f'.//{{{M}}}profiles//{{{M}}}dependencies/{{{M}}}dependency',
        './/profiles//dependencies/dependency',
    ]:
        for d in tree.findall(xpath):
            excl.add(id(d))
    all_deps = tree.findall(f'.//{{{M}}}dependencies/{{{M}}}dependency')
    if not all_deps:
        all_deps = tree.findall('.//dependencies/dependency')
    real = [d for d in all_deps if id(d) not in excl]
    print(len(real))
except: print(0)
" 2>/dev/null || echo 0)
    fi
    METHOD="pom.xml"

    if [[ "$HAS_MVN" == true ]]; then
        local t
        # Fix 1: drop -q — it suppresses all output in multi-module builds.
        # Fix 2: strip ANSI escape codes before grep; mvn emits color codes even
        #         when output is piped, so ^\[INFO\] never matched the raw stream.
        # New pattern ^\[INFO\] [|+\\] matches only tree-branch lines, not the
        # per-module root artifact line (^\[INFO\] group:artifact:...) which the
        # original pattern also counted, inflating the transitive total.
        local _mvn_tree
        _mvn_tree=$(cd "$dir" && mvn dependency:tree -DoutputType=text 2>/dev/null \
            | sed 's/\x1B\[[0-9;]*[mK]//g; s/\[[0-9;]*[mK]//g' || true)
        # Extract group:artifact:type:version:scope lines from tree branch lines
        local _mvn_coords
        _mvn_coords=$(printf '%s\n' "$_mvn_tree" \
            | grep -E '^\[INFO\] [|+\\]' \
            | grep -oE '[a-zA-Z][a-zA-Z0-9._-]+:[a-zA-Z][a-zA-Z0-9._-]+:[a-zA-Z][a-zA-Z0-9._-]*:[^ :]+' \
            | sort -u || true)
        local t
        t=$(printf '%s\n' "$_mvn_coords" | grep -c . || echo -1)
        TRANSITIVE=$t
        TRANSITIVE_DEPS="$_mvn_coords"
        METHOD="mvn_tree"
    fi
    return 0
}

count_gradle() {
    local dir="$1"
    DIRECT=0; TRANSITIVE=-1; METHOD="none"
    DIRECT_DEPS="$(python_parse_gradle_deps "$dir" || true)"
    TRANSITIVE_DEPS=""
    DIRECT_DEPS="$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | sort -u)"
    [[ ! -f "$dir/build.gradle" && ! -f "$dir/build.gradle.kts" ]] && return 1

    # ── direct deps: static parse of build.gradle ────────────────────────────
    # Kafka-style: count entries in a `versions += [...]` or `libs += [...]`
    # block inside a separate dependencies.gradle catalog file.
    # Also count direct `implementation`/`api`/`runtimeOnly` calls that
    # reference an external artifact (group:artifact:version), not subprojects.
    local d=0

    # 1. Version catalog entries (e.g. gradle/dependencies.gradle or similar)
    local dep_catalog
    dep_catalog=$(find "$dir" -maxdepth 3 -name "dependencies.gradle" \
                  -o -name "libs.versions.toml" 2>/dev/null | head -1)
    if [[ -f "$dep_catalog" ]]; then
        case "$dep_catalog" in
            *.toml)
                # TOML version catalog: count entries under [libraries]
                d=$(python3 -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
try:
    with open('$dep_catalog', 'rb') as f:
        t = tomllib.load(f)
    print(len(t.get('libraries', {})))
except:
    print(0)
" 2>/dev/null || echo 0)
                METHOD="gradle_libs_versions_toml"
                ;;
            *.gradle)
                # Groovy/Kotlin ext block: count quoted group:artifact entries
                # e.g.  commonsValidator: "org.apache.commons:commons-validator:1.10"
                d=$(grep -cE ':\s+"[a-zA-Z][^"]+:[a-zA-Z][^"]+:[^"]*"' \
                    "$dep_catalog" 2>/dev/null || true)
                d=${d:-0}
                # fallback: count version-keyed entries like  foo: "1.2.3"
                if [[ "$d" -eq 0 ]]; then
                    d=$(grep -cE '^\s+[a-zA-Z][a-zA-Z0-9_]+\s*:\s+"[0-9]' \
                        "$dep_catalog" 2>/dev/null || true)
                    d=${d:-0}
                fi
                METHOD="gradle_dependencies.gradle"
                ;;
        esac
    fi

    # 2. Inline declarations in build.gradle (external artifacts only)
    if [[ "$d" -eq 0 ]]; then
        d=$(grep -hE "^\s+(implementation|api|runtimeOnly|compile)\s" \
                "$dir/build.gradle" "$dir/build.gradle.kts" 2>/dev/null \
            | grep -v "project('" \
            | grep -cE '[a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+' || true)
        d=${d:-0}
        METHOD="build.gradle"
    fi

    DIRECT=$d
    if [[ -n "$DIRECT_DEPS" ]]; then
        DIRECT=$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | wc -l | tr -d ' ')
        METHOD="gradle_direct"
    fi

    # ── transitive deps: run gradle dependencies ─────────────────────────────
    # Prefer repo's gradlew over system gradle: the wrapper declares the exact
    # Gradle version required by that repo (e.g. Kafka needs 9.x, not system 4.x).
    local gradle_exec=""
    if [[ "$HAS_GRADLE" == true ]]; then
        if [[ -f "$dir/gradlew" && -x "$dir/gradlew" ]]; then
            gradle_exec="$dir/gradlew"
        elif [[ -n "${GRADLE_CMD:-}" ]]; then
            gradle_exec="$GRADLE_CMD"
        fi
    fi

    if [[ -n "$gradle_exec" ]]; then
        # runtimeClasspath is the standard configuration for runtime transitive closure.
        # Multi-module builds (e.g. Kafka): root project often has no runtimeClasspath.
        # Strategy: try root first; if empty, ask Gradle for its own project list
        # (canonical paths, respecting any project renames in settings.gradle) and
        # run dependencies on each non-test/non-example subproject.
        # We extract unique group:artifact coordinates (not tree lines) to avoid
        # double-counting shared deps across subprojects.
        local tmp_deps
        tmp_deps=$(mktemp)

        cd "$dir" || return 1
        "$gradle_exec" --no-daemon --quiet dependencies \
            --configuration runtimeClasspath 2>/dev/null > "$tmp_deps"

        local _gradle_list
        _gradle_list=$(grep -oE '[a-zA-Z][a-zA-Z0-9._-]+:[a-zA-Z][a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+' \
            "$tmp_deps" | grep -v '{' | sort -u || true)
        local unique_coords
        unique_coords=$(printf '%s\n' "$_gradle_list" | awk 'NF' | wc -l | tr -d ' \n')

        # If root has no runtimeClasspath, query subprojects via 'gradle projects'
        if [[ "${unique_coords:-0}" -eq 0 ]]; then
            local subprojects=()
            while IFS= read -r sp; do
                sp="${sp#:}"  # strip leading colon
                echo "$sp" | grep -qiE 'test|example|benchmark|sample|upgrade-system|jmh' && continue
                subprojects+=(":${sp}:dependencies")
            done < <("$gradle_exec" --no-daemon --quiet projects 2>/dev/null \
                     | grep -oE "Project ':[^']+'" \
                     | grep -oE ":[a-zA-Z][^']+" \
                     | grep -v "^:$")

            if [[ "${#subprojects[@]}" -gt 0 ]]; then
                "$gradle_exec" --no-daemon --quiet \
                    "${subprojects[@]}" \
                    --configuration runtimeClasspath 2>/dev/null > "$tmp_deps"
                _gradle_list=$(grep -oE '[a-zA-Z][a-zA-Z0-9._-]+:[a-zA-Z][a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+' \
                    "$tmp_deps" | grep -v '{' | sort -u || true)
                unique_coords=$(printf '%s\n' "$_gradle_list" | awk 'NF' | wc -l | tr -d ' \n')
            fi
        fi
        cd - > /dev/null

        rm -f "$tmp_deps"

        if [[ "${unique_coords:-0}" -gt 0 ]]; then
            TRANSITIVE=$unique_coords
            TRANSITIVE_DEPS="$_gradle_list"
            METHOD="${METHOD}+gradle_dependencies"
        fi
    fi

    [[ "$DIRECT" -gt 0 ]] && return 0 || return 1
}

count_cpp() {
    local dir="$1"
    DIRECT=0; TRANSITIVE=-1; METHOD="none"
    DIRECT_DEPS=""
    TRANSITIVE_DEPS=""

    # ── CMake: count find_package() calls (external libs, not internal targets) ─
    local cmake_count=0
    if [[ -f "$dir/CMakeLists.txt" ]]; then
        cmake_deps=$(grep -hE '^\s*find_package\s*\(' \
                "$dir/CMakeLists.txt" \
                "$dir"/cmake/*.cmake 2>/dev/null \
            | grep -v '#' \
            | grep -oE 'find_package\s*\(\s*[A-Za-z][A-Za-z0-9_]+' \
            | sed -E 's/find_package\s*\(\s*//' \
            | sort -u)
        cmake_count=$(printf '%s\n' "$cmake_deps" | awk 'NF' | wc -l || true)
        cmake_count=${cmake_count:-0}
        DIRECT_DEPS="$DIRECT_DEPS"$'\n'$cmake_deps
        [[ "$cmake_count" -gt 0 ]] && METHOD="CMakeLists.txt"
    fi

    # ── Autotools: count AC_CHECK_LIB and PKG_CHECK_MODULES across configure.ac
    #    and all auxdir/*.m4 macros (each .m4 typically wraps one optional libcrary)
    local autotools_count=0
    if [[ -f "$dir/configure.ac" ]]; then
        # Count distinct library names from AC_CHECK_LIB / PKG_CHECK_MODULES
        local lib_names
        lib_names=$(grep -rh 'AC_CHECK_LIB\|PKG_CHECK_MODULES' \
                "$dir/configure.ac" "$dir"/auxdir/*.m4 2>/dev/null \
            | grep -oE 'AC_CHECK_LIB\([^,)]+|PKG_CHECK_MODULES\([^,)]+' \
            | sed 's/AC_CHECK_LIB(//;s/PKG_CHECK_MODULES(//;s/\[//g;s/\]//g' \
            | awk '{print $1}' | sort -u)
        DIRECT_DEPS="$DIRECT_DEPS"$'\n'$lib_names
        autotools_count=$(echo "$lib_names" | grep -c '[a-zA-Z]' || true)
        autotools_count=${autotools_count:-0}
        # Also count m4 include macros as separate optional deps
        local m4_count
        m4_count=$(ls "$dir"/auxdir/x_ac_*.m4 2>/dev/null | wc -l)
        # Take the larger of the two (m4 files are one-per-dep; AC_CHECK_LIB may miss some)
        [[ "$m4_count" -gt "$autotools_count" ]] && autotools_count=$m4_count
        [[ "$autotools_count" -gt 0 ]] && METHOD="${METHOD:+${METHOD}+}configure.ac"
    fi

    DIRECT=$(( cmake_count + autotools_count ))
    DIRECT_DEPS="$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | sort -u)"
    if [[ -n "$DIRECT_DEPS" ]]; then
        DIRECT=$(printf '%s\n' "$DIRECT_DEPS" | awk 'NF' | wc -l | tr -d ' ')
    fi
    # No transitive resolution available for C/C++ system libraries without building
    [[ "$DIRECT" -gt 0 ]] && return 0 || return 1
}

detect_ecosystems() {
    local dir="$1"
    local ecos=()
    # Python: check root-level manifests first; fall back to a shallow scan for
    # pure monorepos (e.g. cuda-python) that have no root manifest but keep all
    # sub-packages in immediate subdirectories.
    if { [[ -f "$dir/requirements.txt" ]] || [[ -f "$dir/setup.py" ]] || \
         [[ -f "$dir/setup.cfg" ]] || [[ -f "$dir/pyproject.toml" ]]; }; then
        ecos+=("python")
    elif find "$dir" -maxdepth 3 \( -name "pyproject.toml" -o -name "setup.py" \) \
             -not -path "*/.git/*" 2>/dev/null | grep -q .; then
        ecos+=("python")
    fi
    [[ -f "$dir/go.mod" ]]     && ecos+=("go")
    [[ -f "$dir/Cargo.toml" ]] && ecos+=("rust")
    [[ -f "$dir/package.json" ]] && ecos+=("npm")
    # pom.xml = Maven; build.gradle without pom.xml = Gradle (counted separately)
    [[ -f "$dir/pom.xml" ]] && ecos+=("maven")
    { [[ -f "$dir/build.gradle" ]] || [[ -f "$dir/build.gradle.kts" ]]; } \
        && [[ ! -f "$dir/pom.xml" ]] && ecos+=("gradle")
    # C/C++: CMake and autotools both parseable for direct deps
    { [[ -f "$dir/CMakeLists.txt" ]] || [[ -f "$dir/configure.ac" ]] || \
      [[ -f "$dir/BUILD" ]] || [[ -f "$dir/BUILD.bazel" ]]; } && ecos+=("cpp")
    printf '%s,' "${ecos[@]}" | sed 's/,$//'
}

# ── header ────────────────────────────────────────────────────────────────────
echo "Config  : $CONFIG"
echo "Repos   : $total  (all=$MEASURE_ALL)"
printf "Tooling : go=%s  cargo=%s  npm=%s  mvn=%s  gradle=%s  uv=%s  pip_report=%s\n" \
    "$HAS_GO" "$HAS_CARGO" "$HAS_NPM" "$HAS_MVN" "$HAS_GRADLE" "$HAS_UV" "$HAS_PIP_REPORT"
echo "────────────────────────────────────────────────────────────────────"

# ── main loop ─────────────────────────────────────────────────────────────────
current_layer=""
grand_direct=0
grand_transitive=0

while IFS=$'\t' read -r layer repo status role; do
    [[ "$INTERRUPTED" == true ]] && break
    [[ "$MEASURE_ALL" == false && "$status" == "alternative" ]] && continue

    if [[ "$layer" != "$current_layer" ]]; then
        echo ""; echo "▶ $layer"
        current_layer="$layer"
    fi

    current=$((current+1))
    project_name=$(basename "$repo")
    target_dir="$REPOS_DIR/$layer/$project_name"
    marker="$target_dir/.clone_ok"
    cache="$DEPS_DIR/${layer}__${project_name}.json"

    # ── not cloned ────────────────────────────────────────────────────────────
    if [[ ! -f "$marker" ]]; then
        printf "  [%s/%d]  project: %s  (not cloned)\n" \
            "$(pad $current)" "$total" "$repo"
        echo "$HLINE"
        skip_count=$((skip_count+1))
        continue
    fi

    # ── cached ────────────────────────────────────────────────────────────────
    if [[ -f "$cache" ]]; then
        direct=$(jq '.direct_total'     "$cache" 2>/dev/null || echo 0)
        trans=$(jq  '.transitive_total' "$cache" 2>/dev/null || echo -1)
        ecos=$(jq -r '.ecosystems_detected' "$cache" 2>/dev/null || echo "?")
        t_str="$trans"; [[ "$trans" == "-1" ]] && t_str="manifest-only"
        printf "  [%s/%d]  project: %s  (cached)\n" \
            "$(pad $current)" "$total" "$repo"
        echo "$HLINE"
        printf "         total   direct=%5s  transitive=%5s\n" "$direct" "$t_str"
        echo "$HLINE"
        measured_count=$((measured_count+1))
        grand_direct=$((grand_direct + direct))
        [[ "$trans" -gt 0 ]] && grand_transitive=$((grand_transitive + trans))
        # still write CSV row
        method=$(jq -r '.method' "$cache" 2>/dev/null || echo "cached")
        echo "$layer,$project_name,$repo,$status,\"$ecos\",$direct,$trans,$method" >> "$CSV"
        log_row "cached" "$layer" "$repo" "$ecos" "$direct" "$trans" "$method"
        continue
    fi

    # ── measure ───────────────────────────────────────────────────────────────
    printf "  [%s/%d]  project: %s\n" \
        "$(pad $current)" "$total" "$repo"
    echo "$HLINE"
    echo ""

    ecos=$(detect_ecosystems "$target_dir")
    total_direct=0
    total_trans=-1
    all_methods=()
    eco_count=0
    # Per-project transitive dep lists (newline-separated name==version strings)
    proj_trans_python=""
    proj_trans_go=""
    proj_trans_cargo=""
    proj_trans_npm=""
    proj_trans_maven=""
    proj_trans_gradle=""
    # Accumulated direct deps across all ecosystems for this project
    proj_direct_deps=""

    # Python
    if echo "$ecos" | grep -q "python"; then
        if count_python "$target_dir"; then
            t_eco="$TRANSITIVE"; [[ "$TRANSITIVE" == "-1" ]] && t_eco="n/a"
            printf "         python  direct=%5d  transitive=%5s  [%s]\n" \
                "$DIRECT" "$t_eco" "$METHOD"
            total_direct=$((total_direct + DIRECT))
            [[ "$TRANSITIVE" -gt 0 ]] && total_trans=$((total_trans < 0 ? TRANSITIVE : total_trans + TRANSITIVE))
            all_methods+=("python:$METHOD")
            eco_direct[python]=$(( eco_direct[python] + DIRECT ))
            [[ "$TRANSITIVE" -gt 0 ]] && eco_transitive[python]=$(( eco_transitive[python] + TRANSITIVE ))
            proj_trans_python="$TRANSITIVE_DEPS"
            proj_direct_deps="$proj_direct_deps"$'\n'"$DIRECT_DEPS"
            eco_count=$((eco_count + 1))
        fi
    fi

    # Go
    if echo "$ecos" | grep -q '\bgo\b'; then
        if count_go "$target_dir"; then
            t_eco="$TRANSITIVE"; [[ "$TRANSITIVE" == "-1" ]] && t_eco="n/a"
            printf "         go      direct=%5d  transitive=%5s  [%s]\n" \
                "$DIRECT" "$t_eco" "$METHOD"
            total_direct=$((total_direct + DIRECT))
            [[ "$TRANSITIVE" -gt 0 ]] && total_trans=$((total_trans < 0 ? TRANSITIVE : total_trans + TRANSITIVE))
            all_methods+=("go:$METHOD")
            eco_direct[go]=$(( eco_direct[go] + DIRECT ))
            [[ "$TRANSITIVE" -gt 0 ]] && eco_transitive[go]=$(( eco_transitive[go] + TRANSITIVE ))
            proj_trans_go="$TRANSITIVE_DEPS"
            proj_direct_deps="$proj_direct_deps"$'\n'"$DIRECT_DEPS"
            eco_count=$((eco_count + 1))
        fi
    fi

    # Rust
    if echo "$ecos" | grep -q "rust"; then
        if count_rust "$target_dir"; then
            t_eco="$TRANSITIVE"; [[ "$TRANSITIVE" == "-1" ]] && t_eco="n/a"
            printf "         rust    direct=%5d  transitive=%5s  [%s]\n" \
                "$DIRECT" "$t_eco" "$METHOD"
            total_direct=$((total_direct + DIRECT))
            [[ "$TRANSITIVE" -gt 0 ]] && total_trans=$((total_trans < 0 ? TRANSITIVE : total_trans + TRANSITIVE))
            all_methods+=("rust:$METHOD")
            eco_direct[cargo]=$(( eco_direct[cargo] + DIRECT ))
            [[ "$TRANSITIVE" -gt 0 ]] && eco_transitive[cargo]=$(( eco_transitive[cargo] + TRANSITIVE ))
            proj_trans_cargo="$TRANSITIVE_DEPS"
            proj_direct_deps="$proj_direct_deps"$'\n'"$DIRECT_DEPS"
            eco_count=$((eco_count + 1))
        fi
    fi

    # npm
    if echo "$ecos" | grep -q "npm"; then
        if count_npm "$target_dir"; then
            t_eco="$TRANSITIVE"; [[ "$TRANSITIVE" == "-1" ]] && t_eco="n/a"
            printf "         npm     direct=%5d  transitive=%5s  [%s]\n" \
                "$DIRECT" "$t_eco" "$METHOD"
            total_direct=$((total_direct + DIRECT))
            [[ "$TRANSITIVE" -gt 0 ]] && total_trans=$((total_trans < 0 ? TRANSITIVE : total_trans + TRANSITIVE))
            all_methods+=("npm:$METHOD")
            eco_direct[npm]=$(( eco_direct[npm] + DIRECT ))
            [[ "$TRANSITIVE" -gt 0 ]] && eco_transitive[npm]=$(( eco_transitive[npm] + TRANSITIVE ))
            proj_trans_npm="$TRANSITIVE_DEPS"
            proj_direct_deps="$proj_direct_deps"$'\n'"$DIRECT_DEPS"
            eco_count=$((eco_count + 1))
        fi
    fi

    # Maven
    if echo "$ecos" | grep -q "maven"; then
        if count_maven "$target_dir"; then
            t_eco="$TRANSITIVE"; [[ "$TRANSITIVE" == "-1" ]] && t_eco="n/a"
            printf "         maven   direct=%5d  transitive=%5s  [%s]\n" \
                "$DIRECT" "$t_eco" "$METHOD"
            total_direct=$((total_direct + DIRECT))
            [[ "$TRANSITIVE" -gt 0 ]] && total_trans=$((total_trans < 0 ? TRANSITIVE : total_trans + TRANSITIVE))
            all_methods+=("maven:$METHOD")
            eco_direct[maven]=$(( eco_direct[maven] + DIRECT ))
            [[ "$TRANSITIVE" -gt 0 ]] && eco_transitive[maven]=$(( eco_transitive[maven] + TRANSITIVE ))
            proj_trans_maven="$TRANSITIVE_DEPS"
            proj_direct_deps="$proj_direct_deps"$'\n'"$DIRECT_DEPS"
            eco_count=$((eco_count + 1))
        fi
    fi

    # Gradle
    if echo "$ecos" | grep -q "gradle"; then
        if count_gradle "$target_dir"; then
            t_eco="$TRANSITIVE"; [[ "$TRANSITIVE" == "-1" ]] && t_eco="n/a"
            printf "         gradle  direct=%5d  transitive=%5s  [%s]\n" \
                "$DIRECT" "$t_eco" "$METHOD"
            total_direct=$((total_direct + DIRECT))
            [[ "$TRANSITIVE" -gt 0 ]] && total_trans=$((total_trans < 0 ? TRANSITIVE : total_trans + TRANSITIVE))
            all_methods+=("gradle:$METHOD")
            eco_direct[gradle]=$(( eco_direct[gradle] + DIRECT ))
            [[ "$TRANSITIVE" -gt 0 ]] && eco_transitive[gradle]=$(( eco_transitive[gradle] + TRANSITIVE ))
            proj_trans_gradle="$TRANSITIVE_DEPS"
            proj_direct_deps="$proj_direct_deps"$'\n'"$DIRECT_DEPS"
            eco_count=$((eco_count + 1))
        fi
    fi

    # C/C++ (CMake / autotools — direct deps only; no transitive resolution without building)
    if echo "$ecos" | grep -q "cpp"; then
        if count_cpp "$target_dir"; then
            printf "         cpp     direct=%5d  transitive=%5s  [%s]\n" \
                "$DIRECT" "n/a" "$METHOD"
            total_direct=$((total_direct + DIRECT))
            all_methods+=("cpp:$METHOD")
            eco_direct[cpp]=$(( eco_direct[cpp] + DIRECT ))
            proj_direct_deps="$proj_direct_deps"$'\n'"$DIRECT_DEPS"
            eco_count=$((eco_count + 1))
        fi
    fi

    method_str=$(IFS=';'; echo "${all_methods[*]:-none}")
    t_str="$total_trans"; [[ "$total_trans" == "-1" ]] && t_str="manifest-only"

    if [[ "$eco_count" -gt 0 ]]; then
        printf "                        ─────            ─────\n"
        printf "         total   direct=%5d  transitive=%5s\n" "$total_direct" "$t_str"
        echo ""
        echo "$HLINE"
    fi

    # Write cache — use temp files to avoid "Argument list too long" for large dep lists
    # proj_direct_deps accumulates direct deps across all ecosystems for this project
    _tmp_dd=$(mktemp); _tmp_py=$(mktemp); _tmp_go=$(mktemp)
    _tmp_ca=$(mktemp); _tmp_np=$(mktemp); _tmp_mv=$(mktemp); _tmp_gr=$(mktemp)
    printf '%s\n' "$proj_direct_deps"  | awk 'NF' | sort -u > "$_tmp_dd"
    printf '%s\n' "$proj_trans_python" | awk 'NF' | sort -u > "$_tmp_py"
    printf '%s\n' "$proj_trans_go"     | awk 'NF' | sort -u > "$_tmp_go"
    printf '%s\n' "$proj_trans_cargo"  | awk 'NF' | sort -u > "$_tmp_ca"
    printf '%s\n' "$proj_trans_npm"    | awk 'NF' | sort -u > "$_tmp_np"
    printf '%s\n' "$proj_trans_maven"  | awk 'NF' | sort -u > "$_tmp_mv"
    printf '%s\n' "$proj_trans_gradle" | awk 'NF' | sort -u > "$_tmp_gr"
    python3 - "$_tmp_dd" "$_tmp_py" "$_tmp_go" "$_tmp_ca" "$_tmp_np" "$_tmp_mv" "$_tmp_gr" \
              "$cache" "$repo" "$layer" "$status" "$ecos" \
              "$total_direct" "$total_trans" "$method_str" <<'PYCACHE'
import json, sys

def read_lines(path):
    with open(path) as f:
        return sorted(set(line.strip() for line in f if line.strip()))

(tmp_dd, tmp_py, tmp_go, tmp_ca, tmp_np, tmp_mv, tmp_gr,
 cache, repo, layer, status, ecos,
 total_direct, total_trans, method) = sys.argv[1:]

d = {
    'repo': repo, 'layer': layer, 'status': status,
    'ecosystems_detected': ecos,
    'direct_total': int(total_direct),
    'transitive_total': int(total_trans),
    'method': method,
    'direct_deps':           read_lines(tmp_dd),
    'transitive_deps_python': read_lines(tmp_py),
    'transitive_deps_go':     read_lines(tmp_go),
    'transitive_deps_cargo':  read_lines(tmp_ca),
    'transitive_deps_npm':    read_lines(tmp_np),
    'transitive_deps_maven':  read_lines(tmp_mv),
    'transitive_deps_gradle': read_lines(tmp_gr),
}
with open(cache, 'w') as f:
    json.dump(d, f, indent=2)
PYCACHE
    rm -f "$_tmp_dd" "$_tmp_py" "$_tmp_go" "$_tmp_ca" "$_tmp_np" "$_tmp_mv" "$_tmp_gr"
    unset DIRECT_DEPS
    echo "$layer,$project_name,$repo,$status,\"$ecos\",$total_direct,$total_trans,\"$method_str\"" >> "$CSV"
    log_row "success" "$layer" "$repo" "$ecos" "$total_direct" "$total_trans" "$method_str"

    measured_count=$((measured_count+1))
    grand_direct=$((grand_direct + total_direct))
    [[ "$total_trans" -gt 0 ]] && grand_transitive=$((grand_transitive + total_trans))

done <<< "$MANIFEST"

# ── Write merged transitive dependency list ───────────────────────────────────
# Reads all per-project JSON caches and deduplicates by (name, version) pair.
# Format: results/transitive_merged.json
#   { "python": ["name==version", ...], "go": ["module@version", ...], ... }
MERGED_JSON_PATH="$RESULTS_DIR/transitive_merged.json"
DEPS_CACHE_DIR="$DEPS_DIR"
export MERGED_JSON_PATH DEPS_CACHE_DIR
python3 - <<'PYMERGE'
import json, os
from pathlib import Path

deps_dir = Path(os.environ["DEPS_CACHE_DIR"])
out_path  = Path(os.environ["MERGED_JSON_PATH"])

ecosystems = ["python", "go", "cargo", "npm", "maven", "gradle"]
merged = {eco: set() for eco in ecosystems}

for f in sorted(deps_dir.glob("*.json")):
    try:
        d = json.loads(f.read_text())
    except Exception:
        continue
    for eco in ecosystems:
        for entry in d.get(f"transitive_deps_{eco}", []):
            entry = entry.strip()
            if entry:
                merged[eco].add(entry)

out = {eco: sorted(merged[eco]) for eco in ecosystems}
out_path.write_text(json.dumps(out, indent=2))

print(f"  Merged transitive deps: {out_path}")
for eco, pkgs in out.items():
    if pkgs:
        print(f"    {eco:<8}  {len(pkgs):>5} unique (name, version) pairs")
PYMERGE
unset MERGED_JSON_PATH DEPS_CACHE_DIR

# ── LOC estimate constants (median LOC per package, per ecosystem) ────────────
# Values are order-of-magnitude estimates; see script header for sources.
LOC_PYTHON=5000
LOC_GO=8000
LOC_NPM=1500
LOC_MAVEN=15000
LOC_CARGO=5000
LOC_GRADLE=15000   # Gradle projects are predominantly JVM; same median as Maven

# ── summary ───────────────────────────────────────────────────────────────────
ELAPSED=$(( $(date +%s) - START_TS ))
ELAPSED_FMT=$(printf "%02d:%02d:%02d" $((ELAPSED/3600)) $((ELAPSED%3600/60)) $((ELAPSED%60)))
SUMMARY="$RESULTS_DIR/deps_summary.txt"

print_summary() {
    echo ""
    echo "════════════════════════════════════════════════════════════════════"
    echo "  Dependency Count — $(date)"
    echo "════════════════════════════════════════════════════════════════════"
    printf "  %-22s %d / %d\n" "Measured:"    "$measured_count" "$total"
    printf "  %-22s %d\n"      "Skipped:"     "$skip_count"
    printf "  %-22s %s\n"      "Elapsed:"     "$ELAPSED_FMT"
    echo ""

    # Totals
    echo "  Aggregate package counts (sum across all repos; packages shared"
    echo "  across repos may be counted more than once):"
    printf "  %-28s %8s\n" "Direct dependencies:"     "$(fmt $grand_direct)"
    printf "  %-28s %8s\n" "Transitive dependencies:" \
        "$(fmt $grand_transitive) $([ $grand_transitive -eq 0 ] && echo "(tooling unavailable)" || echo "")"
    echo ""

    # Per-ecosystem counts
    echo "  Counts by ecosystem:"
    printf "  %-10s  %10s  %10s  %s\n" "Ecosystem" "Direct" "Transitive" "Coverage"
    printf "  %-10s  %10s  %10s  %s\n" "─────────" "──────────" "──────────" "────────"
    for eco in python go cargo npm maven gradle cpp; do
        d="${eco_direct[$eco]}"
        t="${eco_transitive[$eco]}"
        coverage="full"; [[ "$t" -eq 0 ]] && coverage="manifest-only"
        [[ "$eco" == "cpp" ]] && coverage="direct-only (no registry)"
        printf "  %-10s  %10s  %10s  %s\n" "$eco" "$(fmt $d)" "$(fmt $t)" "$coverage"
    done
    echo ""

    # LOC estimate from package counts
    echo "  ─── Estimated transitive LOC (order-of-magnitude) ─────────────"
    echo "  Based on per-ecosystem median LOC/package (see script header)."
    echo "  Packages shared across repos are not de-duplicated — this is"
    echo "  an upper-bound estimate. Direct-repo LOC is measured, not estimated."
    echo ""
    printf "  %-10s  %10s  %12s  %s\n" "Ecosystem" "Packages" "Est. LOC" "Avg LOC/pkg"
    printf "  %-10s  %10s  %12s  %s\n" "─────────" "────────" "────────" "───────────"

    total_est_loc=0
    for eco in python go cargo npm maven gradle cpp; do
        # Use transitive if available, else direct
        t="${eco_transitive[$eco]}"
        d="${eco_direct[$eco]}"
        pkgs=$t; [[ "$pkgs" -le 0 ]] && pkgs=$d
        case $eco in
            python) avg=$LOC_PYTHON ;;
            go)     avg=$LOC_GO ;;
            cargo)  avg=$LOC_CARGO ;;
            npm)    avg=$LOC_NPM ;;
            maven)  avg=$LOC_MAVEN ;;
            gradle) avg=$LOC_GRADLE ;;
            cpp)    avg=0 ;;  # no registry median; excluded from LOC estimate
        esac
        est_loc=$((pkgs * avg))
        total_est_loc=$((total_est_loc + est_loc))
        note=""; [[ "${eco_transitive[$eco]}" -le 0 ]] && note="(direct only)"
        printf "  %-10s  %10s  %12s  %8d  %s\n" \
            "$eco" "$(fmt $pkgs)" "$(fmt $est_loc)" "$avg" "$note"
    done
    printf "  %-10s  %10s  %12s\n" "─────────" "────────" "────────"
    printf "  %-10s  %10s  %12s\n" "TOTAL est." "" "$(fmt $total_est_loc)"
    echo ""
    echo "  ⚠  This estimate covers transitive open-source dependencies only."
    echo "     It excludes: proprietary code, OS/kernel, GPU drivers, cloud services."
    echo "     See opaque_components in stack_config.yaml for those estimates."
    echo ""

    if [[ "$INTERRUPTED" == true ]]; then
        echo "  ⚡ Run interrupted. Re-run to resume (cached JSON files reused)."
        echo ""
    elif [[ "$current" -lt "$total" ]]; then
        echo "  ⚠  $((total - current)) repos not yet reached. Re-run to continue."
        echo ""
    fi

    echo "  Outputs:"
    echo "    $CSV"
    echo "    $DEPS_DIR/  (per-project JSON cache)"
    echo "    $REPORT_TSV"
    echo "    $SUMMARY"
    echo "════════════════════════════════════════════════════════════════════"
}

print_summary | tee "$SUMMARY"
