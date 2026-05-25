"""KT-Buddy Phase 2 — produce a high-level PDF report for a Java repo.

Sections:
  1. Cover with stats (files / classes / packages)
  2. Claude-generated narrative overview
  3. UML class diagram (PlantUML rendered via plantuml.com, source-text fallback)
  4. Packages and classes (compact: kind + name + extends/implements)

Usage:
    python pdf_generator.py <local-path-or-existing-repo-name>
"""

import html
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from java_parser import parse_java_source
from kt_buddy import OUTPUT_DIR, find_java_files
from llm import LLMError, call_claude


OVERVIEW_PROMPT_PATH = Path(__file__).parent / "prompts" / "repo_overview.txt"
DESIGN_PROMPT_PATH = Path(__file__).parent / "prompts" / "repo_design.txt"
DESIGN_MAX_INPUT_CHARS = 100_000


# ---------- Java metadata extraction (regex-based; tree-sitter comes in Phase 4) ----------


@dataclass
class JavaClass:
    name: str
    kind: str
    package: str
    extends: str | None = None
    implements: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    file_path: Path | None = None


def parse_java_file(path: Path) -> list[JavaClass]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    parsed = parse_java_source(text)
    return [
        JavaClass(
            name=pc.name,
            kind=pc.kind,
            package=parsed.package,
            extends=pc.extends,
            implements=list(pc.implements),
            methods=[m.signature() for m in pc.methods],
            file_path=path,
        )
        for pc in parsed.classes
    ]


def extract_metadata(source_dir: Path) -> dict:
    java_files = list(find_java_files(source_dir))
    all_classes: list[JavaClass] = []
    for jf in java_files:
        all_classes.extend(parse_java_file(jf))

    packages: dict[str, list[JavaClass]] = {}
    for c in all_classes:
        packages.setdefault(c.package, []).append(c)

    return {
        "name": source_dir.name,
        "file_count": len(java_files),
        "class_count": len(all_classes),
        "package_count": len(packages),
        "packages": packages,
    }


# ---------- Overview generation via claude -p ----------


def generate_overview(metadata: dict) -> str:
    if metadata["class_count"] == 0:
        return "(No Java classes were discovered in this repository.)"

    pkg_summary_blocks = []
    for pkg, classes in metadata["packages"].items():
        lines = [f"Package {pkg}:"]
        for c in classes:
            preview = ", ".join(c.methods[:6])
            if len(c.methods) > 6:
                preview += f", ... (+{len(c.methods) - 6} more)"
            lines.append(f"  {c.kind} {c.name}: [{preview}]")
        pkg_summary_blocks.append("\n".join(lines))

    system_prompt = OVERVIEW_PROMPT_PATH.read_text(encoding="utf-8")
    user_content = (
        f"Repository: {metadata['name']}\n"
        f"Java files: {metadata['file_count']} | classes: {metadata['class_count']} | packages: {metadata['package_count']}\n\n"
        "Structure:\n"
        + "\n\n".join(pkg_summary_blocks)
    )

    try:
        return call_claude(system=system_prompt, user=user_content).strip()
    except LLMError as e:
        return f"(Overview generation failed: {e})"


# ---------- Design (HLD) generation via claude -p ----------


def _parse_design_json(text: str) -> dict | None:
    """Strip optional fences and extract a JSON object from claude's output."""
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()[1:]
        while lines and lines[-1].strip() == "```":
            lines.pop()
        s = "\n".join(lines)
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def generate_design(source_dir: Path, metadata: dict) -> dict | None:
    """Call claude -p with all Java source and parse a JSON HLD description."""
    if metadata["class_count"] == 0:
        return None

    bundle_parts = []
    for jf in find_java_files(source_dir):
        try:
            rel = jf.relative_to(source_dir)
            bundle_parts.append(f'<file path="{rel}">\n{jf.read_text(encoding="utf-8", errors="replace")}\n</file>')
        except OSError:
            continue
    bundle = "\n\n".join(bundle_parts)
    if len(bundle) > DESIGN_MAX_INPUT_CHARS:
        bundle = bundle[:DESIGN_MAX_INPUT_CHARS] + "\n\n[... truncated due to size ...]"

    system_prompt = DESIGN_PROMPT_PATH.read_text(encoding="utf-8")

    try:
        raw = call_claude(system=system_prompt, user=bundle)
    except LLMError:
        return None

    design = _parse_design_json(raw)
    if not design or not isinstance(design.get("components"), list) or not design["components"]:
        return None
    return design


# ---------- Native UML rendering (reportlab shapes; works offline) ----------


_BOX_W = 118
_BOX_H = 30
_PKG_PAD = 14
_PKG_LABEL_H = 18
_CLASS_GAP = 10
_PKG_GAP = 26


def _arrow_polygon(start: tuple[float, float], end: tuple[float, float], size: float = 8.0) -> list[float]:
    """Return points for a filled triangle arrowhead at `end`, base perpendicular to start->end."""
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    bx, by = ex - ux * size, ey - uy * size
    perpx, perpy = -uy, ux
    half = size * 0.55
    return [
        ex, ey,
        bx + perpx * half, by + perpy * half,
        bx - perpx * half, by - perpy * half,
    ]


# Component kind -> (fill, stroke) for the HLD diagram. Colors echo the indigo theme.
_KIND_COLORS: dict[str, tuple] = {
    "entry":      (HexColor("#EEF2FF"), HexColor("#6366F1")),
    "ui":         (HexColor("#EEF2FF"), HexColor("#6366F1")),
    "controller": (HexColor("#ECFEFF"), HexColor("#06B6D4")),
    "service":    (HexColor("#F0FDF4"), HexColor("#10B981")),
    "repository": (HexColor("#FEFCE8"), HexColor("#CA8A04")),
    "model":      (HexColor("#FAF5FF"), HexColor("#8B5CF6")),
    "external":   (HexColor("#FEF2F2"), HexColor("#EF4444")),
    "util":       (HexColor("#F5F6FA"), HexColor("#64748B")),
}
_KIND_DEFAULT = (HexColor("#FFFFFF"), HexColor("#0F172A"))


def build_design_diagram(design: dict, max_width: float) -> Drawing:
    """HLD/flow diagram: components grouped by layer, flows drawn as labeled arrows.
    Layer 0 sits at the top; deeper layers stack below it."""
    BOX_W, BOX_H = 140, 54
    LAYER_GAP_Y = 60
    BOX_GAP_X = 22
    LABEL_PAD = 14

    components = design.get("components", [])
    flows = design.get("flows", [])

    layers: dict[int, list[dict]] = {}
    for c in components:
        try:
            layer = int(c.get("layer", 0))
        except (TypeError, ValueError):
            layer = 0
        layers.setdefault(layer, []).append(c)
    sorted_layers = sorted(layers.keys())

    max_per_layer = max(len(layers[l]) for l in sorted_layers)
    total_w = max(max_width, max_per_layer * (BOX_W + BOX_GAP_X) - BOX_GAP_X)
    layer_count = len(sorted_layers)
    total_h = layer_count * BOX_H + (layer_count - 1) * LAYER_GAP_Y + 2 * LABEL_PAD

    d = Drawing(total_w, total_h)
    positions: dict[str, dict] = {}

    for i, layer_num in enumerate(sorted_layers):
        comps = layers[layer_num]
        row_w = len(comps) * BOX_W + (len(comps) - 1) * BOX_GAP_X
        x_start = (total_w - row_w) / 2
        y = total_h - LABEL_PAD - (i + 1) * BOX_H - i * LAYER_GAP_Y

        for j, c in enumerate(comps):
            x = x_start + j * (BOX_W + BOX_GAP_X)
            fill, stroke = _KIND_COLORS.get(str(c.get("kind", "")), _KIND_DEFAULT)
            d.add(Rect(x, y, BOX_W, BOX_H,
                       fillColor=fill, strokeColor=stroke, strokeWidth=1.4,
                       rx=8, ry=8))
            label = str(c.get("label") or c.get("id") or "?")
            lines = label.split("\\n") if "\\n" in label else label.split("\n")
            lines = lines[:2]
            cx = x + BOX_W / 2
            if len(lines) == 1:
                d.add(String(cx, y + BOX_H / 2 - 4, lines[0][:28],
                             fontName="Helvetica-Bold", fontSize=9,
                             fillColor=TEXT, textAnchor="middle"))
            else:
                d.add(String(cx, y + BOX_H - 18, lines[0][:28],
                             fontName="Helvetica-Bold", fontSize=9,
                             fillColor=TEXT, textAnchor="middle"))
                d.add(String(cx, y + BOX_H - 32, lines[1][:30],
                             fontName="Helvetica", fontSize=7.5,
                             fillColor=MUTED, textAnchor="middle"))
            cid = c.get("id")
            if cid:
                positions[cid] = {
                    "x": x, "y": y, "w": BOX_W, "h": BOX_H,
                    "cx": cx, "cy": y + BOX_H / 2,
                    "top": y + BOX_H, "bot": y,
                    "left": x, "right": x + BOX_W,
                }

    for f in flows:
        src_id = f.get("from")
        dst_id = f.get("to")
        if src_id not in positions or dst_id not in positions or src_id == dst_id:
            continue
        src = positions[src_id]
        dst = positions[dst_id]

        if dst["cy"] < src["cy"] - 5:
            start = (src["cx"], src["bot"])
            end = (dst["cx"], dst["top"])
        elif dst["cy"] > src["cy"] + 5:
            start = (src["cx"], src["top"])
            end = (dst["cx"], dst["bot"])
        elif dst["cx"] > src["cx"]:
            start = (src["right"], src["cy"])
            end = (dst["left"], dst["cy"])
        else:
            start = (src["left"], src["cy"])
            end = (dst["right"], dst["cy"])

        d.add(Line(start[0], start[1], end[0], end[1],
                   strokeColor=HexColor("#64748B"), strokeWidth=1.2))
        d.add(Polygon(_arrow_polygon(start, end),
                      fillColor=HexColor("#64748B"),
                      strokeColor=HexColor("#64748B"),
                      strokeWidth=1))

        label = f.get("label")
        if label:
            label = str(label)[:24]
            mid_x = (start[0] + end[0]) / 2
            mid_y = (start[1] + end[1]) / 2
            text_w = len(label) * 4.2 + 8
            d.add(Rect(mid_x - text_w / 2, mid_y - 6, text_w, 12,
                       fillColor=HexColor("#FFFFFF"),
                       strokeColor=HexColor("#E5E7EB"),
                       strokeWidth=0.5, rx=2, ry=2))
            d.add(String(mid_x, mid_y - 3, label,
                         fontName="Helvetica", fontSize=7,
                         fillColor=MUTED, textAnchor="middle"))

    return d


def build_class_diagram(metadata: dict, max_width: float) -> Drawing:
    """Class diagram: classes grouped in package panels; arrows for inheritance/implementation.
    Layout is row-packed within max_width so diagrams scale across repo sizes."""
    packages = list(metadata["packages"].items())
    if not packages:
        d = Drawing(max_width, 40)
        d.add(String(max_width / 2, 18, "(No classes parsed from this repository.)",
                     fontName="Helvetica", fontSize=10,
                     fillColor=MUTED, textAnchor="middle"))
        return d

    pkg_dims = []
    for pkg, classes in packages:
        pkg_w = _BOX_W + 2 * _PKG_PAD
        pkg_h = _PKG_LABEL_H + len(classes) * (_BOX_H + _CLASS_GAP) - _CLASS_GAP + 2 * _PKG_PAD
        pkg_dims.append({"pkg": pkg, "classes": classes, "w": pkg_w, "h": pkg_h})

    # Greedy row packing within max_width
    rows: list[list[dict]] = []
    current_row: list[dict] = []
    current_w = 0.0
    for pd in pkg_dims:
        next_w = current_w + (_PKG_GAP if current_row else 0) + pd["w"]
        if current_row and next_w > max_width:
            rows.append(current_row)
            current_row = [pd]
            current_w = pd["w"]
        else:
            current_row.append(pd)
            current_w = next_w
    if current_row:
        rows.append(current_row)

    row_heights = [max(p["h"] for p in row) for row in rows]
    total_h = sum(row_heights) + (len(rows) - 1) * _PKG_GAP
    total_w = max(sum(p["w"] for p in row) + (len(row) - 1) * _PKG_GAP for row in rows)

    d = Drawing(total_w, total_h)
    boxes: dict[str, dict] = {}  # name -> rect bounds

    y_top = total_h
    for row, row_h in zip(rows, row_heights):
        x_cursor = 0.0
        for pd in row:
            pkg_top = y_top
            pkg_bot = y_top - pd["h"]
            d.add(Rect(x_cursor, pkg_bot, pd["w"], pd["h"],
                       fillColor=SURFACE, strokeColor=HexColor("#CBD5E1"),
                       strokeWidth=0.8, rx=8, ry=8))
            d.add(String(x_cursor + _PKG_PAD, pkg_top - _PKG_PAD - 8, pd["pkg"],
                         fontName="Helvetica-Bold", fontSize=8.5,
                         fillColor=PRIMARY))

            cls_top = pkg_top - _PKG_PAD - _PKG_LABEL_H
            for c in pd["classes"]:
                bx = x_cursor + _PKG_PAD
                by = cls_top - _BOX_H
                d.add(Rect(bx, by, _BOX_W, _BOX_H,
                           fillColor=HexColor("#FFFFFF"),
                           strokeColor=PRIMARY, strokeWidth=1.1,
                           rx=4, ry=4))
                if c.kind == "class":
                    label = c.name
                else:
                    label = f"<<{c.kind}>> {c.name}"
                d.add(String(bx + _BOX_W / 2, by + _BOX_H / 2 - 3, label,
                             fontName="Helvetica-Bold", fontSize=8.5,
                             fillColor=TEXT, textAnchor="middle"))
                boxes[c.name] = {"x": bx, "y": by, "w": _BOX_W, "h": _BOX_H}
                cls_top = by - _CLASS_GAP

            x_cursor += pd["w"] + _PKG_GAP
        y_top -= row_h + _PKG_GAP

    # Relationship arrows: parent at the arrowhead, child at the tail
    for classes in metadata["packages"].values():
        for c in classes:
            if c.name not in boxes:
                continue
            child = boxes[c.name]
            edges: list[tuple[str, bool]] = []
            if c.extends:
                edges.append((c.extends, False))
            edges.extend((impl, True) for impl in c.implements)

            for relation, dashed in edges:
                parent_name = relation.split("<")[0].strip().split(".")[-1]
                if parent_name not in boxes or parent_name == c.name:
                    continue
                parent = boxes[parent_name]
                # Connect closest vertical edges (top-of-lower to bottom-of-upper)
                if parent["y"] > child["y"]:
                    start = (child["x"] + child["w"] / 2, child["y"] + child["h"])
                    end = (parent["x"] + parent["w"] / 2, parent["y"])
                else:
                    start = (child["x"] + child["w"] / 2, child["y"])
                    end = (parent["x"] + parent["w"] / 2, parent["y"] + parent["h"])

                line_kwargs = {"strokeColor": HexColor("#64748B"), "strokeWidth": 1}
                if dashed:
                    line_kwargs["strokeDashArray"] = [3, 2]
                d.add(Line(start[0], start[1], end[0], end[1], **line_kwargs))
                d.add(Polygon(_arrow_polygon(start, end),
                              fillColor=HexColor("#FFFFFF"),
                              strokeColor=HexColor("#64748B"),
                              strokeWidth=1))

    return d


# ---------- PDF build ----------


PRIMARY = HexColor("#6366F1")
TEXT = HexColor("#0F172A")
MUTED = HexColor("#64748B")
BORDER = HexColor("#E5E7EB")
SURFACE = HexColor("#F5F6FA")


def _styles():
    s = getSampleStyleSheet()
    s.add(ParagraphStyle("KTBTitle", fontName="Helvetica-Bold", fontSize=28, leading=34,
                         textColor=PRIMARY, alignment=TA_LEFT, spaceAfter=8))
    s.add(ParagraphStyle("KTBSub", fontName="Helvetica", fontSize=11, leading=15,
                         textColor=MUTED, spaceAfter=20))
    s.add(ParagraphStyle("KTBH1", fontName="Helvetica-Bold", fontSize=18, leading=22,
                         textColor=TEXT, spaceBefore=14, spaceAfter=10))
    s.add(ParagraphStyle("KTBH2", fontName="Helvetica-Bold", fontSize=12, leading=16,
                         textColor=PRIMARY, spaceBefore=12, spaceAfter=6))
    s.add(ParagraphStyle("KTBBody", fontName="Helvetica", fontSize=10, leading=14,
                         textColor=TEXT, spaceAfter=8))
    s.add(ParagraphStyle("KTBClass", fontName="Helvetica", fontSize=10, leading=14,
                         textColor=TEXT, leftIndent=12, spaceAfter=3))
    s.add(ParagraphStyle("KTBBullet", fontName="Helvetica", fontSize=10, leading=14,
                         textColor=TEXT, leftIndent=16, bulletIndent=4,
                         spaceAfter=4))
    s.add(ParagraphStyle("KTBMono", fontName="Courier", fontSize=7.5, leading=10,
                         textColor=TEXT))
    return s


def _render_overview(flow: list, overview: str, styles) -> None:
    """Parse Claude's structured overview (## headers + - bullets) into PDF flowables."""
    lines = overview.splitlines()
    pending_para: list[str] = []

    def flush_para():
        if pending_para:
            text = " ".join(pending_para).strip()
            if text:
                flow.append(Paragraph(_esc(text), styles["KTBBody"]))
            pending_para.clear()

    for raw in lines:
        line = raw.rstrip()
        stripped = line.lstrip()
        if stripped.startswith("## "):
            flush_para()
            flow.append(Paragraph(_esc(stripped[3:].strip()), styles["KTBH2"]))
        elif stripped.startswith("- "):
            flush_para()
            flow.append(Paragraph(_esc(stripped[2:].strip()), styles["KTBBullet"],
                                  bulletText="•"))
        elif not stripped:
            flush_para()
        else:
            pending_para.append(stripped)
    flush_para()


def _esc(s: str | None) -> str:
    return html.escape(s) if s else ""


def build_pdf(metadata: dict, overview: str, design: dict | None, out_path: Path) -> None:
    styles = _styles()
    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"KT-Buddy — {metadata['name']}",
        author="KT-Buddy",
    )

    flow = []

    # Cover
    flow.append(Paragraph("KT-Buddy Report", styles["KTBTitle"]))
    flow.append(Paragraph(
        f"<b>{_esc(metadata['name'])}</b> &middot; generated {date.today().isoformat()}",
        styles["KTBSub"],
    ))

    stats = [
        ["Java files", str(metadata["file_count"])],
        ["Classes / interfaces / enums", str(metadata["class_count"])],
        ["Packages", str(metadata["package_count"])],
    ]
    tbl = Table(stats, colWidths=[8 * cm, 4 * cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), SURFACE),
        ("TEXTCOLOR", (0, 0), (-1, -1), TEXT),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    flow.append(tbl)
    flow.append(Spacer(1, 0.6 * cm))

    # Overview (structured bullets)
    flow.append(Paragraph("Overview", styles["KTBH1"]))
    _render_overview(flow, overview, styles)

    # Architecture / Flow diagram (HLD from claude); falls back to class diagram if design unavailable
    flow.append(PageBreak())
    page_width = A4[0] - 4 * cm
    page_height = A4[1] - 6 * cm
    if design:
        title = design.get("title") or "Architecture &amp; Flow"
        flow.append(Paragraph(_esc(str(title)), styles["KTBH1"]))
        diagram = build_design_diagram(design, max_width=page_width)
    else:
        flow.append(Paragraph("UML Class Diagram", styles["KTBH1"]))
        diagram = build_class_diagram(metadata, max_width=page_width)
    scale = min(1.0, page_width / max(diagram.width, 1), page_height / max(diagram.height, 1))
    if scale < 1.0:
        diagram.scale(scale, scale)
        diagram.width *= scale
        diagram.height *= scale
    flow.append(diagram)

    # Packages & classes (compact)
    flow.append(PageBreak())
    flow.append(Paragraph("Packages &amp; Classes", styles["KTBH1"]))
    if metadata["class_count"] == 0:
        flow.append(Paragraph("No classes were parsed from this repository.", styles["KTBBody"]))
    else:
        for pkg in sorted(metadata["packages"].keys()):
            classes = metadata["packages"][pkg]
            flow.append(Paragraph(_esc(pkg), styles["KTBH2"]))
            for c in sorted(classes, key=lambda x: x.name):
                line = f'<b>{_esc(c.kind)}</b> {_esc(c.name)}'
                if c.extends:
                    line += f' <i>extends</i> {_esc(c.extends)}'
                if c.implements:
                    line += f' <i>implements</i> {_esc(", ".join(c.implements))}'
                flow.append(Paragraph(line, styles["KTBClass"]))
            flow.append(Spacer(1, 0.25 * cm))

    doc.build(flow)


# ---------- Orchestration ----------


def generate_report(source_dir: Path, name: str, output_root: Path = OUTPUT_DIR) -> Path:
    metadata = extract_metadata(source_dir)
    overview = generate_overview(metadata)
    design = generate_design(source_dir, metadata)

    out_dir = output_root / name
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{name}-kt-report.pdf"
    build_pdf(metadata, overview, design, pdf_path)
    return pdf_path


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python pdf_generator.py <local-path-or-existing-repo-name>")
        sys.exit(1)

    arg = sys.argv[1]
    p = Path(arg)
    if p.is_dir():
        src = p.resolve()
        name = src.name
    else:
        candidate = Path("repos") / arg
        if candidate.is_dir():
            src = candidate.resolve()
            name = arg
        else:
            print(f"Not a directory and no clone found at repos/{arg}")
            sys.exit(1)

    print(f"Generating PDF for {src}")
    out = generate_report(src, name)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
