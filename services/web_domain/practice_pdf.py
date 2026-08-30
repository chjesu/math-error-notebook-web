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
    "left", "right", "middle",
)
_MATH_COMMAND_RE = re.compile(r"\\(?:" + "|".join(_MATH_COMMANDS) + r")(?![A-Za-z])")
_HAS_CJK = re.compile(r"[\u3400-\u9fff]")
_SUPERSCRIPTS = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUBSCRIPTS = str.maketrans("0123456789+-=()aeoxn", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₙ")


def practice_mode(mode: str | None, include_answers: bool | None) -> str:
    if mode is not None and mode not in {"review", "self_test"}:
        raise ValueError("invalid practice mode")
    if include_answers is not None and not isinstance(include_answers, bool):
        raise ValueError("invalid include_answers")
    legacy_mode = None
    if include_answers is not None:
        legacy_mode = "review" if include_answers else "self_test"
    if mode is not None and legacy_mode is not None and mode != legacy_mode:
        raise ValueError("conflicting practice mode")
    return mode or legacy_mode or "self_test"


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
        value = re.sub(r"\\operatorname\{vec\}\s*\(([^()]*)\)", r"\1⃗", value)
        value = re.sub(r"\\boxed\{([^{}]*)\}", r"\1", value)
        value = re.sub(r"\\(?:text|mathrm|operatorname|mathbb)\{([^{}]*)\}", r"\1", value)

        def _fraction(match: re.Match[str]) -> str:
            numerator, denominator = (part.strip() for part in match.groups())
            atom = lambda part: part if re.fullmatch(r"[A-Za-z0-9π∞√]+", part) else f"({part})"
            return f"{atom(numerator)}⁄{atom(denominator)}"

        value = re.sub(r"\\(?:frac|dfrac|tfrac)\{([^{}]*)\}\{([^{}]*)\}", _fraction, value)
        value = re.sub(r"\\sqrt\{([^{}]*)\}", lambda m: "√" + (m.group(1) if re.fullmatch(r"[A-Za-z0-9]+", m.group(1)) else f"({m.group(1)})"), value)
        if value == before:
            break
    value = re.sub(r"\^\{([^{}]+)\}", lambda m: m.group(1).translate(_SUPERSCRIPTS), value)
    value = re.sub(r"_\{([^{}]+)\}", lambda m: m.group(1).translate(_SUBSCRIPTS), value)
    value = re.sub(r"\^([0-9A-Za-z])", lambda m: m.group(1).translate(_SUPERSCRIPTS), value)
    value = re.sub(r"_([0-9A-Za-z])", lambda m: m.group(1).translate(_SUBSCRIPTS), value)
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
            Path(r"C:\Windows\Fonts\DejaVuMathTeXGyre.ttf"),
            Path(r"C:\Windows\Fonts\DejaVuSans.ttf"),
            Path(r"C:\Windows\Fonts\DejaVuSansMono.ttf"),
            Path(r"C:\Windows\Fonts\seguisym.ttf"),
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuMathTeXGyre.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ) if item.is_file()), None)
        if font_path is None:
            return None
        px_size = max(18, round(font_size * 3.0))
        key = hashlib.sha256(f"v2|{math_text}|{font_size:.2f}".encode("utf-8")).hexdigest()[:20]
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
    text = str(value)
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


def build_practice_pdf(
    items: list[dict[str, Any]],
    *,
    mode: str | None = None,
    include_answers: bool | None = None,
    logo_path: Path | None = None,
) -> bytes:
    if not items:
        raise ValueError("practice items are required")
    resolved_mode = practice_mode(mode, include_answers)
    is_review = resolved_mode == "review"
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=17 * mm, title="李兆霖数学错题本练习")
    styles = getSampleStyleSheet()
    title = ParagraphStyle("TitleCN", parent=styles["Title"], fontName="STSong-Light", fontSize=18, leading=25, textColor=colors.HexColor("#172033"), alignment=TA_CENTER)
    heading = ParagraphStyle("HeadingCN", parent=styles["Heading2"], fontName="STSong-Light", fontSize=13, leading=19, textColor=colors.HexColor("#3157d5"), spaceBefore=8, spaceAfter=7)
    body = ParagraphStyle("BodyCN", parent=styles["BodyText"], fontName="STSong-Light", fontSize=10.5, leading=18, textColor=colors.HexColor("#172033"), wordWrap="CJK")
    meta = ParagraphStyle("MetaCN", parent=body, fontSize=8.5, leading=14, textColor=colors.HexColor("#647087"))

    def answer_box() -> Table:
        box = Table([[Paragraph("作答区<br/><br/><br/><br/>", meta)]], colWidths=[174 * mm], rowHeights=[32 * mm])
        box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#b7c0ce")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafbfc")),
        ]))
        return box

    story: list[Any] = []
    if logo_path and logo_path.is_file():
        logo = Image(str(logo_path), width=13 * mm, height=13 * mm)
        subtitle = "复习卷（含解析）" if is_review else "巩固自测卷"
        header = Table([[logo, Paragraph(f"李兆霖数学错题本<br/><font size='8'>{subtitle}</font>", body)]], colWidths=[17 * mm, 80 * mm])
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        story.extend([header, Spacer(1, 5 * mm)])
    document_title = "复习卷（含解析）" if is_review else "巩固自测卷"
    story.extend([Paragraph(document_title, title), Spacer(1, 5 * mm)])
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[str(item["error_id"])].append(item)
    for group_no, (error_id, group) in enumerate(groups.items(), 1):
        original = next(item for item in group if item["kind"] == "original")
        story.extend([
            Paragraph(f"{group_no}. 原错题", heading),
            Paragraph(_text(original["stem_text"]), body),
        ])
        if is_review:
            knowledge = "、".join(str(item) for item in original.get("knowledge_points", []) if str(item).strip()) or "待整理"
            story.extend([
                Spacer(1, 2 * mm),
                Paragraph(
                    "<b>错因：</b>" + escape(str(original.get("cause_label") or "待整理"))
                    + "<br/><b>判断依据：</b>" + escape(str(original.get("cause_evidence") or original.get("first_error") or "待整理"))
                    + "<br/><b>知识点：</b>" + escape(knowledge),
                    meta,
                ),
                Paragraph("<b>学生作答：</b>" + _text(original.get("answer_text") or "未填写"), body),
                Paragraph("<b>正确过程：</b>" + _text(original.get("correct_solution") or "待整理"), body),
                Paragraph("<b>最终答案：</b>" + _text(original.get("final_answer") or "待整理"), body),
                Paragraph("<b>防错提示：</b>" + _text(original.get("prevention_cue") or "待整理"), meta),
                Spacer(1, 5 * mm),
            ])
        else:
            story.extend([Spacer(1, 3 * mm), answer_box(), Spacer(1, 5 * mm)])
        recommendations = [item for item in group if item["kind"] == "recommendation"]
        if not recommendations:
            if is_review:
                story.append(Paragraph("暂无符合验证与授权要求的推荐题，本次只重做原题。", meta))
        for index, item in enumerate(recommendations, 1):
            difficulty = "未标注" if item.get("difficulty") is None else str(item["difficulty"])
            story.extend([Paragraph(f"同类型推荐题 {index}", heading)])
            if is_review:
                story.append(Paragraph(f"题库编号：{escape(str(item['question_id']))}　难度：{escape(difficulty)}<br/>推荐原因：{escape(str(item['reason']))}<br/>来源：{escape(str(item['source_title']))}", meta))
            story.append(Paragraph(_text(item["stem_text"]), body))
            if is_review:
                story.append(Spacer(1, 5 * mm))
            else:
                story.extend([Spacer(1, 3 * mm), answer_box(), Spacer(1, 5 * mm)])
    if is_review:
        story.extend([PageBreak(), Paragraph("参考答案", title), Spacer(1, 5 * mm)])
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
