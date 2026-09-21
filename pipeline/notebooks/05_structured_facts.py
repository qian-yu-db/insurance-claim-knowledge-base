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
# MAGIC `max(...)` ignores NULLs and collapses the (consistent) per-document values to one row per claim.

# COMMAND ----------
run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['dim_claim']} AS
SELECT
  claim_id,
  max(carrier)         AS carrier,
  max(peril)           AS peril,
  max(policy_number)   AS policy_number,
  max(insured_name)    AS insured_name,
  max(claimant_name)   AS claimant_name,
  min(date_of_loss)    AS date_of_loss,
  max(loss_year)       AS loss_year,
  max(loss_location)   AS loss_location,
  max(adjuster_name)   AS adjuster_name,
  max(total_amount)    AS max_amount_seen,
  count(*)             AS doc_count,
  count(DISTINCT doc_type) AS distinct_doc_types
FROM {CFG['t']['docs']}
WHERE corpus = 'claim' AND claim_id <> ''
GROUP BY claim_id
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
