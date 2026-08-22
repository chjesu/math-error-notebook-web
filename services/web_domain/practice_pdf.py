"""A4 practice sheets. Answers are included only after an explicit request."""

from __future__ import annotations

from collections import defaultdict
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any


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
    body = ParagraphStyle("BodyCN", parent=styles["BodyText"], fontName="STSong-Light", fontSize=10.5, leading=18, textColor=colors.HexColor("#172033"), wordWrap="CJK")
    meta = ParagraphStyle("MetaCN", parent=body, fontSize=8.5, leading=14, textColor=colors.HexColor("#647087"))
    story: list[Any] = []
    if logo_path and logo_path.is_file():
        logo = Image(str(logo_path), width=13 * mm, height=13 * mm)
        header = Table([[logo, Paragraph("李兆霖数学错题本<br/><font size='8'>今日复习练习</font>", body)]], colWidths=[17 * mm, 80 * mm])
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        story.extend([header, Spacer(1, 5 * mm)])
    story.extend([Paragraph("错题复习练习", title), Spacer(1, 5 * mm)])
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[str(item["error_id"])].append(item)
    for group_no, (error_id, group) in enumerate(groups.items(), 1):
        original = next(item for item in group if item["kind"] == "original")
        story.append(KeepTogether([
            Paragraph(f"{group_no}. 错题编号 {escape(error_id[:8])}", heading),
            Paragraph("错题回顾", meta),
            Paragraph(_text(original["stem_text"]), body),
            Spacer(1, 5 * mm),
        ]))
        recommendations = [item for item in group if item["kind"] == "recommendation"]
        if not recommendations:
            story.append(Paragraph("暂无符合验证与授权要求的推荐题，本次只重做原题。", meta))
        for index, item in enumerate(recommendations, 1):
            difficulty = "未标注" if item.get("difficulty") is None else str(item["difficulty"])
            story.append(KeepTogether([
                Paragraph(f"同类型推荐题 {index}", heading),
                Paragraph(f"题库编号：{escape(str(item['question_id']))}　难度：{escape(difficulty)}<br/>推荐原因：{escape(str(item['reason']))}<br/>来源：{escape(str(item['source_title']))}", meta),
                Paragraph(_text(item["stem_text"]), body),
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
    return escape(str(value)).replace("\n", "<br/>")
