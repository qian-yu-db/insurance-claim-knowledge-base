# Databricks notebook source
# MAGIC %md
# MAGIC # 06 · Gold — chunks + propagated metadata (index source table)
# MAGIC
# MAGIC - Parsed docs → `ai_prep_search` (semantic chunks, context-enriched `chunk_to_embed`).
# MAGIC - Text & caption docs → one chunk each (already short).
# MAGIC
# MAGIC **Every metadata column from nb 04 is carried onto every chunk** — Vector Search filters operate at
# MAGIC chunk granularity, so a column that isn't on the chunk row can't be filtered on. CDF is enabled so a
# MAGIC Delta Sync index tracks changes incrementally.
# MAGIC
# MAGIC **The table is updated in place, never replaced.** A Delta Sync index is bound to the source table's
# MAGIC identity; `CREATE OR REPLACE` gives the table a new id and breaks every later sync
# MAGIC (`DIFFERENT_DELTA_TABLE_READ_BY_STREAMING_SOURCE`). So chunks are built into a staging table and
# MAGIC `MERGE`d in: changed rows are updated, new ones inserted, vanished ones deleted.
# MAGIC
# MAGIC *Masking note:* with `pii_mask='sensitive_only'`, the text/caption branch is masked (it reads
# MAGIC `silver_documents.text_content`), while the parsed branch is chunked from the raw parse. If strict
# MAGIC masking of parsed chunks is required, apply `ai_mask` to `chunk_to_retrieve`/`chunk_to_embed` below.

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
GOLD = CFG['t']['gold']
STAGE = f"{GOLD}_staging"

# Build the full chunk set once into a staging table (ai_prep_search runs exactly once here; a MERGE
# straight from the query could re-evaluate it).
run_sql(f"""
CREATE OR REPLACE TABLE {STAGE} AS
WITH parsed_docs AS (
  SELECT
    sp.doc_id, sd.claim_id, sd.corpus, sd.doc_type, sd.modality, sd.content_source,
    sd.carrier, sd.peril, sd.loss_year, sd.date_of_loss, sd.policy_number, sd.insured_name,
    sd.claimant_name, sd.adjuster_name, sd.total_amount, sd.source_uri, sd.filename,
    sd.pii_masked, sd.ingested_at,
    ai_prep_search(sp.parsed_variant) AS prep
  FROM {CFG['t']['parsed']} sp
  JOIN {CFG['t']['docs']} sd ON sp.doc_id = sd.doc_id
  WHERE sd.has_text
),
parse_chunks AS (
  SELECT
    -- Deterministic id (ai_prep_search's own chunk_id changes every run, which would make the MERGE
    -- delete + re-insert every parsed chunk and force a full re-embed).
    concat(doc_id, '_', variant_get(chunk,'$.chunk_position','INT')) AS chunk_id,
    doc_id,
    variant_get(chunk,'$.chunk_position','INT')       AS chunk_position,
    claim_id, corpus, doc_type, modality, content_source,
    carrier, peril, loss_year, date_of_loss, policy_number, insured_name, claimant_name,
    adjuster_name, total_amount, source_uri, filename, pii_masked, ingested_at,
    variant_get(chunk,'$.chunk_to_retrieve','STRING') AS chunk_to_retrieve,
    variant_get(chunk,'$.chunk_to_embed','STRING')    AS chunk_to_embed
  FROM parsed_docs
  LATERAL VIEW explode(cast(variant_get(prep,'$.document.contents','ARRAY<VARIANT>') AS ARRAY<VARIANT>)) t AS chunk
),
single_chunks AS (
  SELECT
    concat(doc_id,'-0')  AS chunk_id,
    doc_id,
    0                    AS chunk_position,
    claim_id, corpus, doc_type, modality, content_source,
    carrier, peril, loss_year, date_of_loss, policy_number, insured_name, claimant_name,
    adjuster_name, total_amount, source_uri, filename, pii_masked, ingested_at,
    text_content         AS chunk_to_retrieve,
    text_content         AS chunk_to_embed
  FROM {CFG['t']['docs']}
  WHERE has_text AND content_source IN ('text','vision_caption','audio_transcript')
)
SELECT * FROM (
  SELECT * FROM parse_chunks
  UNION ALL
  SELECT * FROM single_chunks
)
WHERE chunk_to_embed IS NOT NULL AND length(chunk_to_embed) > 0  -- no empty embed text in the index
""")

# COMMAND ----------
# First run only: create the gold table (empty, same schema) with CDF for the Delta Sync index.
run_sql(f"""
CREATE TABLE IF NOT EXISTS {GOLD}
TBLPROPERTIES (delta.enableChangeDataFeed = true) AS
SELECT * FROM {STAGE} WHERE false""")

# Update only rows whose text or metadata changed, so unchanged chunks produce no CDF rows (and no
# re-embedding). chunk_to_embed is excluded from the check: ai_prep_search writes an LLM-generated
# context header that is worded differently on every run even when the document is identical.
cols = [f.name for f in spark.table(STAGE).schema.fields if f.name not in ("chunk_id", "chunk_to_embed")]
changed = " OR ".join(f"NOT (t.`{c}` <=> s.`{c}`)" for c in cols)
display(run_sql(f"""
MERGE WITH SCHEMA EVOLUTION INTO {GOLD} t
USING {STAGE} s
ON t.chunk_id = s.chunk_id
WHEN MATCHED AND ({changed}) THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
WHEN NOT MATCHED BY SOURCE THEN DELETE"""))
run_sql(f"DROP TABLE IF EXISTS {STAGE}")

# COMMAND ----------
display(run_sql(f"""
SELECT count(*) chunks, count(distinct doc_id) docs, count(distinct claim_id) claims FROM {GOLD}"""))
display(run_sql(f"SELECT doc_type, modality, count(*) n FROM {GOLD} GROUP BY 1,2 ORDER BY n DESC"))
