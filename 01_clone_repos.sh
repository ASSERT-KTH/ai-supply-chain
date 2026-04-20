#!/usr/bin/env bash
# =============================================================================
# 01_clone_repos.sh — Clone repos from stack_config.yaml
# =============================================================================
# Usage: ./01_clone_repos.sh [--all]
#   --all  include "alternative" repos in addition to "selected"
#
# Safe resume: a .clone_ok marker is written inside each repo directory after
# a successful clone.  Re-running the script:
#   - skips repos whose marker already exists  (done)
#   - re-clones repos whose directory exists but has no marker  (interrupted)
#   - clones repos not yet attempted
#
# Requirements: python3 + PyYAML, git
#   (yq is NOT required; YAML parsing is done with Python for version stability)
#
# Outputs:
#   repos/<layer>/<project>/        shallow clones
#   results/clone_report.tsv        per-repo log; appended across runs
#   results/clone_summary.txt       human-readable summary; overwritten each run
# =============================================================================

set -uo pipefail   # no -e: failures are handled per-repo

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/stack_config.yaml"
LOCK_FILE="$SCRIPT_DIR/stack_lock.yaml"
REPOS_DIR="$SCRIPT_DIR/repos"
RESULTS_DIR="$SCRIPT_DIR/results"
CLONE_DEPTH=1
CLONE_ALL=false
START_TS=$(date +%s)

[[ "${1:-}" == "--all" ]] && CLONE_ALL=true

# ── dependency check ──────────────────────────────────────────────────────────
for cmd in python3 git; do
    command -v "$cmd" &>/dev/null || { echo "ERROR: $cmd not found" >&2; exit 1; }
done
python3 -c "import yaml" 2>/dev/null || {
    echo "ERROR: PyYAML not installed (pip install pyyaml)" >&2; exit 1
}

mkdir -p "$REPOS_DIR" "$RESULTS_DIR"

# ── load pinned SHAs from stack_lock.yaml if present ─────────────────────────
declare -A PINNED_SHA   # repo -> sha  e.g. PINNED_SHA["minio/minio"]="abc123"
if [[ -f "$LOCK_FILE" ]]; then
    current_repo=""
    while IFS= read -r line; do
        trimmed="${line#"${line%%[! ]*}"}"   # ltrim
        if [[ "$trimmed" =~ ^repo:\ (.+)$ ]]; then
            current_repo="${BASH_REMATCH[1]}"
        elif [[ "$trimmed" =~ ^sha:\ ([0-9a-f]{40})$ ]] && [[ -n "$current_repo" ]]; then
            PINNED_SHA["$current_repo"]="${BASH_REMATCH[1]}"
        fi
    done < "$LOCK_FILE"
    echo "Lock    : $LOCK_FILE (${#PINNED_SHA[@]} pinned SHAs)"
else
    echo "Lock    : none — cloning at HEAD (run 00_freeze_versions.sh to pin)"
fi

# ── parse YAML once with Python, emit TSV: layer<TAB>repo<TAB>status<TAB>role ─
MANIFEST=$(STACK_CONFIG="$CONFIG" python3 -c "
import os, yaml
layers = ['data_pipelines', 'training', 'integration_serving', 'cross_cutting']
cfg = yaml.safe_load(open(os.environ['STACK_CONFIG']))
for layer in layers:
    for p in cfg.get(layer, {}).get('projects', []):
        role = p.get('role', '').replace('\t', ' ').replace('\n', ' ')
        print(f\"{layer}\t{p.get('repo','')}\t{p.get('status','')}\t{role}\")
")

# ── per-run log (TSV, header written only once) ───────────────────────────────
REPORT_TSV="$RESULTS_DIR/clone_report.tsv"
if [[ ! -f "$REPORT_TSV" ]]; then
    printf "timestamp\tstatus\tlayer\trepo\trole\treason\tsize_kb\n" > "$REPORT_TSV"
fi

# ── counters ──────────────────────────────────────────────────────────────────
clone_count=0
fail_count=0
already_count=0
current=0
FAILED_REPOS=()
FAILED_REASONS=()
LAYERS=("data_pipelines" "training" "integration_serving" "cross_cutting")

# ── pre-count repos to process (for progress display) ────────────────────────
total=0
while IFS=$'\t' read -r _layer _repo status _role; do
    [[ "$CLONE_ALL" == false && "$status" == "alternative" ]] && continue
    total=$((total+1))
done <<< "$MANIFEST"

# ── graceful interrupt ────────────────────────────────────────────────────────
INTERRUPTED=false
trap 'INTERRUPTED=true; echo ""' INT TERM

# ── helpers ───────────────────────────────────────────────────────────────────
ts()  { date "+%H:%M:%S"; }
pad() { printf "%0${#total}d" "$1"; }

log_row() {
    # log_row <status> <layer> <repo> <role> <reason> [size_kb]
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "$4" "$5" "${6:-0}" \
        >> "$REPORT_TSV"
}

# ── header ────────────────────────────────────────────────────────────────────
existing_done=$(find "$REPOS_DIR" -name ".clone_ok" 2>/dev/null | wc -l | tr -d ' ')
echo "Config  : $CONFIG"
echo "Output  : $REPOS_DIR"
echo "Repos   : $total  (depth=$CLONE_DEPTH  all=$CLONE_ALL)"
[[ "$existing_done" -gt 0 ]] && echo "Resuming: $existing_done repos already marked done"
echo "────────────────────────────────────────────────────────────────────"

# ── main loop ─────────────────────────────────────────────────────────────────
current_layer=""

while IFS=$'\t' read -r layer repo status role; do
    [[ "$INTERRUPTED" == true ]] && break

    # skip alternatives unless --all
    [[ "$CLONE_ALL" == false && "$status" == "alternative" ]] && continue

    # layer heading when it changes
    if [[ "$layer" != "$current_layer" ]]; then
        echo ""
        echo "▶ $layer"
        mkdir -p "$REPOS_DIR/$layer"
        current_layer="$layer"
    fi

    current=$((current+1))
    project_name=$(basename "$repo")
    target_dir="$REPOS_DIR/$layer/$project_name"
    marker="$target_dir/.clone_ok"

    # ── already done: skip ────────────────────────────────────────────────────
    if [[ -f "$marker" ]]; then
        size_kb=$(du -sk "$target_dir" 2>/dev/null | cut -f1 || echo 0)
        printf "  [%s/%d] %s  ✓ done     %s  (%d MB)\n" \
            "$(pad $current)" "$total" "$(ts)" "$repo" "$((size_kb/1024))"
        already_count=$((already_count+1))
        log_row "already_done" "$layer" "$repo" "$role" "" "$size_kb"
        continue
    fi

    # ── stale dir: previous run was interrupted — remove and retry ────────────
    if [[ -d "$target_dir" ]]; then
        printf "  [%s/%d] %s  ↺ reclone  %s  (stale dir, no .clone_ok)\n" \
            "$(pad $current)" "$total" "$(ts)" "$repo"
        rm -rf "$target_dir"
    fi

    # ── clone ─────────────────────────────────────────────────────────────────
    printf "  [%s/%d] %s  ⬇ cloning  %s\n" \
        "$(pad $current)" "$total" "$(ts)" "$repo"

    err_file=$(mktemp)
    if git clone --depth "$CLONE_DEPTH" --quiet \
           "https://github.com/$repo.git" "$target_dir" 2>"$err_file"; then

        # ── pin to locked SHA if available ───────────────────────────────────
        pinned_sha="${PINNED_SHA[$repo]:-}"
        if [[ -n "$pinned_sha" ]]; then
            # shallow clone only has the tip commit; fetch the pinned SHA if different
            current_sha=$(git -C "$target_dir" rev-parse HEAD 2>/dev/null || echo "")
            if [[ "$current_sha" != "$pinned_sha" ]]; then
                git -C "$target_dir" fetch --quiet --depth=1 origin "$pinned_sha" 2>/dev/null \
                    || git -C "$target_dir" fetch --quiet origin 2>/dev/null
            fi
            git -C "$target_dir" checkout --quiet --detach "$pinned_sha" 2>/dev/null
            printf "  [%s/%d] %s  ⚓ pinned   %s  @ %.8s\n" \
                "$(pad $current)" "$total" "$(ts)" "$repo" "$pinned_sha"
        fi

        touch "$marker"
        size_kb=$(du -sk "$target_dir" 2>/dev/null | cut -f1 || echo 0)
        printf "  [%s/%d] %s  ✓ ok       %s  (%d MB)\n" \
            "$(pad $current)" "$total" "$(ts)" "$repo" "$((size_kb/1024))"
        clone_count=$((clone_count+1))
        log_row "success" "$layer" "$repo" "$role" "" "$size_kb"

    else
        # distinguish: our interrupt killed git vs a real failure
        if [[ "$INTERRUPTED" == true ]]; then
            printf "  [%s/%d] %s  ⚡ stopped  %s\n" \
                "$(pad $current)" "$total" "$(ts)" "$repo"
            [[ -d "$target_dir" ]] && rm -rf "$target_dir"
            rm -f "$err_file"
            break
        fi

        reason=$(tr '\n' ' ' < "$err_file" | sed 's/  */ /g; s/^ //; s/ $//')
        printf "  [%s/%d] %s  ✗ failed   %s\n" \
            "$(pad $current)" "$total" "$(ts)" "$repo"
        printf "             └─ %s\n" "$reason"
        fail_count=$((fail_count+1))
        FAILED_REPOS+=("$repo")
        FAILED_REASONS+=("$reason")
        [[ -d "$target_dir" ]] && rm -rf "$target_dir"
        log_row "failed" "$layer" "$repo" "$role" "$reason" "0"
    fi
    rm -f "$err_file"

done <<< "$MANIFEST"

# ── summary ───────────────────────────────────────────────────────────────────
ELAPSED=$(( $(date +%s) - START_TS ))
ELAPSED_FMT=$(printf "%02d:%02d:%02d" $((ELAPSED/3600)) $((ELAPSED%3600/60)) $((ELAPSED%60)))
SUMMARY="$RESULTS_DIR/clone_summary.txt"

print_summary() {
    echo ""
    echo "════════════════════════════════════════════════════════════════════"
    echo "  Clone run — $(date)"
    echo "════════════════════════════════════════════════════════════════════"
    printf "  %-20s %d / %d\n" "Processed:"    "$current"      "$total"
    printf "  %-20s %d\n"      "Newly cloned:" "$clone_count"
    printf "  %-20s %d\n"      "Already done:" "$already_count"
    printf "  %-20s %d\n"      "Failed:"       "$fail_count"
    printf "  %-20s %s\n"      "Elapsed:"      "$ELAPSED_FMT"
    echo ""

    # Disk usage by layer
    echo "  Disk usage by layer:"
    total_kb=0
    for layer in "${LAYERS[@]}"; do
        layer_dir="$REPOS_DIR/$layer"
        [[ -d "$layer_dir" ]] || continue
        kb=$(du -sk "$layer_dir" 2>/dev/null | cut -f1 || echo 0)
        total_kb=$((total_kb + kb))
        printf "    %-26s %6d MB\n" "$layer" "$((kb/1024))"
    done
    printf "    %-26s %6d MB\n" "TOTAL" "$((total_kb/1024))"
    echo ""

    # Largest repos
    echo "  Largest repos (top 8):"
    size_lines=()
    for layer in "${LAYERS[@]}"; do
        layer_dir="$REPOS_DIR/$layer"
        [[ -d "$layer_dir" ]] || continue
        for d in "$layer_dir"/*/; do
            [[ -d "$d" && -f "${d}.clone_ok" ]] || continue
            kb=$(du -sk "$d" 2>/dev/null | cut -f1 || echo 0)
            size_lines+=("$kb $(basename "$d")")
        done
    done
    if [[ ${#size_lines[@]} -gt 0 ]]; then
        printf '%s\n' "${size_lines[@]}" | sort -rn | head -8 | \
            while read -r kb name; do
                printf "    %6d MB  %s\n" "$((kb/1024))" "$name"
            done
    else
        echo "    (no repos cloned yet)"
    fi
    echo ""

    # Repos per layer
    echo "  Repos cloned per layer:"
    for layer in "${LAYERS[@]}"; do
        n_done=0
        layer_dir="$REPOS_DIR/$layer"
        if [[ -d "$layer_dir" ]]; then
            while IFS= read -r -d '' _; do
                n_done=$((n_done+1))
            done < <(find "$layer_dir" -name ".clone_ok" -print0 2>/dev/null)
        fi
        printf "    %-26s %d\n" "$layer" "$n_done"
    done
    echo ""

    # Failures
    if [[ ${#FAILED_REPOS[@]} -gt 0 ]]; then
        echo "  Failed repos:"
        for i in "${!FAILED_REPOS[@]}"; do
            printf "    ✗ %s\n"    "${FAILED_REPOS[$i]}"
            printf "      └─ %s\n" "${FAILED_REASONS[$i]}"
        done
        echo ""
    fi

    if [[ "$INTERRUPTED" == true ]]; then
        echo "  ⚡ Run interrupted. Re-run to resume from where it stopped."
        echo ""
    elif [[ "$current" -lt "$total" ]]; then
        echo "  ⚠  $((total - current)) repos not yet reached. Re-run to continue."
        echo ""
    fi

    echo "  Log (all runs) : $REPORT_TSV"
    echo "  This summary   : $SUMMARY"
    echo "════════════════════════════════════════════════════════════════════"
}

print_summary | tee "$SUMMARY"
