# The Grand Size of the AI Supply Chain

A quantitative analysis framework for measuring the software complexity hidden in compound AI systems. This project systematically measures 48 representative open-source projects across four infrastructure layers, quantifying both direct source code and transitive dependencies using multi-ecosystem dependency resolution.

## Overview

Modern compound AI systems depend on vast software supply chains often invisible to developers and architects. This framework provides tooling and workflows to:

- **Measure direct code complexity**: Count source lines of code in the baseline stack using language-aware analysis
- **Resolve transitive dependencies**: Fully expand dependency closures across Python, Go, Rust, JavaScript, Java, and other ecosystems
- **Estimate total supply chain size**: Convert dependency counts into lines-of-code estimates using per-ecosystem medians
- **Identify cross-layer risk**: Flag dependencies appearing across multiple infrastructure layers as systemic supply chain exposure

The result is an interactive dashboard showing the true scope of the AI software stack with breakdown by layer, language, and ecosystem.

## Project Structure

```
├── stack_config.yaml              # Configuration: selected repos and alternatives by layer
├── stack_lock.yaml                # Pinned commit SHAs for reproducible measurement (auto-generated)
├── 00_freeze_versions.sh          # Snapshot exact commit SHAs
├── 01_clone_repos.sh              # Clone all selected repositories
├── 02_measure_loc.sh              # Count lines of code per repo
├── 03_count_deps.sh               # Resolve direct and transitive dependencies
├── 04_aggregate.py                # Aggregate LOC and dependency data
├── 06_ecosystem_loc_medians.py    # Compute per-ecosystem LOC/package medians
├── 07_measure_dep_loc.py          # Download and measure transitive deps
├── 08_aggregate_dep_loc.py        # Merge deterministic LOC measurements
├── 09_retry_failed_loc.py         # Retry failed dependency measurements
├── 10_build_dep_graph.py          # Build multi-language dependency graph
├── 11_layout_dep_graph.py         # Compute graph layout for visualization
├── 12_gen_shared_deps_report.py   # Identify cross-layer shared dependencies
├── index.html                     # Interactive dashboard (run with Python server)
├── repos/                         # Cloned repositories (4 layers × ~12 projects each)
└── results/                       # Output artifacts (CSVs, JSONs, reports)
```

## Quick Start

### Prerequisites

Install system tools:
```bash
# Ubuntu/Debian
apt-get install git python3 python3-pip scc jq

# Rust/Go/Node/Maven resolvers (optional, per ecosystem needed):
# Go: already installed with `go` command
# Rust: cargo (part of Rust toolchain)
# Node: npm or node (part of Node.js)
# Maven: mvn
```

Install Python dependencies:
```bash
pip install pyyaml pandas matplotlib
```

### Run the Full Pipeline

```bash
# 1. Clone all selected repositories (freezing versions)
./00_freeze_versions.sh
./01_clone_repos.sh

# 2. Measure lines of code
./02_measure_loc.sh

# 3. Count dependencies (direct and transitive)
./03_count_deps.sh

# 4. Aggregate results
python3 04_aggregate.py

# 5. (Optional) Measure transitive LOC deterministically
python3 06_ecosystem_loc_medians.py
python3 07_measure_dep_loc.py
python3 08_aggregate_dep_loc.py

# 6. Build dependency graph
python3 10_build_dep_graph.py
python3 11_layout_dep_graph.py

# 7. Identify cross-layer dependencies
python3 12_gen_shared_deps_report.py

# 8. Serve dashboard
python3 -m http.server 8000
# Open http://localhost:8000
```

For a quick test run with only "selected" projects:
```bash
./01_clone_repos.sh
./02_measure_loc.sh
./03_count_deps.sh
python3 10_build_dep_graph.py
python3 -m http.server 8000
```

To include alternative projects as well:
```bash
./01_clone_repos.sh --all
./02_measure_loc.sh --all
./03_count_deps.sh --all
```

## Scripts: Purpose and Methodology

### 00_freeze_versions.sh
**Purpose**: Create a reproducible snapshot by recording exact commit SHAs.

**Methodology**: Walks through `stack_config.yaml`, finds each repo in `repos/`, reads the current HEAD SHA, and writes it to `stack_lock.yaml`. On subsequent runs, `01_clone_repos.sh` reads this file and checks out pinned commits, ensuring bit-for-bit reproducibility.

**Output**: `stack_lock.yaml`

---

### 01_clone_repos.sh
**Purpose**: Fetch all selected repositories at shallow depth.

**Methodology**:
- Parses `stack_config.yaml` to extract repos marked `status: selected`
- Shallow clones each repo at depth 1 (fast, saves bandwidth)
- Respects `stack_lock.yaml` if present (checks out pinned SHAs for reproducibility)
- Writes a per-repo progress marker (`.clone_ok`) for safe resume; re-running skips completed clones
- Logs each clone attempt to `results/clone_report.tsv` for audit trail

**Key Design Choice**: Shallow clones at depth=1 minimize bandwidth while preserving metadata needed for LOC measurement. Full history is not needed for this analysis.

**Outputs**: `repos/<layer>/<project>/`, `results/clone_report.tsv`, `results/clone_summary.txt`

---

### 02_measure_loc.sh
**Purpose**: Count source lines of code per repository using language-aware analysis.

**Methodology**:
- Uses `scc` (boyter/scc) with `--no-gen` flag to exclude generated/vendored code
- Counts only **code lines** (blank lines and comments excluded)
- Supports ~40 programming languages (Go, Python, Java, Rust, TypeScript, C++, etc.)
- Parses scc JSON output to extract: total code lines, file count, language breakdown
- Skips repos without `.clone_ok` marker (failed clones)
- COCOMO estimation: applies Boehm's model for effort/cost estimates

**Key Design Choice**: Generated code is excluded to measure only hand-written source. This avoids inflating counts from autogenerated protobuf, GraphQL, or vendored dependencies inside repos.

**Outputs**: `results/loc_per_project/` (one JSON per repo), `results/loc_summary.csv`, `results/loc_summary.txt`

---

### 03_count_deps.sh
**Purpose**: Resolve direct and transitive dependencies for each repo using per-ecosystem tooling.

**Methodology**:

| Ecosystem | Method | Limitations |
|-----------|--------|-----------|
| **Python (PyPI)** | pip-compile (manifest-only) | No transitive due to CUDA/GPU env requirements; counts only declared packages |
| **Go** | `go list -m all` | Full transitive closure; accurate and deterministic |
| **Rust** | `cargo metadata` | Full transitive closure; includes workspace members |
| **JavaScript** | `npm ls --json` | Full transitive with npm v7+; Yarn/pnpm may need separate handling |
| **Java/Maven** | `mvn dependency:tree` | Includes compile, runtime, and transitive scopes |
| **Scala** | Maven (via sbt) | Via Maven plugin; transitive included |

- For each repo, extracts manifest (package.json, go.mod, Cargo.toml, pom.xml, etc.)
- Runs ecosystem-specific resolver in the cloned directory
- **Direct deps**: explicit declarations in manifest
- **Transitive deps**: full closure (where tooling allows); Python flagged as "manifest-only"
- Per-ecosystem **median LOC/package** ratios used for supply-chain-size estimates:
  - PyPI: ~5,000 LOC/package
  - Go: ~8,000 LOC/package
  - npm: ~1,500 LOC/package
  - Maven: ~15,000 LOC/package
  - Cargo: ~5,000 LOC/package

**Key Design Choice**: Transitive closure is the key metric for supply-chain risk; direct deps alone understate complexity by 10–100×. Python is manifest-only because installing CUDA packages requires a compatible GPU environment.

**Outputs**: `results/deps_per_project/` (per-repo JSON), `results/deps_summary.csv`, `results/deps_summary.txt`

---

### 04_aggregate.py
**Purpose**: Merge LOC and dependency data into per-layer summaries for paper inclusion.

**Methodology**:
- Reads `results/loc_summary.csv` and `results/deps_summary.csv`
- Groups by layer (data_pipelines, training, integration_serving, cross_cutting)
- Computes aggregates: total projects, total code lines, file count, language set, ecosystem set
- Outputs per-layer tables suitable for paper tables and figures

**Outputs**: `results/table_by_layer.csv`, `results/language_breakdown.csv`

---

### 06_ecosystem_loc_medians.py
**Purpose**: Compute representative median LOC/package for each ecosystem using measured projects.

**Methodology**:
- For each selected repo, calculates: `ecosystem_median = project_loc / transitive_package_count`
- Groups by ecosystem and computes median across all projects using that ecosystem
- This ratio is later applied to other projects' transitive counts as a supply-chain-size estimate

**Outputs**: `results/ecosystem_loc_medians.csv`

---

### 07_measure_dep_loc.py & 08_aggregate_dep_loc.py
**Purpose**: Deterministically measure transitive dependency LOC by downloading packages from ecosystem registries.

**Methodology** (preferred over statistical fallback):
- For each unique (package_name, version) in the transitive closure across all selected repos
- Download from ecosystem registry (PyPI, npm registry, Maven Central, crates.io, Go proxy)
- Measure with `scc` (same as direct code measurement)
- Aggregate by ecosystem and project
- Fallback to median if download/measurement fails

**Key Design Choice**: Deterministic measurement is more accurate than the statistical fallback but slower and ecosystem-dependent. Preferred when registries are accessible.

**Outputs**: `results/dep_loc_measured.json`, updated `results/loc_summary.csv`

---

### 10_build_dep_graph.py
**Purpose**: Construct a multi-ecosystem, multi-layer dependency graph for visualization and cross-layer analysis.

**Methodology**:
- Nodes: projects (colored by layer) + external packages (colored by ecosystem)
- Edges: project → package (labeled by ecosystem)
- Cross-stack edges: detected when a dependency name matches another selected repo (by basename or alias, e.g., `torch` → `pytorch/pytorch`)
- Outputs both a generic JSON format (D3/Cytoscape) and GraphViz DOT (for static visualization)

**Outputs**: `results/dep_graph.json`, `results/dep_graph_projects.dot`, `results/dep_graph_stats.txt`

---

### 11_layout_dep_graph.py
**Purpose**: Compute 2D positions for graph visualization using force-directed layout.

**Methodology**: Applies graph layout algorithm (e.g., Fruchterman–Reingold or similar) to position nodes for aesthetic visualization in the dashboard.

**Outputs**: `results/dep_graph_projects_layout.json`

---

### 12_gen_shared_deps_report.py
**Purpose**: Identify and rank cross-layer shared dependencies as a proxy for systemic supply-chain risk.

**Methodology**:
- For each package in the union of all direct dependencies
- Count how many layers (out of 4) include it
- Flag packages appearing in 2+ layers as "cross-layer shared"
- Rank by frequency and layer span

**Outputs**: `results/shared_deps_report.csv`

---

## Interactive Dashboard

Run a local Python server to view the dashboard:

```bash
python3 -m http.server 8000
# Open http://localhost:8000 in your browser
```

### Dashboard Components

#### 1. **LOC Equation (Top)**
Visual breakdown of total measured complexity:
```
Direct Code (LOC) + Transitive (LOC) + Opaque (estimated range) = Total
```

- **Direct**: measured LOC in the 48 selected repos
- **Transitive**: LOC in dependency closures (deterministic or statistical)
- **Opaque**: proprietary/unmeasurable components (cloud, GPU drivers, OS)
- **Total**: compound AI system complexity estimate

#### 2. **Top Metrics**
Quick glance summary:
- Number of projects
- Total LOC (direct + transitive)
- Number of unique ecosystems
- Languages represented

#### 3. **Language Distribution Bar**
Visual breakdown showing source code distribution across languages (Python, Go, Java, Rust, TypeScript, C++, etc.)

#### 4. **Infrastructure Layers**
Expandable accordion showing each layer:
- **Data Pipelines**: ingestion, storage, preprocessing (e.g., Kafka, Spark, DVC)
- **Training**: ML model training infrastructure (PyTorch, Transformers, DeepSpeed)
- **Integration & Serving**: inference, serving, and orchestration (Ray, vLLM, TensorRT-LLM)
- **Cross-Cutting**: infrastructure tools spanning all layers (Kubernetes, Prometheus, Argo)

Each layer shows:
- Total projects
- Direct + transitive LOC with dependency bars
- Per-project details (name, languages, lines, ecosystem, dependency counts)

#### 5. **Methodology Sidebar**
Step-by-step explanation of measurement process:
1. Stack selection (config-driven)
2. LOC counting (scc)
3. Dependency resolution (per-ecosystem)
4. Cross-layer exposure detection
5. Opaque component estimation
6. Transitive LOC measurement (deterministic vs. statistical)

#### 6. **Cross-Layer Shared Dependencies Panel**
Table of packages appearing in multiple layers, ranked by frequency. A proxy for systemic supply-chain risk.

### Dashboard File Loading

If not served over HTTP, manually load result CSVs and JSONs:
- `stack_config.yaml`
- `ecosystem_loc_medians.csv`
- `loc_summary.csv`
- `deps_summary.csv`
- `shared_deps_report.csv`
- `loc_per_project/` (folder of JSONs)
- `deps_per_project/` (folder of JSONs)

## Configuration: stack_config.yaml

Define the reference AI stack:

```yaml
data_pipelines:
  description: Data ingestion, storage, preprocessing
  projects:
    - repo: apache/kafka
      role: Message broker and stream ingestion
      status: selected       # or "alternative"
      languages: [Java, Scala]
      notes: Core data pipeline infrastructure
      usage_ref: https://...

    - repo: apache/pulsar
      role: Message broker (alternative)
      status: alternative
      languages: [Java, Python]

training:
  description: ML model training
  projects:
    - repo: pytorch/pytorch
      role: Deep learning framework
      status: selected
      languages: [Python, C++, CUDA]
      ...

# ... (integration_serving and cross_cutting layers)
```

**Key Design Choices**:
- `status: selected` projects are measured by default
- `status: alternative` are measured with `--all` flag, allowing alternative stacks to be tested
- Each project records language, URL, and usage reference for paper context

## Output Artifacts

### CSVs (for analysis/papers)

- **loc_summary.csv**: per-project LOC, files, languages
- **deps_summary.csv**: per-project direct/transitive dependency counts
- **ecosystem_loc_medians.csv**: median LOC/package per ecosystem
- **shared_deps_report.csv**: cross-layer shared packages
- **table_by_layer.csv**: aggregated per-layer metrics

### JSONs (for dashboard/graphs)

- **dep_graph.json**: full node/edge list (D3/Cytoscape format)
- **dep_graph_projects_layout.json**: 2D positions for visualization
- **loc_per_project/*.json**: per-repo LOC breakdown (scc output)
- **deps_per_project/*.json**: per-repo dependency manifests and closures

### Reports (TSV logs)

- **clone_report.tsv**: per-clone attempt (time, status, SHA)
- **loc_report.tsv**: per-LOC measurement (project, lines, files, languages)
- **deps_report.tsv**: per-dependency resolution (project, direct, transitive, ecosystem)

## Key Findings & Design Justifications

### Why measure transitive dependencies?

Direct code (the 48 projects) represents ~1–5M LOC. Their transitive closures typically add 10–100× more—often 50–500M LOC. The transitive count is the true supply-chain risk metric.

### Why per-ecosystem median LOC/package?

Ecosystems have wildly different package sizes:
- npm packages: avg ~1.5K LOC (small, granular)
- Maven packages: avg ~15K LOC (heavier, fewer)
- PyPI packages: avg ~5K LOC (moderate)

A single global estimate would be meaningless. Per-ecosystem medians derived from measured projects give order-of-magnitude accuracy.

### Why exclude generated code?

Autogenerated protobuf, GraphQL schema code, and vendored dependencies inflate LOC counts without reflecting maintenance burden. Excluding them with `scc --no-gen` gives a truer picture.

### Why manifest-only for Python?

Transitive Python dependency resolution requires installing packages, which often triggers compilation and requires CUDA/GPU environments. This is impractical at scale. Manifest-only counts reflect declared dependencies; transitive LOC is estimated via statistical fallback.

### Why cross-layer shared dependencies matter?

A package appearing in, say, both training and serving layers is a potential single point of failure affecting multiple subsystems. Frequency and layer span indicate systemic risk concentration.

## Reproducibility

To reproduce measurements exactly:

```bash
# Stack freezing captures reproducibility
./00_freeze_versions.sh    # Records SHAs in stack_lock.yaml

# Subsequent runs use pinned SHAs
./01_clone_repos.sh        # Reads stack_lock.yaml, checks out exact commits
./02_measure_loc.sh
./03_count_deps.sh
```

All output is deterministic given the same tool versions and locked SHAs.

## Extending the Analysis

### Add a new ecosystem resolver

Edit `03_count_deps.sh` to add a new language:

```bash
elif [[ $manifest == "Cargo.toml" ]]; then
    # Run Rust resolver...
    DEPS=$(cargo metadata --format-version 1 --manifest-path "$manifest" | jq -r '.resolve.nodes[] | .id')
elif [[ $manifest == "go.mod" ]]; then
    # Run Go resolver...
    DEPS=$(go list -m all | tail -n +2 | awk '{print $1}')
```

### Replace or add projects

Edit `stack_config.yaml`:

```yaml
training:
  projects:
    - repo: jax-ml/jax                  # Add JAX
      role: NumPy-compatible ML framework
      status: selected
      languages: [Python, C++]
```

Then re-run:

```bash
./01_clone_repos.sh
./02_measure_loc.sh
./03_count_deps.sh
```

### Generate custom reports

Write a Python script reading `results/loc_summary.csv` and `results/deps_summary.csv`:

```python
import pandas as pd

loc = pd.read_csv("results/loc_summary.csv")
deps = pd.read_csv("results/deps_summary.csv")

# Custom analysis...
```

## Requirements Summary

| Tool | Version | Purpose |
|------|---------|---------|
| **git** | any | Clone repositories |
| **scc** | ≥3 | Count source lines (language-aware) |
| **python3** | ≥3.8 | Orchestration and analysis |
| **jq** | any | Parse JSON outputs |
| **go** | (optional) | Resolve Go transitive deps |
| **cargo** | (optional) | Resolve Rust transitive deps |
| **npm** / **node** | (optional) | Resolve JavaScript transitive deps |
| **mvn** | (optional) | Resolve Java/Maven transitive deps |
| **PyYAML** | (pip) | Parse YAML configs |
| **pandas** | (pip) | Data aggregation |
| **matplotlib** | (pip) | Visualization (optional) |

## License

MIT License. See [LICENSE](LICENSE) for details.

---

**Questions or Issues**: See GitHub issues or contribute a pull request.
