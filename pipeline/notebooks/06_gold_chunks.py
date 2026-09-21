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
# MAGIC *Masking note:* with `pii_mask='sensitive_only'`, the text/caption branch is masked (it reads
# MAGIC `silver_documents.text_content`), while the parsed branch is chunked from the raw parse. If strict
# MAGIC masking of parsed chunks is required, apply `ai_mask` to `chunk_to_retrieve`/`chunk_to_embed` below.

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['gold']}
TBLPROPERTIES (delta.enableChangeDataFeed = true) AS
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
    variant_get(chunk,'$.chunk_id','STRING')          AS chunk_id,
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
SELECT * FROM parse_chunks
UNION ALL
SELECT * FROM single_chunks
""")

# Guard against empty/null embed text sneaking into the index
run_sql(f"DELETE FROM {CFG['t']['gold']} WHERE chunk_to_embed IS NULL OR length(chunk_to_embed) = 0")

# COMMAND ----------
display(run_sql(f"""
SELECT count(*) chunks, count(distinct doc_id) docs, count(distinct claim_id) claims FROM {CFG['t']['gold']}"""))
display(run_sql(f"SELECT doc_type, modality, count(*) n FROM {CFG['t']['gold']} GROUP BY 1,2 ORDER BY n DESC"))
