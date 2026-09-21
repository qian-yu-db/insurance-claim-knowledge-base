"""Born-digital PDF documents per claim, built with fpdf2 (no system libs)."""

from __future__ import annotations

import random
from pathlib import Path

from docpdf import ClaimPDF


def _money(x) -> str:
    return f"${x:,.2f}"


def fnol(cid: str, c: dict, out: Path) -> Path:
    v, ins, loss = c["vehicle"], c["insured"], c["loss"]
    p = ClaimPDF(c["carrier"], "FIRST NOTICE OF LOSS — Personal Auto")
    p.h2("Claim Identification")
    p.kv_table([
        ("Claim Number", cid),
        ("Policy Number", c["policy_no"]),
        ("Named Insured", ins["name"]),
        ("Date of Loss", loss["date"]),
        ("Reported Date", loss["reported"]),
        ("Peril", loss["peril"]),
        ("Loss Location", loss["location"]),
    ])
    p.h2("Insured Contact")
    p.kv_table([("Address", ins["address"]), ("Phone", ins["phone"]), ("Email", ins["email"])])
    p.h2("Vehicle")
    p.kv_table([
        ("Year / Make / Model", f"{v['year']} {v['make']} {v['model']}"),
        ("VIN", v["vin"]), ("Plate", v["plate"]),
    ])
    p.h2("Loss Description")
    p.para(loss["description"])
    p.h2("Preliminary Liability Assessment")
    p.para(loss["liability"])
    path = out / f"{cid}_fnol_auto.pdf"
    p.output(str(path))
    return path


def policy_declarations(cid: str, c: dict, out: Path) -> Path:
    ins = c["insured"]
    start, end = c["policy_period"]
    p = ClaimPDF(c["carrier"], f"{c['policy_form']} — Declarations")
    p.h2("Policy Information")
    p.kv_table([
        ("Policy Number", c["policy_no"]),
        ("Policy Period", f"{start} to {end} (12:01 AM standard time)"),
        ("Named Insured", ins["name"]),
        ("Mailing Address", ins["address"]),
        ("Form", c["policy_form"]),
    ])
    p.h2("Coverages and Limits")
    p.money_table(
        ["Coverage", "Limit", "Deductible"],
        [
            ["Bodily Injury Liability", "$250,000 / $500,000", "—"],
            ["Property Damage Liability", "$100,000", "—"],
            ["Collision", "Actual Cash Value", _money(c.get("estimate", {}).get("deductible", 500))],
            ["Comprehensive", "Actual Cash Value", "$250.00"],
            ["Medical Payments", "$10,000", "—"],
            ["Uninsured/Underinsured Motorist", "$250,000 / $500,000", "—"],
        ],
        widths=[95, 55, 30],
    )
    p.para("Premium and coverage subject to all policy terms, conditions, and endorsements attached hereto.")
    path = out / f"{cid}_policy_declarations.pdf"
    p.output(str(path))
    return path


def endorsement(cid: str, c: dict, out: Path) -> Path:
    p = ClaimPDF(c["carrier"], "POLICY ENDORSEMENT")
    p.h2("Endorsement Identification")
    p.kv_table([
        ("Endorsement Number", f"END-{random.randint(4000, 9000)}"),
        ("Base Policy Number", c["policy_no"]),
        ("Named Insured", c["insured"]["name"]),
        ("Effective Date", c["policy_period"][0]),
        ("Endorsement Title", "Rental Reimbursement & Transportation Expense"),
    ])
    p.h2("Coverage Amendment")
    p.para("Coverage is extended to include reimbursement for transportation expenses incurred by the insured while a covered "
           "auto is withdrawn from use due to a loss covered under Collision or Comprehensive coverage. The limit for this "
           "endorsement is $50 per day, subject to a maximum of $1,500 per occurrence.")
    p.h2("All Other Terms Unchanged")
    p.para("Except as amended by this endorsement, all terms, conditions, exclusions, and limitations of the policy remain in "
           "full force and effect without change.")
    path = out / f"{cid}_endorsement_rental.pdf"
    p.output(str(path))
    return path


def adjuster_report(cid: str, c: dict, out: Path, photos: list[Path]) -> Path:
    a, loss, v = c["adjuster"], c["loss"], c["vehicle"]
    p = ClaimPDF(c["carrier"], "CLAIM INSPECTION REPORT")
    p.h2("Claim and Adjuster Information")
    p.kv_table([
        ("Claim Number", cid),
        ("Policy Number", c["policy_no"]),
        ("Named Insured", c["insured"]["name"]),
        ("Date of Loss", loss["date"]),
        ("Peril", loss["peril"]),
        ("Assigned Adjuster", f"{a['name']} · License {a['license']}"),
        ("Adjuster Phone", a["phone"]),
    ])
    p.h2("Summary")
    sev = c["severity"]
    p.para(f"Inspection of the {v['year']} {v['make']} {v['model']} (VIN {v['vin']}) confirms {sev} damage consistent with the "
           f"reported loss. {loss['description']} {loss['liability']}")
    if photos:
        p.h2("Photo Documentation")
        caps = [f"Damage photo {i + 1} ({sev})" for i in range(len(photos))]
        for i in range(0, len(photos), 2):
            p.image_row(photos[i:i + 2], caps[i:i + 2])
    path = out / f"{cid}_adjuster_report.pdf"
    p.output(str(path))
    return path


def repair_estimate_pdf(cid: str, c: dict, out: Path) -> Path:
    est = c["estimate"]
    v = c["vehicle"]
    p = ClaimPDF("Precision Auto Body & Collision", "REPAIR ESTIMATE")
    p.h2("Vehicle & Claim")
    p.kv_table([
        ("Claim Number", cid),
        ("Vehicle", f"{v['year']} {v['make']} {v['model']}"),
        ("VIN", v["vin"]),
        ("Insurer", c["carrier"]),
    ])
    p.h2("Estimate Detail")
    p.money_table(
        ["Line Item", "Amount"],
        [["Parts", _money(est["parts"])], ["Labor", _money(est["labor"])], ["Paint & Materials", _money(est["paint"])],
         ["Tax", _money(est["tax"])], ["Total", _money(est["total"])], ["Less Deductible", _money(est["deductible"])],
         ["Net Payable", _money(est["net"])]],
        widths=[120, 60],
    )
    path = out / f"{cid}_repair_estimate.pdf"
    p.output(str(path))
    return path


def acord_auto_loss(cid: str, c: dict, out: Path) -> Path:
    """A simplified ACORD-style Automobile Loss Notice for the litigation claim."""
    ins, v, loss = c["insured"], c["vehicle"], c["loss"]
    cl = c["claimant"]
    p = ClaimPDF("ACORD", "AUTOMOBILE LOSS NOTICE (ACORD 2 style)")
    p.h2("Policy / Insured")
    p.kv_table([("Carrier", c["carrier"]), ("Policy Number", c["policy_no"]), ("Insured", ins["name"]),
                ("Insured Vehicle", f"{v['year']} {v['make']} {v['model']} — VIN {v['vin']}")])
    p.h2("Loss")
    p.kv_table([("Date/Time of Loss", loss["date"]), ("Location", loss["location"]), ("Description", "")])
    p.para(loss["description"])
    p.h2("Claimant / Injured Party")
    p.kv_table([("Name", cl["name"]), ("Address", cl["address"]), ("Phone", cl["phone"]),
                ("Attorney", f"{cl['attorney']['name']} — {cl['attorney']['firm']}"),
                ("Injuries Alleged", "; ".join(c["medical"]["diagnoses"]))])
    path = out / f"{cid}_acord_auto_loss_notice.pdf"
    p.output(str(path))
    return path


def medical_legal_bundle(cid: str, c: dict, out: Path, scan_images: list[Path]) -> Path:
    """The heavyweight 100+ page mixed PDF: medical records + legal correspondence + embedded scans."""
    med = c["medical"]
    cl = c["claimant"]
    p = ClaimPDF(med["provider"], "CLAIMANT MEDICAL RECORDS & LEGAL FILE (COMBINED)")
    # --- cover / index ---
    p.h2("Combined Records Index")
    p.kv_table([
        ("Claim Number", cid),
        ("Claimant", cl["name"]),
        ("Provider", med["provider"]),
        ("Treating Physician", med["physician"]),
        ("Total Pages", str(med["records_pages"])),
        ("Diagnoses", "; ".join(med["diagnoses"])),
        ("Total Billed", _money(med["billed"])),
    ])
    p.para("This combined file contains: (1) intake and history, (2) imaging and physician notes, (3) itemized billing, and "
           "(4) legal correspondence including the plaintiff demand. Scanned pages are embedded where originals were faxed.")

    visit_dates = ["2026-02-11", "2026-02-18", "2026-03-02", "2026-03-20", "2026-04-08", "2026-05-06", "2026-06-01"]
    target = med["records_pages"]
    embed_pts = {12, 34, 61, 88}  # pages where we drop a scanned image

    page = p.page_no()
    while page < target:
        p.add_page()
        page = p.page_no()
        if page in embed_pts and scan_images:
            img = scan_images[page % len(scan_images)]
            p.h2(f"Scanned Record — Page {page}")
            p.para("The following page was received by fax and scanned into the record. OCR required.")
            p.image(str(img), x=25, y=p.get_y(), w=p.w - 50)
            continue
        vd = visit_dates[page % len(visit_dates)]
        p.h2(f"Progress Note — {vd}")
        p.kv_table([("Patient", cl["name"]), ("Date of Service", vd), ("Provider", med["physician"]),
                    ("Assessment", med["diagnoses"][page % len(med["diagnoses"])])])
        p.para(
            f"Subjective: Patient reports ongoing neck and lower-back pain rated {4 + page % 5}/10, aggravated by prolonged "
            "sitting. Sleep disturbance noted. Objective: Reduced cervical range of motion; positive straight-leg raise on "
            "the left. Tenderness on palpation of the paraspinal musculature. Assessment: Injuries remain consistent with the "
            f"motor-vehicle collision of {c['loss']['date']}. Plan: Continue physical therapy 2x/week, NSAID as needed, "
            f"follow-up in 3 weeks. Diagnostic imaging on file. Billed charges to date: {_money(med['billed'] * page / target)}."
        )

    # --- legal correspondence tail ---
    p.add_page()
    p.h2("Legal Correspondence — Plaintiff Demand Letter")
    p.para(f"From: {cl['attorney']['name']}, {cl['attorney']['firm']}")
    p.para(f"To: {c['adjuster']['name']}, {c['carrier']}")
    p.para(f"Re: {cl['name']} v. {c['insured']['name']} — Claim {cid}")
    p.para(
        f"Demand is hereby made in the amount of {_money(c['demand']['amount'])} in full settlement of all claims arising from "
        f"the collision of {c['loss']['date']}. Special damages include medical expenses of {_money(med['billed'])} and lost "
        "wages; general damages reflect ongoing pain, suffering, and diminished quality of life. This demand remains open for "
        "30 days. Governing venue is Collin County, TX."
    )
    path = out / f"{cid}_medical_legal_combined.pdf"
    p.output(str(path))
    return path
