"""Thin fpdf2 layout helpers used by every PDF generator, plus raster utilities."""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from fpdf import FPDF
from PIL import Image

_FONT_DIR = "/System/Library/Fonts/Supplemental"
FONT = "Body"


def register_unicode_fonts(pdf: FPDF, family: str = FONT):
    """Register a Unicode TTF (Arial) so em-dashes, curly quotes, etc. render. Returns family name."""
    pdf.add_font(family, "", f"{_FONT_DIR}/Arial.ttf")
    pdf.add_font(family, "B", f"{_FONT_DIR}/Arial Bold.ttf")
    pdf.add_font(family, "I", f"{_FONT_DIR}/Arial Italic.ttf")
    return family


class ClaimPDF(FPDF):
    """FPDF with insurance-document layout primitives (header band, KV tables, sections)."""

    def __init__(self, carrier: str, doc_title: str):
        super().__init__(orientation="P", unit="mm", format="Letter")
        self.carrier = carrier
        self.doc_title = doc_title
        register_unicode_fonts(self)
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 16, 18)
        self.add_page()

    def header(self):
        self.set_font(FONT, "B", 15)
        self.set_text_color(20, 40, 80)
        self.cell(0, 8, self.carrier, new_x="LMARGIN", new_y="NEXT")
        self.set_font(FONT, "", 10)
        self.set_text_color(90, 90, 90)
        self.cell(0, 5, self.doc_title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(20, 40, 80)
        self.set_line_width(0.6)
        y = self.get_y() + 1
        self.line(18, y, self.w - 18, y)
        self.ln(5)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-14)
        self.set_font(FONT, "I", 7)
        self.set_text_color(140, 140, 140)
        self.cell(0, 5, f"CONFIDENTIAL — Synthetic data for testing.  Page {self.page_no()}", align="C")

    def h2(self, text: str):
        self.ln(2)
        self.set_font(FONT, "B", 11)
        self.set_text_color(20, 40, 80)
        self.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def kv_table(self, rows: list[tuple[str, str]], label_w: int = 55):
        self.set_font(FONT, "", 10)
        for k, v in rows:
            y0 = self.get_y()
            self.set_font(FONT, "B", 10)
            self.multi_cell(label_w, 6, str(k), border=0, new_x="RIGHT", new_y="TOP", max_line_height=5)
            self.set_xy(18 + label_w, y0)
            self.set_font(FONT, "", 10)
            self.multi_cell(self.w - 36 - label_w, 6, str(v), border=0, new_x="LMARGIN", new_y="NEXT", max_line_height=5)

    def para(self, text: str, size: int = 10):
        self.set_font(FONT, "", size)
        self.multi_cell(0, 5.2, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def money_table(self, header: list[str], rows: list[list[str]], widths: list[int]):
        self.set_font(FONT, "B", 9)
        self.set_fill_color(230, 235, 245)
        for w, h in zip(widths, header):
            self.cell(w, 7, h, border=1, fill=True, align="L")
        self.ln()
        self.set_font(FONT, "", 9)
        for row in rows:
            for w, c in zip(widths, row):
                self.cell(w, 6.5, str(c), border=1, align="L")
            self.ln()
        self.ln(2)

    def image_row(self, image_paths: list[Path], captions: list[str] | None = None, h: int = 48):
        captions = captions or [""] * len(image_paths)
        x = 18
        gap = 5
        n = len(image_paths)
        w = (self.w - 36 - gap * (n - 1)) / max(n, 1)
        y0 = self.get_y()
        for p, cap in zip(image_paths, captions):
            self.image(str(p), x=x, y=y0, w=w, h=h)
            self.set_xy(x, y0 + h + 1)
            self.set_font(FONT, "I", 7)
            self.multi_cell(w, 3.5, cap, align="C")
            x += w + gap
        self.set_y(y0 + h + 8)


def render_pages_to_image(pdf_path: Path, page_indices: list[int], dpi: int = 150) -> Image.Image:
    """Rasterize selected PDF pages and stack them vertically into one PIL image."""
    doc = pymupdf.open(pdf_path)
    imgs = []
    for i in page_indices:
        pix = doc[i].get_pixmap(dpi=dpi)
        imgs.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
    doc.close()
    w = max(im.width for im in imgs)
    total_h = sum(im.height for im in imgs)
    canvas = Image.new("RGB", (w, total_h), "white")
    y = 0
    for im in imgs:
        canvas.paste(im, (0, y))
        y += im.height
    return canvas
