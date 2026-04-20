#!/usr/bin/env python3
"""
10_build_dep_graph.py
Build a multi-language direct-dependency graph of the selected AI stack.

Reads:
    results/deps_per_project/*.json   — per-repo direct/transitive deps
    stack_config.yaml                 — layer and repo metadata

Produces:
    results/dep_graph.json            — node/edge list (generic, for D3/Cytoscape)
    results/dep_graph_projects.dot    — GraphViz DOT (projects-only view)
    results/dep_graph_stats.txt       — summary stats and cross-layer shared deps

Node types:
    - project: one of the 48 selected repos (layer-colored)
    - package: an external dependency (ecosystem-colored)

Edges: project --(ecosystem)--> dep
Cross-stack edges (project -> project) are flagged when a dep name matches
another selected repo (by repo basename or known package alias).

Requirements: pip install pyyaml
"""

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing pyyaml. Install: pip install pyyaml")

SCRIPT_DIR = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"
DEPS_DIR = RESULTS_DIR / "deps_per_project"
CONFIG_FILE = SCRIPT_DIR / "stack_config.yaml"

LAYER_COLORS = {
    "data_pipelines": "#1f77b4",
    "training": "#8c6bb1",
    "integration_serving": "#2ca02c",
    "cross_cutting": "#6c757d",
}
CROSS_STACK_COLOR = "#17becf"
ECOSYSTEM_COLORS = {
    "python": "#3572A5",
    "go": "#00ADD8",
    "rust": "#DEA584",
    "cpp": "#F34B7D",
    "java": "#B07219",
    "maven": "#B07219",
    "gradle": "#B07219",
    "javascript": "#F1E05A",
    "typescript": "#2B7489",
    "scala": "#C22D40",
}

# Package-name aliases: map a dep string to a selected-repo key when they
# refer to the same artifact across ecosystems. Keep conservative.
PACKAGE_ALIASES = {
    "torch": "pytorch/pytorch",
    "pytorch": "pytorch/pytorch",
    "tensorflow": "tensorflow/tensorflow",
    "jax": "jax-ml/jax",
    "ray": "ray-project/ray",
    "transformers": "huggingface/transformers",
    "datasets": "huggingface/datasets",
    "tokenizers": "huggingface/tokenizers",
    "peft": "huggingface/peft",
    "trl": "huggingface/trl",
    "huggingface-hub": "huggingface/huggingface_hub",
    "huggingface_hub": "huggingface/huggingface_hub",
    "sentence-transformers": "UKPLab/sentence-transformers",
    "mlflow": "mlflow/mlflow",
    "wandb": "wandb/wandb",
    "vllm": "vllm-project/vllm",
    "onnx": "onnx/onnx",
    "onnxruntime": "microsoft/onnxruntime",
    "deepspeed": "microsoft/DeepSpeed",
    "langchain": "langchain-ai/langchain",
    "dvc": "iterative/dvc",
    "great-expectations": "great-expectations/great_expectations",
    "great_expectations": "great-expectations/great_expectations",
    "guardrails-ai": "guardrails-ai/guardrails",
    "promptflow": "microsoft/promptflow",
    "mergekit": "arcee-ai/mergekit",
    "prefect": "PrefectHQ/prefect",
    "dagster": "dagster-io/dagster",
    "apache-airflow": "apache/airflow",
    "airflow": "apache/airflow",
    "pyspark": "apache/spark",
    "dask": "dask/dask",
    "label-studio": "HumanSignal/label-studio",
    "faiss-cpu": "facebookresearch/faiss",
    "faiss-gpu": "facebookresearch/faiss",
    "chromadb": "chroma-core/chroma",
    "weaviate-client": "weaviate/weaviate",
    "minio": "minio/minio",
    # Infra client libraries and ecosystem bindings
    "pymilvus": "milvus-io/milvus",
    "kafka-python": "apache/kafka",
    "confluent-kafka": "apache/kafka",
    "aiokafka": "apache/kafka",
    "prometheus-client": "prometheus/prometheus",
    "prometheus_client": "prometheus/prometheus",
    "grafana-api": "grafana/grafana",
    "opentelemetry-api": "open-telemetry/opentelemetry-collector",
    "opentelemetry-sdk": "open-telemetry/opentelemetry-collector",
    "kubernetes": "kubernetes/kubernetes",
    "kfp": "kubeflow/pipelines",
    "tritonclient": "triton-inference-server/server",
    "python-terraform": "hashicorp/terraform",
}

# Prefix rules for Go modules and organization-scoped packages.
# Matched after exact alias lookup; first match wins.
PACKAGE_PREFIXES = [
    ("k8s.io/",                            "kubernetes/kubernetes"),
    ("sigs.k8s.io/",                       "kubernetes/kubernetes"),
    ("github.com/kubernetes/",             "kubernetes/kubernetes"),
    ("github.com/containerd/",             "containerd/containerd"),
    ("github.com/prometheus/",             "prometheus/prometheus"),
    ("go.opentelemetry.io/",               "open-telemetry/opentelemetry-collector"),
    ("github.com/open-telemetry/",         "open-telemetry/opentelemetry-collector"),
    ("github.com/grafana/loki",            "grafana/loki"),
    ("github.com/grafana/",                "grafana/grafana"),
    ("github.com/hashicorp/terraform",     "hashicorp/terraform"),
    ("github.com/envoyproxy/",             "envoyproxy/envoy"),
    ("github.com/ray-project/",            "ray-project/ray"),
    ("github.com/kubeflow/",               "kubeflow/pipelines"),
    ("github.com/argoproj/argo-workflows", "argoproj/argo-workflows"),
    ("github.com/argoproj/argo-cd",        "argoproj/argo-cd"),
    ("github.com/minio/",                  "minio/minio"),
    ("github.com/milvus-io/",              "milvus-io/milvus"),
    ("github.com/nvidia/",                 "NVIDIA/nvidia-container-toolkit"),
    ("github.com/nvidia/nvidia-container", "NVIDIA/nvidia-container-toolkit"),
]


def load_loc_map():
    """Return dict: repo_full_name -> code_lines (int)."""
    out = {}
    p = RESULTS_DIR / "loc_summary.csv"
    if not p.exists():
        return out
    with p.open() as f:
        for row in csv.DictReader(f):
            try:
                out[row["repo"]] = int(row["code_lines"])
            except (KeyError, ValueError):
                pass
    return out


def fmt_loc(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}k"
    return str(n)


def load_config():
    with CONFIG_FILE.open() as f:
        return yaml.safe_load(f)


def selected_repos(config):
    """Return dict: repo_full_name -> {layer, languages, role}."""
    out = {}
    layer_keys = ["data_pipelines", "training", "integration_serving", "cross_cutting"]
    for layer in layer_keys:
        for p in config.get(layer, {}).get("projects", []):
            if p.get("status") != "selected":
                continue
            out[p["repo"]] = {
                "layer": layer,
                "languages": p.get("languages", []),
                "role": p.get("role", ""),
            }
    return out


def normalize_dep_name(name):
    """Strip version pins and extras to get a bare package name."""
    s = name.strip()
    s = re.split(r"[<>=!~;\s\[]", s, 1)[0]
    return s.lower()


def dep_to_repo_key(dep_name, selected):
    """Map a raw dep string to a selected-repo key if it refers to one."""
    n = normalize_dep_name(dep_name)
    if n in PACKAGE_ALIASES and PACKAGE_ALIASES[n] in selected:
        return PACKAGE_ALIASES[n]
    # Prefix match (Go modules, org-scoped packages). Use case-insensitive match
    # on the full normalized string; prefixes are already lowercase.
    for prefix, target in PACKAGE_PREFIXES:
        if n.startswith(prefix) and target in selected:
            return target
    # Try matching repo basename directly (e.g. "prometheus" -> prometheus/prometheus)
    for repo in selected:
        base = repo.split("/", 1)[1].lower()
        if n == base:
            return repo
    return None


def primary_ecosystem(ecosystems_field):
    """Pick the primary ecosystem string for edge coloring."""
    if not ecosystems_field:
        return "unknown"
    return ecosystems_field.split(",")[0].strip().lower()


def build_graph():
    config = load_config()
    selected = selected_repos(config)
    loc_map = load_loc_map()

    nodes = {}   # node_id -> attrs
    edges = []   # list of {source, target, ecosystem, cross_stack}
    dep_uses = defaultdict(set)  # package_node_id -> set of project repos that depend on it

    # Preload per-project dep totals.
    total_deps_map = {}
    for repo in selected:
        safe = repo.split("/", 1)[1].replace("/", "_")
        layer = selected[repo]["layer"]
        fpath = DEPS_DIR / f"{layer}__{safe}.json"
        if fpath.exists():
            try:
                with fpath.open() as f:
                    d = json.load(f)
                total_deps_map[repo] = int(d.get("transitive_total") or d.get("direct_total") or 0)
            except Exception:
                total_deps_map[repo] = 0

    # Add project nodes.
    for repo, meta in selected.items():
        nid = f"repo:{repo}"
        nodes[nid] = {
            "id": nid,
            "label": repo.split("/", 1)[1],
            "kind": "project",
            "repo": repo,
            "layer": meta["layer"],
            "languages": meta["languages"],
            "color": LAYER_COLORS.get(meta["layer"], "#888888"),
            "loc": loc_map.get(repo, 0),
            "total_deps": total_deps_map.get(repo, 0),
        }

    missing_files = []
    for repo in selected:
        safe = repo.split("/", 1)[1].replace("/", "_")
        layer = selected[repo]["layer"]
        fpath = DEPS_DIR / f"{layer}__{safe}.json"
        if not fpath.exists():
            missing_files.append(str(fpath))
            continue

        with fpath.open() as f:
            data = json.load(f)

        ecosystem = primary_ecosystem(data.get("ecosystems_detected", ""))
        src_id = f"repo:{repo}"

        for dep in data.get("direct_deps", []):
            raw = dep.strip()
            if not raw:
                continue
            target_repo = dep_to_repo_key(raw, selected)
            if target_repo and target_repo != repo:
                tgt_id = f"repo:{target_repo}"
                edges.append({
                    "source": src_id, "target": tgt_id,
                    "ecosystem": ecosystem, "cross_stack": True,
                })
            else:
                norm = normalize_dep_name(raw)
                tgt_id = f"pkg:{ecosystem}:{norm}"
                if tgt_id not in nodes:
                    nodes[tgt_id] = {
                        "id": tgt_id,
                        "label": norm,
                        "kind": "package",
                        "ecosystem": ecosystem,
                        "color": ECOSYSTEM_COLORS.get(ecosystem, "#AAAAAA"),
                    }
                dep_uses[tgt_id].add(repo)
                edges.append({
                    "source": src_id, "target": tgt_id,
                    "ecosystem": ecosystem, "cross_stack": False,
                })

    # Annotate package nodes with fan-in (how many projects use them).
    for nid, attrs in nodes.items():
        if attrs["kind"] == "package":
            attrs["fan_in"] = len(dep_uses.get(nid, ()))

    return nodes, edges, dep_uses, missing_files


def write_json(nodes, edges, path):
    with open(path, "w") as f:
        json.dump({
            "nodes": list(nodes.values()),
            "edges": edges,
        }, f, indent=2)


def write_dot(nodes, edges, path):
    """Projects-only DOT: top row data -> training -> serving, bottom cross_cutting."""
    import xml.sax.saxutils as sx
    esc = lambda s: sx.escape(str(s)).replace('"', r'\"')

    lines = [
        "digraph stack_projects {",
        '  rankdir=TB; newrank=true; nodesep=0.25; ranksep=0.6;',
        '  graph [ratio="compress"];',
        '  node [style=filled, fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=8, color="#66666680"];',
    ]

    def project_label(n):
        return f'{esc(n["label"])}\\nLOC: {fmt_loc(n.get("loc",0))} | deps: {n.get("total_deps",0)}'

    by_layer = defaultdict(list)
    for nid, n in nodes.items():
        if n["kind"] == "project":
            by_layer[n["layer"]].append((nid, n))

    top_order = ["data_pipelines", "training", "integration_serving"]
    lines.append('  subgraph cluster_top {')
    lines.append('    label=""; style=invis;')
    for layer in top_order:
        items = by_layer.get(layer, [])
        lines.append(f'    subgraph cluster_{layer} {{')
        lines.append(f'      label="{layer}"; style=rounded; color="{LAYER_COLORS[layer]}"; penwidth=2;')
        for nid, n in items:
            lines.append(
                f'      "{nid}" [label="{project_label(n)}", shape=box, '
                f'fillcolor="{n["color"]}", fontcolor=white];'
            )
        lines.append('    }')
    lines.append('  }')

    cc_items = by_layer.get("cross_cutting", [])
    if cc_items:
        mid = (len(cc_items) + 1) // 2
        cc_row1 = cc_items[:mid]
        cc_row2 = cc_items[mid:]
        lines.append('  subgraph cluster_cross_cutting {')
        lines.append(f'    label="cross_cutting"; style=rounded; color="{LAYER_COLORS["cross_cutting"]}"; penwidth=2;')
        # _cc_left/_cc_right are invisible rail nodes placed inside the cluster.
        # They are rank-same with row 1 and horizontally tied (constraint=false)
        # to the leftmost/rightmost top anchors, stretching the cluster border.
        lines.append('    _cc_left  [style=invis, width=0, height=0, label=""];')
        lines.append('    _cc_right [style=invis, width=0, height=0, label=""];')
        for nid, n in cc_items:
            lines.append(
                f'    "{nid}" [label="{project_label(n)}", shape=box, '
                f'fillcolor="{n["color"]}", fontcolor=white];'
            )
        lines.append('  }')

    # Pin top three clusters side-by-side on the same rank, left-to-right
    # in the order: data_pipelines -> training -> integration_serving.
    # The invisible chain of edges enforces both the rank and the L-R order.
    top_anchors = [by_layer.get(l, [])[0][0] for l in top_order if by_layer.get(l)]
    for a, b in zip(top_anchors, top_anchors[1:]):
        lines.append(f'  "{a}" -> "{b}" [style=invis, constraint=true, minlen=4];')
    if top_anchors:
        lines.append('  { rank=same; ' + '; '.join(f'"{a}"' for a in top_anchors) + '; }')

    # Force cross_cutting below the top clusters and split it into two rows.
    if cc_items and top_anchors:
        cc_anchor = cc_row1[0][0]
        # Vertical placement: push cc row 1 below the top clusters.
        lines.append('  _row_spacer [style=invis, width=0, height=0, label=""];')
        lines.append(f'  "{top_anchors[0]}" -> _row_spacer [style=invis, minlen=3];')
        lines.append(f'  _row_spacer -> "{cc_anchor}" [style=invis, minlen=3];')

        # Row 1: all nodes + rail sentinels on the same rank.
        row1_ids = ['_cc_left'] + [nid for nid, _ in cc_row1] + ['_cc_right']
        lines.append('  { rank=same; ' + '; '.join(f'"{x}"' for x in row1_ids) + '; }')

        # Horizontal rail: tie left sentinel to leftmost top anchor and right
        # sentinel to rightmost top anchor. constraint=false means no vertical
        # pull — only horizontal alignment is enforced.
        lines.append(f'  "{top_anchors[0]}"  -> "_cc_left"  [style=invis, constraint=false];')
        lines.append(f'  "{top_anchors[-1]}" -> "_cc_right" [style=invis, constraint=false];')

        # Row 2: below row 1 via an invisible edge from the row 1 anchor.
        if cc_row2:
            cc_anchor2 = cc_row2[0][0]
            lines.append(f'  "{cc_anchor}" -> "{cc_anchor2}" [style=invis, minlen=2];')
            if len(cc_row2) > 1:
                lines.append('  { rank=same; ' + '; '.join(f'"{nid}"' for nid, _ in cc_row2) + '; }')

    seen = set()
    for e in edges:
        if not e["cross_stack"]:
            continue
        key = (e["source"], e["target"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f'  "{e["source"]}" -> "{e["target"]}" [color="{CROSS_STACK_COLOR}", penwidth=1.5];'
        )

    lines.append("}")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def write_stats(nodes, edges, dep_uses, missing_files, path):
    n_projects = sum(1 for n in nodes.values() if n["kind"] == "project")
    n_packages = sum(1 for n in nodes.values() if n["kind"] == "package")
    n_cross = sum(1 for e in edges if e["cross_stack"])
    eco_count = defaultdict(int)
    for e in edges:
        eco_count[e["ecosystem"]] += 1

    # Top shared packages (fan-in).
    shared = sorted(
        ((nodes[nid]["label"], nodes[nid]["ecosystem"], len(users), sorted(users))
         for nid, users in dep_uses.items() if len(users) >= 2),
        key=lambda x: -x[2],
    )

    lines = []
    lines.append("=== Dependency Graph Summary ===")
    lines.append(f"project nodes:        {n_projects}")
    lines.append(f"package nodes:        {n_packages}")
    lines.append(f"edges (total):        {len(edges)}")
    lines.append(f"edges (cross-stack):  {n_cross}")
    lines.append("")
    lines.append("Edges by ecosystem:")
    for eco, c in sorted(eco_count.items(), key=lambda x: -x[1]):
        lines.append(f"  {eco:12s} {c}")
    lines.append("")
    lines.append(f"Shared packages (fan-in >= 2): {len(shared)}")
    lines.append("Top 30 most-shared packages:")
    for name, eco, k, users in shared[:30]:
        lines.append(f"  [{eco:8s}] {name:35s}  used by {k}: {', '.join(users)}")
    if missing_files:
        lines.append("")
        lines.append(f"Missing dep JSONs ({len(missing_files)}):")
        for m in missing_files:
            lines.append(f"  {m}")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:20]))


def main():
    nodes, edges, dep_uses, missing = build_graph()
    write_json(nodes, edges, RESULTS_DIR / "dep_graph.json")
    write_stats(nodes, edges, dep_uses, missing, RESULTS_DIR / "dep_graph_stats.txt")

    out = RESULTS_DIR / "dep_graph_projects.dot"
    write_dot(nodes, edges, out)
    print(f"Wrote {out}")

    print("\nRender:")
    print("  dot -Tpdf results/dep_graph_projects.dot -o results/dep_graph_projects.pdf")


if __name__ == "__main__":
    main()
