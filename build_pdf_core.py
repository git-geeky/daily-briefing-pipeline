"""
Convert the news report markdown to a professionally styled PDF.
Uses reportlab Platypus for paginated, multi-style document generation.

Usage:
    python build_pdf.py                    # uses today's date in JST
    python build_pdf.py --date 2026-04-07  # explicit date
    BRIEFING_DATE=2026-04-07 python build_pdf.py  # via env var

Reads:  the assembled markdown path from briefing_paths.py
Writes: the PDF path from briefing_paths.py
"""
import argparse
import hashlib
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import briefing_paths as briefing_paths_module
from briefing_manifest import RASTER_IMAGE_EXTENSIONS as RASTER_IMAGE_EXTENSIONS_DOTTED
from run_reporting import set_step_timing, update_run_report
from validate_briefing import validate_briefing
try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    PIL_LANCZOS = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
except Exception:
    Image = None
    PIL_LANCZOS = None

# Ensure stdout/stderr can emit any Unicode character. The default Windows
# console is cp1252, which crashes f-strings containing characters like
# "→" (the image-progress line) or any CJK font name we log. reconfigure()
# is idempotent when stdout is already UTF-8. errors='replace' guarantees
# that even a truly non-encodable target (e.g. stdout captured by a non-UTF-8
# parent) degrades to "?" rather than killing the build mid-way.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch, mm
from reportlab.lib.colors import HexColor, black, toColor, white
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.lib.utils import ImageReader
from reportlab.graphics.shapes import (
    Circle,
    Drawing,
    Ellipse,
    Group,
    Line,
    Path as RLPath,
    Polygon,
    PolyLine,
    Rect,
    String,
)
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, HRFlowable,
    Table, TableStyle, KeepTogether, Image as RLImage
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

# ── CJK font registration ────────────────────────────────
# reportlab's built-in Helvetica only covers WinAnsi (Latin) glyphs; any CJK
# codepoint renders as .notdef — the filled rectangle ("tofu") the user saw.
# We register a Japanese-capable font and wrap CJK runs via <font> tags in
# md_inline() so Helvetica stays for the Latin body and only the CJK runs
# swap to the Japanese face. Primary is Yu Gothic (Windows-bundled TTC);
# fallback is HeiseiKakuGo-W5 (reportlab built-in CID font, no file path).
JP_FONT = None
try:
    pdfmetrics.registerFont(
        TTFont('YuGothic', r'C:\Windows\Fonts\YuGothR.ttc', subfontIndex=0))
    pdfmetrics.registerFont(
        TTFont('YuGothic-Bold', r'C:\Windows\Fonts\YuGothB.ttc', subfontIndex=0))
    registerFontFamily('YuGothic',
                       normal='YuGothic', bold='YuGothic-Bold',
                       italic='YuGothic', boldItalic='YuGothic-Bold')
    JP_FONT = 'YuGothic'
    print("CJK font: Yu Gothic registered")
except Exception as e:
    print(f"  ! Yu Gothic registration failed ({type(e).__name__}: {e}); "
          f"falling back to HeiseiKakuGo-W5")
    try:
        pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
        JP_FONT = 'HeiseiKakuGo-W5'
        print("CJK font: HeiseiKakuGo-W5 registered (fallback)")
    except Exception as e2:
        print(f"  ! HeiseiKakuGo-W5 also failed ({type(e2).__name__}: {e2}); "
              f"CJK text will render as tofu")

# Runs of CJK characters are wrapped in <font name="{JP_FONT}">...</font>.
# Blocks covered: CJK punctuation, hiragana, katakana, CJK ideographs (+ ext A),
# and halfwidth/fullwidth forms. Adjust if you need Hangul or extension B-F.
CJK_RE = re.compile(
    r'([\u3000-\u303f\u3040-\u309f\u30a0-\u30ff'
    r'\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]+)')
XML_TAG_RE = re.compile(r'(<[^>]+>)')
SVG_LENGTH_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)(?:px|pt)?\s*$", re.IGNORECASE)
SVG_POINTS_RE = re.compile(r"-?\d+(?:\.\d+)?")
SVG_PATH_TOKEN_RE = re.compile(r"[MmLlHhVvZz]|-?\d+(?:\.\d+)?")
SVG_STYLE_KEYS = (
    "fill",
    "stroke",
    "stroke-width",
    "fill-opacity",
    "stroke-opacity",
    "opacity",
    "font-size",
    "font-family",
    "text-anchor",
)
# Dotless raster extensions, derived from the canonical dotted set in
# briefing_manifest (single source). Dotless because download_image() matches a
# URL tail extension that has already had its leading "." stripped.
RASTER_IMAGE_EXTENSIONS = tuple(ext.lstrip(".") for ext in RASTER_IMAGE_EXTENSIONS_DOTTED)
SUPPORTED_IMAGE_EXTENSIONS = RASTER_IMAGE_EXTENSIONS + ("svg",)
PDF_IMAGE_MAX_EDGE = max(900, int(os.environ.get("BRIEFING_PDF_IMAGE_MAX_EDGE", "1400")))
PDF_IMAGE_JPEG_QUALITY = min(95, max(40, int(os.environ.get("BRIEFING_PDF_IMAGE_JPEG_QUALITY", "82"))))


# ── Date / Paths (resolved at runtime) ───────────────────
JST = timezone(timedelta(hours=9))


def resolve_date() -> str:
    """Pick the briefing date from --date arg, BRIEFING_DATE env var, or today (JST)."""
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--date", help="Briefing date YYYY-MM-DD (default: today JST)")
    args, _ = parser.parse_known_args()
    if args.date:
        return args.date
    if os.environ.get("BRIEFING_DATE"):
        return os.environ["BRIEFING_DATE"]
    return datetime.now(JST).strftime("%Y-%m-%d")


BRIEFING_DATE = resolve_date()
_dt = datetime.strptime(BRIEFING_DATE, "%Y-%m-%d")
DATE_HUMAN = _dt.strftime("%B %#d, %Y") if os.name == "nt" else _dt.strftime("%B %-d, %Y")
def _optional_text_attr(name: str, default: str) -> str:
    func = getattr(briefing_paths_module, name, None)
    if callable(func):
        return func()
    return default


BRIEFING_LABEL = _optional_text_attr("briefing_label", "The Daily Intelligence Briefing")
BRIEFING_AUTHOR = _optional_text_attr("briefing_author", "Intelligence Report")
PREPARED_BY_LINE = _optional_text_attr("prepared_by_line", "Prepared by Claude")
briefing_paths_module.ensure_layout()
MD_PATH = briefing_paths_module.assembled_markdown_path(BRIEFING_DATE)
PDF_PATH = briefing_paths_module.pdf_path(BRIEFING_DATE)
IMG_CACHE = briefing_paths_module.image_cache_dir(BRIEFING_DATE)
IMG_CACHE.mkdir(parents=True, exist_ok=True)


# ── Image handling ───────────────────────────────────────
def download_image(url_or_path: str):
    """Download an image to the per-date cache directory, or resolve a local path.
    Returns local Path or None on failure. Cached by URL hash so re-runs are cheap."""
    # Local file path support (from fetch_briefing_images.py pre-processing)
    local = Path(url_or_path)
    if local.exists() and (local.suffix.lower() == ".svg" or local.stat().st_size >= 200):
        return local
    # HTTP(S) URL download
    if not url_or_path.startswith(('http://', 'https://')):
        return None
    try:
        clean = url_or_path.split('?')[0].split('#')[0]
        ext = clean.rsplit('.', 1)[-1].lower() if '.' in clean.rsplit('/', 1)[-1] else 'jpg'
        if ext not in SUPPORTED_IMAGE_EXTENSIONS:
            ext = 'jpg'
        h = hashlib.md5(url_or_path.encode()).hexdigest()[:16]
        svg_path = IMG_CACHE / f"{h}.svg"
        if svg_path.exists() and svg_path.stat().st_size > 0:
            return svg_path
        path = IMG_CACHE / f"{h}.{ext}"
        if path.exists() and path.stat().st_size > 0:
            return path
        req = urllib.request.Request(url_or_path, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) DailyBriefing/1.0',
            'Accept': 'image/avif,image/webp,image/png,image/*,*/*;q=0.8',
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "").lower()
        is_svg = ext == "svg" or "svg" in ctype or looks_like_svg_bytes(data)
        if not is_svg and len(data) < 200:  # too small to be a real raster image
            return None
        if is_svg:
            path = svg_path
        path.write_bytes(data)
        return path
    except Exception as e:
        print(f"  ! image download failed: {url_or_path[:80]} ({type(e).__name__}: {e})")
        return None


def looks_like_svg_bytes(data: bytes) -> bool:
    return b"<svg" in data[:512].lower()


def image_has_alpha(img) -> bool:
    bands = getattr(img, "getbands", lambda: ())()
    if "A" in bands:
        return True
    return img.mode == "P" and "transparency" in getattr(img, "info", {})


def optimize_raster_for_pdf(path: Path) -> Path:
    """Create a smaller raster copy for PDF embedding so email delivery stays under size limits."""
    if Image is None or path.suffix.lower() not in RASTER_IMAGE_EXTENSIONS_DOTTED:
        return path
    try:
        with Image.open(path) as src:
            src.load()
            img = src.copy()
        if max(img.size) > PDF_IMAGE_MAX_EDGE:
            img.thumbnail((PDF_IMAGE_MAX_EDGE, PDF_IMAGE_MAX_EDGE), PIL_LANCZOS)
        has_alpha = image_has_alpha(img)
        out_ext = ".png" if has_alpha else ".jpg"
        out_path = IMG_CACHE / f"{path.stem}-pdf{out_ext}"
        if has_alpha:
            img = img.convert("RGBA")
            img.save(out_path, format="PNG", optimize=True)
        else:
            img = img.convert("RGB")
            img.save(
                out_path,
                format="JPEG",
                quality=PDF_IMAGE_JPEG_QUALITY,
                optimize=True,
                progressive=True,
            )
        return out_path
    except Exception as e:
        print(f"  ! image optimization failed: {path.name} ({type(e).__name__}: {e})")
        return path


def _svg_tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def parse_svg_length(value, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.endswith("%"):
        return default
    match = SVG_LENGTH_RE.match(text)
    if match:
        return float(match.group(1))
    try:
        return float(text)
    except ValueError:
        return default


def parse_svg_viewbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    parts = [part for part in re.split(r"[\s,]+", value.strip()) if part]
    if len(parts) != 4:
        return None
    try:
        return tuple(float(part) for part in parts)
    except ValueError:
        return None


def parse_svg_color(value: str | None):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "transparent"}:
        return None
    try:
        return toColor(text)
    except ValueError:
        return None


def parse_svg_opacity(value, default: float = 1.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def merge_svg_style(element: ET.Element, inherited: dict[str, str]) -> dict[str, str]:
    style = dict(inherited)
    inline = element.attrib.get("style", "")
    for declaration in inline.split(";"):
        if ":" not in declaration:
            continue
        key, raw_value = declaration.split(":", 1)
        key = key.strip().lower()
        value = raw_value.strip()
        if key and value:
            style[key] = value
    for key in SVG_STYLE_KEYS:
        if key in element.attrib:
            style[key] = element.attrib[key]
    return style


def apply_svg_shape_style(shape, style: dict[str, str]) -> None:
    opacity = parse_svg_opacity(style.get("opacity"), 1.0)
    fill = parse_svg_color(style.get("fill", "black"))
    stroke = parse_svg_color(style.get("stroke"))
    stroke_width = parse_svg_length(style.get("stroke-width"), 1.0)

    if hasattr(shape, "fillColor"):
        shape.fillColor = fill
    if hasattr(shape, "strokeColor"):
        shape.strokeColor = stroke
    if hasattr(shape, "strokeWidth"):
        shape.strokeWidth = stroke_width
    if hasattr(shape, "fillOpacity"):
        shape.fillOpacity = parse_svg_opacity(style.get("fill-opacity"), 1.0) * opacity
    if hasattr(shape, "strokeOpacity"):
        shape.strokeOpacity = parse_svg_opacity(style.get("stroke-opacity"), 1.0) * opacity


def parse_svg_points(value: str) -> list[float]:
    return [float(part) for part in SVG_POINTS_RE.findall(value or "")]


def parse_svg_path_shape(path_data: str, style: dict[str, str]) -> RLPath | None:
    tokens = SVG_PATH_TOKEN_RE.findall(path_data or "")
    if not tokens:
        return None
    shape = RLPath()
    idx = 0
    command = None
    current = (0.0, 0.0)
    start = (0.0, 0.0)

    def next_number() -> float:
        nonlocal idx
        if idx >= len(tokens):
            raise ValueError("path data ended unexpectedly")
        value = float(tokens[idx])
        idx += 1
        return value

    while idx < len(tokens):
        token = tokens[idx]
        if re.fullmatch(r"[MmLlHhVvZz]", token):
            command = token
            idx += 1
        if command is None:
            return None

        if command in {"M", "m"}:
            x = next_number()
            y = next_number()
            if command == "m":
                x += current[0]
                y += current[1]
            shape.moveTo(x, y)
            current = (x, y)
            start = (x, y)
            command = "L" if command == "M" else "l"
            continue

        if command in {"L", "l"}:
            x = next_number()
            y = next_number()
            if command == "l":
                x += current[0]
                y += current[1]
            shape.lineTo(x, y)
            current = (x, y)
            continue

        if command in {"H", "h"}:
            x = next_number()
            if command == "h":
                x += current[0]
            shape.lineTo(x, current[1])
            current = (x, current[1])
            continue

        if command in {"V", "v"}:
            y = next_number()
            if command == "v":
                y += current[1]
            shape.lineTo(current[0], y)
            current = (current[0], y)
            continue

        if command in {"Z", "z"}:
            shape.closePath()
            current = start
            command = None
            continue

        return None

    apply_svg_shape_style(shape, style)
    return shape


def svg_element_to_shape(element: ET.Element, inherited_style: dict[str, str]):
    tag = _svg_tag(element)
    style = merge_svg_style(element, inherited_style)

    if tag in {"g", "svg"}:
        group = Group()
        for child in element:
            child_shape = svg_element_to_shape(child, style)
            if child_shape is not None:
                group.add(child_shape)
        return group if getattr(group, "contents", None) else None

    if tag == "rect":
        shape = Rect(
            parse_svg_length(element.attrib.get("x"), 0.0),
            parse_svg_length(element.attrib.get("y"), 0.0),
            parse_svg_length(element.attrib.get("width"), 0.0),
            parse_svg_length(element.attrib.get("height"), 0.0),
        )
        apply_svg_shape_style(shape, style)
        return shape

    if tag == "line":
        shape = Line(
            parse_svg_length(element.attrib.get("x1"), 0.0),
            parse_svg_length(element.attrib.get("y1"), 0.0),
            parse_svg_length(element.attrib.get("x2"), 0.0),
            parse_svg_length(element.attrib.get("y2"), 0.0),
        )
        apply_svg_shape_style(shape, style)
        return shape

    if tag == "circle":
        shape = Circle(
            parse_svg_length(element.attrib.get("cx"), 0.0),
            parse_svg_length(element.attrib.get("cy"), 0.0),
            parse_svg_length(element.attrib.get("r"), 0.0),
        )
        apply_svg_shape_style(shape, style)
        return shape

    if tag == "ellipse":
        shape = Ellipse(
            parse_svg_length(element.attrib.get("cx"), 0.0),
            parse_svg_length(element.attrib.get("cy"), 0.0),
            parse_svg_length(element.attrib.get("rx"), 0.0),
            parse_svg_length(element.attrib.get("ry"), 0.0),
        )
        apply_svg_shape_style(shape, style)
        return shape

    if tag == "polyline":
        points = parse_svg_points(element.attrib.get("points", ""))
        if len(points) >= 4:
            shape = PolyLine(points)
            apply_svg_shape_style(shape, style)
            return shape
        return None

    if tag == "polygon":
        points = parse_svg_points(element.attrib.get("points", ""))
        if len(points) >= 6:
            shape = Polygon(points)
            apply_svg_shape_style(shape, style)
            return shape
        return None

    if tag == "path":
        return parse_svg_path_shape(element.attrib.get("d", ""), style)

    if tag == "text":
        text = "".join(element.itertext()).strip()
        if not text:
            return None
        shape = String(
            parse_svg_length(element.attrib.get("x"), 0.0),
            parse_svg_length(element.attrib.get("y"), 0.0),
            text,
        )
        font_size = parse_svg_length(style.get("font-size"), 12.0)
        if font_size:
            shape.fontSize = font_size
        font_family = style.get("font-family")
        if font_family:
            shape.fontName = font_family.split(",")[0].strip().strip("'\"")
        text_anchor = style.get("text-anchor", "").strip().lower()
        if text_anchor in {"start", "middle", "end"}:
            shape.textAnchor = text_anchor
        fill = parse_svg_color(style.get("fill", "black"))
        shape.fillColor = fill or black
        if hasattr(shape, "fillOpacity"):
            shape.fillOpacity = parse_svg_opacity(style.get("fill-opacity"), 1.0) * parse_svg_opacity(style.get("opacity"), 1.0)
        return shape

    return None


def svg_to_drawing(path: Path, *, max_w: float, max_h: float) -> Drawing:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    if _svg_tag(root) != "svg":
        raise ValueError("root element is not <svg>")

    viewbox = parse_svg_viewbox(root.attrib.get("viewBox"))
    if viewbox is not None:
        min_x, min_y, width, height = viewbox
    else:
        min_x, min_y = 0.0, 0.0
        width = parse_svg_length(root.attrib.get("width"), 240.0)
        height = parse_svg_length(root.attrib.get("height"), 120.0)

    width = width or parse_svg_length(root.attrib.get("width"), 240.0)
    height = height or parse_svg_length(root.attrib.get("height"), 120.0)
    if width <= 0 or height <= 0:
        raise ValueError("SVG width/height must be positive")

    scale = min(max_w / width, max_h / height, 1.0)
    drawing = Drawing(width * scale, height * scale)
    group = Group()
    group.transform = [scale, 0, 0, scale, -min_x * scale, -min_y * scale]

    base_style = {
        "fill": root.attrib.get("fill", "black"),
        "stroke": root.attrib.get("stroke", "none"),
        "stroke-width": root.attrib.get("stroke-width", "1"),
        "font-size": root.attrib.get("font-size", "12"),
    }
    for child in root:
        child_shape = svg_element_to_shape(child, base_style)
        if child_shape is not None:
            group.add(child_shape)
    if not getattr(group, "contents", None):
        raise ValueError("SVG did not contain supported drawable elements")
    drawing.add(group)
    drawing.hAlign = "CENTER"
    return drawing


def make_image_flowable(url: str, alt: str, styles):
    """Create a sized, captioned, page-break-aware image flowable. Returns None on failure."""
    path = download_image(url)
    if not path:
        return None
    try:
        max_w = 5.2 * inch
        max_h = 3.1 * inch
        if path.suffix.lower() == ".svg":
            img = svg_to_drawing(path, max_w=max_w, max_h=max_h)
        else:
            render_path = optimize_raster_for_pdf(path)
            ir = ImageReader(str(render_path))
            iw, ih = ir.getSize()
            ratio = min(max_w / iw, max_h / ih, 1.0)
            w = iw * ratio
            h = ih * ratio
            img = RLImage(str(render_path), width=w, height=h)
            img.hAlign = 'CENTER'
        flow = [Spacer(1, 6), img]
        if alt and alt.strip():
            flow.append(Paragraph(f"<i>{md_inline(alt.strip())}</i>", styles["ItalicCenter"]))
        flow.append(Spacer(1, 6))
        return KeepTogether(flow)
    except Exception as e:
        print(f"  ! image embed failed: {path.name} ({type(e).__name__}: {e})")
        return None

# ── Colors ───────────────────────────────────────────────
DARK = HexColor("#1a1a2e")
ACCENT = HexColor("#16213e")
RULE_COLOR = HexColor("#444444")
LIGHT_BG = HexColor("#f0f0f0")

# ── Custom Styles ────────────────────────────────────────
def make_styles():
    s = getSampleStyleSheet()

    s.add(ParagraphStyle(
        "MainTitle", parent=s["Title"],
        fontSize=26, leading=32, spaceAfter=4,
        textColor=DARK, alignment=TA_CENTER,
        fontName="Helvetica-Bold",
    ))
    s.add(ParagraphStyle(
        "Subtitle", parent=s["Normal"],
        fontSize=13, leading=17, spaceAfter=2,
        textColor=HexColor("#555555"), alignment=TA_CENTER,
        fontName="Helvetica-Oblique",
    ))
    s.add(ParagraphStyle(
        "DateLine", parent=s["Normal"],
        fontSize=10, leading=14, spaceAfter=18,
        textColor=HexColor("#888888"), alignment=TA_CENTER,
        fontName="Helvetica",
    ))
    s.add(ParagraphStyle(
        "PartHeader", parent=s["Heading1"],
        fontSize=20, leading=26, spaceBefore=28, spaceAfter=10,
        textColor=DARK, fontName="Helvetica-Bold",
        borderWidth=0, borderPadding=0,
        keepWithNext=1,
    ))
    s.add(ParagraphStyle(
        "SectionHeader", parent=s["Heading2"],
        fontSize=15, leading=20, spaceBefore=18, spaceAfter=6,
        textColor=ACCENT, fontName="Helvetica-Bold",
        keepWithNext=1,
    ))
    s.add(ParagraphStyle(
        "SubSection", parent=s["Heading3"],
        fontSize=12, leading=16, spaceBefore=12, spaceAfter=4,
        textColor=HexColor("#333333"), fontName="Helvetica-Bold",
        keepWithNext=1,
    ))
    s.add(ParagraphStyle(
        "Body", parent=s["Normal"],
        fontSize=10, leading=14.5, spaceAfter=8,
        alignment=TA_JUSTIFY, fontName="Helvetica",
        firstLineIndent=0,
        allowWidows=0, allowOrphans=0,
    ))
    s.add(ParagraphStyle(
        "BoldBody", parent=s["Normal"],
        fontSize=10, leading=14.5, spaceAfter=6,
        fontName="Helvetica-Bold",
    ))
    s.add(ParagraphStyle(
        "ItalicCenter", parent=s["Normal"],
        fontSize=10, leading=14.5, spaceAfter=8,
        fontName="Helvetica-Oblique", alignment=TA_CENTER,
        textColor=HexColor("#555555"),
    ))
    s.add(ParagraphStyle(
        "BulletItem", parent=s["Normal"],
        fontSize=10, leading=14, spaceAfter=3,
        fontName="Helvetica", leftIndent=24, bulletIndent=12,
        alignment=TA_LEFT,
    ))
    s.add(ParagraphStyle(
        "SourceBulletItem", parent=s["Normal"],
        fontSize=8.5, leading=10.5, spaceAfter=2,
        fontName="Helvetica", leftIndent=20, bulletIndent=10,
        alignment=TA_LEFT, textColor=HexColor("#444444"),
    ))
    s.add(ParagraphStyle(
        "CalloutBox", parent=s["Normal"],
        fontSize=9.5, leading=13, spaceBefore=6, spaceAfter=8,
        fontName="Helvetica", textColor=DARK,
        borderWidth=0.6, borderPadding=8, borderColor=HexColor("#c7d3df"),
        backColor=HexColor("#f6f8fb"),
    ))
    s.add(ParagraphStyle(
        "TableCell", parent=s["Normal"],
        fontSize=8.5, leading=11, fontName="Helvetica",
    ))
    s.add(ParagraphStyle(
        "TableHeader", parent=s["Normal"],
        fontSize=8.5, leading=11, fontName="Helvetica-Bold",
        textColor=white,
    ))
    s.add(ParagraphStyle(
        "Footer", parent=s["Normal"],
        fontSize=8, leading=10, alignment=TA_CENTER,
        textColor=HexColor("#999999"),
    ))
    return s


# ── Page template callbacks ──────────────────────────────
def on_first_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(DARK)
    canvas.rect(0, letter[1] - 3*mm, letter[0], 3*mm, fill=True, stroke=False)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, 0, letter[0], 3*mm, fill=True, stroke=False)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(HexColor("#999999"))
    canvas.drawString(doc.leftMargin, letter[1] - 0.35*inch,
                      f"{BRIEFING_LABEL.upper()} — {DATE_HUMAN}")
    canvas.drawRightString(letter[0] - doc.rightMargin, letter[1] - 0.35*inch,
                           PREPARED_BY_LINE)
    canvas.restoreState()

def on_later_pages(canvas, doc):
    canvas.saveState()
    # Top rule
    canvas.setStrokeColor(RULE_COLOR)
    canvas.setLineWidth(0.5)
    canvas.line(doc.leftMargin, letter[1] - 0.4*inch,
                letter[0] - doc.rightMargin, letter[1] - 0.4*inch)
    # Header text
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(HexColor("#999999"))
    canvas.drawString(doc.leftMargin, letter[1] - 0.35*inch,
                      f"{BRIEFING_LABEL.upper()} — {DATE_HUMAN}")
    canvas.drawRightString(letter[0] - doc.rightMargin, letter[1] - 0.35*inch,
                           f"Page {doc.page}")
    # Bottom rule
    canvas.line(doc.leftMargin, 0.45*inch,
                letter[0] - doc.rightMargin, 0.45*inch)
    canvas.restoreState()


# ── Markdown → flowables ─────────────────────────────────
def md_inline(text):
    """Convert basic inline markdown to reportlab XML."""
    # First escape HTML entities in the raw text
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    # Now apply markdown formatting on the escaped text
    # Bold + italic
    text = re.sub(r'\*\*\*(.*?)\*\*\*', r'<b><i>\1</i></b>', text)
    # Bold
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Italic (but not bullet points)
    text = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'<i>\1</i>', text)
    # Inline code — just use bold courier-ish styling
    text = re.sub(r'`([^`]+)`', r'<b>\1</b>', text)
    # Links — just show text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    if JP_FONT:
        text = wrap_unicode_font_runs(text)
    return text


def wrap_unicode_font_runs(text: str) -> str:
    """Wrap characters unsupported by the base Latin font in the Unicode fallback.

    ReportLab's default Helvetica flow uses a WinAnsi-like encoding, which
    covers normal Latin text plus common punctuation but not symbols like `π₀`.
    We split around already-inserted XML tags and wrap only the unsupported
    character runs so the paragraph parser keeps the surrounding markup intact.
    """
    parts = XML_TAG_RE.split(text)
    return "".join(_wrap_unicode_segment(part) if not part.startswith("<") else part for part in parts)


def _wrap_unicode_segment(text: str) -> str:
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        out.append(rf'<font name="{JP_FONT}">{"".join(buffer)}</font>')
        buffer.clear()

    for ch in text:
        if char_needs_unicode_font(ch):
            buffer.append(ch)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out)


def char_needs_unicode_font(ch: str) -> bool:
    if CJK_RE.fullmatch(ch):
        return True
    try:
        ch.encode("cp1252")
    except UnicodeEncodeError:
        return True
    return False


SOURCE_URL_RE = re.compile(r'https?://[^\s)>\]]+')
ARTICLE_HEADING_RE = re.compile(r'^(?:(?:Article|Analysis|Update|Story)\b|\d+\.\s)', re.IGNORECASE)


def heading_match(line: str):
    return re.match(r'^(#{1,6})\s+(.*)', line.rstrip())


def is_hr_line(line: str) -> bool:
    return line.strip() in ('---', '***', '___')


def is_bullet_line(line: str) -> bool:
    return re.match(r'^[-*]\s', line.strip()) is not None


def is_numbered_line(line: str) -> bool:
    return re.match(r'^\d+\.\s', line.strip()) is not None


def is_table_line(line: str) -> bool:
    return '|' in line and line.strip().startswith('|')


def is_blockquote_line(line: str) -> bool:
    return re.match(r'^\s*>\s?', line) is not None


def is_part_header(level: int, text: str) -> bool:
    upper = text.upper()
    return (
        level == 1
        and "DAILY INTELLIGENCE" not in upper
        and "EXECUTIVE SUMMARY" not in upper
    )


def is_article_header(level: int, text: str) -> bool:
    return level == 2 and ARTICLE_HEADING_RE.match(text.strip()) is not None


def compact_source_bullet_text(text: str) -> str:
    """Back-compat shim: returns plain-text compacted bullet (no hyperlinks).

    Kept for any callers that want a text-only rendering. The PDF render path
    now uses render_source_bullet_with_links() to produce clickable hyperlinks.
    """
    urls = SOURCE_URL_RE.findall(text)
    if not urls:
        return text

    domains: list[str] = []
    for url in urls:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host and host not in domains:
            domains.append(host)

    compact = SOURCE_URL_RE.sub("", text)
    compact = re.sub(r"\s*[-:]\s*$", "", compact).strip(" -")
    compact = re.sub(r"\s{2,}", " ", compact).strip()

    if compact and domains:
        return f"{compact} ({', '.join(domains)})"
    if domains:
        return ", ".join(domains)
    return compact or text


LINK_COLOR_HEX = "#1f4e79"  # Muted navy — legible on white, WCAG-AA compliant


def render_source_bullet_with_links(text: str) -> str:
    """Convert a raw source-bullet line into ReportLab-XML with clickable links.

    The Primary Sources block in every article is a bulleted list where each
    bullet typically has the form "Description - https://example.com/path".
    compact_source_bullet_text() strips URLs and emits plain-text domains; this
    function instead wraps each URL in `<link href="URL"><u>domain</u></link>`
    so the domain name becomes a clickable hyperlink in the PDF while the
    description text remains readable. The description is escaped for XML
    before the link tags are inserted so user-supplied markdown (e.g. a literal
    `<` in the description) cannot corrupt the paragraph parser.
    """
    urls = SOURCE_URL_RE.findall(text)
    if not urls:
        # No URL — just XML-escape and return as body text.
        return md_inline(text)

    # Strip URLs and any trailing separators, so we can render description + links separately.
    description = SOURCE_URL_RE.sub("", text)
    description = re.sub(r"\s*[-:]\s*$", "", description).strip(" -")
    description = re.sub(r"\s{2,}", " ", description).strip()

    link_parts: list[str] = []
    seen: set[str] = set()
    for url in urls:
        # Sanitize URL: must be http(s) and contain no double quote (quote-breaking attack).
        if '"' in url or not url.startswith(("http://", "https://")):
            continue
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if not host or host in seen:
            continue
        seen.add(host)
        # XML-escape the URL attribute and the visible text.
        safe_url = url.replace("&", "&amp;").replace('"', "&quot;")
        safe_host = host.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        link_parts.append(
            f'<link href="{safe_url}"><font color="{LINK_COLOR_HEX}"><u>{safe_host}</u></font></link>'
        )

    if not link_parts:
        return md_inline(description) if description else md_inline(text)

    links_xml = ", ".join(link_parts)
    if description:
        return f"{md_inline(description)} ({links_xml})"
    return links_xml


def normalize_header_label(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def skip_blank_lines(lines, index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def make_heading_flowable(level: int, text: str, styles):
    text = md_inline(text)
    if level == 1:
        if "DAILY INTELLIGENCE" in text.upper():
            return Paragraph(text, styles["MainTitle"])
        if "EXECUTIVE SUMMARY" in text.upper():
            return KeepTogether([Spacer(1, 8), Paragraph(text, styles["PartHeader"])])
        return Paragraph(text, styles["PartHeader"])
    if level == 2:
        return Paragraph(text, styles["SectionHeader"])
    return Paragraph(text, styles["SubSection"])


def consume_paragraph_flowable(lines, start_index: int, styles):
    para_lines = [lines[start_index].strip()]
    index = start_index + 1
    while index < len(lines) and lines[index].strip() and \
          heading_match(lines[index]) is None and \
          not is_hr_line(lines[index]) and \
          not is_table_line(lines[index]) and \
          not is_bullet_line(lines[index]) and \
          not is_numbered_line(lines[index]):
        para_lines.append(lines[index].strip())
        index += 1
    text = md_inline(' '.join(para_lines))
    return Paragraph(text, styles["Body"]), index


def consume_article_opener(lines, start_index: int, styles):
    opener = []
    index = start_index

    match = heading_match(lines[index])
    if not match:
        return opener, start_index

    level = len(match.group(1))
    text = match.group(2).strip()
    opener.append(make_heading_flowable(level, text, styles))
    index += 1

    while index < len(lines):
        index = skip_blank_lines(lines, index)
        if index >= len(lines):
            break

        line = lines[index].rstrip()
        if heading_match(line) or is_hr_line(line) or is_table_line(line) or is_bullet_line(line) or is_numbered_line(line):
            break

        m_img = re.match(r'^!\[(.*?)\]\(([^\s)]+)\)\s*$', line.strip())
        if m_img:
            img_flow = make_image_flowable(m_img.group(2), m_img.group(1), styles)
            if img_flow:
                opener.append(img_flow)
            index += 1
            continue

        if line.startswith('*') and line.endswith('*') and not line.startswith('**'):
            opener.append(Paragraph(md_inline(line.strip('*').strip()), styles["ItalicCenter"]))
            index += 1
            continue

        paragraph, index = consume_paragraph_flowable(lines, index, styles)
        opener.append(paragraph)
        break

    return opener, index


def consume_blockquote_flowables(lines, start_index: int, styles):
    block_lines: list[str] = []
    index = start_index
    while index < len(lines) and is_blockquote_line(lines[index]):
        block_lines.append(re.sub(r'^\s*>\s?', '', lines[index].rstrip()).strip())
        index += 1

    content_lines = [line for line in block_lines if line]
    if not content_lines:
        return [], index

    rendered_lines: list[str] = []
    for line in content_lines:
        if re.match(r'^[-*]\s', line):
            rendered_lines.append(f"&bull; {md_inline(re.sub(r'^[-*]\s', '', line).strip())}")
        else:
            rendered_lines.append(md_inline(line))

    content = "<br/>".join(rendered_lines)
    return [Paragraph(content, styles["CalloutBox"])], index


def parse_markdown(md_text, styles):
    """Parse markdown text into a list of reportlab flowables."""
    flowables = []
    lines = md_text.split('\n')
    i = 0
    in_table = False
    table_rows = []
    in_primary_sources = False

    while i < len(lines):
        line = lines[i].rstrip()

        # Skip empty lines
        if not line.strip():
            i += 1
            continue

        # Block-level image: line consists of just ![alt](url)
        m_img = re.match(r'^!\[(.*?)\]\(([^\s)]+)\)\s*$', line.strip())
        if m_img:
            if in_table and table_rows:
                flowables.append(build_table(table_rows, styles))
                table_rows = []
                in_table = False
            alt, url = m_img.group(1), m_img.group(2)
            print(f"  → image: {url[:80]}")
            img_flow = make_image_flowable(url, alt, styles)
            if img_flow:
                flowables.append(img_flow)
            i += 1
            continue

        # Horizontal rule
        if line.strip() in ('---', '***', '___'):
            if in_table and table_rows:
                flowables.append(build_table(table_rows, styles))
                table_rows = []
                in_table = False
            flowables.append(Spacer(1, 6))
            flowables.append(HRFlowable(
                width="100%", thickness=0.5, color=RULE_COLOR,
                spaceAfter=6, spaceBefore=2
            ))
            i += 1
            continue

        # Table detection
        if '|' in line and line.strip().startswith('|'):
            if not in_table:
                in_table = True
                table_rows = []
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            # Skip separator rows
            if all(set(c.strip()) <= {'-', ':', ' '} for c in cells):
                i += 1
                continue
            table_rows.append(cells)
            i += 1
            continue
        else:
            if in_table and table_rows:
                flowables.append(build_table(table_rows, styles))
                table_rows = []
                in_table = False

        if is_blockquote_line(line):
            block_flowables, next_index = consume_blockquote_flowables(lines, i, styles)
            flowables.extend(block_flowables)
            i = next_index
            continue

        # Headings
        m_heading = heading_match(line)
        if m_heading:
            level = len(m_heading.group(1))
            raw_text = m_heading.group(2).strip()
            in_primary_sources = raw_text.lower() == "primary sources"

            if is_part_header(level, raw_text):
                opener = [make_heading_flowable(level, raw_text, styles)]
                next_index = skip_blank_lines(lines, i + 1)
                if next_index < len(lines):
                    next_heading = heading_match(lines[next_index])
                    if next_heading and is_article_header(len(next_heading.group(1)), next_heading.group(2).strip()):
                        article_opener, next_index = consume_article_opener(lines, next_index, styles)
                        opener.extend(article_opener)
                        flowables.append(KeepTogether(opener))
                        i = next_index
                        continue
                flowables.append(KeepTogether(opener))
                i += 1
                continue

            if is_article_header(level, raw_text):
                opener, next_index = consume_article_opener(lines, i, styles)
                flowables.append(KeepTogether(opener))
                i = next_index
                continue

            flowables.append(make_heading_flowable(level, raw_text, styles))
            i += 1
            continue

        # Subtitle line (italic block at top)
        if line.startswith('*') and line.endswith('*') and not line.startswith('**'):
            text = line.strip('*').strip()
            flowables.append(Paragraph(md_inline(text), styles["ItalicCenter"]))
            i += 1
            continue

        # Bullet points
        if re.match(r'^[-*]\s', line.strip()):
            text = re.sub(r'^[-*]\s', '', line.strip())
            if in_primary_sources:
                rendered = render_source_bullet_with_links(text)
            else:
                rendered = md_inline(text)
            flowables.append(Paragraph(
                f"\u2022  {rendered}", styles["SourceBulletItem" if in_primary_sources else "BulletItem"]
            ))
            i += 1
            continue

        # Numbered list
        if re.match(r'^\d+\.\s', line.strip()):
            m = re.match(r'^(\d+)\.\s(.*)', line.strip())
            if m:
                num = m.group(1)
                text = md_inline(m.group(2))
                flowables.append(Paragraph(
                    f"<b>{num}.</b>  {text}", styles["BulletItem"]
                ))
            i += 1
            continue

        # Regular paragraph — collect continuation lines
        para_lines = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and \
              not lines[i].strip().startswith('#') and \
              not lines[i].strip().startswith('---') and \
              not lines[i].strip().startswith('|') and \
              not re.match(r'^[-*]\s', lines[i].strip()) and \
              not re.match(r'^\d+\.\s', lines[i].strip()):
            para_lines.append(lines[i].strip())
            i += 1

        text = ' '.join(para_lines)
        text = md_inline(text)

        # Check if it's a bold-lead paragraph
        if text.startswith('<b>') and '</b>' in text[:80]:
            flowables.append(Paragraph(text, styles["Body"]))
        else:
            flowables.append(Paragraph(text, styles["Body"]))

    # Flush remaining table
    if in_table and table_rows:
        flowables.append(build_table(table_rows, styles))

    return flowables


def table_column_widths(headers, avail: float):
    """Choose readable column widths instead of forcing equal-width tables."""
    normalized = [normalize_header_label(cell) for cell in headers]
    counterpoint_headers = [
        "claim",
        "interested party",
        "structural incentive",
        "tier 4 counterpoint source",
        "net assessment",
    ]

    if normalized == counterpoint_headers:
        weights = [1.1, 0.92, 1.18, 1.18, 1.42]
    else:
        weights = [max(0.9, min(1.6, 0.8 + (len(cell) / 18.0))) for cell in normalized]

    total = sum(weights) or 1.0
    widths = [avail * weight / total for weight in weights]
    if widths:
        widths[-1] = avail - sum(widths[:-1])
    return widths


def build_table(rows, styles):
    """Build a reportlab Table from parsed rows."""
    if not rows:
        return Spacer(1, 1)

    # First row is header
    header = rows[0]
    data_rows = rows[1:] if len(rows) > 1 else []

    col_count = len(header)
    table_data = []

    # Header
    hdr = [Paragraph(md_inline(c), styles["TableHeader"]) for c in header]
    table_data.append(hdr)

    # Data rows
    for row in data_rows:
        # Pad row to match header length
        while len(row) < col_count:
            row.append("")
        r = [Paragraph(md_inline(c), styles["TableCell"]) for c in row[:col_count]]
        table_data.append(r)

    avail = 6.3 * inch
    tbl = Table(table_data, colWidths=table_column_widths(header, avail), repeatRows=1)
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), ACCENT),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8.25),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.4, HexColor("#cccccc")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, LIGHT_BG]),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4.5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4.5),
    ]))
    return tbl


# ── Main ─────────────────────────────────────────────────
def main():
    started = time.time()
    cleanup_generated_outputs = getattr(briefing_paths_module, "cleanup_generated_outputs", None)
    if callable(cleanup_generated_outputs):
        cleanup_generated_outputs(ttl_days=3)

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--skip-validation-reason", default="")
    args, _ = parser.parse_known_args()

    if args.skip_validation:
        allowed = os.environ.get("BRIEFING_ALLOW_SKIP_VALIDATION") == "1"
        reason = args.skip_validation_reason.strip()
        if not allowed:
            sys.stderr.write(
                "ERROR: --skip-validation refused. Set BRIEFING_ALLOW_SKIP_VALIDATION=1 "
                "in the environment to explicitly authorize bypassing quality gates, "
                "and pass --skip-validation-reason=\"<explanation>\" so the bypass is "
                "recorded on stdout for the build log.\n"
            )
            return 2
        if not reason:
            sys.stderr.write(
                "ERROR: --skip-validation requires --skip-validation-reason=\"<explanation>\" "
                "(non-empty) so the bypass is recorded.\n"
            )
            return 2
        banner = "=" * 70
        print(banner)
        print("  !!! VALIDATION BYPASSED — quality gates NOT enforced !!!")
        print(f"  Date:   {BRIEFING_DATE}")
        print(f"  Reason: {reason}")
        print("  Skipped checks include: per-article word minimums, article counts,")
        print("  source counts, Counterpoint Matrix presence, Watch Next 48h presence,")
        print("  banned-topic matches, and header cross-reference integrity.")
        print(banner)
        update_run_report(
            BRIEFING_DATE,
            run_status="degraded",
            validation_status="bypassed",
            notes=[f"PDF built with validation bypass: {reason}"],
        )
    else:
        print("Validating briefing structure and quality gates...")
        validate_briefing(BRIEFING_DATE)

    print("Reading markdown...")
    md_text = MD_PATH.read_text(encoding="utf-8")

    styles = make_styles()

    print("Parsing markdown into flowables...")
    flowables = parse_markdown(md_text, styles)

    print(f"Generated {len(flowables)} flowables")

    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=letter,
        leftMargin=0.85*inch,
        rightMargin=0.85*inch,
        topMargin=0.7*inch,
        bottomMargin=0.65*inch,
        title=f"{BRIEFING_LABEL} - {DATE_HUMAN}",
        author=BRIEFING_AUTHOR,
        subject=f"{BRIEFING_LABEL} — {DATE_HUMAN}",
    )

    print("Building PDF...")
    doc.build(flowables, onFirstPage=on_first_page, onLaterPages=on_later_pages)
    print(f"PDF saved to {PDF_PATH}")
    print(f"Size: {PDF_PATH.stat().st_size / 1024:.0f} KB")
    ended = time.time()
    update_run_report(
        BRIEFING_DATE,
        run_status="degraded" if args.skip_validation else "completed",
    )
    set_step_timing(
        BRIEFING_DATE,
        "pdf_build",
        status="bypassed" if args.skip_validation else "passed",
        started_at=datetime.fromtimestamp(started).astimezone().isoformat(),
        ended_at=datetime.fromtimestamp(ended).astimezone().isoformat(),
        duration_seconds=round(ended - started, 3),
        details={
            "pdf_path": str(PDF_PATH),
            "pdf_bytes": PDF_PATH.stat().st_size,
        },
    )


if __name__ == "__main__":
    main()
