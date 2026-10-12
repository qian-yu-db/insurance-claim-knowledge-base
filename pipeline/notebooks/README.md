# Claims Multi-Modal KB — Default Workflow Notebooks

A parameterized, medallion batch workflow that turns a UC Volume of raw claim artifacts into a
**Lakebase hybrid search index** (pgvector-compatible ANN + BM25) for an adjuster knowledge base, plus **structured fact tables** for SQL/Genie.
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
| 07 | `07_lakebase_embed_sync` | embed `gold_chunks` → Lakebase `claims.gold_chunks_embeddings` + ANN/BM25 indexes | 06 |
| 08 | `08_lakebase_metadata_sync` | copy `dim_claim`, `bronze_file_registry`, `silver_documents` → Lakebase `claims` schema | 04, 05 |

Run 02 before 03 (03 reads 02's parse output to find no-text images); 03c is independent of 02/03.
05 and 06 can run in parallel after 04; 07 and 08 are independent of each other.
Re-run 07 and 08 whenever the raw data changes. They fully reload their Lakebase tables. As a Lakeflow Job, wire tasks with these dependencies.

## Lakebase prerequisites (nb 07, 08)

1. A Lakebase Autoscaling project (default `insurance-claim`).
2. **Enable Lakebase Search on the project, then restart its compute.** In the Lakebase UI, open the
   project's **Settings** and turn on **Lakebase Search**. Then restart the `primary` compute under
   **Branches → production → Computes**. The setting only takes effect after a restart. Until then,
   `CREATE EXTENSION lakebase_vector` fails with
   `lakebase_vector must be loaded via shared_preload_libraries`. Check with
   `SHOW shared_preload_libraries;` — it should include `lakebase_vector,lakebase_text`.
3. After deploying the app, grant its service principal read access (run as the project owner):
   ```sql
   GRANT USAGE ON SCHEMA claims TO "<APP_SP_CLIENT_ID>";
   GRANT SELECT ON ALL TABLES IN SCHEMA claims TO "<APP_SP_CLIENT_ID>";
   ALTER DEFAULT PRIVILEGES IN SCHEMA claims GRANT SELECT ON TABLES TO "<APP_SP_CLIENT_ID>";
   ```
   The default privileges cover the tables that 07/08 drop and recreate on each run.

## Parameters (widgets in `00_config`)

| Widget | Default | Notes |
|---|---|---|
| `catalog` / `schema` | `fins_genai` / `claims_multimodal_kb` | where tables + index are created |
| `landing_path` | `/Volumes/fins_genai/claims_multimodal_kb/landing/claims` | raw files (66, incl. 6 audio) |
| `embedding_endpoint` | `databricks-gte-large-en` | index embeddings |
| `vision_endpoint` | `databricks-llama-4-maverick` | image captions |
| `email_endpoint` | `databricks-claude-haiku-4-5` | `.eml` body cleanup |
| `audio_endpoint` | `whisper-large-v3` | Whisper transcription (nb 03c) |
| `lakebase_project_id` / `lakebase_branch_id` / `lakebase_endpoint_id` | `insurance-claim` / `production` / `primary` | Lakebase target (nb 07, 08) |
| `lakebase_database` / `lakebase_schema` | `databricks_postgres` / `claims` | Lakebase database + schema |
| `pii_mask` | `off` | `sensitive_only` masks SSN/bank/CC (keeps names/claim#) |

Defaults target **`fevm-classic-stable`** (`fins_genai.claims_multimodal_kb`) where the data and the
`whisper-large-v3` endpoint live. Repoint the widgets for any other workspace.

`00_config` verifies the foundation-model endpoints exist in the target workspace — run it first.

## Metadata columns (the design)

The whole point of the metadata is retrieval control. Columns are built in **nb 04** (typed) and carried
onto every chunk in **nb 06** (search filters operate at chunk granularity), then embedded and loaded
into Lakebase in **nb 07**.

- **Filters (hard predicates, applied before ranking):** `claim_id`, `corpus`, `doc_type`, `modality`,
  `content_source`, `carrier`, `peril`, `loss_year` (INT range), `policy_number`.
- **Hybrid search:** the app fuses a vector (`lakebase_ann`, cosine) ranking with a BM25 (`lakebase_bm25`)
  ranking over the chunk text using Reciprocal Rank Fusion (k=60) — catches exact tokens (policy numbers, names, "L4-L5") pure semantics miss. It does **not**
  search metadata columns; those are for filtering/faceting.
- **Provenance & governance:** `source_uri`, `filename`, `chunk_position` (cite/deep-link), `pii_masked`,
  `content_source` (flags model-generated captions vs. parsed text), `ingested_at`.

Guidance: keep filter columns typed and low-cardinality; cast dates and expose an INT `loss_year` for
range filters; don't use free text as a filter.

## What's built vs. deferred

- **Built & validated:** parse/OCR, text/email extraction, **vision image captions** (confirmed on a
  damage photo), classify + expanded extract, metadata engineering, structured facts (CSV read
  confirmed), `ai_prep_search` chunking, Lakebase embeddings + ANN/BM25 indexes + hybrid query.
- **Audio transcription (nb 03c) — validated** against the live `whisper-large-v3` endpoint:
  `{"dataframe_split": {"data": [["<b64>"]]}}` → `{"predictions": ["<transcript>"]}`. **mp3 works,
  including multi-minute files** (5.5-min call → ~44s, server-side chunking). **m4a/AAC is not decodable
  by the endpoint**, so nb 03c transcodes m4a→wav with `ffmpeg` first (needs ffmpeg on the cluster — ML
  runtimes include it; otherwise those rows record a clear error and are skipped).
- **Deferred:** video (`mov/mp4/3gp`, `route='media_pending'`) — no video for now.

## Running

Interactively: open `00_config`, set widgets, then run 01→08 in order. As a Job: create one task per
notebook with the dependencies above; all share the same widget values (set as job parameters). Compute
must provide `ai_parse_document` (DBR 17.3+) and `ai_prep_search` (DBR 18.2+) — a serverless SQL
warehouse or matching cluster.

> Note: defaults target `fevm-fins-voice-mm`. The earlier one-file SQL (`../claims_multimodal_pipeline.sql`)
> was validated end-to-end on `fevm-classic-stable`; these notebooks expand it (image captions, structured
> facts, richer metadata) and parameterize the target.
