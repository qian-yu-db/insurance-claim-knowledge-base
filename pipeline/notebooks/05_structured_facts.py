# Databricks notebook source
# MAGIC %md
# MAGIC # 05 · Structured facts (Delta for SQL / Genie — NOT the vector index)
# MAGIC
# MAGIC Semantic search over a payment ledger is weak; exact aggregation is the right tool. This stage lands
# MAGIC structured data as Delta tables for SQL/BI/Genie, complementing the vector index:
# MAGIC - **`fact_payments`** — line items from the claim payment-ledger CSVs.
# MAGIC - **`dim_claim`** — one row per claim, the best extracted value per field (the claim's "header").
# MAGIC
# MAGIC A serving layer (later) can combine narrative answers (vector index) with exact facts (these tables).

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
# MAGIC %md ## fact_payments — from payment-ledger CSVs

# COMMAND ----------
run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['fact_pay']} AS
SELECT
  regexp_extract(_metadata.file_path, '/(CLM-[0-9]{{4}}-[0-9]{{5}})/', 1) AS claim_id,
  claim_number,
  transaction_type,
  try_cast(date AS DATE)          AS txn_date,
  payee,
  try_cast(amount_usd AS DOUBLE)  AS amount_usd,
  status,
  _metadata.file_path             AS source_uri
FROM read_files('{CFG['landing_path']}', format => 'csv', header => true, recursiveFileLookup => true)
WHERE _metadata.file_path LIKE '%payment_ledger%'
""")
display(run_sql(f"SELECT claim_id, transaction_type, payee, amount_usd, status FROM {CFG['t']['fact_pay']} ORDER BY claim_id"))

# COMMAND ----------
# MAGIC %md ## dim_claim — per-claim header from extracted metadata
# MAGIC Each field is extracted per document (nb 04), and documents disagree (OCR misreads, a first name
# MAGIC only, a template address). For each claim and field, the value is chosen in this order:
# MAGIC 1. the claim's metadata record (`doc_type = 'claim_metadata'`), the system-of-record values;
# MAGIC 2. otherwise the value the most documents agree on;
# MAGIC 3. ties go to the FNOL / ACORD form, then to the longer (more specific) value.
# MAGIC
# MAGIC Adjuster names drop a trailing "- License …". A field no document provides stays NULL (e.g. no
# MAGIC claimant extracted: it isn't guessed from the insured, which is wrong for third-party claims).

# COMMAND ----------
FIELDS = ["carrier", "peril", "policy_number", "insured_name", "claimant_name",
          "date_of_loss", "loss_location", "adjuster_name"]
stack = ", ".join(f"'{f}', cast({f} AS STRING)" for f in FIELDS)
pick = ",\n  ".join(f"max(CASE WHEN field = '{f}' THEN value END) AS {f}" for f in FIELDS)

run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['dim_claim']} AS
WITH claim_docs AS (
  SELECT * FROM {CFG['t']['docs']} WHERE corpus = 'claim' AND claim_id <> ''
),
vals AS (
  SELECT claim_id, f.field,
         CASE WHEN f.field = 'adjuster_name'
              THEN regexp_replace(trim(f.value), '\\\\s*-\\\\s*License.*$', '') ELSE trim(f.value) END AS value,
         CASE WHEN doc_type = 'claim_metadata' THEN 1
              WHEN doc_type IN ('fnol_claim_form', 'acord_form') THEN 2 ELSE 3 END AS prio
  FROM claim_docs
  LATERAL VIEW stack({len(FIELDS)}, {stack}) f AS field, value
  WHERE nullif(trim(f.value), '') IS NOT NULL
),
best AS (
  SELECT claim_id, field, value FROM (
    SELECT claim_id, field, value, min(prio) AS prio, count(*) AS n FROM vals GROUP BY 1, 2, 3)
  QUALIFY row_number() OVER (PARTITION BY claim_id, field
                             ORDER BY prio = 1 DESC, n DESC, prio, length(value) DESC) = 1
),
header AS (
  SELECT claim_id,
  {pick}
  FROM best GROUP BY claim_id
),
stats AS (
  SELECT claim_id, max(total_amount) AS max_amount_seen, count(*) AS doc_count,
         count(DISTINCT doc_type) AS distinct_doc_types
  FROM claim_docs GROUP BY claim_id
)
SELECT
  s.claim_id, h.carrier, h.peril, h.policy_number, h.insured_name,
  h.claimant_name,
  cast(h.date_of_loss AS DATE)              AS date_of_loss,
  year(cast(h.date_of_loss AS DATE))        AS loss_year,
  h.loss_location, h.adjuster_name,
  s.max_amount_seen, s.doc_count, s.distinct_doc_types
FROM stats s LEFT JOIN header h USING (claim_id)
""")
display(run_sql(f"SELECT * FROM {CFG['t']['dim_claim']} ORDER BY claim_id"))

# COMMAND ----------
# Join example: total paid per claim (structured), alongside the header
display(run_sql(f"""
SELECT c.claim_id, c.carrier, c.peril, c.insured_name,
       sum(CASE WHEN p.transaction_type='indemnity' THEN p.amount_usd ELSE 0 END) AS indemnity_paid,
       sum(p.amount_usd) AS total_paid
FROM {CFG['t']['dim_claim']} c
LEFT JOIN {CFG['t']['fact_pay']} p USING (claim_id)
GROUP BY 1,2,3,4 ORDER BY 1"""))
