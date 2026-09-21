# Synthetic Multi-Modal Insurance Claim Data

Three internally-consistent **auto-collision claims**, each a self-contained bundle where every
artifact (email, PDF, image, Word/Excel doc, audio, long-tail file) shares the same claim
number, policy number, insured, loss date, parties, and vehicle. Built to exercise a Databricks
unstructured/multi-modal processing pipeline (parsing, OCR, transcription, extraction,
classification, entity resolution, RAG).

All data is **synthetic**. Damage photos are DALL·E-generated fakes from the
[dbdemos smart-claims dataset](https://github.com/databricks-demos/dbdemos-dataset/tree/main/fsi/smart-claims/Images).

## The three claims

| Claim | Profile | Highlights |
|-------|---------|-----------|
| `CLM-2026-04412` | Minor collision, fast-track | Small bundle: FNOL, short adjuster report, `ok`/`minor` photos, XLSX estimate, quick settlement, claimant voicemail. |
| `CLM-2026-04487` | Major collision + bodily injury + litigation | Heavyweight: **109-page** combined medical/legal PDF (with embedded scanned pages), defense-counsel & negotiation email threads, ACORD form, coverage letter + release (`.docx`), `major` damage photos, HEIC held-in-hand doc photo, recorded statement (`.m4a`). |
| `CLM-2026-04531` | Moderate collision, disputed cause | SIU investigation: email whose attachments include one file **also in the folder** (repair estimate) and one **attachment-only** file (`SIU_field_photo_log.pdf`, never filed in the folder), plus RTF note, TIFF scan. |

## Layout

```
claims/
├── ground_truth.csv          # every file → claim_id, modality, doc_type, format, key attributes
├── eval_questions.json       # paired retrieval-eval questions + expected facts, per claim
├── _shared_templates/        # coverage_letter_TEMPLATE.dot
└── <CLAIM_ID>/
    ├── manifest.json         # per-claim artifact manifest
    ├── correspondence/       # .eml (inline signature GIF, attachments)
    ├── documents/            # .pdf, .docx, .rtf
    ├── images/               # damage photos (jpg/png/heic/tiff), scans, signature gif
    ├── structured/           # .xlsx, .csv, .html, .xml, .json
    └── audio/                # .mp3, .m4a
```

## Format coverage (69 artifacts)

- **Documents:** PDF (incl. 100+ page mixed bundle), DOCX, RTF, DOT template
- **Email:** EML (attachments in-folder vs. attachment-only; inline signature graphic)
- **Images:** JPG, PNG, HEIC, TIFF, GIF (damage photos, scanned docs, held-in-hand phone photo, email signature)
- **Audio:** MP3 (reused policy-narration calls + TTS voicemail), M4A (TTS recorded statement / SIU note)
- **Structured / long-tail:** XLSX, CSV, HTML (vendor appraisal), XML + JSON (metadata)

## Design decisions worth knowing

- **`.eml`, not `.msg`.** True Outlook `.msg` (CFBF) needs Windows/Outlook or a paid library. `.eml` is
  fully parseable and carries the same structure (threads, attachments, inline images). Convert on a
  Windows box if genuine `.msg` is later required.
- **Attachment-in-folder vs. attachment-only** is modeled explicitly (see `CLM-2026-04531`) — this is
  the "do email attachments also show up in the claim folder?" question from the use-case brief. The
  ground truth flags both `attachment_in_folder` and `attachment_only`.
- **Ground truth** (`ground_truth.csv` + per-claim `manifest.json`) lets you score classification,
  extraction, transcription, and cross-modal linkage. `eval_questions.json` follows the Databricks
  Knowledge-Assistant / retrieval-eval paired-question pattern.

## Regenerating

```bash
cd ../../generator && uv run python main.py
```

Deterministic (seeded image selection). Requires macOS `say` + `ffmpeg` for audio.
