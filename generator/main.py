"""Orchestrate generation of 3 coherent multi-modal claim bundles + ground truth."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import audio
import claims
import emails
import images
import longtail
import office
import pdf_docs

MODALITY_BY_EXT = {
    ".pdf": "document", ".docx": "document", ".rtf": "document", ".dot": "template",
    ".eml": "email",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".heic": "image", ".tiff": "image", ".gif": "image",
    ".mp3": "audio", ".m4a": "audio",
    ".mov": "video", ".mp4": "video", ".3gp": "video",
    ".xlsx": "structured", ".csv": "structured", ".html": "structured",
    ".xml": "metadata", ".json": "metadata",
}


def _entry(path: Path, cid: str, c: dict, **extra) -> dict:
    return {
        "claim_id": cid,
        "relative_path": str(path.relative_to(claims.CLAIMS_ROOT)),
        "modality": MODALITY_BY_EXT.get(path.suffix.lower(), "other"),
        "format": path.suffix.lstrip("."),
        "peril": c["loss"]["peril"],
        "severity": c["severity"],
        "carrier": c["carrier"],
        "policy_no": c["policy_no"],
        "insured": c["insured"]["name"],
        **extra,
    }


def build_claim(cid: str, c: dict) -> list[dict]:
    root = claims.CLAIMS_ROOT / cid
    d_corr = root / "correspondence"
    d_docs = root / "documents"
    d_img = root / "images"
    d_aud = root / "audio"
    d_struct = root / "structured"
    for d in (d_corr, d_docs, d_img, d_aud, d_struct):
        d.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []

    # --- 1. images first (PDFs embed them) ---
    photo_srcs = images.pick_damage_images(claims.CLAIM_IMAGE_PLAN[cid])
    photo_meta = images.place_damage_photos(cid, c, photo_srcs, d_img)
    photo_paths = [m["path"] for m in photo_meta]
    for m in photo_meta:
        entries.append(_entry(m["path"], cid, c, doc_type=m["doc_type"], source="dbdemos-smart-claims"))

    sig = images.signature_gif(cid, c, d_img)
    entries.append(_entry(sig["path"], cid, c, doc_type=sig["doc_type"]))

    scan_pngs: list[Path] = []
    if c["complexity"] == "litigation-bodily-injury":
        scan_pngs = images.medical_scan_pngs(cid, c, d_img, n=3)
        for p in scan_pngs:
            entries.append(_entry(p, cid, c, doc_type="medical_scan_page"))
        hih = images.held_in_hand(cid, c, d_img)
        entries.append(_entry(hih["path"], cid, c, doc_type=hih["doc_type"]))

    scan = images.scanned_doc(cid, c, d_img)
    entries.append(_entry(scan["path"], cid, c, doc_type=scan["doc_type"]))

    # --- 2. PDFs ---
    artifacts: dict[str, Path] = {}
    entries.append(_entry(pdf_docs.fnol(cid, c, d_docs), cid, c, doc_type="fnol_claim_form"))
    entries.append(_entry(pdf_docs.policy_declarations(cid, c, d_docs), cid, c, doc_type="policy_document"))
    entries.append(_entry(pdf_docs.endorsement(cid, c, d_docs), cid, c, doc_type="endorsement"))
    entries.append(_entry(pdf_docs.adjuster_report(cid, c, d_docs, photo_paths), cid, c, doc_type="adjuster_report"))

    if "estimate" in c:
        est_pdf = pdf_docs.repair_estimate_pdf(cid, c, d_docs)
        artifacts["repair_estimate_pdf"] = est_pdf
        entries.append(_entry(est_pdf, cid, c, doc_type="repair_estimate"))

    if c["complexity"] == "litigation-bodily-injury":
        med_pdf = pdf_docs.medical_legal_bundle(cid, c, d_docs, scan_pngs)
        artifacts["medical_legal_pdf"] = med_pdf
        entries.append(_entry(med_pdf, cid, c, doc_type="medical_legal_combined", note="100+ page mixed bundle"))
        acord = pdf_docs.acord_auto_loss(cid, c, d_docs)
        entries.append(_entry(acord, cid, c, doc_type="acord_form"))

    # --- 3. Word / Excel ---
    if c["complexity"] == "litigation-bodily-injury":
        entries.append(_entry(office.coverage_letter_docx(cid, c, d_docs), cid, c, doc_type="coverage_letter"))
        entries.append(_entry(office.release_docx(cid, c, d_docs), cid, c, doc_type="release_form"))
        entries.append(_entry(office.evaluation_docx(cid, c, d_docs), cid, c, doc_type="claim_evaluation"))
    if "estimate" in c:
        entries.append(_entry(office.estimate_xlsx(cid, c, d_struct), cid, c, doc_type="repair_estimate_sheet"))

    # --- 4. long-tail ---
    entries.append(_entry(longtail.vendor_appraisal_html(cid, c, d_struct), cid, c, doc_type="vendor_appraisal"))
    entries.append(_entry(longtail.metadata_xml(cid, c, d_struct), cid, c, doc_type="claim_metadata"))
    entries.append(_entry(longtail.payments_csv(cid, c, d_struct), cid, c, doc_type="payment_ledger"))
    if c["complexity"] == "investigation-disputed":
        entries.append(_entry(longtail.rtf_note(cid, c, d_docs), cid, c, doc_type="adjuster_note"))

    # --- 5. emails (may reference artifacts as attachments) ---
    email_entries = emails.build_emails(cid, c, d_corr, artifacts, sig["path"])
    for e in email_entries:
        extra = {k: v for k, v in e.items() if k not in ("path", "doc_type", "format")}
        entries.append(_entry(e["path"], cid, c, doc_type=e["doc_type"], **extra))

    # --- 6. audio ---
    for a in audio.build_audio(cid, c, d_aud):
        entries.append(_entry(a["path"], cid, c, doc_type=a["doc_type"], source=a["source"]))

    # --- 7. ingestion metadata (references final count) ---
    ing = longtail.ingestion_json(cid, c, d_struct, len(entries) + 1)
    entries.append(_entry(ing, cid, c, doc_type="ingestion_metadata"))

    (root / "manifest.json").write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")
    return entries


def write_ground_truth(all_entries: list[dict]):
    cols = ["claim_id", "relative_path", "modality", "format", "doc_type", "peril", "severity",
            "carrier", "policy_no", "insured", "email_category", "attachment_in_folder",
            "attachment_only", "source", "note"]
    path = claims.CLAIMS_ROOT / "ground_truth.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for e in all_entries:
            row = dict(e)
            for k in ("attachment_in_folder", "attachment_only"):
                if isinstance(row.get(k), list):
                    row[k] = "; ".join(row[k])
            w.writerow(row)
    return path


def write_eval_questions():
    """Paired retrieval-eval questions (skill pattern), keyed by claim, with queryable facts."""
    q = {
        "CLM-2026-04412": [
            {"question": "What is the net amount payable to the insured after the deductible on claim CLM-2026-04412?",
             "expected_fact": "$2,660.25 net payable after a $500 deductible."},
            {"question": "What is the VIN of the insured vehicle in claim CLM-2026-04412?",
             "expected_fact": "1HGCV1F13NA004217 (2022 Honda Accord EX)."},
        ],
        "CLM-2026-04487": [
            {"question": "What is the plaintiff demand amount in claim CLM-2026-04487 and who made it?",
             "expected_fact": "$225,000 demand by Ramirez Law Group on behalf of Denise R. Varga."},
            {"question": "What are the alleged diagnoses in the claimant medical records for CLM-2026-04487?",
             "expected_fact": "Cervical strain, lumbar disc herniation L4-L5, and post-concussive syndrome."},
            {"question": "What settlement amount did the carrier offer in CLM-2026-04487?",
             "expected_fact": "$132,500, negotiated, pending release."},
        ],
        "CLM-2026-04531": [
            {"question": "Why was claim CLM-2026-04531 referred to the Special Investigations Unit?",
             "expected_fact": "Damage pattern is inconsistent with the reported deer strike; SIU recommends examination under oath (ref SIU-2026-0771)."},
            {"question": "Which attachment in the SIU investigation email is NOT filed in the claim folder?",
             "expected_fact": "The SIU field photo log (CLM-2026-04531_SIU_field_photo_log.pdf) exists only as an email attachment."},
        ],
    }
    path = claims.CLAIMS_ROOT / "eval_questions.json"
    path.write_text(json.dumps(q, indent=2), encoding="utf-8")
    return path


def main():
    claims.CLAIMS_ROOT.mkdir(parents=True, exist_ok=True)
    templates_dir = claims.CLAIMS_ROOT / "_shared_templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    longtail.dot_template(templates_dir)
    all_entries: list[dict] = []
    for cid, c in claims.CLAIMS.items():
        print(f"Building {cid} ({c['complexity']}) ...")
        all_entries.extend(build_claim(cid, c))
    gt = write_ground_truth(all_entries)
    eq = write_eval_questions()
    print(f"\nDone. {len(all_entries)} artifacts across {len(claims.CLAIMS)} claims.")
    print(f"Ground truth: {gt}")
    print(f"Eval questions: {eq}")


if __name__ == "__main__":
    main()
