# Databricks notebook source
# MAGIC %pip install -q "psycopg[binary]>=3.1.0" "databricks-sdk>=0.118.0"
# MAGIC %restart_python
# COMMAND ----------
# MAGIC %run ./00_config
# COMMAND ----------
import psycopg
from databricks.sdk import WorkspaceClient
LB = CFG["lakebase"]; SCHEMA = LB["schema"]; T = CFG["t"]
w = WorkspaceClient()
def _conn():
    host = w.postgres.get_endpoint(name=LB["endpoint_path"]).status.hosts.host
    user = w.current_user.me().user_name
    token = w.postgres.generate_database_credential(endpoint=LB["endpoint_path"]).token
    return psycopg.connect(host=host, dbname=LB["database"], user=user, password=token, sslmode="require")
# COMMAND ----------
DDL = {
 "dim_claim": """(claim_id TEXT PRIMARY KEY, carrier TEXT, peril TEXT, policy_number TEXT,
   insured_name TEXT, claimant_name TEXT, date_of_loss DATE, loss_year INT, loss_location TEXT,
   adjuster_name TEXT, max_amount_seen DOUBLE PRECISION, doc_count BIGINT, distinct_doc_types BIGINT)""",
 "bronze_file_registry": """(doc_id TEXT PRIMARY KEY, source_uri TEXT, claim_id TEXT, filename TEXT,
   ext TEXT, size_bytes BIGINT, modified_at TIMESTAMP, ingested_at TIMESTAMP, modality TEXT,
   route TEXT, corpus TEXT)""",
 "silver_documents": """(doc_id TEXT PRIMARY KEY, doc_type TEXT, content_source TEXT, text_content TEXT)""",
}
SELECTS = {
 "dim_claim": f"SELECT claim_id, carrier, peril, policy_number, insured_name, claimant_name, date_of_loss, loss_year, loss_location, adjuster_name, max_amount_seen, doc_count, distinct_doc_types FROM {T['dim_claim']}",
 "bronze_file_registry": f"SELECT doc_id, source_uri, claim_id, filename, ext, size_bytes, modified_at, ingested_at, modality, route, corpus FROM {T['bronze']}",
 "silver_documents": f"SELECT doc_id, doc_type, content_source, text_content FROM {T['docs']}",
}

def load(table):
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{table} {DDL[table]}")
        cur.execute(f"TRUNCATE {SCHEMA}.{table}")
        rows = [r.asDict() for r in spark.sql(SELECTS[table]).collect()]
        cols = list(rows[0].keys()) if rows else []
        if rows:
            ph = ",".join(["%s"] * len(cols))
            for r in rows:
                cur.execute(f"INSERT INTO {SCHEMA}.{table} ({','.join(cols)}) VALUES ({ph})",
                            tuple(r[c] for c in cols))
    conn.commit()
    print(f"{table}: {len(rows)} rows")

for t in ("dim_claim", "bronze_file_registry", "silver_documents"):
    load(t)
# COMMAND ----------
for t, src in [("dim_claim", T['dim_claim']), ("bronze_file_registry", T['bronze']), ("silver_documents", T['docs'])]:
    uc = spark.sql(f"SELECT count(*) c FROM {src}").collect()[0]["c"]
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SCHEMA}.{t}"); lb = cur.fetchone()[0]
    print(t, "uc:", uc, "lakebase:", lb, "OK" if uc == lb else "MISMATCH")
