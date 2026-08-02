"""Minimal markdown -> PDF renderer using reportlab (already a dependency
via catalyst_brief.py -- no new packages, per the box's no-new-deps
footing).

Exists because heff reads PDFs, not .md files. Handles exactly what this
project's reports use: ATX headings, paragraphs, bullet lists, pipe tables,
fenced code, horizontal rules, and inline **bold** / `code`.

Usage: python md_to_pdf.py IN.md OUT.pdf "Title"
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

SS = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=SS["BodyText"], fontSize=9, leading=12.5,
                      spaceAfter=5)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=7.6, leading=9.6, spaceAfter=0)
CELLH = ParagraphStyle("cellh", parent=CELL, fontName="Helvetica-Bold")
CODE = ParagraphStyle("code", parent=BODY, fontName="Courier", fontSize=7.6,
                      leading=9.5, backColor=colors.HexColor("#f2f2f2"),
                      borderPadding=4, spaceBefore=4, spaceAfter=6)
H = {
    1: ParagraphStyle("h1", parent=SS["Heading1"], fontSize=16, spaceBefore=6, spaceAfter=8),
    2: ParagraphStyle("h2", parent=SS["Heading2"], fontSize=12.5, spaceBefore=12, spaceAfter=5),
    3: ParagraphStyle("h3", parent=SS["Heading3"], fontSize=10.5, spaceBefore=9, spaceAfter=4),
    4: ParagraphStyle("h4", parent=SS["Heading4"], fontSize=9.5, spaceBefore=7, spaceAfter=3),
}


def inline(text: str) -> str:
    """Escape, then re-apply the small inline subset reportlab understands."""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', t)
    t = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", t)
    return t


def is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|")


def split_row(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def build(md: str):
    flow = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()

        if not s:
            i += 1
            continue

        if s.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            flow.append(Paragraph("<br/>".join(buf) or "&nbsp;", CODE))
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            flow.append(Spacer(1, 4))
            flow.append(HRFlowable(width="100%", thickness=0.6,
                                   color=colors.HexColor("#bbbbbb")))
            flow.append(Spacer(1, 6))
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl = min(len(m.group(1)), 4)
            flow.append(Paragraph(inline(m.group(2)), H[lvl]))
            i += 1
            continue

        if is_table_row(s):
            rows = []
            while i < len(lines) and is_table_row(lines[i]):
                rows.append(split_row(lines[i]))
                i += 1
            # Drop the |---|---| separator row.
            body = [r for r in rows
                    if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in r)]
            if not body:
                continue
            ncol = max(len(r) for r in body)
            data = []
            for ri, r in enumerate(body):
                r = list(r) + [""] * (ncol - len(r))
                st = CELLH if ri == 0 else CELL
                data.append([Paragraph(inline(c), st) for c in r])
            avail = 7.0 * inch
            t = Table(data, colWidths=[avail / ncol] * ncol, repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#f7f7f7")]),
            ]))
            flow.append(Spacer(1, 3))
            flow.append(t)
            flow.append(Spacer(1, 7))
            continue

        m = re.match(r"^[-*+]\s+(.*)$", s)
        if m:
            flow.append(Paragraph(inline(m.group(1)), BODY, bulletText="•"))
            i += 1
            continue

        m = re.match(r"^(\d+)\.\s+(.*)$", s)
        if m:
            flow.append(Paragraph(inline(m.group(2)), BODY, bulletText=f"{m.group(1)}."))
            i += 1
            continue

        # Paragraph: gather until blank / structural line.
        buf = [s]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith("#") or is_table_row(nxt)
                    or nxt.startswith("```") or re.match(r"^[-*+]\s+", nxt)
                    or re.match(r"^\d+\.\s+", nxt)
                    or re.match(r"^(-{3,}|\*{3,}|_{3,})$", nxt)):
                break
            buf.append(nxt)
            i += 1
        flow.append(Paragraph(inline(" ".join(buf)), BODY))
    return flow


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    title = sys.argv[3] if len(sys.argv) > 3 else src.stem
    doc = SimpleDocTemplate(
        str(dst), pagesize=letter, title=title, author="BOT_NEXUS",
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.65 * inch, bottomMargin=0.65 * inch)
    doc.build(build(src.read_text(encoding="utf-8")))
    print(f"wrote {dst} ({dst.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
