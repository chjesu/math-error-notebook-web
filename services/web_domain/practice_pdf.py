"""A4 practice sheets. Answers are included only after an explicit request."""

from __future__ import annotations

from collections import defaultdict
from html import escape
from io import BytesIO
import hashlib
import re
from pathlib import Path
from typing import Any


_MATH_CACHE_DIR = Path(__file__).resolve().parents[2] / "tmp" / "pdfs" / "math"
_MATH_COMMANDS = (
    "frac", "dfrac", "tfrac", "sqrt", "vec", "overrightarrow", "angle",
    "pi", "infty", "cdot", "times", "pm", "le", "leq", "ge", "geq",
    "ne", "neq", "perp", "parallel", "in", "notin", "triangle", "ell",
    "rightarrow", "leftarrow", "Rightarrow", "Leftarrow", "Leftrightarrow",
    "mathbb", "mathrm", "operatorname", "text", "boxed", "langle", "rangle",
    "left", "right", "middle", "alpha", "beta", "gamma", "theta", "omega",
    "sin", "cos", "tan", "circ",
)
_MATH_COMMAND_RE = re.compile(r"\\(?:" + "|".join(_MATH_COMMANDS) + r")(?![A-Za-z])")
_HAS_CJK = re.compile(r"[\u3400-\u9fff]")
_SUPERSCRIPTS = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUBSCRIPTS = str.maketrans("0123456789+-=()aeoxn", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₙ")


def _replace_math_args(value: str) -> str:
    """Convert common LaTeX commands to readable Unicode math text."""
    replacements = {
        r"\left": "", r"\right": "", r"\middle": "", r"\,": " ",
        r"\;": " ", r"\quad": "  ", r"\leq": "≤", r"\le": "≤",
        r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠",
        r"\perp": "⊥", r"\parallel": "∥", r"\pi": "π", r"\infty": "∞",
        r"\cdot": "·", r"\times": "×", r"\pm": "±", r"\angle": "∠",
        r"\triangle": "△", r"\ell": "ℓ", r"\in": "∈", r"\notin": "∉",
        r"\rightarrow": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒",
        r"\Leftarrow": "⇐", r"\Leftrightarrow": "⇔",
        r"\langle": "⟨", r"\rangle": "⟩",
        r"\alpha": "α", r"\beta": "β", r"\gamma": "γ",
        r"\theta": "θ", r"\omega": "ω", r"\circ": "°",
    }
    for command, symbol in replacements.items():
        value = value.replace(command, symbol)
    for _ in range(8):
        before = value

        def _vector(match: re.Match[str]) -> str:
            base, braced_subscript, bare_subscript = match.groups()
            subscript = braced_subscript or bare_subscript
            return base + "⃗" + (f"_{{{subscript}}}" if subscript else "")

        value = re.sub(
            r"\\(?:vec|overrightarrow)\{([^{}_]+)(?:_\{([^{}]+)\}|_([^{}]))?\}",
            _vector,
            value,
        )
        value = re.sub(r"\\(?:vec|overrightarrow)\s*([A-Za-z])", lambda m: m.group(1) + "⃗", value)
        value = re.sub(r"\\operatorname\{vec\}\s*\(([^()]*)\)", r"\1⃗", value)
        value = re.sub(r"\\boxed\{([^{}]*)\}", r"\1", value)
        value = re.sub(r"\\(?:text|mathrm|operatorname|mathbb)\{([^{}]*)\}", r"\1", value)

        def _fraction(match: re.Match[str]) -> str:
            numerator, denominator = (part.strip() for part in match.groups())
            atom = lambda part: part if re.fullmatch(r"[A-Za-z0-9π∞√]+", part) else f"({part})"
            return f"{atom(numerator)}⁄{atom(denominator)}"

        value = re.sub(r"\\(?:frac|dfrac|tfrac)\{([^{}]*)\}\{([^{}]*)\}", _fraction, value)
        value = re.sub(r"\\(?:frac|dfrac|tfrac)\s*([0-9A-Za-zπ])\s*([0-9A-Za-zπ])", lambda m: f"{m.group(1)}⁄{m.group(2)}", value)
        value = re.sub(r"\\sqrt\{([^{}]*)\}", lambda m: "√" + (m.group(1) if re.fullmatch(r"[A-Za-z0-9]+", m.group(1)) else f"({m.group(1)})"), value)
        value = re.sub(r"\\sqrt\s*([0-9A-Za-z])", lambda m: "√" + m.group(1), value)
        if value == before:
            break
    value = re.sub(r"\^\{([^{}]+)\}", lambda m: m.group(1).translate(_SUPERSCRIPTS), value)
    value = re.sub(r"_\{([^{}]+)\}", lambda m: m.group(1).translate(_SUBSCRIPTS), value)
    value = re.sub(r"\^([0-9A-Za-z])", lambda m: m.group(1).translate(_SUPERSCRIPTS), value)
    value = re.sub(r"_([0-9A-Za-z])", lambda m: m.group(1).translate(_SUBSCRIPTS), value)
    value = value.replace("^°", "°")
    value = value.replace("\\\\", " ").replace("{", "").replace("}", "")
    return re.sub(r"\\([A-Za-z]+)", r"\1", value)


def _math_spans(value: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    index = 0
    while index < len(value):
        opener = next((item for item in ("$$", r"\[", r"\(", "$") if value.startswith(item, index)), None)
        if opener is None:
            index += 1
            continue
        closer = {"$$": "$$", r"\[": r"\]", r"\(": r"\)", "$": "$"}[opener]
        right = value.find(closer, index + len(opener))
        if right < 0:
            index += len(opener)
            continue
        spans.append((index, right + len(closer), value[index + len(opener):right]))
        index = right + len(closer)
    for match in _MATH_COMMAND_RE.finditer(value):
        if any(left <= match.start() < right for left, right, _ in spans):
            continue
        end = match.start()
        while end < len(value) and not _HAS_CJK.search(value[end]):
            if value[end] in "，。；：！？":
                break
            end += 1
        if end > match.start():
            spans.append((match.start(), end, value[match.start():end]))
    return sorted(spans)


def _render_math_image(math_text: str, font_size: float) -> tuple[Path, float, float] | None:
    if _HAS_CJK.search(math_text):
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
        font_path = next((item for item in (
            Path(r"C:\Windows\Fonts\DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuMathTeXGyre.ttf"),
        ) if item.is_file()), None)
        if font_path is None:
            return None
        px_size = max(18, round(font_size * 3.0))
        key = hashlib.sha256(f"v4|{math_text}|{font_size:.2f}".encode("utf-8")).hexdigest()[:20]
        output = _MATH_CACHE_DIR / f"math-{key}.png"
        if not output.is_file():
            _MATH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            font = ImageFont.truetype(str(font_path), px_size)
            probe = Image.new("RGBA", (8, 8), (255, 255, 255, 0))
            bounds = ImageDraw.Draw(probe).textbbox((0, 0), _replace_math_args(math_text), font=font)
            width, height = max(8, bounds[2] - bounds[0] + 10), max(8, bounds[3] - bounds[1] + 8)
            image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
            ImageDraw.Draw(image).text((5 - bounds[0], 3 - bounds[1]), _replace_math_args(math_text), font=font, fill="#172033")
            image.save(output, format="PNG", optimize=True)
        with Image.open(output) as image:
            width, height = image.size
        scale = 72.0 / 216.0
        return output, width * scale, height * scale
    except Exception:
        return None


def _formatted_text(value: Any, font_size: float = 10.5) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", lambda match: f"【{match.group(1) or '附图'}】", str(value))
    spans = _math_spans(text)
    if not spans:
        return escape(text).replace("\n", "<br/>")
    pieces: list[str] = []
    cursor = 0
    for left, right, math_text in spans:
        pieces.append(escape(text[cursor:left]).replace("\n", "<br/>"))
        rendered = _render_math_image(math_text, font_size)
        if rendered is None:
            pieces.append(escape(_replace_math_args(math_text)))
        else:
            path, width, height = rendered
            # ReportLab's mini-HTML parser expects POSIX separators even on
            # Windows; native backslashes make it silently drop the image.
            image_src = path.resolve().as_posix()
            pieces.append(f'<img src="{escape(image_src)}" width="{width:.1f}" height="{height:.1f}" valign="-2"/>')
        cursor = right
    pieces.append(escape(text[cursor:]).replace("\n", "<br/>"))
    return "".join(pieces)


def build_practice_pdf(items: list[dict[str, Any]], *, include_answers: bool, logo_path: Path | None = None) -> bytes:
    if not items:
        raise ValueError("practice items are required")
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=17 * mm, title="李兆霖数学错题本练习")
    styles = getSampleStyleSheet()
    title = ParagraphStyle("TitleCN", parent=styles["Title"], fontName="STSong-Light", fontSize=18, leading=25, textColor=colors.HexColor("#172033"), alignment=TA_CENTER)
    heading = ParagraphStyle("HeadingCN", parent=styles["Heading2"], fontName="STSong-Light", fontSize=13, leading=19, textColor=colors.HexColor("#3157d5"), spaceBefore=8, spaceAfter=7)
    # ReportLab's CJK line breaker cannot handle inline image fragments.  Math
    # formulas are rendered as inline images, so dense mixed Chinese/math text
    # could leave a zero-length fragment and abort the whole PDF.  STSong-Light
    # still wraps Chinese correctly with the normal line breaker.
    body = ParagraphStyle("BodyCN", parent=styles["BodyText"], fontName="STSong-Light", fontSize=10.5, leading=18, textColor=colors.HexColor("#172033"))
    meta = ParagraphStyle("MetaCN", parent=body, fontSize=8.5, leading=14, textColor=colors.HexColor("#647087"))

    def answer_space() -> Spacer:
        return Spacer(1, 28 * mm)

    story: list[Any] = []
    if logo_path and logo_path.is_file():
        logo = Image(str(logo_path), width=13 * mm, height=13 * mm)
        header = Table([[logo, Paragraph("李兆霖数学错题本<br/><font size='8'>今日复习练习</font>", body)]], colWidths=[17 * mm, 80 * mm])
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        story.extend([header, Spacer(1, 5 * mm)])
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[str(item["error_id"])].append(item)
    recommendation_count = sum(item["kind"] == "recommendation" for item in items)
    redo_count = sum(
        bool(next(item for item in group if item["kind"] == "original").get("requires_original"))
        or not any(item["kind"] == "recommendation" for item in group)
        for group in groups.values()
    )
    task_count = redo_count + recommendation_count
    story.extend([
        Paragraph("错题复习练习", title),
        Spacer(1, 3 * mm),
        Paragraph(f"本次需完成 {task_count} 道：原题重做 {redo_count} 道，推荐训练 {recommendation_count} 道。另展示 {len(groups)} 道错题原题；未标记“需重做”的原题仅作推荐依据。", meta),
        Spacer(1, 5 * mm),
    ])
    for group_no, (error_id, group) in enumerate(groups.items(), 1):
        original = next(item for item in group if item["kind"] == "original")
        recommendations = [item for item in group if item["kind"] == "recommendation"]
        requires_original = bool(original.get("requires_original")) or not recommendations
        stage = int(original.get("review_stage", 1))
        status_text = "需重做" if requires_original else "仅作推荐依据"
        if not recommendations and not original.get("requires_original"):
            status_text = "推荐缺口，改为重做"
        original_block = [
            Paragraph(f"{group_no}. 错题编号 {escape(error_id[:8])}", heading),
            Paragraph(f"第 {stage} 阶段 · {status_text}", meta),
            Paragraph(_text(original["stem_text"]), body),
        ]
        if requires_original:
            original_block.extend([Paragraph("原题作答区", meta), answer_space(), Spacer(1, 4 * mm)])
        else:
            original_block.append(Spacer(1, 4 * mm))
        story.append(KeepTogether(original_block))
        if not recommendations:
            story.append(Paragraph("暂无符合验证与授权要求的推荐题，本次改为重做原题。", meta))
        for index, item in enumerate(recommendations, 1):
            difficulty = "未标注" if item.get("difficulty") is None else str(item["difficulty"])
            story.append(KeepTogether([
                Paragraph(f"本阶段推荐训练题 {index}", heading),
                Paragraph(f"题库编号：{escape(str(item['question_id']))}　难度：{escape(difficulty)}<br/>推荐原因：{escape(str(item['reason']))}<br/>来源：{escape(str(item['source_title']))}", meta),
                Paragraph(_text(item["stem_text"]), body),
                Paragraph("推荐题作答区", meta),
                answer_space(),
                Spacer(1, 5 * mm),
            ]))
    if include_answers:
        story.extend([PageBreak(), Paragraph("答案", title), Spacer(1, 5 * mm)])
        answer_no = 0
        for item in items:
            if item["kind"] != "recommendation":
                continue
            answer_no += 1
            answer = item.get("answer_text") or "题库未提供答案"
            story.append(Paragraph(f"{answer_no}. 题库编号 {escape(str(item['question_id']))}", heading))
            story.append(Paragraph(_text(answer), body))

    def footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#647087"))
        canvas.drawString(18 * mm, 9 * mm, "李兆霖数学错题本")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"第 {document.page} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def _text(value: Any) -> str:
    return _formatted_text(value)
