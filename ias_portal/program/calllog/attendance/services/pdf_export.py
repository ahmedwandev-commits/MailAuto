"""PDF export of any report. Optional: needs ``reportlab``, ``arabic-reshaper`` and ``python-bidi`` (requirements-pdf.txt) and a TrueType
font with Arabic glyphs. Without them ``available()`` is False and the UI falls back to the browser's Print / Save as PDF."""
import io
import os
import re

_AR = re.compile(r"[؀-ۿ]")
PDF_SKIP_COLUMNS = {"Data notes"}
FONT_CANDIDATES = ("tahoma.ttf", "arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "NotoSansArabic-Regular.ttf")
FONT_DIRS = ("{WINDIR}/Fonts", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/noto", "/Library/Fonts", "/System/Library/Fonts/Supplemental")


def find_font(configured=None):
    """Configured path (config ``pdf_font`` / env ATTENDANCE_PDF_FONT) first, then well-known system font folders."""
    cands = [configured or os.environ.get("ATTENDANCE_PDF_FONT")]
    for d in FONT_DIRS:
        d = d.replace("{WINDIR}", os.environ.get("WINDIR", os.environ.get("SystemRoot", "")))
        cands += [os.path.join(d, f) for f in FONT_CANDIDATES]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def available(configured=None):
    try:
        import arabic_reshaper, bidi, reportlab  # noqa: F401,E401  (availability probe)
        import PIL  # noqa: F401  (reportlab needs Pillow to draw a page: without it a PDF would be an error page)
    except ImportError:
        return False
    return find_font(configured) is not None


def _shape(text):
    s = "" if text is None else str(text)
    if not _AR.search(s):
        return s
    import arabic_reshaper
    from bidi.algorithm import get_display
    return get_display(arabic_reshaper.reshape(s))


def render(report, d1, d2, subtitle="", configured_font=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A3, A4, landscape
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle

    path = find_font(configured_font)
    if "AttFont" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("AttFont", path))
    # long free-text columns are left to the Excel export; everything else must fit on the page
    keep = [i for i, c in enumerate(report["columns"]) if c not in PDF_SKIP_COLUMNS]
    columns = [report["columns"][i] for i in keep]
    rows = [[row[i] for i in keep] for row in report["rows"]]
    if report.get("totals"):                     # the same totals line as the page and the Excel file
        rows.append([report["totals"][i] for i in keep])
    ncol = len(columns)
    page = landscape(A3 if ncol > 14 else A4)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=24, rightMargin=24, topMargin=30, bottomMargin=30,
                            title=report["title"], author="Attendance module")
    avail_w = page[0] - 48
    size = 8.5
    head_style = ParagraphStyle("h", fontName="AttFont", fontSize=15, leading=18, textColor=colors.HexColor("#006a4e"))
    sub_style = ParagraphStyle("s", fontName="AttFont", fontSize=8.5, leading=11, textColor=colors.HexColor("#52514e"))

    def width(s, sz=None):
        return pdfmetrics.stringWidth(s, "AttFont", sz or size)

    # column widths: header and content, capped; scaled to the page only when they really do not fit
    def natural(i):
        content = max((width(_shape(r[i])) for r in rows[:300] if isinstance(r[i], str)), default=0)
        numbers = max((width(f"{r[i]:.1f}") for r in rows[:300] if isinstance(r[i], (int, float))), default=0)
        return min(max(content, numbers, width(_shape(columns[i]), size - 1)) + 10, 250)
    widths = [natural(i) for i in range(ncol)]
    if sum(widths) > avail_w:
        scale = avail_w / sum(widths)
        size = max(5.5, size * max(scale, 0.7))
        widths = [natural(i) * (avail_w / sum(natural(j) for j in range(ncol))) for i in range(ncol)]

    def cell(v, w):
        if v is None:
            return "-"
        if isinstance(v, float):
            s = f"{v:.1f}".rstrip("0").rstrip(".")
            return "0" if s in ("-0", "") else s
        s = str(v)
        if width(_shape(s)) <= w - 8:           # cut the LOGICAL text, then shape, so Arabic stays correct
            return _shape(s)
        while s and width(_shape(s + "..")) > w - 8:
            s = s[:-1]
        return _shape(s + "..")

    data = [[_shape(c) for c in columns]] + [[cell(v, widths[i]) for i, v in enumerate(row)] for row in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "AttFont"), ("FONTSIZE", (0, 0), (-1, -1), size),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#006a4e")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f3f0")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c3c2b7")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))
    story = [Paragraph(_shape(report["title"]), head_style), Paragraph(f"Period {d1} to {d2}{(' - ' + subtitle) if subtitle else ''}", sub_style),
             Spacer(1, 8), t]
    for n in report["notes"]:
        story += [Spacer(1, 6), Paragraph(_shape(n), sub_style)]

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("AttFont", 7)
        canvas.setFillColor(colors.HexColor("#898781"))
        canvas.drawString(24, 16, "Attendance & Workforce Management")
        canvas.drawRightString(page[0] - 24, 16, f"Page {doc_.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
