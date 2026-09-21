# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Bronze — file registry + modality routing
# MAGIC
# MAGIC Catalogs every file under the landing volume and derives the **routing metadata** that drives the
# MAGIC rest of the workflow. Lean by design: metadata + `source_uri` only, no inlined bytes (the parse
# MAGIC stages re-read content on demand). This table is also the backbone of a future document-browse UI.

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['bronze']} AS
SELECT
  md5(path)                                              AS doc_id,
  path                                                   AS source_uri,
  regexp_extract(path, '/(CLM-[0-9]{{4}}-[0-9]{{5}})/', 1) AS claim_id,
  regexp_extract(path, '/([^/]+)$', 1)                   AS filename,
  lower(regexp_extract(path, '\\\\.([A-Za-z0-9]+)$', 1)) AS ext,
  length                                                 AS size_bytes,
  modificationTime                                       AS modified_at,
  current_timestamp()                                    AS ingested_at,
  CASE
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('pdf','docx','rtf','dot') THEN 'document'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) = 'eml' THEN 'email'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('jpg','jpeg','png','heic','tiff','gif') THEN 'image'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('mp3','m4a') THEN 'audio'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('mov','mp4','3gp') THEN 'video'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('xlsx','csv','html') THEN 'structured'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('xml','json') THEN 'metadata'
    ELSE 'other' END                                     AS modality,
  CASE
    WHEN path LIKE '%/_shared_templates/%'
      OR regexp_extract(path,'/([^/]+)$',1) IN ('ground_truth.csv','eval_questions.json','README.md')
      OR regexp_extract(path,'/([^/]+)$',1) LIKE '%manifest.json'
      OR regexp_extract(path,'/([^/]+)$',1) LIKE '%ingestion_metadata.json' THEN 'skip'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('pdf','docx','jpg','jpeg','png','tiff') THEN 'parse'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('eml','html','xml','rtf','csv') THEN 'text'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('heic','gif') THEN 'image'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('mp3','m4a') THEN 'audio'
    WHEN lower(regexp_extract(path,'\\\\.([A-Za-z0-9]+)$',1)) IN ('mov','mp4','3gp') THEN 'media_pending'
    ELSE 'skip' END                                      AS route,
  CASE WHEN regexp_extract(path,'/(CLM-[0-9]{{4}}-[0-9]{{5}})/',1) <> '' THEN 'claim' ELSE 'reference' END AS corpus
FROM read_files('{CFG['landing_path']}', format => 'binaryFile', recursiveFileLookup => true)
""")

# COMMAND ----------
# MAGIC %md
# MAGIC ### Routing note (each file has exactly one `route`)
# MAGIC - **`parse`** — `pdf/docx/jpg/png/tiff` → `ai_parse_document` (OCR). Scanned forms yield text; damage photos yield ~empty.
# MAGIC - **`text`** — `eml/html/xml/rtf/csv` → decode/strip.
# MAGIC - **`image`** — `heic/gif` → not parseable, caption-only.
# MAGIC - **`audio`** — `mp3/m4a` → transcribed by the Whisper endpoint (nb 03c).
# MAGIC - **`media_pending`** — video (`mov/mp4/3gp`) → still deferred (no video for now).
# MAGIC
# MAGIC Image captioning (nb 03) then covers **every image that has no usable OCR text**: the `route='image'`
# MAGIC files plus any `parse`+`image` file whose parsed text is empty (the damage photos). `has_text` in nb 04
# MAGIC picks the surviving representation per document, so a scanned form is indexed as OCR text and a damage
# MAGIC photo as its caption — never both.

# COMMAND ----------
display(run_sql(f"SELECT route, modality, count(*) n FROM {CFG['t']['bronze']} GROUP BY 1,2 ORDER BY 1,2"))
