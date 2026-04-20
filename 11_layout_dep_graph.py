#!/usr/bin/env python3
"""
11_layout_dep_graph.py
Interactive Tkinter layout editor for the projects-only dependency graph.

Loads:
    results/dep_graph.json                 — node/edge data (built by step 10)
    results/dep_graph_projects_layout.json — saved positions (if present)

Writes:
    results/dep_graph_projects_layout.json    — node positions (canvas coords)
    results/dep_graph_projects_positioned.dot — DOT with pos="x,y!" per node

Render with neato -n2 (honors pinned positions + curved splines):
    neato -n2 -Tpdf results/dep_graph_projects_positioned.dot \\
          -o results/dep_graph_projects_positioned.pdf

Controls:
    - Drag nodes with the mouse to move them.
    - Drag the edge or corner of a layer box to resize it. Saved layouts
      persist the custom box rectangles in `layer_boxes` inside the JSON.
    - "Save" writes layout JSON + positioned DOT.
    - "Reset" rearranges nodes in a grid inside their layer box.
    - "Auto-position" runs force-directed layout within each layer box.
    - "Toggle edges" shows/hides edges.
"""

import json
import math
import random
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

SCRIPT_DIR = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"
GRAPH_JSON = RESULTS_DIR / "dep_graph.json"
CONFIG_FILE = SCRIPT_DIR / "stack_config.yaml"
LAYOUT_JSON = RESULTS_DIR / "dep_graph_projects_layout.json"
OUT_DOT = RESULTS_DIR / "dep_graph_projects_positioned.dot"
OUT_DOT_FRAMES = RESULTS_DIR / "dep_graph_projects_frames.dot"

# --- Layer palettes (CACM print-safe) ---
# User-specified soft colors for better visibility in figures
LAYER_COLORS_OPT1 = {
    "data_pipelines":      "#E1ECFA",  # soft blue
    "training":            "#EAE1F5",  # soft purple
    "integration_serving": "#E1F5E6",  # soft green
    "cross_cutting":       "#EBEBEB",  # soft grey
}
# Active palette
LAYER_COLORS = LAYER_COLORS_OPT1
CROSS_STACK_COLOR = "#17becf"


def _darken_hex(hex_color, factor=0.55):
    """Return a darker variant of #RRGGBB; factor in (0,1], lower = darker."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = max(0, min(255, int(r * factor)))
    g = max(0, min(255, int(g * factor)))
    b = max(0, min(255, int(b * factor)))
    return f"#{r:02X}{g:02X}{b:02X}"


LAYER_BORDER_COLORS = {k: _darken_hex(v, 0.55) for k, v in LAYER_COLORS.items()}

CANVAS_W = 1800
PAD = 20
TOP_H = 400
BOT_H = 215
CANVAS_H = 1200
NODE_W = 150
NODE_H = 46

SIZE_W_SCALE = 2.5
SIZE_H_SCALE = 1.8

TOP_LAYERS = ["data_pipelines", "training", "integration_serving"]

# Relative widths for top-layer columns (must sum to 1.0).
# Training gets a larger share than the other two.
TOP_LAYER_WIDTH_FRAC = {
    "data_pipelines": 0.28,
    "training": 0.44,
    "integration_serving": 0.28,
}

# Force-directed layout parameters
FD_ITERATIONS = 600
FD_K = 80.0          # natural spring length (pixels)
FD_REPULSION = 18000.0
FD_ATTRACTION = 0.06
FD_COOLING_START = 12.0
FD_COOLING_END = 0.3
FD_ANIMATE_EVERY = 10  # redraw every N iterations


def compute_node_sizes(nodes, scale_by_loc):
    """
    Return dict nid -> (width, height) in pixels.

    - no scale: uniform (NODE_W, NODE_H)
    - scale:    sqrt(LOC) → width [NODE_W, NODE_W*SIZE_W_SCALE],
                height [NODE_H, NODE_H*SIZE_H_SCALE]
    """
    if not scale_by_loc:
        return {nid: (NODE_W, NODE_H) for nid in nodes}

    vals = [max(n.get("loc", 0) or 0, 0) for n in nodes.values()]
    pos_vals = [v for v in vals if v > 0]
    if not pos_vals:
        return {nid: (NODE_W, NODE_H) for nid in nodes}

    lo = math.sqrt(min(pos_vals))
    hi = math.sqrt(max(pos_vals))
    span = hi - lo or 1.0

    out = {}
    for nid, n in nodes.items():
        loc = max(n.get("loc", 0) or 0, 0)
        if loc <= 0:
            out[nid] = (NODE_W, NODE_H)
            continue
        t = (math.sqrt(loc) - lo) / span
        w = NODE_W * (1 + t * (SIZE_W_SCALE - 1))
        h = NODE_H * (1 + t * (SIZE_H_SCALE - 1))
        out[nid] = (w, h)
    return out


def fmt_loc(n):
    if n < 0:
        return "N/A"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}k"
    return str(n)


def layer_box(layer):
    """Return (x1, y1, x2, y2) for a layer's container rectangle."""
    total_inner_w = CANVAS_W - 4 * PAD
    if layer in TOP_LAYERS:
        idx = TOP_LAYERS.index(layer)
        x1 = PAD
        for prev in TOP_LAYERS[:idx]:
            x1 += int(total_inner_w * TOP_LAYER_WIDTH_FRAC[prev]) + PAD
        col_w = int(total_inner_w * TOP_LAYER_WIDTH_FRAC[layer])
        y1 = PAD
        return x1, y1, x1 + col_w, y1 + TOP_H
    # cross_cutting spans full width bottom
    y1 = PAD + TOP_H + PAD
    return PAD, y1, CANVAS_W - PAD, y1 + BOT_H


LAYER_COLS = {
    "training": 3,
}
LAYER_ROWS = {
    "cross_cutting": 3,
}


def grid_positions(nodes_by_layer, sizes=None, layer_boxes=None):
    """
    Deterministic grid layout inside each layer box with guaranteed
    no-overlap spacing. Nodes are placed left-to-right, top-to-bottom.

    Per-layer overrides:
      - LAYER_COLS forces a column count (row count derived from n).
      - LAYER_ROWS forces a row count (column count derived from n).

    Variable sizes supported: when `sizes` is provided (nid -> (w, h)),
    column widths use the max node width in that column and row heights
    use the max in that row. A short last row is horizontally centered.

    If `layer_boxes` (dict layer -> (x1,y1,x2,y2)) is given, use those
    rectangles instead of the default `layer_box(layer)`.
    """
    gap_x = 16
    gap_y = 14
    pos = {}

    def sz(nid):
        return sizes[nid] if sizes else (NODE_W, NODE_H)

    def _box(layer):
        if layer_boxes and layer in layer_boxes:
            return tuple(layer_boxes[layer])
        return layer_box(layer)

    for layer, items in nodes_by_layer.items():
        n = len(items)
        if n == 0:
            continue
        x1, y1, x2, y2 = _box(layer)
        inner_w = x2 - x1 - 2 * PAD
        inner_h = y2 - y1 - 2 * PAD - 20

        if layer in LAYER_ROWS:
            rows = min(LAYER_ROWS[layer], n)
            cols = (n + rows - 1) // rows
            this_gap_x = gap_x
        elif layer in LAYER_COLS:
            cols = min(LAYER_COLS[layer], n)
            rows = (n + cols - 1) // cols
            this_gap_x = gap_x
        else:
            step_x = NODE_W + gap_x  # approximate; refined below
            max_cols = max(1, int(inner_w // step_x))
            cols = min(n, max_cols)
            rows = (n + cols - 1) // cols
            this_gap_x = gap_x

        # Per-column max width and per-row max height (variable sizing).
        col_w = [NODE_W] * cols
        row_h = [NODE_H] * rows
        for i, nid in enumerate(items):
            r, c = divmod(i, cols)
            w, h = sz(nid)
            if w > col_w[c]:
                col_w[c] = w
            if h > row_h[r]:
                row_h[r] = h

        total_w = sum(col_w) + (cols - 1) * this_gap_x
        total_h = sum(row_h) + (rows - 1) * gap_y

        # If too wide for the box, shrink horizontal gap (floor at 2px).
        if total_w > inner_w and cols > 1:
            overflow = total_w - inner_w
            shrink = min(this_gap_x - 2, overflow / (cols - 1))
            this_gap_x -= shrink
            total_w = sum(col_w) + (cols - 1) * this_gap_x

        start_x = x1 + PAD + max(0, (inner_w - total_w) / 2)
        # Reserve 20px for the layer title. For cross_cutting the title is at
        # the bottom, so don't offset start_y by the title height.
        label_top = 0 if layer == "cross_cutting" else 20
        start_y = y1 + PAD + label_top + max(0, (inner_h - total_h) / 2)

        # Column x-centres and row y-centres.
        col_cx = []
        acc = start_x
        for c in range(cols):
            col_cx.append(acc + col_w[c] / 2)
            acc += col_w[c] + this_gap_x
        row_cy = []
        acc = start_y
        for r in range(rows):
            row_cy.append(acc + row_h[r] / 2)
            acc += row_h[r] + gap_y

        last_row_idx = (n - 1) // cols
        last_row_count = n - last_row_idx * cols
        if last_row_count < cols:
            last_row_w = sum(col_w[:last_row_count]) + max(0, last_row_count - 1) * this_gap_x
            full_row_w = sum(col_w) + (cols - 1) * this_gap_x
            last_row_shift = (full_row_w - last_row_w) / 2
        else:
            last_row_shift = 0

        for i, nid in enumerate(items):
            r, c = divmod(i, cols)
            cx = col_cx[c] + (last_row_shift if r == last_row_idx else 0)
            cy = row_cy[r]
            pos[nid] = (cx, cy)
    return pos


def _clamp_to_box(cx, cy, layer, w=NODE_W, h=NODE_H, box=None):
    """Clamp a node centre to the safe area inside its layer box.

    If `box` (x1,y1,x2,y2) is given it overrides the default layer_box(layer).
    """
    if box is not None:
        x1, y1, x2, y2 = box
    else:
        x1, y1, x2, y2 = layer_box(layer)
    inner_pad = 6
    label_space = 22
    cx = max(x1 + w / 2 + inner_pad, min(x2 - w / 2 - inner_pad, cx))
    if layer == "cross_cutting":
        cy = max(y1 + h / 2 + inner_pad,
                 min(y2 - h / 2 - inner_pad - label_space, cy))
    else:
        cy = max(y1 + h / 2 + inner_pad + label_space,
                 min(y2 - h / 2 - inner_pad, cy))
    return cx, cy


def force_directed_positions(nodes, edges, start_pos, iterations=FD_ITERATIONS,
                              progress_cb=None):
    """
    Spring-repulsion layout confined per layer box.

    Only edges whose both endpoints share the same layer attract; cross-layer
    edges are ignored for force purposes (they would pull nodes out of boxes).
    Returns a new pos dict.
    """
    pos = {nid: list(xy) for nid, xy in start_pos.items()}
    nids = list(nodes.keys())

    # Build adjacency within the same layer only
    same_layer_edges = [
        (s, d) for s, d in edges
        if nodes[s]["layer"] == nodes[d]["layer"]
    ]

    cooling_range = FD_COOLING_START - FD_COOLING_END

    for it in range(iterations):
        temp = FD_COOLING_START - cooling_range * (it / iterations)
        disp = {nid: [0.0, 0.0] for nid in nids}

        # Repulsion: all pairs within the same layer
        by_layer = {}
        for nid in nids:
            by_layer.setdefault(nodes[nid]["layer"], []).append(nid)

        for layer_nodes in by_layer.values():
            for i in range(len(layer_nodes)):
                for j in range(i + 1, len(layer_nodes)):
                    u, v = layer_nodes[i], layer_nodes[j]
                    dx = pos[u][0] - pos[v][0]
                    dy = pos[u][1] - pos[v][1]
                    dist = max(math.hypot(dx, dy), 1.0)
                    force = FD_REPULSION / (dist * dist)
                    fx, fy = (dx / dist) * force, (dy / dist) * force
                    disp[u][0] += fx
                    disp[u][1] += fy
                    disp[v][0] -= fx
                    disp[v][1] -= fy

        # Attraction: same-layer edges only
        for s, d in same_layer_edges:
            dx = pos[d][0] - pos[s][0]
            dy = pos[d][1] - pos[s][1]
            dist = max(math.hypot(dx, dy), 1.0)
            force = FD_ATTRACTION * (dist - FD_K)
            fx, fy = (dx / dist) * force, (dy / dist) * force
            disp[s][0] += fx
            disp[s][1] += fy
            disp[d][0] -= fx
            disp[d][1] -= fy

        # Apply displacement, clamped by temperature and layer box
        for nid in nids:
            dx, dy = disp[nid]
            mag = max(math.hypot(dx, dy), 1.0)
            step = min(mag, temp)
            nx = pos[nid][0] + (dx / mag) * step
            ny = pos[nid][1] + (dy / mag) * step
            nx, ny = _clamp_to_box(nx, ny, nodes[nid]["layer"])
            pos[nid] = [nx, ny]

        if progress_cb and (it % FD_ANIMATE_EVERY == 0 or it == iterations - 1):
            progress_cb({nid: tuple(xy) for nid, xy in pos.items()}, it, iterations)

    return {nid: tuple(xy) for nid, xy in pos.items()}


def _cubic_bezier_points(sx, sy, tx, ty, offset, n_steps=30):
    """
    Return a flat list of (x, y) points along a cubic Bezier curve from
    (sx, sy) to (tx, ty) with perpendicular control-point offset.
    These are passed to create_line with smooth=False so Tkinter does not
    double-smooth an already-curved point list.
    """
    dx = tx - sx
    dy = ty - sy
    length = math.hypot(dx, dy) or 1.0
    # Perpendicular unit vector
    px = -dy / length
    py = dx / length
    # Place control points at 1/3 and 2/3 along the line, offset perpendicularly.
    # Using a single consistent offset direction gives a smooth S-free arc.
    c1x = sx + dx / 3 + px * offset
    c1y = sy + dy / 3 + py * offset
    c2x = sx + 2 * dx / 3 + px * offset
    c2y = sy + 2 * dy / 3 + py * offset
    pts = []
    for i in range(n_steps + 1):
        t = i / n_steps
        u = 1 - t
        x = u**3 * sx + 3 * u**2 * t * c1x + 3 * u * t**2 * c2x + t**3 * tx
        y = u**3 * sy + 3 * u**2 * t * c1y + 3 * u * t**2 * c2y + t**3 * ty
        pts.extend([x, y])
    return pts


def _edge_offsets(edges):
    """
    For each (src, dst) pair compute a perpendicular offset so that parallel
    edges between the same two nodes are visually separated.
    Returns dict (src, dst) -> offset_pixels.
    """
    # Group parallel edges (same unordered pair)
    groups = {}
    for s, d in edges:
        key = tuple(sorted([s, d]))
        groups.setdefault(key, []).append((s, d))

    offsets = {}
    base = 30  # pixels between parallel edges
    for key, group in groups.items():
        n = len(group)
        for i, (s, d) in enumerate(group):
            # spread symmetrically around a non-zero centre so even single
            # edges get a gentle arc (centre = base/2 for n==1)
            centre = base / 2 if n == 1 else 0
            off = centre + (i - (n - 1) / 2) * base
            offsets[(s, d)] = off
    return offsets


class LayoutApp:
    def __init__(self, root, nodes, edges, transitive_loc_map=None, dep_counts_map=None):
        self.root = root
        self.nodes = nodes  # dict nid -> attrs
        self.edges = edges  # list of (src, dst)
        self.transitive_loc_map = transitive_loc_map or {}  # repo -> transitive_loc
        self.dep_counts_map = dep_counts_map or {}  # repo -> (direct_count, transitive_count)
        self.by_layer = {}
        for nid, n in nodes.items():
            self.by_layer.setdefault(n["layer"], []).append(nid)

        self._edge_offsets = _edge_offsets(edges)

        root.title("Dependency graph layout editor")

        toolbar = ttk.Frame(root)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Save", command=self.save).pack(side="left", padx=4, pady=4)
        ttk.Button(toolbar, text="Reset layout", command=self.reset_layout).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Auto-position", command=self.auto_position).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Toggle edges", command=self.toggle_edges).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Toggle size", command=self.toggle_size).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Toggle LOC mode", command=self.toggle_loc_mode).pack(side="left", padx=4)
        self.status = ttk.Label(toolbar, text="")
        self.status.pack(side="right", padx=8)

        self.canvas = tk.Canvas(root, width=CANVAS_W, height=CANVAS_H, bg="white")
        self.canvas.pack(fill="both", expand=True)

        self.pos = {}          # nid -> (cx, cy)
        self.node_items = {}   # nid -> (rect_id, text_id)
        self.edge_items = []   # list of canvas ids
        self.box_items = {}    # layer -> rect_canvas_id (for dragging detection)
        self.show_edges = True
        self.scale_by_loc = False
        self.show_transitive_loc = False  # Toggle between direct and direct+transitive
        self.sizes = compute_node_sizes(self.nodes, self.scale_by_loc)
        self._drag_nid = None
        self._drag_dx = 0
        self._drag_dy = 0
        self._auto_running = False

        # Per-layer box rectangles (mutable, user-resizable).
        # Seeded from the default layer_box() formulas; overwritten from
        # saved JSON in load_positions() if present.
        self.layer_boxes = {
            layer: list(layer_box(layer))
            for layer in TOP_LAYERS + ["cross_cutting"]
        }

        # Box-resize drag state
        self._box_drag = None  # dict: {layer, edges: set('l'|'r'|'t'|'b'), start_box, start_xy}
        self._edge_hit = 6     # pixels within an edge to grab it
        self._min_box = 80     # minimum width/height when resizing

        self.load_positions()
        self.draw_all()

        self.canvas.tag_bind("node", "<ButtonPress-1>", self.on_press)
        self.canvas.tag_bind("node", "<B1-Motion>", self.on_drag)
        self.canvas.tag_bind("node", "<ButtonRelease-1>", self.on_release)

        # Box-edge interaction (bound to the whole canvas; handlers decide
        # whether a click landed on a box edge). Node handlers fire first
        # because "node" is a more-specific tag binding.
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_press, add="+")
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag, add="+")
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release, add="+")

    # --- persistence ---
    def load_positions(self):
        data = {}
        raw = {}
        if LAYOUT_JSON.exists():
            try:
                raw = json.loads(LAYOUT_JSON.read_text())
                data = raw.get("positions", {})
            except Exception:
                data, raw = {}, {}
        if raw.get("scale_by_loc"):
            self.scale_by_loc = True
        if raw.get("show_transitive_loc"):
            self.show_transitive_loc = True
        saved_boxes = raw.get("layer_boxes") or {}
        for layer, rect in saved_boxes.items():
            if layer in self.layer_boxes and isinstance(rect, list) and len(rect) == 4:
                self.layer_boxes[layer] = [float(v) for v in rect]
        self.sizes = compute_node_sizes(self.nodes, self.scale_by_loc)
        auto = grid_positions(self.by_layer, self.sizes, self.layer_boxes)
        for nid in self.nodes:
            if nid in data:
                self.pos[nid] = tuple(data[nid])
            else:
                self.pos[nid] = auto.get(nid, (CANVAS_W / 2, CANVAS_H / 2))

    def save(self):
        import subprocess, tempfile, os
        LAYOUT_JSON.write_text(json.dumps({
            "canvas": [CANVAS_W, CANVAS_H],
            "scale_by_loc": self.scale_by_loc,
            "show_transitive_loc": self.show_transitive_loc,
            "layer_boxes": {k: list(v) for k, v in self.layer_boxes.items()},
            "positions": {nid: list(xy) for nid, xy in self.pos.items()},
        }, indent=2))
        self.write_dot()

        # Compose output paths
        out_pdf = OUT_DOT.parent / "dep_graph_projects_positioned.pdf"
        # Use temp files for intermediate PDFs
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_pdf = Path(tmpdir) / "_graph.pdf"
            frames_pdf = Path(tmpdir) / "_frames.pdf"
            # Render nodes+edges
            subprocess.run([
                "neato", "-n", "-Tpdf", str(OUT_DOT), "-o", str(graph_pdf)
            ], check=True)
            # Render frames
            subprocess.run([
                "neato", "-n2", "-Tpdf", str(OUT_DOT_FRAMES), "-o", str(frames_pdf)
            ], check=True)
            # Overlay frames on graph
            subprocess.run([
                "pdftk", str(graph_pdf), "background", str(frames_pdf), "output", str(out_pdf)
            ], check=True)

        # Remove intermediate DOT files, keep only layout and PDF
        try:
            OUT_DOT.unlink()
        except Exception:
            pass
        try:
            OUT_DOT_FRAMES.unlink()
        except Exception:
            pass

        self.status.config(text=f"Saved and rendered {out_pdf.name} (only layout and PDF kept)")

    def write_dot(self):
        """
        Emit two pinned-position DOT files:
          - OUT_DOT        : nodes + edges, NO frame rectangles. Rendered
                             with neato -n so the spline router avoids only
                             the real project nodes.
          - OUT_DOT_FRAMES : frame rectangles only. Rendered independently
                             so its nodes never clash with project nodes.

        Both files share invisible corner-anchor nodes at (0,0) and
        (CANVAS_W, CANVAS_H) to force identical page extents, so overlaying
        the two PDFs produces a correctly-registered composite.
        
        NOTE: Uses the current UI LOC mode (direct or direct+transitive)
        in the PDF output to match what was toggled in the interactive view.
        """
        # Content bbox (canvas coords) over layer frames and nodes, with a
        # small padding, so the exported page crops to just the drawing.
        pad_out = 1
        xs1, ys1, xs2, ys2 = [], [], [], []
        for layer in TOP_LAYERS + ["cross_cutting"]:
            if layer not in self.by_layer:
                continue
            bx1, by1, bx2, by2 = self.layer_boxes[layer]
            xs1.append(bx1); ys1.append(by1)
            xs2.append(bx2); ys2.append(by2)
        for nid, (cx, cy) in self.pos.items():
            nw, nh = self.sizes.get(nid, (NODE_W, NODE_H))
            xs1.append(cx - nw / 2); ys1.append(cy - nh / 2)
            xs2.append(cx + nw / 2); ys2.append(cy + nh / 2)
        bbox_x1 = (min(xs1) if xs1 else 0) - pad_out
        bbox_y1 = (min(ys1) if ys1 else 0) - pad_out
        bbox_x2 = (max(xs2) if xs2 else CANVAS_W) + pad_out
        bbox_y2 = (max(ys2) if ys2 else CANVAS_H) + pad_out

        # Corner anchors pin the page extent to the content bbox so both
        # PDFs share identical geometry and overlay cleanly.
        def anchor_lines():
            return [
                f'  "_anchor_tl" [shape=point, style=invis, width=0, height=0, '
                f'label="", pos="{bbox_x1:.1f},{CANVAS_H - bbox_y1:.1f}!"];',
                f'  "_anchor_br" [shape=point, style=invis, width=0, height=0, '
                f'label="", pos="{bbox_x2:.1f},{CANVAS_H - bbox_y2:.1f}!"];',
            ]

        # --- File 1: nodes + edges (no frames) ---
        lines = [
            "digraph stack_projects_positioned {",
            '  graph [splines=true, bgcolor="transparent", margin=0];',
            '  node [fontname="Helvetica", fontsize=18];',
            '  edge [fontname="Helvetica", fontsize=9];',
        ]
        lines.extend(anchor_lines())

        for nid, n in self.nodes.items():
            cx, cy = self.pos[nid]
            gy = CANVAS_H - cy
            w, h = self.sizes.get(nid, (NODE_W, NODE_H))
            # Use transitive LOC and deps if toggled in UI; otherwise direct only
            direct_loc = n.get("loc", 0)
            repo = n.get("repo", "")
            direct_deps, transitive_deps = self.dep_counts_map.get(repo, (0, 0))

            if self.show_transitive_loc:
                transitive_loc = self.transitive_loc_map.get(repo, 0)
                total_loc = direct_loc + transitive_loc
                if direct_deps < 0 or transitive_deps < 0:
                    total_deps = "N/A"
                else:
                    total_deps = direct_deps + transitive_deps
                label = (f'{n["label"]}\\n'
                         f'LOC: {fmt_loc(total_loc)} | deps: {total_deps}')
            else:
                deps_str = "N/A" if direct_deps < 0 else direct_deps
                label = (f'{n["label"]}\\n'
                         f'LOC: {fmt_loc(direct_loc)} | deps: {deps_str}')
            color = LAYER_COLORS.get(n["layer"], "#888888")
            lines.append(
                f'  "{nid}" [shape=box, style=filled, label="{label}", '
                f'pos="{cx:.1f},{gy:.1f}!", '
                f'width={w/72:.2f}, height={h/72:.2f}, fixedsize=false, '
                f'margin="0.08,0.05", '
                f'fillcolor="{color}", fontcolor=black];'
            )

        for src, dst in self.edges:
            lines.append(
                f'  "{src}" -> "{dst}" [color="{CROSS_STACK_COLOR}", penwidth=1.5];'
            )

        lines.append("}")
        OUT_DOT.write_text("\n".join(lines))

        # --- File 2: frames only ---
        frame_lines = [
            "digraph stack_projects_frames {",
            '  graph [splines=false, bgcolor="transparent", margin=0];',
            '  node [fontname="Helvetica", fontsize=18];',
        ]
        frame_lines.extend(anchor_lines())

        # Frames match the user-drawn layer boxes (self.layer_boxes),
        # so the exported figure has the same frame geometry as the editor.
        layer_labels = {
            "data_pipelines": "Data Production",
            "training": "Model Training",
            "integration_serving": "Integration & Inference",
            "cross_cutting": "Cross-Cutting Substrate",
        }
        for layer in TOP_LAYERS + ["cross_cutting"]:
            items = self.by_layer.get(layer, [])
            if not items:
                continue
            x1, y1, x2, y2 = self.layer_boxes[layer]
            labelloc = "b" if layer == "cross_cutting" else "t"
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            w = (x2 - x1) / 72.0
            h = (y2 - y1) / 72.0
            gy = CANVAS_H - cy
            color = LAYER_BORDER_COLORS[layer]
            label = layer_labels.get(layer, layer)
            frame_lines.append(
                f'  "frame_{layer}" [shape=box, style="rounded,dashed", '
                f'color="{color}", penwidth=2, fixedsize=true, margin=0, '
                f'width={w:.2f}, height={h:.2f}, pos="{cx:.1f},{gy:.1f}!", '
                f'label="{label}", labelloc={labelloc}, fontcolor="#333333"];'
            )

        frame_lines.append("}")
        OUT_DOT_FRAMES.write_text("\n".join(frame_lines))

    # --- drawing ---
    def draw_all(self):
        self.canvas.delete("all")
        # Layer boxes
        self.box_items = {}
        layer_labels = {
            "data_pipelines": "Data Production",
            "training": "Model Training",
            "integration_serving": "Integration & Inference",
            "cross_cutting": "Cross-Cutting Substrate",
        }
        for layer in TOP_LAYERS + ["cross_cutting"]:
            if layer not in self.by_layer:
                continue
            x1, y1, x2, y2 = self.layer_boxes[layer]
            rid = self.canvas.create_rectangle(
                x1, y1, x2, y2, outline=LAYER_BORDER_COLORS[layer], width=2, dash=(4, 3),
                tags=("layerbox", f"box:{layer}"),
            )
            self.box_items[layer] = rid
            label = layer_labels.get(layer, layer)
            if layer == "cross_cutting":
                self.canvas.create_text(
                    x1 + 10, y2 - 10, text=label, anchor="sw",
                    fill="#333333", font=("Helvetica", 13, "bold"),
                )
            else:
                self.canvas.create_text(
                    x1 + 10, y1 + 10, text=label, anchor="nw",
                    fill="#333333", font=("Helvetica", 13, "bold"),
                )
        # Edges first (so nodes paint over them)
        self.edge_items = []
        if self.show_edges:
            self._draw_edges()
        # Nodes
        self.node_items = {}
        for nid, n in self.nodes.items():
            self._draw_node(nid, n)

    def _draw_edges(self):
        for src, dst in self.edges:
            if src in self.pos and dst in self.pos:
                lid = self._create_edge_line(src, dst)
                self.edge_items.append(lid)

    def _create_edge_line(self, src, dst):
        sx, sy = self.pos[src]
        tx, ty = self.pos[dst]
        offset = self._edge_offsets.get((src, dst), 0)
        pts = _cubic_bezier_points(sx, sy, tx, ty, offset)
        lid = self.canvas.create_line(
            *pts,
            fill=CROSS_STACK_COLOR, width=1.3,
            smooth=False, arrow=tk.LAST, arrowshape=(10, 12, 4),
        )
        return lid

    def _draw_node(self, nid, n):
        cx, cy = self.pos[nid]
        w, h = self.sizes.get(nid, (NODE_W, NODE_H))
        color = LAYER_COLORS.get(n["layer"], "#888888")
        shape = self.canvas.create_rectangle(
            cx - w / 2, cy - h / 2,
            cx + w / 2, cy + h / 2,
            fill=color, outline="black", width=1, tags=("node", nid),
        )
        # Get LOC: direct or direct+transitive depending on mode
        direct_loc = n.get("loc", 0)
        if self.show_transitive_loc:
            repo = n.get("repo", "")
            transitive_loc = self.transitive_loc_map.get(repo, 0)
            total_loc = direct_loc + transitive_loc
            loc_label = f"LOC: {fmt_loc(direct_loc)} (direct) + {fmt_loc(transitive_loc)} (trans)"
        else:
            total_loc = direct_loc
            loc_label = f"LOC: {fmt_loc(direct_loc)}"

        # Get dependency count: direct or direct+transitive depending on mode
        repo = n.get("repo", "")
        direct_deps, transitive_deps = self.dep_counts_map.get(repo, (0, 0))
        if self.show_transitive_loc:
            if direct_deps < 0 or transitive_deps < 0:
                total_deps = "N/A"
            else:
                total_deps = direct_deps + transitive_deps
        else:
            total_deps = "N/A" if direct_deps < 0 else direct_deps

        text = self.canvas.create_text(
            cx, cy,
            text=f'{n["label"]}\n{loc_label}  deps: {total_deps}',
            fill="black", font=("Helvetica", 12, "bold"), tags=("node", nid),
        )
        self.node_items[nid] = (shape, text)
        return shape, text

    def refresh_edges(self):
        for lid in self.edge_items:
            self.canvas.delete(lid)
        self.edge_items = []
        if not self.show_edges:
            return
        for src, dst in self.edges:
            if src in self.pos and dst in self.pos:
                lid = self._create_edge_line(src, dst)
                self.canvas.tag_lower(lid)
                self.edge_items.append(lid)

    def _raise_all_nodes(self):
        for r, t in self.node_items.values():
            self.canvas.tag_raise(r)
            self.canvas.tag_raise(t)

    # --- interaction ---
    def _nid_from_event(self, event):
        item = self.canvas.find_withtag("current")
        if not item:
            return None
        tags = self.canvas.gettags(item[0])
        for t in tags:
            if t in self.nodes:
                return t
        return None

    def on_press(self, event):
        if self._auto_running:
            return
        nid = self._nid_from_event(event)
        if not nid:
            return
        self._drag_nid = nid
        cx, cy = self.pos[nid]
        self._drag_dx = cx - event.x
        self._drag_dy = cy - event.y
        rect, text = self.node_items[nid]
        self.canvas.tag_raise(rect)
        self.canvas.tag_raise(text)

    def on_drag(self, event):
        if not self._drag_nid:
            return
        nid = self._drag_nid
        w, h = self.sizes.get(nid, (NODE_W, NODE_H))
        cx = event.x + self._drag_dx
        cy = event.y + self._drag_dy
        layer = self.nodes[nid]["layer"]
        cx, cy = _clamp_to_box(cx, cy, layer, w, h, box=self.layer_boxes[layer])
        self.pos[nid] = (cx, cy)
        rect, text = self.node_items[nid]
        self.canvas.coords(rect, cx - w / 2, cy - h / 2,
                           cx + w / 2, cy + h / 2)
        self.canvas.coords(text, cx, cy)
        self.refresh_edges()
        self._raise_all_nodes()

    def on_release(self, event):
        self._drag_nid = None

    # --- box-resize interaction ---
    def _edges_at(self, x, y):
        """
        Return (layer, edges_set) if (x,y) is near any box edge, else None.
        edges_set is a subset of {'l','r','t','b'}; corners have two members.
        """
        hit = self._edge_hit
        for layer, (x1, y1, x2, y2) in self.layer_boxes.items():
            if layer not in self.by_layer:
                continue
            # Must be roughly within the box's bounding rect (plus hit margin)
            if not (x1 - hit <= x <= x2 + hit and y1 - hit <= y <= y2 + hit):
                continue
            edges = set()
            if abs(x - x1) <= hit and y1 - hit <= y <= y2 + hit:
                edges.add("l")
            if abs(x - x2) <= hit and y1 - hit <= y <= y2 + hit:
                edges.add("r")
            if abs(y - y1) <= hit and x1 - hit <= x <= x2 + hit:
                edges.add("t")
            if abs(y - y2) <= hit and x1 - hit <= x <= x2 + hit:
                edges.add("b")
            if edges:
                return layer, edges
        return None

    def _cursor_for_edges(self, edges):
        if edges == {"l"} or edges == {"r"}:
            return "sb_h_double_arrow"
        if edges == {"t"} or edges == {"b"}:
            return "sb_v_double_arrow"
        if edges == {"l", "t"} or edges == {"r", "b"}:
            return "size_nw_se"
        if edges == {"r", "t"} or edges == {"l", "b"}:
            return "size_ne_sw"
        return ""

    def on_canvas_motion(self, event):
        if self._drag_nid is not None or self._box_drag is not None:
            return
        hit = self._edges_at(event.x, event.y)
        cursor = self._cursor_for_edges(hit[1]) if hit else ""
        try:
            self.canvas.config(cursor=cursor)
        except tk.TclError:
            self.canvas.config(cursor="")

    def on_canvas_press(self, event):
        # If the click landed on a node, node-tag binding handled it.
        if self._drag_nid is not None or self._auto_running:
            return
        hit = self._edges_at(event.x, event.y)
        if not hit:
            return
        layer, edges = hit
        self._box_drag = {
            "layer": layer,
            "edges": edges,
            "start_box": list(self.layer_boxes[layer]),
            "start_xy": (event.x, event.y),
        }

    def on_canvas_drag(self, event):
        bd = self._box_drag
        if bd is None:
            return
        dx = event.x - bd["start_xy"][0]
        dy = event.y - bd["start_xy"][1]
        x1, y1, x2, y2 = bd["start_box"]
        if "l" in bd["edges"]:
            x1 = min(x1 + dx, x2 - self._min_box)
            x1 = max(0, x1)
        if "r" in bd["edges"]:
            x2 = max(x2 + dx, x1 + self._min_box)
            x2 = min(CANVAS_W, x2)
        if "t" in bd["edges"]:
            y1 = min(y1 + dy, y2 - self._min_box)
            y1 = max(0, y1)
        if "b" in bd["edges"]:
            y2 = max(y2 + dy, y1 + self._min_box)
            y2 = min(CANVAS_H, y2)
        self.layer_boxes[bd["layer"]] = [x1, y1, x2, y2]
        # Re-clamp nodes in this layer so they stay inside the new box.
        layer = bd["layer"]
        for nid in self.by_layer.get(layer, []):
            cx, cy = self.pos[nid]
            w, h = self.sizes.get(nid, (NODE_W, NODE_H))
            cx, cy = _clamp_to_box(cx, cy, layer, w, h, box=self.layer_boxes[layer])
            self.pos[nid] = (cx, cy)
        self.draw_all()

    def on_canvas_release(self, event):
        if self._box_drag is not None:
            self._box_drag = None
            self.status.config(text="Box resized. Press Save to persist.")

    def reset_layout(self):
        if self._auto_running:
            return
        if not messagebox.askyesno("Reset", "Discard current positions and regrid?"):
            return
        self.pos = grid_positions(self.by_layer, self.sizes, self.layer_boxes)
        self.draw_all()

    def toggle_edges(self):
        self.show_edges = not self.show_edges
        self.refresh_edges()

    def toggle_size(self):
        """Flip between uniform node sizes and LOC-scaled sizes."""
        self.scale_by_loc = not self.scale_by_loc
        self.sizes = compute_node_sizes(self.nodes, self.scale_by_loc)
        self.pos = grid_positions(self.by_layer, self.sizes, self.layer_boxes)
        self.draw_all()
        mode = "LOC-scaled" if self.scale_by_loc else "uniform"
        self.status.config(text=f"Size mode: {mode}. Press Save to persist.")

    def toggle_loc_mode(self):
        """Toggle between showing direct LOC only vs. direct + transitive."""
        self.show_transitive_loc = not self.show_transitive_loc
        self.draw_all()
        mode = "direct + transitive" if self.show_transitive_loc else "direct only"
        self.status.config(text=f"LOC mode: {mode}. Press Save to persist.")

    # --- auto-position ---
    def auto_position(self):
        """Snap all nodes to a clean grid inside their layer box."""
        if self._auto_running:
            return
        self.pos = grid_positions(self.by_layer, self.sizes, self.layer_boxes)
        self.draw_all()
        self.status.config(text="Snapped to grid. Press Save to persist.")

    def _apply_auto_pos(self, new_pos, it, total):
        self.pos.update(new_pos)
        # Move node canvas items directly (faster than full redraw)
        for nid, (cx, cy) in new_pos.items():
            if nid not in self.node_items:
                continue
            rect, text = self.node_items[nid]
            self.canvas.coords(rect, cx - NODE_W / 2, cy - NODE_H / 2,
                               cx + NODE_W / 2, cy + NODE_H / 2)
            self.canvas.coords(text, cx, cy)
        self.refresh_edges()
        self._raise_all_nodes()
        pct = int(100 * it / FD_ITERATIONS)
        self.status.config(text=f"Auto-positioning… {pct}%")

    def _auto_done(self):
        self._auto_running = False
        self.status.config(text="Auto-position complete. Press Save to persist.")


def load_dep_loc_measured():
    """Load measured LOC for all transitive dependencies."""
    p = RESULTS_DIR / "dep_loc_measured.json"
    if not p.exists():
        return {}
    with p.open() as f:
        return json.load(f)


def load_config():
    """Load stack configuration."""
    if yaml is None:
        return {}
    try:
        with CONFIG_FILE.open() as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


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


def normalize_pkg_name(name):
    """Normalize package name for lookup: strip version specs, lowercase."""
    import re
    s = name.strip()
    s = re.split(r"[<>=!~;\s\[]", s, maxsplit=1)[0]
    return s.lower()


def lookup_pkg_loc(pkg_name, dep_loc_measured):
    """
    Look up LOC for a package across all ecosystems.
    Returns the code lines (int) or 0 if not found/not measured.
    """
    normalized = normalize_pkg_name(pkg_name)
    # Try exact match in each ecosystem
    for ecosystem, pkgs in dep_loc_measured.items():
        if isinstance(pkgs, dict):
            for key in pkgs:
                if normalize_pkg_name(key) == normalized:
                    v = pkgs[key]
                    if v.get("scc_ok"):
                        return v.get("code", 0)
    return 0


def calculate_transitive_loc(repo, selected, dep_loc_measured):
    """
    Calculate total transitive LOC for a project by summing all transitive deps.
    """
    safe = repo.split("/", 1)[1].replace("/", "_")
    layer = selected[repo]["layer"]
    fpath = RESULTS_DIR / "deps_per_project" / f"{layer}__{safe}.json"
    
    if not fpath.exists():
        return 0
    
    try:
        with fpath.open() as f:
            data = json.load(f)
    except Exception:
        return 0
    
    total = 0
    # Sum LOC from all transitive dependencies across all ecosystems
    for eco_key in ["transitive_deps_python", "transitive_deps_go", 
                    "transitive_deps_cargo", "transitive_deps_npm",
                    "transitive_deps_maven", "transitive_deps_gradle"]:
        for pkg_name in data.get(eco_key, []):
            total += lookup_pkg_loc(pkg_name, dep_loc_measured)
    
    return total


def load_dep_counts(repo, selected):
    """
    Load direct and transitive dependency counts for a project.
    Returns (direct_count, transitive_count).
    """
    safe = repo.split("/", 1)[1].replace("/", "_")
    layer = selected[repo]["layer"]
    fpath = RESULTS_DIR / "deps_per_project" / f"{layer}__{safe}.json"
    
    if not fpath.exists():
        return 0, 0
    
    try:
        with fpath.open() as f:
            data = json.load(f)
    except Exception:
        return 0, 0
    
    direct = data.get("direct_total", 0)
    transitive = data.get("transitive_total", 0)
    return direct, transitive


def load_graph():
    data = json.loads(GRAPH_JSON.read_text())
    nodes = {}
    for n in data["nodes"]:
        if n.get("kind") != "project":
            continue
        nodes[n["id"]] = n
    edges = []
    seen = set()
    for e in data["edges"]:
        if not e.get("cross_stack"):
            continue
        if e["source"] in nodes and e["target"] in nodes:
            key = (e["source"], e["target"])
            if key in seen:
                continue
            seen.add(key)
            edges.append(key)
    return nodes, edges


def main():
    if not GRAPH_JSON.exists():
        raise SystemExit(f"Missing {GRAPH_JSON}. Run 10_build_dep_graph.py first.")
    nodes, edges = load_graph()
    
    # Load transitive LOC data
    dep_loc_measured = load_dep_loc_measured()
    
    # Load selected repos and calculate transitive LOC per project
    config = load_config()
    selected = selected_repos(config)
    transitive_loc_map = {}
    dep_counts_map = {}
    for repo in selected:
        transitive_loc_map[repo] = calculate_transitive_loc(repo, selected, dep_loc_measured)
        dep_counts_map[repo] = load_dep_counts(repo, selected)
    
    root = tk.Tk()
    LayoutApp(root, nodes, edges, transitive_loc_map, dep_counts_map)
    root.mainloop()


if __name__ == "__main__":
    main()
