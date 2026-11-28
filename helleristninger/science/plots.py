"""Publication-grade PNG + vector SVG chart renderer for Nordic rock-art findings.

Uses Pillow (2x supersampled RGBA compositing + Segoe UI / Arial TrueType fonts)
so it executes deterministically with zero native libcairo DLL dependencies.
"""

from __future__ import annotations

import html
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

PALETTE = {
    "bg": "#FAF8F4",
    "panel": "#FFFFFF",
    "border": "#D8D2C5",
    "grid": "#EAE5DA",
    "ink": "#1C252C",
    "muted": "#5C666E",
    "stone": "#2D5B75",      # Fjord blue (Stone Age veideristninger)
    "bronze": "#B85D3B",     # Ochre/terracotta (Bronze Age jordbruksristninger)
    "sage": "#4A7C59",       # Lichen green
    "amber": "#C98A2C",      # Amber / mixed
    "slate": "#7D8790",      # Unclassified / other
    "plum": "#6D4C6E",       # Accent
}

FONT_STACK = "Segoe UI, Arial, DejaVu Sans, sans-serif"


def _hex_rgba(hex_col: str, alpha: float = 1.0) -> Tuple[int, int, int, int]:
    h = hex_col.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    a = max(0, min(255, int(round(alpha * 255))))
    return (r, g, b, a)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"]
        if bold
        else [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"]
    )
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


class DualCanvas:
    """Draws publication-grade charts simultaneously to 2x supersampled RGBA PNG and SVG."""

    def __init__(self, width: int, height: int, bg: str = PALETTE["bg"]) -> None:
        self.w = width
        self.h = height
        self.ss = 2  # 2x supersample for crisp antialiased downsampling
        self.img = Image.new("RGBA", (width * self.ss, height * self.ss), _hex_rgba(bg, 1.0))
        self.draw = ImageDraw.Draw(self.img, "RGBA")
        self.svg: List[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            f'<rect width="{width}" height="{height}" fill="{bg}"/>',
        ]
        self._font_cache: Dict[Tuple[int, bool], ImageFont.ImageFont] = {}

    def _font(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        key = (size * self.ss, bold)
        if key not in self._font_cache:
            self._font_cache[key] = _load_font(size * self.ss, bold=bold)
        return self._font_cache[key]

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: Optional[str] = None,
        fill_opacity: float = 1.0,
        stroke: Optional[str] = None,
        stroke_width: float = 1.0,
        rx: float = 0.0,
    ) -> None:
        if w <= 0 or h <= 0:
            return
        ss = self.ss
        box = [x * ss, y * ss, (x + w) * ss, (y + h) * ss]
        rad = rx * ss
        f_col = _hex_rgba(fill, fill_opacity) if fill else None
        s_col = _hex_rgba(stroke, 1.0) if stroke else None
        sw = max(1, int(round(stroke_width * ss))) if stroke else 0

        if fill and fill_opacity < 1.0:
            overlay = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay, "RGBA")
            if rad > 0:
                od.rounded_rectangle(box, radius=rad, fill=f_col, outline=s_col, width=sw)
            else:
                od.rectangle(box, fill=f_col, outline=s_col, width=sw)
            self.img = Image.alpha_composite(self.img, overlay)
            self.draw = ImageDraw.Draw(self.img, "RGBA")
        else:
            if rad > 0:
                self.draw.rounded_rectangle(box, radius=rad, fill=f_col, outline=s_col, width=sw)
            else:
                self.draw.rectangle(box, fill=f_col, outline=s_col, width=sw)

        attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'width="{w:.1f}"', f'height="{h:.1f}"']
        if rx > 0:
            attrs.append(f'rx="{rx:.1f}"')
        if fill:
            attrs.append(f'fill="{fill}"')
            if fill_opacity < 1.0:
                attrs.append(f'fill-opacity="{fill_opacity:.2f}"')
        else:
            attrs.append('fill="none"')
        if stroke:
            attrs.append(f'stroke="{stroke}" stroke-width="{stroke_width:.1f}"')
        self.svg.append(f"<rect {' '.join(attrs)}/>")

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        stroke: str,
        stroke_width: float = 1.0,
        dashed: bool = False,
    ) -> None:
        ss = self.ss
        col = _hex_rgba(stroke, 1.0)
        sw = max(1, int(round(stroke_width * ss)))
        if not dashed:
            self.draw.line([(x1 * ss, y1 * ss), (x2 * ss, y2 * ss)], fill=col, width=sw)
        else:
            dx, dy = x2 - x1, y2 - y1
            dist = math.hypot(dx, dy)
            step = 6.0
            n = max(1, int(dist / step))
            for k in range(0, n, 2):
                t0 = k / n
                t1 = min(1.0, (k + 1) / n)
                self.draw.line(
                    [((x1 + dx * t0) * ss, (y1 + dy * t0) * ss), ((x1 + dx * t1) * ss, (y1 + dy * t1) * ss)],
                    fill=col,
                    width=sw,
                )
        dash_attr = ' stroke-dasharray="4 4"' if dashed else ""
        self.svg.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" stroke-width="{stroke_width:.1f}"{dash_attr}/>'
        )

    def circle(self, cx: float, cy: float, r: float, fill: str) -> None:
        ss = self.ss
        col = _hex_rgba(fill, 1.0)
        self.draw.ellipse([(cx - r) * ss, (cy - r) * ss, (cx + r) * ss, (cy + r) * ss], fill=col)
        self.svg.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}"/>')

    def text(
        self,
        x: float,
        y: float,
        txt: str,
        size: int = 12,
        bold: bool = False,
        fill: str = PALETTE["ink"],
        anchor: str = "start",
    ) -> None:
        ss = self.ss
        font = self._font(size, bold=bold)
        col = _hex_rgba(fill, 1.0)
        bbox = self.draw.textbbox((0, 0), txt, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        px = x * ss
        if anchor == "middle":
            px -= tw / 2.0
        elif anchor == "end":
            px -= tw
        # Align baseline approximately to SVG y
        py = y * ss - th * 0.85
        self.draw.text((px, py), txt, font=font, fill=col)

        esc = html.escape(txt, quote=True)
        fw = ' font-weight="700"' if bold else ""
        anc = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.svg.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT_STACK}" font-size="{size}"{fw}{anc} fill="{fill}">{esc}</text>'
        )

    def save(self, out_png: Path) -> None:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        self.svg.append("</svg>")
        out_png.with_suffix(".svg").write_text("\n".join(self.svg), encoding="utf-8")
        final = self.img.resize((int(self.w * 1.5), int(self.h * 1.5)), Image.Resampling.LANCZOS).convert("RGB")
        final.save(out_png, format="PNG", dpi=(200, 200))


def _header(cv: DualCanvas, title: str, subtitle: str) -> None:
    cv.text(48, 46, title, size=22, bold=True, fill=PALETTE["ink"])
    cv.text(48, 72, subtitle, size=13, fill=PALETTE["muted"])


def _footer(cv: DualCanvas, caption: str) -> None:
    cv.text(48, cv.h - 18, caption, size=11, fill=PALETTE["muted"])


def plot_corpus_overview(summary: Dict[str, Any], out_png: Path) -> None:
    """Figure 1: Two-panel corpus distribution by core region & period, plus calibration/licence split."""
    w, h = 1280, 680
    cv = DualCanvas(w, h)
    _header(
        cv,
        "Nordic Rock-Art Scientific Corpus — Geographic, Chronological & Archival Coverage",
        f"N = {summary['total_figures']} traced figures (painted_photo excluded) · Release v2026.W48",
    )

    lx, ly, lw, lh = 48, 102, 650, 510
    cv.rect(lx, ly, lw, lh, fill=PALETTE["panel"], stroke=PALETTE["border"], rx=8)
    cv.text(lx + 20, ly + 32, "A. Regional Distribution by Tradition / Period", size=15, bold=True)

    reg_data: Dict[str, Dict[str, int]] = summary.get("region_period_counts", {})
    regions = list(reg_data.keys())
    periods = ["stone_age_hunter", "bronze_age_agrarian", "mixed", "unknown"]
    period_labels = {
        "stone_age_hunter": "Stone Age (veideristninger)",
        "bronze_age_agrarian": "Bronze Age (jordbruksristninger)",
        "mixed": "Mixed",
        "unknown": "Undated",
    }
    period_colors = {
        "stone_age_hunter": PALETTE["stone"],
        "bronze_age_agrarian": PALETTE["bronze"],
        "mixed": PALETTE["amber"],
        "unknown": PALETTE["slate"],
    }

    leg_x, leg_y = lx + 20, ly + 52
    for p in periods:
        cv.rect(leg_x, leg_y, 12, 12, fill=period_colors[p], rx=2)
        cv.text(leg_x + 17, leg_y + 10, period_labels[p], size=11)
        leg_x += 195 if p == "bronze_age_agrarian" else (165 if p == "stone_age_hunter" else 90)

    max_reg = max([sum(reg_data[r].values()) for r in regions] + [1])
    bar_x0 = lx + 175
    bar_max_w = lw - 245
    row_h = min(52, (lh - 110) // max(1, len(regions)))

    for idx, reg in enumerate(regions):
        ry = ly + 92 + idx * row_h
        tot = sum(reg_data[reg].values())
        cv.text(bar_x0 - 12, ry + 21, reg, size=12, bold=True, anchor="end")
        cv.rect(bar_x0, ry + 6, bar_max_w, 22, fill=PALETTE["grid"], rx=3)
        cur_x = float(bar_x0)
        for p in periods:
            val = reg_data[reg].get(p, 0)
            if val <= 0:
                continue
            seg_w = (val / max_reg) * bar_max_w
            cv.rect(cur_x, ry + 6, seg_w, 22, fill=period_colors[p])
            cur_x += seg_w
        cv.text(cur_x + 8, ry + 21, str(tot), size=11, bold=True)

    # Panel B
    rx, ry0, rw, rh1 = 720, 102, 512, 235
    cv.rect(rx, ry0, rw, rh1, fill=PALETTE["panel"], stroke=PALETTE["border"], rx=8)
    cv.text(rx + 20, ry0 + 32, "B. Measurement & Provenance Coverage", size=15, bold=True)

    cov_items = [
        ("Physical Scale Calibrated (mm)", summary.get("calibrated_count", 0), summary["total_figures"], PALETTE["sage"]),
        ("Verified Coordinates (EPSG:4326)", summary.get("coords_count", 0), summary["total_figures"], PALETTE["stone"]),
        ("Askeladden / RAÄ FMIS ID", summary.get("site_id_count", 0), summary["total_figures"], PALETTE["bronze"]),
        ("Commercial-Eligible Licence", summary.get("commercial_ok_count", 0), summary["total_figures"], PALETTE["amber"]),
    ]
    for i, (label, cnt, tot, col) in enumerate(cov_items):
        cy = ry0 + 60 + i * 42
        pct = (100.0 * cnt / tot) if tot else 0.0
        bx, bw = rx + 20, rw - 40
        cv.text(bx, cy, label, size=12)
        cv.text(bx + bw, cy, f"{cnt}/{tot} ({pct:.1f}%)", size=12, bold=True, anchor="end")
        cv.rect(bx, cy + 7, bw, 12, fill=PALETTE["grid"], rx=4)
        cv.rect(bx, cy + 7, bw * pct / 100.0, 12, fill=col, rx=4)

    # Panel C
    ry1, rh2 = 355, 257
    cv.rect(rx, ry1, rw, rh2, fill=PALETTE["panel"], stroke=PALETTE["border"], rx=8)
    cv.text(rx + 20, ry1 + 32, "C. Licence Distribution", size=15, bold=True)

    lic_counts: Dict[str, int] = summary.get("licence_counts", {})
    top_lics = sorted(lic_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    max_lic = max([v for _, v in top_lics] + [1])
    for i, (lic, cnt) in enumerate(top_lics):
        ly_row = ry1 + 60 + i * 31
        lbl = lic if len(lic) <= 24 else lic[:21] + "..."
        col = PALETTE["stone"] if "nc" not in lic.lower() else PALETTE["plum"]
        cv.text(rx + 20, ly_row + 12, lbl, size=11)
        bx, bw_max = rx + 195, rw - 270
        cv.rect(bx, ly_row, bw_max, 15, fill=PALETTE["grid"], rx=3)
        cv.rect(bx, ly_row, (cnt / max_lic) * bw_max, 15, fill=col, rx=3)
        cv.text(bx + bw_max + 8, ly_row + 12, str(cnt), size=11, bold=True)

    _footer(cv, "Source: helleristninger/scientific_record/figures.jsonl. Scale-calibrated metrics strictly reflect verified archival scale bars.")
    cv.save(out_png)


def plot_morphometric_comparison(
    morph_results: List[Dict[str, Any]],
    raw_groups: Dict[str, Dict[str, List[float]]],
    out_png: Path,
) -> None:
    """Figure 2: Stone Age (veideristninger) vs Bronze Age (jordbruksristninger) boxplot comparison."""
    metrics = [m for m in morph_results if m.get("tested")]
    if not metrics:
        return
    metrics = metrics[:6]
    cols = 3
    rows = math.ceil(len(metrics) / cols)
    w, h = 1280, 155 + rows * 275
    cv = DualCanvas(w, h)
    _header(
        cv,
        "Morphometric Comparison: Stone Age (Veideristninger) vs Bronze Age (Jordbruksristninger)",
        "Boxes: median & IQR; whiskers: 5th-95th pct; Mann-Whitney U with Benjamini-Hochberg FDR (q) & Cliff's delta",
    )

    cv.rect(48, 88, 14, 14, fill=PALETTE["stone"], rx=2)
    cv.text(68, 100, "Stone Age Hunter-Gatherer (veideristninger)", size=12, bold=True)
    cv.rect(390, 88, 14, 14, fill=PALETTE["bronze"], rx=2)
    cv.text(410, 100, "Bronze Age Agrarian (jordbruksristninger)", size=12, bold=True)

    pw, ph = 380, 245
    gap_x, gap_y = 22, 26
    for idx, m in enumerate(metrics):
        r, c = idx // cols, idx % cols
        px = 48 + c * (pw + gap_x)
        py = 120 + r * (ph + gap_y)
        cv.rect(px, py, pw, ph, fill=PALETTE["panel"], stroke=PALETTE["border"], rx=8)

        key = m["metric"]
        stone_vals = np.asarray(raw_groups["stone_age_hunter"].get(key, []), dtype=float)
        bronze_vals = np.asarray(raw_groups["bronze_age_agrarian"].get(key, []), dtype=float)
        all_vals = np.concatenate([stone_vals, bronze_vals]) if (len(stone_vals) and len(bronze_vals)) else np.array([0.0, 1.0])

        lo = float(np.percentile(all_vals, 2))
        hi = float(np.percentile(all_vals, 98))
        if hi <= lo:
            lo, hi = float(np.min(all_vals)), float(np.max(all_vals)) + 1e-6
        span = hi - lo
        lo_pad = max(0.0, lo - 0.05 * span) if lo >= 0 else lo - 0.05 * span
        hi_pad = hi + 0.10 * span

        q_val = m.get("q_value")
        delta = m.get("cliffs_delta")
        stat_str = f"δ={delta:+.2f} · q={q_val:.3g}" if (q_val is not None and delta is not None) else ""
        cv.text(px + 16, py + 26, m["label"], size=13, bold=True)
        cv.text(px + pw - 16, py + 26, stat_str, size=11, bold=True, fill=PALETTE["muted"], anchor="end")

        ax_x, ax_y, ax_w, ax_h = px + 68, py + 46, pw - 90, ph - 84
        for t in range(4):
            frac = t / 3.0
            yv = ax_y + ax_h - frac * ax_h
            val_tick = lo_pad + frac * (hi_pad - lo_pad)
            cv.line(ax_x, yv, ax_x + ax_w, yv, stroke=PALETTE["grid"], stroke_width=1.0)
            val_fmt = f"{val_tick:.0f}" if abs(val_tick) >= 100 else (f"{val_tick:.2f}" if abs(val_tick) < 10 else f"{val_tick:.1f}")
            cv.text(ax_x - 8, yv + 4, val_fmt, size=10, fill=PALETTE["muted"], anchor="end")

        def y_map(v: float) -> float:
            vc = max(lo_pad, min(hi_pad, v))
            return ax_y + ax_h - ((vc - lo_pad) / max(1e-9, hi_pad - lo_pad)) * ax_h

        for g_idx, (vals, color, g_name) in enumerate([
            (stone_vals, PALETTE["stone"], f"Stone (n={len(stone_vals)})"),
            (bronze_vals, PALETTE["bronze"], f"Bronze (n={len(bronze_vals)})"),
        ]):
            cx = ax_x + ax_w * (0.30 if g_idx == 0 else 0.72)
            box_w = 54
            if len(vals) > 0:
                p5, p25, p50, p75, p95 = np.percentile(vals, [5, 25, 50, 75, 95])
                y5, y25, y50, y75, y95 = y_map(p5), y_map(p25), y_map(p50), y_map(p75), y_map(p95)
                cv.line(cx, y95, cx, y5, stroke=color, stroke_width=2.0)
                cv.line(cx - 12, y95, cx + 12, y95, stroke=color, stroke_width=2.0)
                cv.line(cx - 12, y5, cx + 12, y5, stroke=color, stroke_width=2.0)
                box_h = max(2.0, y25 - y75)
                cv.rect(cx - box_w / 2, y75, box_w, box_h, fill=color, fill_opacity=0.28, stroke=color, stroke_width=2.0, rx=3)
                cv.line(cx - box_w / 2, y50, cx + box_w / 2, y50, stroke=PALETTE["ink"], stroke_width=2.5)
            cv.text(cx, ax_y + ax_h + 18, g_name, size=11, anchor="middle")

    _footer(cv, "Note: Physical mm dimensions strictly use scale-calibrated figures; dimensionless shape indices span all valid traced polygons.")
    cv.save(out_png)


def plot_regional_iconography(reg_summary: Dict[str, Any], out_png: Path) -> None:
    """Figure 3: Motif-family proportions + Shannon H'/Pielou J' + Bray-Curtis dissimilarity heatmap."""
    regions: List[str] = reg_summary.get("regions_compared", [])
    if not regions:
        return
    families: List[str] = reg_summary.get("families", [])
    fam_props: Dict[str, Dict[str, float]] = reg_summary.get("family_proportions", {})
    div_stats: Dict[str, Dict[str, Any]] = reg_summary.get("diversity", {})
    bc_mat: List[List[float]] = reg_summary.get("bray_curtis_matrix", [])

    w, h = 1280, 680
    cv = DualCanvas(w, h)
    _header(
        cv,
        "Regional Iconographic Structure & Beta-Diversity Across Scandinavian Carvings",
        "Core & neighbouring regions: motif-family share, Shannon H' / Pielou J', and Bray-Curtis dissimilarity",
    )

    lx, ly, lw, lh = 48, 102, 640, 515
    cv.rect(lx, ly, lw, lh, fill=PALETTE["panel"], stroke=PALETTE["border"], rx=8)
    cv.text(lx + 20, ly + 30, "A. Motif-Family Share (%) & Diversity Indices", size=15, bold=True)

    cell_x0 = lx + 155
    cell_w = min(72, (lw - 275) // max(1, len(families)))
    cell_h = min(58, (lh - 120) // max(1, len(regions)))

    for j, fam in enumerate(families):
        cx = cell_x0 + j * cell_w + cell_w / 2
        lbl = "Scene/Uncl." if fam == "scene_or_unclassified" else fam.replace("_", " ").title()[:10]
        cv.text(cx, ly + 66, lbl, size=11, bold=True, anchor="middle")
    cv.text(cell_x0 + len(families) * cell_w + 55, ly + 66, "H' / J' (N)", size=11, bold=True, anchor="middle")

    for i, reg in enumerate(regions):
        ry = ly + 82 + i * cell_h
        cv.text(cell_x0 - 10, ry + cell_h * 0.58, reg, size=12, bold=True, anchor="end")
        for j, fam in enumerate(families):
            p = fam_props.get(reg, {}).get(fam, 0.0)
            cx = cell_x0 + j * cell_w
            alpha = min(1.0, max(0.08, p * 1.15)) if p > 0 else 0.35
            fill = PALETTE["stone"] if p > 0 else PALETTE["grid"]
            tcol = "#FFFFFF" if p >= 0.45 else PALETTE["ink"]
            cv.rect(cx + 2, ry + 2, cell_w - 4, cell_h - 6, fill=fill, fill_opacity=alpha, rx=4)
            cv.text(cx + cell_w / 2, ry + cell_h * 0.58, f"{p * 100:.1f}%", size=11, bold=True, fill=tcol, anchor="middle")

        ds = div_stats.get(reg, {})
        h_val, j_val, n_val = ds.get("shannon_h"), ds.get("pielou_j"), ds.get("n", 0)
        h_str = f"{h_val:.2f}" if h_val is not None else "n/a"
        j_str = f"{j_val:.2f}" if j_val is not None else "n/a"
        cv.text(cell_x0 + len(families) * cell_w + 55, ry + cell_h * 0.58, f"{h_str} / {j_str} ({n_val})", size=11, anchor="middle")

    rx, ry0, rw, rh = 710, 102, 522, 515
    cv.rect(rx, ry0, rw, rh, fill=PALETTE["panel"], stroke=PALETTE["border"], rx=8)
    cv.text(rx + 20, ry0 + 30, "B. Pairwise Bray-Curtis Dissimilarity", size=15, bold=True)

    n_r = len(regions)
    mx0, my0 = rx + 135, ry0 + 92
    side = min(58, (rw - 165) // max(1, n_r))

    for j, reg in enumerate(regions):
        cx = mx0 + j * side + side / 2
        cv.text(cx, my0 - 12, reg.split("/")[0][:10], size=10, bold=True, anchor="middle")

    for i, reg in enumerate(regions):
        cy = my0 + i * side
        cv.text(mx0 - 10, cy + side * 0.58, reg.split("/")[0], size=11, bold=True, anchor="end")
        for j in range(n_r):
            val = bc_mat[i][j] if i < len(bc_mat) and j < len(bc_mat[i]) else 0.0
            cx = mx0 + j * side
            fill = PALETTE["grid"] if i == j else PALETTE["bronze"]
            alpha = 0.45 if i == j else min(0.95, max(0.08, val))
            tcol = "#FFFFFF" if (i != j and val >= 0.55) else PALETTE["ink"]
            cv.rect(cx + 2, cy + 2, side - 4, side - 4, fill=fill, fill_opacity=alpha, rx=4)
            cv.text(cx + side / 2, cy + side * 0.58, f"{val:.2f}", size=11, bold=True, fill=tcol, anchor="middle")

    _footer(cv, "Indices computed over motif_class abundances: H' = -sum(p ln p), J' = H'/ln(S). Bray-Curtis: 0 = identical, 1 = disjoint.")
    cv.save(out_png)


def plot_dual_trace_reliability(qa_summary: Dict[str, Any], out_png: Path) -> None:
    """Figure 4: Dual-trace measurement reliability (IoU by archive, motif family, complexity, period)."""
    w, h = 1280, 680
    cv = DualCanvas(w, h)
    overall = qa_summary.get("overall", {})
    _header(
        cv,
        "Dual-Trace Measurement Reliability — Independent Algorithm A vs Algorithm B",
        f"Stratified QA n = {overall.get('n', 0)} ({overall.get('coverage_pct', 0.0):.1f}% of corpus) · Mean IoU = {(overall.get('mean_iou') or 0.0):.3f} · Median IoU = {(overall.get('median_iou') or 0.0):.3f}",
    )

    strata_panels = [
        ("A. IoU by Source Archive", qa_summary.get("by_archive", {}), 48, 102, PALETTE["stone"]),
        ("B. IoU by Motif Family", qa_summary.get("by_family", {}), 652, 102, PALETTE["bronze"]),
        ("C. IoU by Geometric Complexity", qa_summary.get("by_complexity", {}), 48, 372, PALETTE["sage"]),
        ("D. IoU by Chronological Period", qa_summary.get("by_period", {}), 652, 372, PALETTE["amber"]),
    ]

    pw, ph = 580, 248
    for title, groups, px, py, col in strata_panels:
        cv.rect(px, py, pw, ph, fill=PALETTE["panel"], stroke=PALETTE["border"], rx=8)
        cv.text(px + 18, py + 28, title, size=14, bold=True)

        items = list(groups.items())[:6]
        ax_x0, ax_w = px + 160, pw - 238
        row_h = min(30, (ph - 62) // max(1, len(items)))

        for gval in (0.0, 0.5, 0.8, 1.0):
            gx = ax_x0 + gval * ax_w
            cv.line(gx, py + 42, gx, py + ph - 26, stroke=PALETTE["muted"] if gval == 0.8 else PALETTE["grid"], dashed=(gval == 0.8))
            cv.text(gx, py + ph - 10, f"{gval:.1f}", size=10, fill=PALETTE["muted"], anchor="middle")

        for i, (k, st) in enumerate(items):
            ry = py + 48 + i * row_h
            lbl = k if len(k) <= 20 else k[:17] + "..."
            cv.text(ax_x0 - 8, ry + 14, lbl, size=11, anchor="end")

            mean_iou = float(st.get("mean_iou") or 0.0)
            q25 = float(st.get("q25_iou") if st.get("q25_iou") is not None else mean_iou)
            q75 = float(st.get("q75_iou") if st.get("q75_iou") is not None else mean_iou)
            med_iou = float(st.get("median_iou") if st.get("median_iou") is not None else mean_iou)
            n_st = int(st.get("n", 0))

            x_q25 = ax_x0 + max(0.0, min(1.0, q25)) * ax_w
            x_q75 = ax_x0 + max(0.0, min(1.0, q75)) * ax_w
            x_med = ax_x0 + max(0.0, min(1.0, med_iou)) * ax_w
            x_mean = ax_x0 + max(0.0, min(1.0, mean_iou)) * ax_w

            cv.rect(ax_x0, ry + 3, ax_w, 14, fill=PALETTE["grid"], fill_opacity=0.45, rx=2)
            cv.rect(ax_x0, ry + 4, max(2.0, x_mean - ax_x0), 12, fill=col, fill_opacity=0.35, rx=2)
            cv.line(x_q25, ry + 10, x_q75, ry + 10, stroke=col, stroke_width=2.5)
            cv.circle(x_med, ry + 10, 4.0, fill=PALETTE["ink"])
            cv.text(ax_x0 + ax_w + 8, ry + 14, f"{med_iou:.3f} (n={n_st})", size=10, bold=True)

    _footer(cv, "Bars = mean IoU; whiskers = IQR (25th-75th pct); dark dots = median IoU; dashed line = 0.80 threshold.")
    cv.save(out_png)
