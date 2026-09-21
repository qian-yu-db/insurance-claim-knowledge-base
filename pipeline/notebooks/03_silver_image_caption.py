# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · Silver — image captions (vision)
# MAGIC
# MAGIC Gives a **text representation to images that carry no OCR text** — damage photos, the held-in-hand
# MAGIC document photo, email-signature graphics — so they become searchable in the same index.
# MAGIC
# MAGIC Caption set = images that need it:
# MAGIC - `route='image'` (heic/gif — not parseable at all), **plus**
# MAGIC - `route='parse'` + `modality='image'` whose OCR text came back empty (the damage photos).
# MAGIC
# MAGIC Uses `ai_query(vision_endpoint, prompt, files => content)` — direct image bytes, no upload step.
# MAGIC `failOnError => false` so an unsupported format (e.g. HEIC on some models) parks in a sidecar
# MAGIC column instead of failing the batch.

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
CAPTION_PROMPT = (
    "You are an insurance claims assistant. Describe this image factually in 2-4 sentences for a "
    "searchable claim file. If it shows vehicle or property damage, state the object, the damaged "
    "areas, and apparent severity (minor/moderate/severe). If it is a document/form/photo of a "
    "document, say so and summarize any legible text. Do not speculate about fault."
)

# COMMAND ----------
# Which image docs still need a caption (no usable OCR text in silver_parsed)?
run_sql(f"""
CREATE OR REPLACE TEMP VIEW _to_caption AS
SELECT b.doc_id, b.claim_id, b.corpus, b.source_uri, b.filename, b.ext, b.modality, b.ingested_at
FROM {CFG['t']['bronze']} b
LEFT JOIN {CFG['t']['parsed']} p ON b.doc_id = p.doc_id
WHERE b.modality = 'image'
  AND (b.route = 'image' OR length(coalesce(p.text_content, '')) <= 30)
""")

run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['captions']} AS
SELECT doc_id, claim_id, corpus, source_uri, filename, ext, modality, ingested_at,
       cap.errorMessage AS parse_error,
       cap.result       AS text_content
FROM (
  SELECT v.doc_id, v.claim_id, v.corpus, v.source_uri, v.filename, v.ext, v.modality, v.ingested_at,
         ai_query('{CFG['vision_endpoint']}', '{CAPTION_PROMPT}', files => r.content, failOnError => false) AS cap
  FROM _to_caption v
  JOIN read_files('{CFG['landing_path']}', format => 'binaryFile', recursiveFileLookup => true) r
    ON r.path = v.source_uri
)
""")
display(run_sql(f"""
SELECT ext, count(*) n, sum(CASE WHEN parse_error IS NOT NULL THEN 1 ELSE 0 END) errors,
       round(avg(length(text_content))) avg_len
FROM {CFG['t']['captions']} GROUP BY ext ORDER BY ext"""))

# COMMAND ----------
# MAGIC %md
# MAGIC **Note on HEIC/GIF:** most vision endpoints accept JPEG/PNG. HEIC/GIF may return an error
# MAGIC (captured in `parse_error`, `text_content` NULL) — those rows simply won't reach the index.
# MAGIC If HEIC coverage matters, normalize images to PNG at ingest (a small pre-step in the landing volume).
