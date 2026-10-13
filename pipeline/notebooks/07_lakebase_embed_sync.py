# Databricks notebook source
# MAGIC %pip install -q "psycopg[binary]>=3.1.0" "databricks-sdk>=0.118.0" mlflow
# MAGIC %restart_python

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
import psycopg
import mlflow.deployments
from databricks.sdk import WorkspaceClient

LB = CFG["lakebase"]
SCHEMA = LB["schema"]
GOLD = CFG["t"]["gold"]
EMB_ENDPOINT = CFG["embedding_endpoint"]
EMB_DIM = 1024
BATCH = 20
TABLE = "gold_chunks_embeddings"
STAGE = f"{TABLE}_staging"   # built off to the side, then swapped in so search never sees a partial table
INDEXES = {"idx_chunks_embedding": "USING lakebase_ann (embedding vector_cosine_ops)",
           "idx_chunks_bm25": "USING lakebase_bm25 (chunk_tsv)",
           "idx_chunks_claim": "(claim_id)"}

w = WorkspaceClient()
def _conn():
    host = w.postgres.get_endpoint(name=LB["endpoint_path"]).status.hosts.host
    user = w.current_user.me().user_name
    token = w.postgres.generate_database_credential(endpoint=LB["endpoint_path"]).token
    return psycopg.connect(host=host, dbname=LB["database"], user=user, password=token, sslmode="require")

# COMMAND ----------
with _conn() as conn:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE")
        cur.execute("CREATE EXTENSION IF NOT EXISTS lakebase_text")
        cur.execute("CREATE EXTENSION IF NOT EXISTS lakebase_tokenizer")
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{STAGE}")
        cur.execute(f"""
            CREATE TABLE {SCHEMA}.{STAGE} (
                chunk_id TEXT PRIMARY KEY, doc_id TEXT, chunk_position INT, claim_id TEXT,
                doc_type TEXT, corpus TEXT, carrier TEXT, peril TEXT, loss_year INT,
                chunk_to_retrieve TEXT, chunk_to_embed TEXT,
                embedding vector({EMB_DIM}),
                chunk_tsv tsvector
            )
        """)
    conn.commit()
print(f"Staging table {STAGE} ready; the live table keeps serving search until the swap.")

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
assert len(embeddings) == len(rows), f"Embedding count mismatch: {len(embeddings)} != {len(rows)}"

with _conn() as conn:  # refresh (token may have expired during embedding)
    with conn.cursor() as cur:
        cur.executemany(f"""
            INSERT INTO {SCHEMA}.{STAGE}
            (chunk_id, doc_id, chunk_position, claim_id, doc_type, corpus,
             carrier, peril, loss_year, chunk_to_retrieve, chunk_to_embed,
             embedding, chunk_tsv)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_tsvector('english', %s))
        """, [(r["chunk_id"], r["doc_id"], r["chunk_position"], r["claim_id"],
               r["doc_type"], r["corpus"], r["carrier"], r["peril"], r["loss_year"],
               r["chunk_to_retrieve"], r["chunk_to_embed"],
               "[" + ",".join(f"{v:.8f}" for v in emb) + "]",
               r["chunk_to_retrieve"] or "")
              for r, emb in zip(rows, embeddings)])
        # Indexes on the staging table (temporary names), built after the load.
        for name, spec in INDEXES.items():
            cur.execute(f"CREATE INDEX {name}_staging ON {SCHEMA}.{STAGE} {spec}")
    conn.commit()
print(f"Loaded {len(rows)} rows and built indexes on {STAGE}.")

# COMMAND ----------
# Swap in one transaction: readers see either the old table or the new one, never an empty/unindexed one.
with _conn() as conn:
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{TABLE}")
        cur.execute(f"ALTER TABLE {SCHEMA}.{STAGE} RENAME TO {TABLE}")
        for name in INDEXES:
            cur.execute(f"ALTER INDEX {SCHEMA}.{name}_staging RENAME TO {name}")
        cur.execute(f"ALTER INDEX {SCHEMA}.{STAGE}_pkey RENAME TO {TABLE}_pkey")
    conn.commit()
print(f"Swapped {STAGE} -> {TABLE}.")

# COMMAND ----------
qvec = deploy.predict(endpoint=EMB_ENDPOINT, inputs={"input": ["damage estimate"]}).data[0]["embedding"]
qstr = "[" + ",".join(f"{v:.8f}" for v in qvec) + "]"
with _conn() as conn:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SCHEMA}.{TABLE}")
        print("rows:", cur.fetchone()[0])
        cur.execute(f"""SELECT chunk_id, claim_id FROM {SCHEMA}.{TABLE}
                        ORDER BY embedding <=> '{qstr}'::vector LIMIT 3""")
        print("ann sample:", cur.fetchall())
        cur.execute(f"""SELECT chunk_id FROM {SCHEMA}.{TABLE}
                        WHERE chunk_tsv <@> to_bm25query(to_tsvector('english','damage estimate'),
                              '{SCHEMA}.idx_chunks_bm25'::regclass) < 0 LIMIT 3""")
        print("bm25 sample:", cur.fetchall())
