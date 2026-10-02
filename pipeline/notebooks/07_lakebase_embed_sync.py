# Databricks notebook source
# MAGIC %pip install -q "psycopg[binary]>=3.1.0" "databricks-sdk>=0.118.0" mlflow
# MAGIC %restart_python

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
import time
import psycopg
import mlflow.deployments
from databricks.sdk import WorkspaceClient

LB = CFG["lakebase"]
SCHEMA = LB["schema"]
GOLD = CFG["t"]["gold"]
EMB_ENDPOINT = CFG["embedding_endpoint"]
EMB_DIM = 1024
BATCH = 20

w = WorkspaceClient()
def _conn():
    host = w.postgres.get_endpoint(name=LB["endpoint_path"]).status.hosts.host
    user = w.current_user.me().user_name
    token = w.postgres.generate_database_credential(endpoint=LB["endpoint_path"]).token
    return psycopg.connect(host=host, dbname=LB["database"], user=user, password=token, sslmode="require")

# COMMAND ----------
conn = _conn()
with conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE")
    cur.execute("CREATE EXTENSION IF NOT EXISTS lakebase_text")
    cur.execute("CREATE EXTENSION IF NOT EXISTS lakebase_tokenizer")
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.gold_chunks_embeddings")
    cur.execute(f"""
        CREATE TABLE {SCHEMA}.gold_chunks_embeddings (
            chunk_id TEXT PRIMARY KEY, doc_id TEXT, chunk_position INT, claim_id TEXT,
            doc_type TEXT, corpus TEXT, carrier TEXT, peril TEXT, loss_year INT,
            chunk_to_retrieve TEXT, chunk_to_embed TEXT,
            embedding vector({EMB_DIM}),
            chunk_tsv tsvector
        )
    """)
conn.commit()
print("Table ready (indexes deferred until after load).")

# COMMAND ----------
rows = [r.asDict() for r in spark.sql(f"""
    SELECT chunk_id, doc_id, chunk_position, claim_id, doc_type, corpus,
           carrier, peril, loss_year, chunk_to_retrieve, chunk_to_embed
    FROM {GOLD}
""").collect()]
print(f"Read {len(rows)} chunks")

deploy = mlflow.deployments.get_deploy_client("databricks")
embeddings = []
for i in range(0, len(rows), BATCH):
    texts = [r["chunk_to_embed"] or "" for r in rows[i:i+BATCH]]
    resp = deploy.predict(endpoint=EMB_ENDPOINT, inputs={"input": texts})
    embeddings.extend(d["embedding"] for d in resp.data)
print(f"Generated {len(embeddings)} embeddings")

conn = _conn()  # refresh (token may have expired during embedding)
with conn.cursor() as cur:
    for r, emb in zip(rows, embeddings):
        emb_str = "[" + ",".join(f"{v:.8f}" for v in emb) + "]"
        cur.execute(f"""
            INSERT INTO {SCHEMA}.gold_chunks_embeddings
            (chunk_id, doc_id, chunk_position, claim_id, doc_type, corpus,
             carrier, peril, loss_year, chunk_to_retrieve, chunk_to_embed,
             embedding, chunk_tsv)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_tsvector('english', %s))
        """, (r["chunk_id"], r["doc_id"], r["chunk_position"], r["claim_id"],
              r["doc_type"], r["corpus"], r["carrier"], r["peril"], r["loss_year"],
              r["chunk_to_retrieve"], r["chunk_to_embed"], emb_str,
              r["chunk_to_retrieve"] or ""))
conn.commit()
print("Loaded rows into Lakebase.")

# COMMAND ----------
conn = _conn()
with conn.cursor() as cur:
    cur.execute(f"""CREATE INDEX idx_chunks_embedding ON {SCHEMA}.gold_chunks_embeddings
                    USING lakebase_ann (embedding vector_cosine_ops)""")
    cur.execute(f"""CREATE INDEX idx_chunks_bm25 ON {SCHEMA}.gold_chunks_embeddings
                    USING lakebase_bm25 (chunk_tsv)""")
    cur.execute(f"""CREATE INDEX idx_chunks_claim ON {SCHEMA}.gold_chunks_embeddings (claim_id)""")
conn.commit()
print("Indexes built on loaded data.")

# COMMAND ----------
qvec = deploy.predict(endpoint=EMB_ENDPOINT, inputs={"input": ["damage estimate"]}).data[0]["embedding"]
qstr = "[" + ",".join(f"{v:.8f}" for v in qvec) + "]"
conn = _conn()
with conn.cursor() as cur:
    cur.execute(f"SELECT count(*) FROM {SCHEMA}.gold_chunks_embeddings")
    print("rows:", cur.fetchone()[0])
    cur.execute(f"""SELECT chunk_id, claim_id FROM {SCHEMA}.gold_chunks_embeddings
                    ORDER BY embedding <=> '{qstr}'::vector LIMIT 3""")
    print("ann sample:", cur.fetchall())
    cur.execute(f"""SELECT chunk_id FROM {SCHEMA}.gold_chunks_embeddings
                    WHERE chunk_tsv <@> to_bm25query(to_tsvector('english','damage estimate'),
                          '{SCHEMA}.idx_chunks_bm25'::regclass) < 0 LIMIT 3""")
    print("bm25 sample:", cur.fetchall())
