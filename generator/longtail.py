"""Long-tail formats present in smaller quantities: HTML, XML, JSON, CSV, RTF, DOT."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from xml.sax.saxutils import escape


def vendor_appraisal_html(cid: str, c: dict, out: Path) -> Path:
    v, est = c["vehicle"], c.get("estimate")
    total = est["total"] if est else c.get("settlement", {}).get("amount", 0)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Appraisal {cid}</title></head>
<body style="font-family:Arial,sans-serif">
<h1>Independent Vehicle Appraisal Report</h1>
<p><b>Vendor:</b> Apex Appraisal Services &middot; <b>Report #:</b> APX-{cid[-5:]}</p>
<table border="1" cellpadding="6" cellspacing="0">
<tr><th>Field</th><th>Value</th></tr>
<tr><td>Claim Number</td><td>{cid}</td></tr>
<tr><td>Carrier</td><td>{c['carrier']}</td></tr>
<tr><td>Vehicle</td><td>{v['year']} {v['make']} {v['model']}</td></tr>
<tr><td>VIN</td><td>{v['vin']}</td></tr>
<tr><td>Damage Severity</td><td>{c['severity']}</td></tr>
<tr><td>Appraised Repair / Value</td><td>${total:,.2f}</td></tr>
</table>
<p>Appraisal performed per industry standard estimating guidelines. Prior unrelated damage not observed.</p>
</body></html>"""
    path = out / f"{cid}_vendor_appraisal.html"
    path.write_text(html, encoding="utf-8")
    return path


def metadata_xml(cid: str, c: dict, out: Path) -> Path:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ClaimMetadata>
  <ClaimNumber>{escape(cid)}</ClaimNumber>
  <PolicyNumber>{escape(c['policy_no'])}</PolicyNumber>
  <Carrier>{escape(c['carrier'])}</Carrier>
  <Peril>{escape(c['loss']['peril'])}</Peril>
  <Severity>{escape(c['severity'])}</Severity>
  <DateOfLoss>{escape(c['loss']['date'])}</DateOfLoss>
  <Insured>{escape(c['insured']['name'])}</Insured>
  <Adjuster license="{escape(c['adjuster']['license'])}">{escape(c['adjuster']['name'])}</Adjuster>
</ClaimMetadata>
"""
    path = out / f"{cid}_metadata.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def ingestion_json(cid: str, c: dict, out: Path, file_count: int) -> Path:
    payload = {
        "claim_number": cid,
        "policy_number": c["policy_no"],
        "carrier": c["carrier"],
        "peril": c["loss"]["peril"],
        "severity": c["severity"],
        "complexity": c["complexity"],
        "date_of_loss": c["loss"]["date"],
        "reported_date": c["loss"]["reported"],
        "insured": c["insured"]["name"],
        "artifact_count": file_count,
        "source_system": "synthetic-generator",
    }
    path = out / f"{cid}_ingestion_metadata.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def payments_csv(cid: str, c: dict, out: Path) -> Path:
    path = out / f"{cid}_payment_ledger.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["claim_number", "transaction_type", "date", "payee", "amount_usd", "status"])
        s = c.get("settlement", {})
        if s.get("status", "").lower().startswith(("paid", "negotiated")):
            w.writerow([cid, "indemnity", s.get("date") or "", c["insured"]["name"], f"{s.get('amount', 0):.2f}", s["status"]])
        w.writerow([cid, "expense", c["loss"]["reported"], "Apex Appraisal Services", "350.00", "Paid"])
        if c["complexity"] == "litigation-bodily-injury":
            w.writerow([cid, "defense_expense", c["loss"]["reported"], c["defense_counsel"]["firm"], "12500.00", "Paid"])
    return path


def rtf_note(cid: str, c: dict, out: Path) -> Path:
    body = (f"ADJUSTER FILE NOTE\\par Claim {cid} \\par Adjuster: {c['adjuster']['name']}\\par\\par "
            f"{c['loss']['description']} {c['loss']['liability']}\\par")
    rtf = "{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Arial;}}\\fs22 " + body + "}"
    path = out / f"{cid}_adjuster_note.rtf"
    path.write_text(rtf, encoding="utf-8")
    return path


def dot_template(out: Path) -> Path:
    """A reusable coverage-letter template (RTF content, .dot extension) shared as reference."""
    rtf = ("{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Arial;}}\\fs22 "
           "{\\b [[CARRIER]] \\u8212? COVERAGE POSITION LETTER}\\par\\par "
           "Re: Claim [[CLAIM_NUMBER]] \\u8212? Insured [[INSURED_NAME]] \\u8212? Date of Loss [[LOSS_DATE]]\\par\\par "
           "Dear [[INSURED_NAME]],\\par [[CARRIER]] is investigating the above claim under a reservation of rights...\\par\\par "
           "Sincerely,\\par [[ADJUSTER_NAME]]\\par Claims Department}")
    path = out / "coverage_letter_TEMPLATE.dot"
    path.write_text(rtf, encoding="utf-8")
    return path
