# Databricks notebook source
# MAGIC %md
# MAGIC # 04 · Silver — unified documents + **metadata engineering**
# MAGIC
# MAGIC Unions the three representations (parsed / text / caption) into one row per artifact, then builds the
# MAGIC **metadata columns** that make the downstream index filterable and hybrid-searchable.
# MAGIC
# MAGIC ### Why metadata columns matter (the design)
# MAGIC Every column here is chosen for a specific retrieval job. Vector Search will sync these to the index;
# MAGIC keep them **typed and low-cardinality where they're used as filters**.
# MAGIC
# MAGIC | Column | Type | Retrieval role |
# MAGIC |---|---|---|
# MAGIC | `claim_id` | STRING | **Filter** — scope to one claim (the #1 adjuster query) |
# MAGIC | `corpus` | STRING | **Filter** — `claim` vs `reference` (SOW/guidelines/policies) |
# MAGIC | `doc_type` | STRING | **Filter / facet** — restrict to medical, legal, FNOL, … |
# MAGIC | `modality` | STRING | **Filter / facet** — document vs image-caption vs email |
# MAGIC | `content_source` | STRING | **Filter / trust** — `parsed` / `text` / `vision_caption` (captions are model-generated) |
# MAGIC | `carrier`, `peril` | STRING | **Filter / facet** — portfolio slicing |
# MAGIC | `loss_year` | INT | **Range filter** — numeric, supports `>=`/`<=` in filters |
# MAGIC | `date_of_loss` | DATE | Display + range; keep the INT `loss_year` for fast filtering |
# MAGIC | `policy_number`, `insured_name`, `claimant_name`, `adjuster_name` | STRING | **Hybrid-search anchors** — exact-token matches the vector side misses |
# MAGIC | `total_amount` | DOUBLE | Display / range filter |
# MAGIC | `source_uri`, `filename` | STRING | **Provenance** — cite & deep-link the original file |
# MAGIC | `pii_masked` | BOOLEAN | **Governance** — was sensitive PII masked before indexing |
# MAGIC | `has_text` | BOOLEAN | Gate — only `true` rows reach the index |
# MAGIC
# MAGIC **Filtering vs. hybrid search — the distinction:**
# MAGIC - *Filtering* uses these columns as hard predicates (`filters={"claim_id": "...", "loss_year >=": 2025}`) — applied **before** ranking.
# MAGIC - *Hybrid search* combines vector similarity with **keyword/BM25 over the chunk text** — it does **not** search metadata columns. Its value here is catching exact tokens (a policy number, a name, "L4-L5") that pure semantics can miss. We embed context-enriched text (nb 06) so hybrid has strong keyword signal.

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
# Optional PII masking (default off). For an internal adjuster KB you normally KEEP party names and
# claim/policy numbers — they are the point of the search. Only mask genuinely sensitive identifiers.
if CFG["pii_mask"] == "sensitive_only":
    text_expr = "ai_mask(text_content, array('us_social_security_number','bank_account_number','credit_card_number'))"
else:
    text_expr = "text_content"

DOC_TYPES = ('["fnol_claim_form","policy_document","endorsement","adjuster_report","repair_estimate",'
             '"medical_record","acord_form","coverage_letter","release_form","claim_evaluation",'
             '"vendor_appraisal","payment_ledger","claim_metadata","email_correspondence",'
             '"adjuster_note","damage_photo","other"]')
EXTRACT_FIELDS = ('["claim_number","policy_number","insured_name","claimant_name","carrier_name",'
                  '"peril","date_of_loss","loss_location","adjuster_name","total_amount"]')

run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['docs']} AS
WITH unioned AS (
  SELECT doc_id,claim_id,corpus,source_uri,filename,ext,modality,ingested_at,parse_error,text_content,'parsed' AS content_source        FROM {CFG['t']['parsed']}
  UNION ALL
  SELECT doc_id,claim_id,corpus,source_uri,filename,ext,modality,ingested_at,parse_error,text_content,'text' AS content_source          FROM {CFG['t']['text']}
  UNION ALL
  SELECT doc_id,claim_id,corpus,source_uri,filename,ext,modality,ingested_at,parse_error,text_content,'vision_caption' AS content_source FROM {CFG['t']['captions']}
  UNION ALL
  SELECT doc_id,claim_id,corpus,source_uri,filename,ext,modality,ingested_at,parse_error,text_content,'audio_transcript' AS content_source FROM {CFG['t']['audio']}
),
flagged AS (SELECT *, (length(text_content) > 30) AS has_text FROM unioned),
enriched AS (
  SELECT *,
    CASE WHEN has_text THEN ai_classify(substr(text_content,1,4000), '{DOC_TYPES}'):response[0]::STRING END AS doc_type,
    CASE WHEN has_text THEN ai_extract(substr(text_content,1,6000), '{EXTRACT_FIELDS}') END AS ex
  FROM flagged
)
SELECT
  doc_id, claim_id, corpus, source_uri, filename, ext, modality, content_source, ingested_at, parse_error, has_text,
  {text_expr} AS text_content,
  ({'true' if CFG['pii_mask']=='sensitive_only' else 'false'}) AS pii_masked,
  doc_type,
  ex:response:policy_number:value::STRING  AS policy_number,
  ex:response:insured_name:value::STRING   AS insured_name,
  ex:response:claimant_name:value::STRING  AS claimant_name,
  ex:response:carrier_name:value::STRING   AS carrier,
  ex:response:peril:value::STRING          AS peril,
  try_cast(ex:response:date_of_loss:value::STRING AS DATE)                       AS date_of_loss,
  year(try_cast(ex:response:date_of_loss:value::STRING AS DATE))                 AS loss_year,
  ex:response:loss_location:value::STRING  AS loss_location,
  ex:response:adjuster_name:value::STRING  AS adjuster_name,
  try_cast(regexp_replace(ex:response:total_amount:value::STRING, '[$,]', '') AS DOUBLE) AS total_amount
FROM enriched
""")

# COMMAND ----------
display(run_sql(f"""
SELECT modality, content_source, count(*) docs, sum(CASE WHEN has_text THEN 1 ELSE 0 END) has_text
FROM {CFG['t']['docs']} GROUP BY 1,2 ORDER BY 1,2"""))
display(run_sql(f"""
SELECT claim_id, doc_type, carrier, peril, loss_year, policy_number
FROM {CFG['t']['docs']} WHERE has_text AND corpus='claim' ORDER BY claim_id LIMIT 20"""))
