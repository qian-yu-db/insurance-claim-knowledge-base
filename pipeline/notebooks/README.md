# Claims Multi-Modal KB — Default Workflow Notebooks

A parameterized, medallion batch workflow that turns a UC Volume of raw claim artifacts into a
**Vector Search index** for an adjuster knowledge base, plus **structured fact tables** for SQL/Genie.
Implements the default architecture in [`../../docs/claims_kb_architecture.md`](../../docs/claims_kb_architecture.md).

## Notebook order (→ Job DAG)

| # | Notebook | Stage | Depends on |
|---|---|---|---|
| 00 | `00_config` | widgets + shared `CFG` (via `%run`) | — |
| 01 | `01_bronze_ingest` | file registry + modality routing | 00 |
| 02 | `02_silver_parse_text` | `ai_parse_document` (parse) + decode/LLM-clean (text) | 01 |
| 03 | `03_silver_image_caption` | vision `ai_query` captions for no-text images | 01, 02 |
| 03c | `03c_silver_audio` | Whisper transcription of mp3/m4a | 01 |
| 04 | `04_silver_documents` | unify + `ai_classify` + `ai_extract` + **metadata columns** | 02, 03, 03c |
| 05 | `05_structured_facts` | `fact_payments`, `dim_claim` (Delta for SQL/Genie) | 04 |
| 06 | `06_gold_chunks` | `ai_prep_search` chunks + metadata propagation | 04 |
| 07 | `07_vector_search_index` | Delta Sync index + filtered hybrid query | 06 |

Run 02 before 03 (03 reads 02's parse output to find no-text images); 03c is independent of 02/03.
05 and 06 can run in parallel after 04. As a Lakeflow Job, wire tasks with these dependencies.

## Parameters (widgets in `00_config`)

| Widget | Default | Notes |
|---|---|---|
| `catalog` / `schema` | `fins_genai` / `claims_multimodal_kb` | where tables + index are created |
| `landing_path` | `/Volumes/fins_genai/claims_multimodal_kb/landing/claims` | raw files (78, incl. 6 audio) |
| `embedding_endpoint` | `databricks-gte-large-en` | index embeddings |
| `vision_endpoint` | `databricks-llama-4-maverick` | image captions |
| `email_endpoint` | `databricks-claude-haiku-4-5` | `.eml` body cleanup |
| `audio_endpoint` | `whisper-large-v3` | Whisper transcription (nb 03c) |
| `vs_endpoint` | *(empty — set before nb 07)* | Vector Search endpoint |
| `pii_mask` | `off` | `sensitive_only` masks SSN/bank/CC (keeps names/claim#) |

Defaults target **`fevm-classic-stable`** (`fins_genai.claims_multimodal_kb`) where the data and the
`whisper-large-v3` endpoint live. Repoint the widgets for any other workspace.

`00_config` verifies the foundation-model endpoints exist in the target workspace — run it first.

## Metadata columns (the design)

The whole point of the metadata is retrieval control. Columns are built in **nb 04** (typed) and carried
onto every chunk in **nb 06** (Vector Search filters operate at chunk granularity), then synced to the
index in **nb 07**.

- **Filters (hard predicates, applied before ranking):** `claim_id`, `corpus`, `doc_type`, `modality`,
  `content_source`, `carrier`, `peril`, `loss_year` (INT range), `policy_number`.
- **Hybrid search:** `query_type="HYBRID"` blends vector similarity with keyword/BM25 over the chunk
  text — catches exact tokens (policy numbers, names, "L4-L5") pure semantics miss. It does **not**
  search metadata columns; those are for filtering/faceting.
- **Provenance & governance:** `source_uri`, `filename`, `chunk_position` (cite/deep-link), `pii_masked`,
  `content_source` (flags model-generated captions vs. parsed text), `ingested_at`.

Guidance: keep filter columns typed and low-cardinality; cast dates and expose an INT `loss_year` for
range filters; don't use free text as a filter.

## What's built vs. deferred

- **Built & validated:** parse/OCR, text/email extraction, **vision image captions** (confirmed on a
  damage photo), classify + expanded extract, metadata engineering, structured facts (CSV read
  confirmed), `ai_prep_search` chunking, Delta Sync index with metadata + hybrid query.
- **Audio transcription (nb 03c) — validated** against the live `whisper-large-v3` endpoint:
  `{"dataframe_split": {"data": [["<b64>"]]}}` → `{"predictions": ["<transcript>"]}`. **mp3 works,
  including multi-minute files** (5.5-min call → ~44s, server-side chunking). **m4a/AAC is not decodable
  by the endpoint**, so nb 03c transcodes m4a→wav with `ffmpeg` first (needs ffmpeg on the cluster — ML
  runtimes include it; otherwise those rows record a clear error and are skipped).
- **Deferred:** video (`mov/mp4/3gp`, `route='media_pending'`) — no video for now.

## Running

Interactively: open `00_config`, set widgets, then run 01→07 in order. As a Job: create one task per
notebook with the dependencies above; all share the same widget values (set as job parameters). Compute
must provide `ai_parse_document` (DBR 17.3+) and `ai_prep_search` (DBR 18.2+) — a serverless SQL
warehouse or matching cluster.

> Note: defaults target `fevm-fins-voice-mm`. The earlier one-file SQL (`../claims_multimodal_pipeline.sql`)
> was validated end-to-end on `fevm-classic-stable`; these notebooks expand it (image captions, structured
> facts, richer metadata) and parameterize the target.
