"""Email correspondence as .eml (RFC-822): threads, attachments, inline signature graphic.

Encodes the flagged real-world scenario: some email attachments ALSO exist as files in
the claim folder, and some exist ONLY inside the email (never in the folder).
"""

from __future__ import annotations

import mimetypes
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from fpdf import FPDF

from docpdf import FONT, register_unicode_fonts


def _attach(msg: EmailMessage, path: Path):
    ctype, _ = mimetypes.guess_type(path.name)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)


def _mini_pdf_bytes(title: str, lines: list[str]) -> bytes:
    pdf = FPDF(format="Letter", unit="mm")
    register_unicode_fonts(pdf)
    pdf.add_page()
    pdf.set_font(FONT, "B", 14)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FONT, "", 11)
    for ln in lines:
        pdf.multi_cell(0, 7, ln, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _write_eml(dest: Path, *, subject, sender, to, date, body_text, sig_gif: Path | None,
               carrier: str, attachments: list[Path] | None = None,
               inline_attach: list[tuple[str, bytes, str]] | None = None) -> Path:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = date
    msg["Message-ID"] = make_msgid(domain="claims.example.com")
    msg.set_content(body_text)

    if sig_gif is not None:
        cid = make_msgid(domain="sig.example.com")[1:-1]
        html = (f"<html><body><p>{body_text.replace(chr(10), '<br>')}</p>"
                f'<img src="cid:{cid}" alt="signature"><br>'
                f"<span style='color:#888;font-size:11px'>{carrier} — CONFIDENTIAL</span></body></html>")
        msg.add_alternative(html, subtype="html")
        img_part = msg.get_payload()[-1]
        img_part.add_related(sig_gif.read_bytes(), maintype="image", subtype="gif", cid=f"<{cid}>",
                             filename=sig_gif.name)
    for p in (attachments or []):
        _attach(msg, p)
    for fname, data, ctype in (inline_attach or []):
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)

    dest.write_bytes(bytes(msg))
    return dest


def build_emails(cid: str, c: dict, corr_dir: Path, artifacts: dict, sig_gif: Path) -> list[dict]:
    corr_dir.mkdir(parents=True, exist_ok=True)
    ins, adj = c["insured"], c["adjuster"]
    entries: list[dict] = []

    def rec(path, category, **extra):
        entries.append({"path": path, "doc_type": "email", "email_category": category, "format": "eml", **extra})

    # ----- always: general correspondence acknowledging the claim -----
    p = _write_eml(
        corr_dir / f"{cid}_01_general_correspondence.eml",
        subject=f"[{cid}] Claim received — next steps",
        sender=f"{adj['name']} <{adj['email']}>", to=f"{ins['name']} <{ins['email']}>",
        date=formatdate(), body_text=(
            f"Dear {ins['name'].split()[0]},\n\nThank you for reporting your claim ({cid}). I have been assigned as your "
            f"adjuster. We have your {c['vehicle']['year']} {c['vehicle']['make']} {c['vehicle']['model']} on file and will "
            f"follow up regarding inspection and next steps.\n\nRegards,\n{adj['name']}\n{c['carrier']} Claims"),
        sig_gif=sig_gif, carrier=c["carrier"])
    rec(p, "general_correspondence")

    if c["complexity"] == "fast-track":
        # estimate transmittal, attaching the repair estimate that ALSO lives in the folder
        p = _write_eml(
            corr_dir / f"{cid}_02_estimate_and_settlement.eml",
            subject=f"[{cid}] Repair estimate & settlement",
            sender=f"{adj['name']} <{adj['email']}>", to=f"{ins['name']} <{ins['email']}>",
            date=formatdate(), body_text=(
                f"Hi {ins['name'].split()[0]},\n\nAttached is the approved repair estimate. Net payable after your "
                f"${c['estimate']['deductible']:.0f} deductible is ${c['estimate']['net']:.2f}. Payment has been issued.\n\n"
                f"Best,\n{adj['name']}"),
            sig_gif=sig_gif, carrier=c["carrier"],
            attachments=[artifacts["repair_estimate_pdf"]])
        rec(p, "negotiation_settlement", attachment_in_folder=[artifacts["repair_estimate_pdf"].name])

    elif c["complexity"] == "litigation-bodily-injury":
        dc, cl = c["defense_counsel"], c["claimant"]
        # 1) defense counsel assignment
        p = _write_eml(
            corr_dir / f"{cid}_02_defense_counsel.eml",
            subject=f"[{cid}] Defense counsel assignment — {cl['name']} v. {ins['name']}",
            sender=f"{adj['name']} <{adj['email']}>", to=f"{dc['name']} <{dc['email']}>",
            date=formatdate(), body_text=(
                f"Counsel,\n\nWe are assigning the defense of the above matter to {dc['firm']}. Suit has been filed in Collin "
                f"County. Reserves are set at ${c['reserves']['bi']:.0f} BI / ${c['reserves']['pd']:.0f} PD. The combined "
                f"medical/legal file is attached for your review.\n\n{adj['name']}\n{c['carrier']}"),
            sig_gif=sig_gif, carrier=c["carrier"],
            attachments=[artifacts["medical_legal_pdf"]])
        rec(p, "defense_counsel", attachment_in_folder=[artifacts["medical_legal_pdf"].name])

        # 2) plaintiff demand transmittal (attachment ALSO in folder = the medical/legal bundle)
        p = _write_eml(
            corr_dir / f"{cid}_03_plaintiff_demand.eml",
            subject=f"[{cid}] Policy-limits demand — {cl['attorney']['firm']}",
            sender=f"{cl['attorney']['name']} <{cl['attorney']['email']}>", to=f"{adj['name']} <{adj['email']}>",
            date=formatdate(), body_text=(
                f"Adjuster {adj['name'].split()[-1]},\n\nEnclosed please find our demand of ${c['demand']['amount']:.0f} on "
                f"behalf of {cl['name']}. Medical specials total ${c['medical']['billed']:.2f}. The demand remains open for 30 "
                f"days.\n\n{cl['attorney']['name']}\n{cl['attorney']['firm']}"),
            sig_gif=None, carrier=cl["attorney"]["firm"])
        rec(p, "legal_correspondence")

        # 3) negotiation / settlement thread
        p = _write_eml(
            corr_dir / f"{cid}_04_negotiation_settlement.eml",
            subject=f"[{cid}] RE: Policy-limits demand — counteroffer",
            sender=f"{adj['name']} <{adj['email']}>", to=f"{cl['attorney']['name']} <{cl['attorney']['email']}>",
            date=formatdate(), body_text=(
                f"Counsel,\n\nWithout admission of liability, {c['carrier']} offers ${c['settlement']['amount']:.0f} to resolve "
                f"all claims. This reflects comparative fault and available coverage. A release will follow upon agreement.\n\n"
                f"{adj['name']}"),
            sig_gif=sig_gif, carrier=c["carrier"])
        rec(p, "negotiation_settlement")

    elif c["complexity"] == "investigation-disputed":
        siu = c["siu"]
        # investigation report email: TWO attachments, one in folder + one attachment-only
        photo_log = _mini_pdf_bytes(
            f"SIU FIELD PHOTO LOG — {siu['ref']}",
            [f"Claim: {cid}", f"Investigator: {siu['investigator']}",
             "Note: 11 field photos catalogued. Damage pattern inconsistent with a deer strike;",
             "paint transfer suggests contact with a painted structure. Recommend EUO.",
             "THIS LOG IS ATTACHED TO THIS EMAIL ONLY — not filed in the claim folder."])
        p = _write_eml(
            corr_dir / f"{cid}_02_investigation_report.eml",
            subject=f"[{cid}] SIU referral — disputed cause of loss ({siu['ref']})",
            sender=f"{siu['investigator']} <{siu['email']}>", to=f"{adj['name']} <{adj['email']}>",
            date=formatdate(), body_text=(
                f"{adj['name'].split()[0]},\n\nCompleting my initial SIU review of claim {cid}. Two items attached:\n"
                f"  1) The shop repair estimate (also filed in the claim folder).\n"
                f"  2) My field photo log ({siu['ref']}) — provided here only; not added to the general claim folder.\n\n"
                f"The reported deer strike is not consistent with the observed damage. Recommend examination under oath before "
                f"any payment.\n\n{siu['investigator']}\n{siu['unit']}"),
            sig_gif=sig_gif, carrier=c["carrier"],
            attachments=[artifacts["repair_estimate_pdf"]],
            inline_attach=[(f"{cid}_SIU_field_photo_log.pdf", photo_log, "application/pdf")])
        rec(p, "investigation_report",
            attachment_in_folder=[artifacts["repair_estimate_pdf"].name],
            attachment_only=[f"{cid}_SIU_field_photo_log.pdf"])

    return entries
