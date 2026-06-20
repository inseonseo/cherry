"""
report.py
역할: 점검 결과를 받아서 PDF 보고서 생성 (reportlab)

한국어 폰트: fonts/ 폴더의 NanumGothic 3종 사용
없으면 Helvetica fallback (한글 깨짐)
"""

import io
import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# ── 한국어 폰트 등록 ────────────────────────────────────────
_FONTS_DIR = os.environ.get("FONTS_DIR", os.path.join(os.path.dirname(__file__), "fonts"))

try:
    pdfmetrics.registerFont(TTFont("NanumGothic",          os.path.join(_FONTS_DIR, "NanumGothic.ttf")))
    pdfmetrics.registerFont(TTFont("NanumGothicLight",     os.path.join(_FONTS_DIR, "NanumGothicLight.ttf")))
    pdfmetrics.registerFont(TTFont("NanumGothicExtraBold", os.path.join(_FONTS_DIR, "NanumGothicExtraBold.ttf")))
    _FONT_NAME  = "NanumGothic"
    _LIGHT_NAME = "NanumGothicLight"
    _BOLD_NAME  = "NanumGothicExtraBold"
except Exception:
    _FONT_NAME  = "Helvetica"
    _LIGHT_NAME = "Helvetica"
    _BOLD_NAME  = "Helvetica-Bold"

# ── 스타일 ──────────────────────────────────────────────────
def _styles():
    return {
        "title": ParagraphStyle("title", fontName=_BOLD_NAME, fontSize=16,
                                 alignment=TA_CENTER, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontName=_LIGHT_NAME, fontSize=10,
                                    alignment=TA_CENTER, textColor=colors.grey, spaceAfter=12),
        "section": ParagraphStyle("section", fontName=_BOLD_NAME, fontSize=12,
                                   spaceBefore=12, spaceAfter=4),
        "body": ParagraphStyle("body", fontName=_FONT_NAME, fontSize=10,
                                leading=16, spaceAfter=2),
        "ok": ParagraphStyle("ok", fontName=_FONT_NAME, fontSize=10,
                              textColor=colors.HexColor("#1D9E75"), leading=16),
        "error": ParagraphStyle("error", fontName=_FONT_NAME, fontSize=10,
                                 textColor=colors.HexColor("#D94040"), leading=16),
    }


def generate_report(누락서류, 전체_결과, 적용규칙, 서류별_페이지,
                    checklist, 상품유형, 고객군, 고령투자자, 보완서류=None) -> bytes:
    """점검 결과를 PDF 바이트로 반환"""

    if 보완서류 is None:
        보완서류 = []

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm
    )

    s = _styles()
    story = []

    # ── 제목 ────────────────────────────────────────────────
    story.append(Paragraph("서류 점검 결과 보고서", s["title"]))
    story.append(Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M"), s["subtitle"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 6))

    # ── 고객 정보 ────────────────────────────────────────────
    story.append(Paragraph("고객 정보", s["section"]))
    info_data = [
        ["상품유형", 상품유형, "고객군", 고객군],
        ["고령투자자", "예" if 고령투자자 else "아니오",
         "적용규칙", 적용규칙 if 적용규칙 else "해당 없음"],
    ]
    info_table = Table(info_data, colWidths=[35*mm, 55*mm, 35*mm, 55*mm])
    info_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _FONT_NAME),
        ("FONTNAME", (0, 0), (0, -1), _BOLD_NAME),
        ("FONTNAME", (2, 0), (2, -1), _BOLD_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F0F0F0")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F0F0F0")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 8))

    # ── 누락 서류 ────────────────────────────────────────────
    story.append(Paragraph("필요 서류 확인", s["section"]))
    if 누락서류:
        for s_id in 누락서류:
            서류명 = checklist.get(s_id, {}).get("서류명", s_id)
            story.append(Paragraph(f"  ✗  {서류명} — 누락", s["error"]))
    else:
        story.append(Paragraph("  ✓  필수 서류 모두 제출됨", s["ok"]))
    story.append(Spacer(1, 8))

    # ── 보완 서류 (고령투자자) ────────────────────────────────
    if 보완서류:
        story.append(Paragraph("보완 서류 (고령투자자 보호)", s["section"]))
        for s_id in 보완서류:
            서류명 = checklist.get(s_id, {}).get("서류명", s_id)
            story.append(Paragraph(f"  ⚠  {서류명} — 필수", s["error"]))
        story.append(Spacer(1, 8))

    # ── 서류별 점검 결과 ─────────────────────────────────────
    story.append(Paragraph("서류별 점검 결과", s["section"]))
    story.append(HRFlowable(width="100%", thickness=0.3, color=colors.lightgrey))

    for 서류ID, 결과 in 전체_결과.items():
        # 미분류 항목 제외
        if 서류ID == "unknown":
            continue
        
        서류명 = checklist.get(서류ID, {}).get("서류명", 서류ID)
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"▶ {서류명}", s["section"]))

        errors = 결과.get("errors", [])
        if 결과.get("pass", False):
            story.append(Paragraph("  ✓  이상 없음", s["ok"]))
        else:
            for err in errors:
                항목 = err.get("항목", "")
                오류 = err.get("오류", "")
                story.append(Paragraph(f"  ✗  [{항목}] {오류}", s["error"]))

        if not errors and not 결과.get("pass"):
            story.append(Paragraph("  (점검 항목 없음)", s["body"]))

    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))

    # ── 최종 판정 ────────────────────────────────────────────
    총_오류 = len(누락서류) + len(보완서류) + sum(len(r.get("errors", [])) for r in 전체_결과.values() if r != 전체_결과.get("unknown"))

    if 총_오류 > 0:
        verdict = f"보완 필요  (오류 {총_오류}건)"
        verdict_color = colors.HexColor("#D94040")
    else:
        verdict = "적합"
        verdict_color = colors.HexColor("#1D9E75")

    verdict_style = ParagraphStyle(
        "verdict", fontName=_BOLD_NAME, fontSize=13,
        alignment=TA_CENTER, textColor=verdict_color, spaceBefore=8
    )
    story.append(Paragraph(f"최종 판정: {verdict}", verdict_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()
