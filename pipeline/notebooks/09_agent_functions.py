# Databricks notebook source
# MAGIC %md
# MAGIC # 09 · Agent functions — fixed SQL table functions the app's agent calls as tools
# MAGIC
# MAGIC The agent answers structured questions (payments, totals, claim header, note timelines) through these
# MAGIC Unity Catalog functions. The app runs them on its SQL warehouse (`adjuster-console/agent_server/
# MAGIC claims_functions.py`). Fixed, parameterized SQL: no SQL generation, totals computed in SQL (not by the
# MAGIC LLM), deterministic results, governed by UC.
# MAGIC
# MAGIC **Adding or changing a function** also needs the app side: a tool wrapper in `claims_functions.py`
# MAGIC (its docstring is what the LLM reads, so keep it in step with the `COMMENT`) and an `EXECUTE` grant in
# MAGIC the app's `databricks.yml`.
# MAGIC
# MAGIC Payment semantics (the same ones the Genie space used):
# MAGIC - amounts are USD; `transaction_type` is `indemnity` (to the insured/claimant), `expense` (appraisals,
# MAGIC   inspections, IA fees) or `defense_expense` (legal defense);
# MAGIC - status `Paid` (any case, trimmed) is disbursed; anything else, including a missing status (e.g.
# MAGIC   "Negotiated — pending release"), is agreed but not paid, reported as *pending*. So paid + pending
# MAGIC   always equals total claim cost;
# MAGIC - total paid = sum of paid amounts; total claim cost = indemnity + all expenses, paid or pending.
# MAGIC
# MAGIC Claim ids are matched exactly or by suffix, so `04487` and `2026-04487` find `CLM-2026-04487`. A suffix
# MAGIC can match several claims; every result carries `claim_id` so the agent can tell them apart.
# MAGIC `CREATE OR REPLACE`, so reruns are safe. Runs after 05 (and reads the claim-notes table).

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
FQ = CFG["fq"]
T = CFG["t"]
PAID = "coalesce(lower(trim(status)) = 'paid', false)"   # a NULL / unknown status counts as pending
PAID_P = PAID.replace("status", "p.status")


def claim_match(col: str, fn: str) -> str:
    """Predicate: `col` is the claim id passed to function `fn` (exact, or by trailing number)."""
    p = f"trim({fn}.claim_id)"
    return f"({col} = upper({p}) OR endswith({col}, concat('-', {p})))"


FUNCTIONS = {
"claim_details": f"""
CREATE OR REPLACE FUNCTION {FQ}.claim_details(
  claim_id STRING COMMENT 'Claim number, e.g. CLM-2026-04487 (or just 04487)')
RETURNS TABLE (claim_id STRING, carrier STRING, peril STRING, policy_number STRING,
               insured_name STRING, claimant_name STRING, date_of_loss DATE, loss_location STRING,
               adjuster_name STRING, doc_count BIGINT)
COMMENT 'Claim header for one claim: carrier (insurer), peril, policy number, insured, claimant, date and location of loss, assigned adjuster, number of documents on file. Use for who / what / when / where lookups about a claim.'
RETURN SELECT claim_id, carrier, peril, policy_number, insured_name, claimant_name, date_of_loss,
              loss_location, adjuster_name, doc_count
       FROM {T['dim_claim']} WHERE {claim_match('claim_id', 'claim_details')}
""",
"claim_payments": f"""
CREATE OR REPLACE FUNCTION {FQ}.claim_payments(
  claim_id STRING COMMENT 'Claim number, e.g. CLM-2026-04487 (or just 04487)')
RETURNS TABLE (claim_id STRING, txn_date DATE, transaction_type STRING, payee STRING,
               amount_usd DOUBLE, status STRING, source_uri STRING)
COMMENT 'Every payment line on one claim, oldest first: date, transaction_type (indemnity / expense / defense_expense), payee, amount in USD, status (Paid, or negotiated but pending), and the source ledger file. Use to list or explain individual payments.'
RETURN SELECT claim_id, txn_date, transaction_type, payee, amount_usd, status, source_uri
       FROM {T['fact_pay']} WHERE {claim_match('claim_id', 'claim_payments')}
       ORDER BY txn_date, transaction_type
""",
"claim_financials": f"""
CREATE OR REPLACE FUNCTION {FQ}.claim_financials(
  claim_id STRING COMMENT 'Claim number, e.g. CLM-2026-04487 (or just 04487)')
RETURNS TABLE (claim_id STRING, indemnity_paid DOUBLE, expense_paid DOUBLE, defense_expense_paid DOUBLE,
               total_paid DOUBLE, pending_amount DOUBLE, total_claim_cost DOUBLE,
               payment_count BIGINT, largest_payment DOUBLE)
COMMENT 'Financial summary of one claim in USD: paid indemnity, paid expense, paid defense expense, total paid, pending (agreed but not yet paid) amount, total claim cost (indemnity + all expenses, paid or pending), number of payment lines, largest single payment. Use for how much has been paid / is outstanding / the claim costs.'
RETURN SELECT claim_id,
         sum(CASE WHEN {PAID} AND transaction_type = 'indemnity' THEN amount_usd ELSE 0 END),
         sum(CASE WHEN {PAID} AND transaction_type = 'expense' THEN amount_usd ELSE 0 END),
         sum(CASE WHEN {PAID} AND transaction_type = 'defense_expense' THEN amount_usd ELSE 0 END),
         sum(CASE WHEN {PAID} THEN amount_usd ELSE 0 END),
         sum(CASE WHEN NOT {PAID} THEN amount_usd ELSE 0 END),
         sum(amount_usd),
         count(*),
         max(amount_usd)
       FROM {T['fact_pay']} WHERE {claim_match('claim_id', 'claim_financials')}
       GROUP BY claim_id
""",
"payment_totals": f"""
CREATE OR REPLACE FUNCTION {FQ}.payment_totals(
  group_by STRING DEFAULT 'all' COMMENT 'One of: all, carrier, peril, adjuster, loss_year, transaction_type, status')
RETURNS TABLE (group_value STRING, claim_count BIGINT, payment_count BIGINT, total_paid DOUBLE,
               pending_amount DOUBLE, total_claim_cost DOUBLE)
COMMENT 'Portfolio payment totals in USD across all claims, grouped by carrier, peril, adjuster, loss_year, transaction_type or status (or all = one overall row): number of claims, payment lines, total paid, pending (agreed, not yet paid), total claim cost. Use for totals, comparisons and distributions across claims.'
RETURN SELECT
         CASE lower(trim(payment_totals.group_by))
           WHEN 'all' THEN 'all claims'
           WHEN 'carrier' THEN c.carrier
           WHEN 'peril' THEN c.peril
           WHEN 'adjuster' THEN c.adjuster_name
           WHEN 'loss_year' THEN cast(c.loss_year AS STRING)
           WHEN 'transaction_type' THEN p.transaction_type
           WHEN 'status' THEN p.status
           ELSE raise_error(concat('group_by must be one of: all, carrier, peril, adjuster, loss_year, ',
                                   'transaction_type, status (got ', payment_totals.group_by, ')'))
         END AS group_value,
         count(DISTINCT p.claim_id),
         count(*),
         sum(CASE WHEN {PAID_P} THEN p.amount_usd ELSE 0 END),
         sum(CASE WHEN NOT {PAID_P} THEN p.amount_usd ELSE 0 END),
         sum(p.amount_usd)
       FROM {T['fact_pay']} p LEFT JOIN {T['dim_claim']} c ON p.claim_id = c.claim_id
       GROUP BY 1
       ORDER BY 4 DESC
""",
"top_claims": f"""
CREATE OR REPLACE FUNCTION {FQ}.top_claims(
  metric STRING DEFAULT 'total_paid' COMMENT 'One of: total_paid, total_claim_cost, pending_amount',
  n INT DEFAULT 5 COMMENT 'How many claims to return')
RETURNS TABLE (claim_id STRING, insured_name STRING, carrier STRING, peril STRING, metric_value DOUBLE)
COMMENT 'The n claims with the highest total_paid, total_claim_cost or pending_amount (USD), largest first, with insured, carrier and peril. Use for which claims cost / paid / owe the most.'
RETURN WITH f AS (
         SELECT claim_id,
                sum(CASE WHEN {PAID} THEN amount_usd ELSE 0 END) AS total_paid,
                sum(amount_usd) AS total_claim_cost,
                sum(CASE WHEN NOT {PAID} THEN amount_usd ELSE 0 END) AS pending_amount
         FROM {T['fact_pay']} GROUP BY claim_id)
       , m AS (
         SELECT f.claim_id, c.insured_name, c.carrier, c.peril,
                CASE lower(trim(top_claims.metric))
                  WHEN 'total_paid' THEN f.total_paid
                  WHEN 'total_claim_cost' THEN f.total_claim_cost
                  WHEN 'pending_amount' THEN f.pending_amount
                  ELSE raise_error(concat('metric must be one of: total_paid, total_claim_cost, ',
                                          'pending_amount (got ', top_claims.metric, ')'))
                END AS metric_value
         FROM f LEFT JOIN {T['dim_claim']} c ON f.claim_id = c.claim_id)
       SELECT claim_id, insured_name, carrier, peril, metric_value FROM (
         SELECT *, row_number() OVER (ORDER BY metric_value DESC, claim_id) AS rn FROM m)
       WHERE rn <= top_claims.n
       ORDER BY metric_value DESC
""",
"claim_notes": f"""
CREATE OR REPLACE FUNCTION {FQ}.claim_notes(
  claim_id STRING COMMENT 'Claim number, e.g. CLM-2026-04487 (or just 04487)',
  last_n INT DEFAULT 20 COMMENT 'Return only the most recent n notes')
RETURNS TABLE (claim_id STRING, note_time STRING, adjuster_id STRING, adjuster_name STRING, log STRING)
COMMENT 'Adjuster claim notes (activity log) for one claim, oldest first, limited to the most recent last_n: when (note_time, as recorded by the notes system), who (adjuster / manager / SIU id and name) and the note text. Use for the claim timeline, the latest note or status, and who did what when.'
RETURN SELECT claim_id, date_format(timestamp, 'yyyy-MM-dd HH:mm') AS note_time, adjuster_id, adjuster_name, log FROM (
         -- alias the table: it may share the function's name, which qualifies the parameters
         SELECT nt.*, row_number() OVER (PARTITION BY nt.claim_id ORDER BY nt.timestamp DESC) AS rn
         FROM {T['notes']} AS nt WHERE {claim_match('nt.claim_id', 'claim_notes')})
       WHERE rn <= claim_notes.last_n
       ORDER BY claim_id, timestamp
""",
}

for name, ddl in FUNCTIONS.items():
    run_sql(ddl)
    print(f"created {FQ}.{name}")

# COMMAND ----------
# Smoke checks against known values for the sample data.
display(run_sql(f"SELECT * FROM {FQ}.claim_details('04487')"))
display(run_sql(f"SELECT * FROM {FQ}.claim_financials('CLM-2026-04487')"))
display(run_sql(f"SELECT * FROM {FQ}.payment_totals('carrier')"))
display(run_sql(f"SELECT * FROM {FQ}.top_claims('total_claim_cost', 3)"))
display(run_sql(f"SELECT * FROM {FQ}.claim_notes('CLM-2026-04531', 3)"))
