"""Export a Markdown bid draft to PDF (reportlab, no external binaries)."""

from __future__ import annotations

import io
import re
from html import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_SEPARATOR = re.compile(r":?-{2,}:?")


def inline(text: str) -> str:
    """Markdown inline (bold/italic) -> reportlab mini-HTML, with XML escaping."""
    s = escape(text.rstrip(), quote=False).replace("\\|", "|")
    s = _BOLD.sub(r"<b>\1</b>", s)
    return _ITAL.sub(r"<i>\1</i>", s)


def split_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


def markdown_to_flowables(md: str) -> list[Flowable]:
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=13)
    cell = ParagraphStyle("cell", parent=body, fontSize=8.5, leading=11)
    heads = {1: styles["Title"], 2: styles["Heading2"], 3: styles["Heading3"]}
    out: list[Flowable] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            out.append(Spacer(1, 4))
            i += 1
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)", stripped)
        if heading:
            out.append(Paragraph(inline(heading.group(2)), heads[len(heading.group(1))]))
            i += 1
            continue
        if stripped.startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = split_row(lines[i])
                if not all(_SEPARATOR.fullmatch(c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                data = [[Paragraph(inline(c), cell) for c in r + [""] * (width - len(r))] for r in rows]
                table = Table(data, repeatRows=1, hAlign="LEFT", colWidths=[170 * mm / width] * width)
                table.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]))
                out.append(table)
            continue
        if re.match(r"^[-*]\s+", stripped):
            out.append(Paragraph(inline(re.sub(r"^[-*]\s+", "", stripped)), body, bulletText="•"))
        else:
            out.append(Paragraph(inline(stripped), body))
        i += 1
    return out


def markdown_to_pdf(md: str, title: str = "Bid draft") -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=title, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    doc.build(markdown_to_flowables(md) or [Spacer(1, 1)])
    return buf.getvalue()
