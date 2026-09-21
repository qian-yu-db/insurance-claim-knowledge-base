"""Image artifacts: damage photos (format-converted), scanned docs, held-in-hand photo, signature GIF."""

from __future__ import annotations

import io
import random
from pathlib import Path

import pillow_heif
import pymupdf
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import claims
from docpdf import FONT, register_unicode_fonts

pillow_heif.register_heif_opener()


def pick_damage_images(plan: dict[str, int]) -> list[Path]:
    """Deterministically pick source damage PNGs by severity bucket (files like '34-major.png')."""
    out = []
    for sev, n in plan.items():
        pool = sorted(claims.DAMAGE_IMG_DIR.glob(f"*-{sev}.png"))
        rng = random.Random(f"{sev}-{n}")
        out.extend(rng.sample(pool, min(n, len(pool))))
    return out


def _font(size: int):
    for name in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def place_damage_photos(cid: str, c: dict, srcs: list[Path], out_dir: Path) -> list[dict]:
    """Copy/convert damage photos into the claim, spreading formats (jpg, png, heic, tiff)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # iPhone-style + mixed formats to match the described real-world spread.
    fmt_cycle = ["jpg", "heic", "png", "tiff", "jpg"]
    results = []
    for i, src in enumerate(srcs):
        fmt = fmt_cycle[i % len(fmt_cycle)]
        img = Image.open(src).convert("RGB")
        # downscale a touch to feel phone-captured
        img.thumbnail((1600, 1600))
        stem = f"IMG_{4200 + i}_{cid}"
        dest = out_dir / f"{stem}.{fmt}"
        if fmt == "heic":
            img.save(dest, format="HEIF", quality=90)
        elif fmt == "tiff":
            img.save(dest, format="TIFF")
        elif fmt == "png":
            img.save(dest, format="PNG")
        else:
            img.save(dest, format="JPEG", quality=88)
        results.append({"path": dest, "doc_type": "damage_photo", "format": fmt, "severity": c["severity"]})
    return results


def _one_page_doc_image(title: str, lines: list[tuple[str, str]], dpi: int = 150) -> Image.Image:
    """Render a quick one-page form to a PIL image (used as the source for scans / held photos)."""
    pdf = FPDF(format="Letter", unit="mm")
    register_unicode_fonts(pdf)
    pdf.set_auto_page_break(True, 18)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    pdf.set_font(FONT, "B", 14)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font(FONT, "", 11)
    for k, v in lines:
        pdf.set_font(FONT, "B", 11)
        pdf.cell(60, 8, k)
        pdf.set_font(FONT, "", 11)
        pdf.multi_cell(0, 8, v, new_x="LMARGIN", new_y="NEXT")
    data = pdf.output()
    doc = pymupdf.open(stream=bytes(data), filetype="pdf")
    pix = doc[0].get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    doc.close()
    return img


def scanned_doc(cid: str, c: dict, out_dir: Path) -> dict:
    """A faxed/scanned release-form page: skew + grayscale + noise, saved as TIFF."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _one_page_doc_image("RELEASE OF ALL CLAIMS", [
        ("Claim Number:", cid),
        ("Carrier:", c["carrier"]),
        ("Insured:", c["insured"]["name"]),
        ("Date:", c["loss"]["date"]),
        ("Notice:", "Received by fax. Signature block on page 2."),
    ])
    g = base.convert("L").rotate(-1.4, expand=True, fillcolor=255)
    # add speckle noise
    px = g.load()
    rng = random.Random(cid)
    for _ in range(int(g.width * g.height * 0.004)):
        x, y = rng.randrange(g.width), rng.randrange(g.height)
        px[x, y] = rng.choice([0, 60, 200])
    g = g.filter(ImageFilter.GaussianBlur(0.4))
    dest = out_dir / f"{cid}_release_scanned.tiff"
    g.save(dest, format="TIFF")
    return {"path": dest, "doc_type": "scanned_document", "format": "tiff"}


def medical_scan_pngs(cid: str, c: dict, out_dir: Path, n: int = 3) -> list[Path]:
    """Grayscale, degraded 'scanned/faxed' medical pages for embedding in the long PDF bundle."""
    out_dir.mkdir(parents=True, exist_ok=True)
    med = c.get("medical", {})
    paths = []
    for i in range(n):
        img = _one_page_doc_image(f"IMAGING REPORT — {med.get('provider', '')}", [
            ("Patient:", c.get("claimant", {}).get("name", "")),
            ("Study:", ["MRI Cervical Spine", "MRI Lumbar Spine", "CT Head"][i % 3]),
            ("Findings:", med.get("diagnoses", ["n/a"])[i % max(len(med.get("diagnoses", [1])), 1)]),
            ("Impression:", "Findings consistent with acute traumatic injury."),
            ("Note:", "Faxed copy — scan quality reduced."),
        ], dpi=130)
        g = img.convert("L").rotate(-0.8, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.4))
        dest = out_dir / f"{cid}_scan_page_{i + 1}.png"
        g.save(dest, format="PNG")
        paths.append(dest)
    return paths


def held_in_hand(cid: str, c: dict, out_dir: Path) -> dict:
    """Simulate a phone photo of a document held in hand: rotated doc on a surface with shadow + finger tabs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = _one_page_doc_image("PROOF OF INSURANCE CARD", [
        ("Carrier:", c["carrier"]),
        ("Policy:", c["policy_no"]),
        ("Insured:", c["insured"]["name"]),
        ("Vehicle:", f"{c['vehicle']['year']} {c['vehicle']['make']} {c['vehicle']['model']}"),
        ("VIN:", c["vehicle"]["vin"]),
    ], dpi=120)
    doc.thumbnail((900, 1200))
    bg = Image.new("RGB", (int(doc.width * 1.5), int(doc.height * 1.35)), (54, 52, 50))  # dark table
    # subtle surface gradient
    grad = Image.new("L", bg.size)
    gd = ImageDraw.Draw(grad)
    for y in range(bg.height):
        gd.line([(0, y), (bg.width, y)], fill=int(30 + 40 * y / bg.height))
    bg = Image.composite(Image.new("RGB", bg.size, (90, 86, 80)), bg, grad)
    doc_r = doc.rotate(-6, expand=True, fillcolor=(255, 255, 255))
    ox, oy = (bg.width - doc_r.width) // 2, (bg.height - doc_r.height) // 2
    shadow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle([ox + 12, oy + 16, ox + doc_r.width + 12, oy + doc_r.height + 16], fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    bg = Image.alpha_composite(bg.convert("RGBA"), shadow).convert("RGB")
    bg.paste(doc_r, (ox, oy))
    # finger/thumb tabs at bottom edge to imply "held"
    d = ImageDraw.Draw(bg)
    for fx in (ox + int(doc_r.width * 0.28), ox + int(doc_r.width * 0.62)):
        fy = oy + doc_r.height - 20
        d.ellipse([fx, fy, fx + 70, fy + 120], fill=(196, 154, 122))
    dest = out_dir / f"IMG_{cid}_doc_in_hand.heic"
    bg.save(dest, format="HEIF", quality=88)
    return {"path": dest, "doc_type": "document_photo_in_hand", "format": "heic"}


def signature_gif(cid: str, c: dict, out_dir: Path) -> dict:
    """A tiny email-signature graphic (embedded in an .eml), saved as GIF."""
    out_dir.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (420, 90), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 8, 90], fill=(20, 40, 80))
    d.text((22, 14), c["carrier"], font=_font(20), fill=(20, 40, 80))
    d.text((22, 46), f"{c['adjuster']['name']}  |  Claims  |  {c['adjuster']['phone']}", font=_font(13), fill=(80, 80, 80))
    dest = out_dir / f"{cid}_signature.gif"
    img.save(dest, format="GIF")
    return {"path": dest, "doc_type": "email_signature_graphic", "format": "gif"}
