# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Silver — parse & text branches
# MAGIC
# MAGIC - **Parse branch** (`route='parse'`): `ai_parse_document` on PDF/DOCX + OCR of scanned images.
# MAGIC   Persists the parsed VARIANT so nb 06 can feed it to `ai_prep_search`.
# MAGIC - **Text branch** (`route='text'`): decode/strip eml/html/xml/rtf/csv; an LLM lifts the readable
# MAGIC   body out of `.eml` MIME.
# MAGIC
# MAGIC Both re-read bytes from the volume on demand (Bronze holds no bytes).

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
# MAGIC %md ## Parse branch → `silver_parsed`

# COMMAND ----------
run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['parsed']} AS
WITH src AS (
  SELECT b.doc_id, b.claim_id, b.corpus, b.source_uri, b.filename, b.ext, b.modality, b.ingested_at,
         ai_parse_document(r.content) AS parsed
  FROM read_files('{CFG['landing_path']}', format => 'binaryFile', recursiveFileLookup => true) r
  JOIN {CFG['t']['bronze']} b ON r.path = b.source_uri
  WHERE b.route = 'parse'
)
SELECT doc_id, claim_id, corpus, source_uri, filename, ext, modality, ingested_at,
       parsed:error_status::STRING AS parse_error,
       concat_ws('\\n', transform(cast(parsed:document:elements AS ARRAY<VARIANT>), e -> e:content::STRING)) AS text_content,
       parsed AS parsed_variant
FROM src
""")
display(run_sql(f"""
SELECT ext, count(*) n, sum(CASE WHEN length(text_content)>30 THEN 1 ELSE 0 END) has_text, count(parse_error) errors
FROM {CFG['t']['parsed']} GROUP BY ext ORDER BY ext"""))

# COMMAND ----------
# MAGIC %md ## Text branch → `silver_text`

# COMMAND ----------
run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['text']} AS
SELECT b.doc_id, b.claim_id, b.corpus, b.source_uri, b.filename, b.ext, b.modality, b.ingested_at,
  CAST(NULL AS STRING) AS parse_error,
  CASE b.ext
    WHEN 'eml'  THEN ai_query('{CFG['email_endpoint']}',
                    concat('Return ONLY the human-readable body of this email (sender line, greeting, message, sign-off). ',
                           'Omit MIME headers, boundaries, base64 blobs, and attachments. Email follows:\\n\\n',
                           substr(decode(r.content,'UTF-8'), 1, 12000)),
                    failOnError => false).result
    WHEN 'html' THEN regexp_replace(regexp_replace(decode(r.content,'UTF-8'), '<[^>]+>', ' '), '\\\\s+', ' ')
    WHEN 'rtf'  THEN regexp_replace(regexp_replace(decode(r.content,'UTF-8'), '\\\\\\\\[a-zA-Z]+[0-9-]*', ' '), '[{{}}]', ' ')
    ELSE decode(r.content,'UTF-8')
  END AS text_content
FROM read_files('{CFG['landing_path']}', format => 'binaryFile', recursiveFileLookup => true) r
JOIN {CFG['t']['bronze']} b ON r.path = b.source_uri
WHERE b.route = 'text'
""")
display(run_sql(f"SELECT ext, count(*) n, round(avg(length(text_content))) avg_len FROM {CFG['t']['text']} GROUP BY ext ORDER BY ext"))
