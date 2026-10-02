# V2 Lakebase pg-vector Hybrid Retrieval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Adjuster Console's AI Search retrieval with Lakebase `lakebase_ann` + `lakebase_bm25` hybrid search (RRF), with embedding/metadata sync owned by the pipeline and the app reading only.

**Architecture:** The pipeline embeds `gold_chunks` into Lakebase (`claims.gold_chunks_embeddings`, notebook `07`) and syncs metadata tables (notebook `08`). The app gains a `LakebasePool` + an in-process `claims_document_search` tool (replacing the Vector Search MCP server), and its data API reads Lakebase with a SQL-warehouse fallback. Branded UI and streaming from `v1.1.0` are unchanged.

**Tech Stack:** Python 3.11, Databricks SDK (`w.postgres`), psycopg3, pgvector/`lakebase_vector`/`lakebase_text`, FastAPI, OpenAI Agents SDK, MLflow, DABs.

**Spec:** `docs/superpowers/specs/2026-10-01-v2-lakebase-pgvector-design.md`

## Global Constraints

- Lakebase: project `insurance-claim`, branch `production`, endpoint `primary`, db `databricks_postgres`, schema `claims`. Endpoint path `projects/insurance-claim/branches/production/endpoints/primary`.
- Instance id (reference only): `5bf96c8d-74c8-4782-88a8-c1c11f589485`.
- Embedding model `databricks-gte-large-en`, dimension `1024`, column type `vector(1024)`.
- Orchestrator model `databricks-gpt-5-2` (unchanged).
- UC source: `fins_genai.claims_multimodal_kb` (`gold_chunks`, `dim_claim`, `bronze_file_registry`, `silver_documents`).
- Databricks profile for CLI/local runs: `fevm-classic-stable`.
- App SDK floor: `databricks-sdk>=0.118.0`; add `psycopg[binary]>=3.1`. `sse-starlette` already present.
- Retrieval returns `source_uri` so the `v1.1.0` UI citation links keep working; the agent cites `source_uri` (not raw `doc_id`).
- Index access methods: vector `USING lakebase_ann (embedding vector_cosine_ops)`; BM25 `USING lakebase_bm25 (chunk_tsv)`. BM25 operator `<@>` returns a negative score (ascending = best). RRF constant k=60; candidate pool = `top_k*3`.
- No jobs in the app bundle. Pipeline owns all Lakebase population.
- Commit attribution: end commit messages with `Co-authored-by: Isaac <no-reply@databricks.com>`.

## Review Focus

- **Lakebase unavailable at startup** → data API must serve metadata from the SQL warehouse, not 500. (Task 7 tests.)
- **`claim_id` filter present vs absent** → hybrid SQL must bind params in the right order for both branches. (Task 5 tests.)
- **BM25 returns zero matches** → RRF `FULL OUTER JOIN` must still return the vector-only candidates. (Task 5 tests.)
- **Vector side returns zero (empty table / no index)** → `search_documents` returns `[]`, agent says "no documents found" rather than erroring. (Task 5 + Task 6 tests.)
- **App SP cannot read the `claims` schema** → explicit GRANT after load; deploy-before-local ordering. (Task 10.)

---

## Phase 1 — Pipeline indexing

### Task 1: Lakebase config in the pipeline

**Files:**
- Modify: `pipeline/notebooks/00_config.py`

**Interfaces:**
- Produces: `CFG["lakebase"]` dict with keys `project_id, branch_id, endpoint_id, database, schema, endpoint_path`; `CFG["embedding_endpoint"]` (already exists).

- [ ] **Step 1: Add Lakebase widgets + config block**

Add after the existing widget definitions in `00_config.py`:

```python
dbutils.widgets.text("lakebase_project_id", "insurance-claim", "Lakebase project id")
dbutils.widgets.text("lakebase_branch_id", "production", "Lakebase branch")
dbutils.widgets.text("lakebase_endpoint_id", "primary", "Lakebase endpoint")
dbutils.widgets.text("lakebase_database", "databricks_postgres", "Lakebase database")
dbutils.widgets.text("lakebase_schema", "claims", "Lakebase schema")

_lb_proj = dbutils.widgets.get("lakebase_project_id")
_lb_branch = dbutils.widgets.get("lakebase_branch_id")
_lb_endpoint = dbutils.widgets.get("lakebase_endpoint_id")
CFG["lakebase"] = {
    "project_id": _lb_proj,
    "branch_id": _lb_branch,
    "endpoint_id": _lb_endpoint,
    "database": dbutils.widgets.get("lakebase_database"),
    "schema": dbutils.widgets.get("lakebase_schema"),
    "endpoint_path": f"projects/{_lb_proj}/branches/{_lb_branch}/endpoints/{_lb_endpoint}",
}
```

- [ ] **Step 2: Verify config loads**

Run the `00_config` cell in the pipeline (or `%run ./00_config` from a scratch cell). Expected: `CFG["lakebase"]["endpoint_path"] == "projects/insurance-claim/branches/production/endpoints/primary"` and `CFG["embedding_endpoint"] == "databricks-gte-large-en"`.

- [ ] **Step 3: Commit**

```bash
git add pipeline/notebooks/00_config.py
git commit -m "Add Lakebase connection config to pipeline 00_config

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: Notebook 07 — embed `gold_chunks` into Lakebase (replaces the VS index)

**Files:**
- Delete: `pipeline/notebooks/07_vector_search_index.py`
- Create: `pipeline/notebooks/07_lakebase_embed_sync.py`

**Interfaces:**
- Consumes: `CFG["t"]["gold"]`, `CFG["lakebase"]`, `CFG["embedding_endpoint"]`.
- Produces: Lakebase table `claims.gold_chunks_embeddings` with columns per the spec (incl. stored `chunk_tsv`) and indexes `idx_chunks_embedding` (lakebase_ann), `idx_chunks_bm25` (lakebase_bm25 on `chunk_tsv`), `idx_chunks_claim` (btree). Built **after** load.

- [ ] **Step 1: Create the notebook with install + config cells**

```python
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
```

- [ ] **Step 2: Create extensions, schema, and table (no indexes yet)**

```python
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
```

Note: `DROP TABLE` keeps the pipeline idempotent and ensures indexes are rebuilt on fresh data each run.

- [ ] **Step 3: Read gold_chunks, embed in batches, insert with `chunk_tsv`**

```python
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
```

- [ ] **Step 4: Build indexes AFTER load (optimization #2)**

```python
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
```

- [ ] **Step 5: Verify counts + a sample hybrid query**

```python
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
```
Expected: `rows` equals the `gold_chunks` row count; both samples return rows.

- [ ] **Step 6: Delete the old VS-index notebook and commit**

```bash
git rm pipeline/notebooks/07_vector_search_index.py
git add pipeline/notebooks/07_lakebase_embed_sync.py
git commit -m "Replace VS index (notebook 07) with Lakebase embed+sync

Embeds gold_chunks into claims.gold_chunks_embeddings with a stored chunk_tsv
column; builds lakebase_ann + lakebase_bm25 indexes after load.

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 3: Notebook 08 — sync metadata tables into Lakebase

**Files:**
- Create: `pipeline/notebooks/08_lakebase_metadata_sync.py`

**Interfaces:**
- Consumes: `CFG["t"]` table names, `CFG["lakebase"]`.
- Produces: Lakebase `claims.dim_claim`, `claims.bronze_file_registry`, `claims.silver_documents` with the columns the data API reads (incl. `dim_claim.loss_location, adjuster_name`; `bronze_file_registry.source_uri, filename, ext, modality, size_bytes, route, doc_id, claim_id`; `silver_documents.doc_id, doc_type, content_source, text_content`).

- [ ] **Step 1: Create the notebook (install + config + connect)**

```python
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
```

- [ ] **Step 2: Create metadata tables + a generic Spark→Lakebase loader**

```python
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
 "silver_documents": f"SELECT doc_id, doc_type, content_source, text_content FROM {T['silver_docs']}",
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
```

Confirm `T` keys (`dim_claim`, `bronze`, `silver_docs`) against `00_config.py`; adjust the `SELECTS` keys to the actual `CFG["t"]` names before running.

- [ ] **Step 3: Verify counts match UC**

```python
# COMMAND ----------
for t, src in [("dim_claim", T['dim_claim']), ("bronze_file_registry", T['bronze']), ("silver_documents", T['silver_docs'])]:
    uc = spark.sql(f"SELECT count(*) c FROM {src}").collect()[0]["c"]
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SCHEMA}.{t}"); lb = cur.fetchone()[0]
    print(t, "uc:", uc, "lakebase:", lb, "OK" if uc == lb else "MISMATCH")
```
Expected: all `OK`.

- [ ] **Step 4: Commit**

```bash
git add pipeline/notebooks/08_lakebase_metadata_sync.py
git commit -m "Add pipeline notebook 08: sync metadata tables to Lakebase

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Phase 2 — App wiring

### Task 4: `LakebasePool` connection module

**Files:**
- Create: `adjuster-console/agent_server/lakebase.py`

**Interfaces:**
- Produces: module singleton `pool` with `pool.initialize()`, `pool.available` (bool), `pool.query(sql, params=None) -> list[dict]`. Reads env `LAKEBASE_PROJECT_ID` (default `insurance-claim`), `LAKEBASE_BRANCH_ID` (`production`), `LAKEBASE_ENDPOINT_ID` (`primary`), `LAKEBASE_DATABASE` (`databricks_postgres`), `LAKEBASE_SCHEMA` (`claims`).

- [ ] **Step 1: Write the module**

```python
"""Lakebase Postgres connection pool with OAuth token rotation (read-only queries)."""
from __future__ import annotations
import logging, os, threading, time
from contextlib import contextmanager
from typing import Any
import psycopg
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)
PROJECT_ID = os.environ.get("LAKEBASE_PROJECT_ID", "insurance-claim")
BRANCH_ID = os.environ.get("LAKEBASE_BRANCH_ID", "production")
ENDPOINT_ID = os.environ.get("LAKEBASE_ENDPOINT_ID", "primary")
DATABASE = os.environ.get("LAKEBASE_DATABASE", "databricks_postgres")
SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "claims")
_REFRESH_INTERVAL = 40 * 60

class LakebasePool:
    def __init__(self):
        self._w = WorkspaceClient()
        self._endpoint = ""; self._host = ""; self._user = ""; self._token = ""
        self._lock = threading.Lock(); self._initialized = False

    def _refresh_token(self) -> None:
        cred = self._w.postgres.generate_database_credential(endpoint=self._endpoint)
        with self._lock:
            self._token = cred.token

    def initialize(self) -> None:
        if self._initialized or not PROJECT_ID:
            if not PROJECT_ID:
                logger.warning("LAKEBASE_PROJECT_ID unset; using SQL warehouse fallback")
            return
        self._endpoint = f"projects/{PROJECT_ID}/branches/{BRANCH_ID}/endpoints/{ENDPOINT_ID}"
        self._host = self._w.postgres.get_endpoint(name=self._endpoint).status.hosts.host
        self._user = self._w.current_user.me().user_name
        self._refresh_token()
        def _loop():
            while True:
                time.sleep(_REFRESH_INTERVAL)
                try: self._refresh_token()
                except Exception: logger.exception("Lakebase token refresh failed")
        threading.Thread(target=_loop, daemon=True).start()
        self._initialized = True
        logger.info("Lakebase pool initialized (project=%s)", PROJECT_ID)

    @property
    def available(self) -> bool:
        return self._initialized

    @contextmanager
    def connection(self):
        with self._lock:
            token = self._token
        # scale-to-zero: compute wakes in ~100ms; retry once on connect failure
        try:
            conn = psycopg.connect(host=self._host, dbname=DATABASE, user=self._user,
                                   password=token, sslmode="require",
                                   options=f"-c search_path={SCHEMA},public")
        except psycopg.OperationalError:
            time.sleep(0.5)
            conn = psycopg.connect(host=self._host, dbname=DATABASE, user=self._user,
                                   password=token, sslmode="require",
                                   options=f"-c search_path={SCHEMA},public")
        try:
            yield conn
        finally:
            conn.close()

    def query(self, sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '30s'")
                cur.execute(sql, params)
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

pool = LakebasePool()
```

- [ ] **Step 2: Smoke-import (no DB needed)**

Run: `cd adjuster-console && DATABRICKS_CONFIG_PROFILE=fevm-classic-stable uv run python -c "from agent_server.lakebase import pool; print('available before init:', pool.available)"`
Expected: prints `available before init: False` (construction doesn't connect).

- [ ] **Step 3: Commit**

```bash
git add adjuster-console/agent_server/lakebase.py
git commit -m "Add LakebasePool: OAuth-rotating Lakebase connection pool

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 5: `lakebase_search` hybrid retriever (with testable SQL builder)

**Files:**
- Create: `adjuster-console/agent_server/lakebase_search.py`
- Test: `adjuster-console/tests/test_lakebase_search.py`

**Interfaces:**
- Consumes: `agent_server.lakebase.pool`.
- Produces: `build_hybrid_sql(emb_str: str, claim_id: str | None, top_k: int) -> tuple[str, tuple]` (pure, testable); `search_documents(query: str, claim_id: str | None = None, top_k: int = 10) -> list[dict]` (each dict has `chunk_id, doc_id, claim_id, doc_type, corpus, carrier, peril, filename, source_uri, content, relevance_score`).

- [ ] **Step 1: Write failing tests for the SQL builder**

```python
# adjuster-console/tests/test_lakebase_search.py
from agent_server.lakebase_search import build_hybrid_sql

def test_no_claim_filter_param_order():
    sql, params = build_hybrid_sql("[0.1,0.2]", None, top_k=10)
    assert params == ("damage",) is False  # placeholder; real check below
    # 3 bm25 bind points (SELECT/WHERE/ORDER BY) each receive the query text
    assert params.count  # params is a tuple
    assert len(params) == 3
    assert "FULL OUTER JOIN" in sql
    assert "chunk_tsv <@> to_bm25query" in sql
    assert "to_tsvector('english', g.chunk_to_retrieve)" not in sql  # optimization #1
    assert "LIMIT 30" in sql  # candidate_k = top_k*3
    assert "LIMIT 10" in sql  # final

def test_claim_filter_adds_two_params():
    sql, params = build_hybrid_sql("[0.1,0.2]", "CLM-1", top_k=5)
    # order: (claim_id, query, query, claim_id, query)
    assert params == ("CLM-1", "<q>", "<q>", "CLM-1", "<q>") is False
    assert len(params) == 5
    assert params[0] == "CLM-1" and params[3] == "CLM-1"
    assert "g.claim_id = %s" in sql
```

Replace the two intentionally-false placeholder asserts when writing the real test: assert the exact param tuples — `("<q>","<q>","<q>")` becomes the literal query string you pass in. Use `q = "water damage"` and assert `params == (q, q, q)` / `(cid, q, q, cid, q)`.

Final test bodies:
```python
def test_no_claim_filter_param_order():
    q = "water damage"
    sql, params = build_hybrid_sql("[0.1,0.2]", None, top_k=10, query_text=q)
    assert params == (q, q, q)
    assert "FULL OUTER JOIN" in sql and "chunk_tsv <@> to_bm25query" in sql
    assert "to_tsvector('english', g.chunk_to_retrieve)" not in sql
    assert "LIMIT 30" in sql and sql.rstrip().endswith("LIMIT 10")

def test_claim_filter_adds_two_params():
    q = "water damage"
    sql, params = build_hybrid_sql("[0.1,0.2]", "CLM-1", top_k=5, query_text=q)
    assert params == ("CLM-1", q, q, "CLM-1", q)
    assert "g.claim_id = %s" in sql
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd adjuster-console && uv run pytest tests/test_lakebase_search.py -v`
Expected: FAIL (`build_hybrid_sql` not defined).

- [ ] **Step 3: Implement the module**

```python
"""Lakebase hybrid search: lakebase_ann (cosine) + lakebase_bm25, fused with RRF (k=60)."""
from __future__ import annotations
import logging, os
from typing import Any
import mlflow, mlflow.deployments
from mlflow.entities import Document
from agent_server.lakebase import pool as lb

logger = logging.getLogger(__name__)
EMBEDDING_ENDPOINT = os.environ.get("EMBEDDING_ENDPOINT", "databricks-gte-large-en")
SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "claims")
TABLE = "gold_chunks_embeddings"
BM25_INDEX = f"{SCHEMA}.idx_chunks_bm25"
RRF_K = 60
_deploy = None

def _embed(text: str) -> list[float]:
    global _deploy
    if _deploy is None:
        _deploy = mlflow.deployments.get_deploy_client("databricks")
    return _deploy.predict(endpoint=EMBEDDING_ENDPOINT, inputs={"input": [text]}).data[0]["embedding"]

def build_hybrid_sql(emb_str: str, claim_id: str | None, top_k: int, query_text: str = "") -> tuple[str, tuple]:
    candidate_k = top_k * 3
    bm25 = f"g.chunk_tsv <@> to_bm25query(to_tsvector('english', %s), '{BM25_INDEX}'::regclass)"
    if claim_id:
        vector_where = "WHERE g.claim_id = %s"
        text_and_claim = "AND g.claim_id = %s"
        params: tuple = (claim_id, query_text, query_text, claim_id, query_text)
    else:
        vector_where = ""
        text_and_claim = ""
        params = (query_text, query_text, query_text)
    sql = f"""
    WITH vector_cte AS (
        SELECT g.chunk_id, ROW_NUMBER() OVER (ORDER BY g.embedding <=> '{emb_str}'::vector ASC) AS vrank
        FROM {SCHEMA}.{TABLE} g {vector_where}
        ORDER BY g.embedding <=> '{emb_str}'::vector ASC LIMIT {candidate_k}
    ),
    text_cte AS (
        SELECT g.chunk_id, ROW_NUMBER() OVER (ORDER BY ({bm25}) ASC) AS trank
        FROM {SCHEMA}.{TABLE} g
        WHERE ({bm25}) < 0 {text_and_claim}
        ORDER BY ({bm25}) ASC LIMIT {candidate_k}
    ),
    rrf AS (
        SELECT COALESCE(v.chunk_id, t.chunk_id) AS chunk_id,
               COALESCE(1.0/({RRF_K}.0+v.vrank),0.0) + COALESCE(1.0/({RRF_K}.0+t.trank),0.0) AS rrf_score
        FROM vector_cte v FULL OUTER JOIN text_cte t ON v.chunk_id = t.chunk_id
    )
    SELECT g.chunk_id, g.doc_id, g.claim_id, g.doc_type, g.corpus, g.carrier, g.peril,
           g.chunk_to_retrieve, b.filename, b.source_uri, rrf.rrf_score
    FROM rrf JOIN {SCHEMA}.{TABLE} g ON rrf.chunk_id = g.chunk_id
    LEFT JOIN {SCHEMA}.bronze_file_registry b ON g.doc_id = b.doc_id
    ORDER BY rrf.rrf_score DESC LIMIT {top_k}"""
    return sql, params

def search_documents(query: str, claim_id: str | None = None, top_k: int = 10) -> list[dict]:
    if not lb.available:
        logger.warning("Lakebase unavailable for hybrid search")
        return []
    emb_str = "[" + ",".join(f"{v:.8f}" for v in _embed(query)) + "]"
    sql, params = build_hybrid_sql(emb_str, claim_id, top_k, query_text=query)
    try:
        with mlflow.start_span(name="hybrid_retriever", span_type="RETRIEVER") as span:
            span.set_inputs({"query": query, "claim_id": claim_id, "top_k": top_k})
            rows = lb.query(sql, params)
            results = [{
                "chunk_id": r.get("chunk_id"), "doc_id": r.get("doc_id"), "claim_id": r.get("claim_id"),
                "doc_type": r.get("doc_type"), "corpus": r.get("corpus"), "carrier": r.get("carrier"),
                "peril": r.get("peril"), "filename": r.get("filename"), "source_uri": r.get("source_uri"),
                "content": r.get("chunk_to_retrieve"), "relevance_score": float(r.get("rrf_score", 0.0)),
            } for r in rows]
            span.set_outputs([Document(id=r["chunk_id"], page_content=r["content"] or "",
                              metadata={k: v for k, v in r.items() if k not in ("content", "chunk_id")})
                              for r in results])
        return results
    except Exception as e:
        logger.error("Lakebase hybrid search failed: %s", e)
        return []
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd adjuster-console && uv run pytest tests/test_lakebase_search.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add adjuster-console/agent_server/lakebase_search.py adjuster-console/tests/test_lakebase_search.py
git commit -m "Add Lakebase hybrid search (ann + bm25 + RRF) with stored chunk_tsv

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 6: Swap the agent's retrieval tool

**Files:**
- Modify: `adjuster-console/agent_server/agent.py`
- Test: `adjuster-console/tests/test_agent_tool.py`

**Interfaces:**
- Consumes: `agent_server.lakebase_search.search_documents`.
- Produces: `build_mcp_servers()` returns only Genie + python_exec (no VS); `claims_document_search(query, claim_id="")` function tool registered on the agent.

- [ ] **Step 1: Write failing test (MCP server list excludes VS; tool present)**

```python
# adjuster-console/tests/test_agent_tool.py
import os
os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", "fevm-classic-stable")

def test_no_vector_search_mcp():
    from agent_server import agent
    urls = [getattr(s, "url", "") for s in agent.build_mcp_servers()]
    assert not any("vector-search" in u for u in urls)
    assert any("genie" in u for u in urls)

def test_doc_search_tool_registered():
    from agent_server import agent
    assert hasattr(agent, "claims_document_search")
```

- [ ] **Step 2: Run test, verify fail**

Run: `cd adjuster-console && uv run pytest tests/test_agent_tool.py -v`
Expected: FAIL (`vector-search` still present / import error).

- [ ] **Step 3: Edit `agent.py`**

Remove the Vector Search `McpServer` entry from `build_mcp_servers()` (keep Genie + python_exec). Delete the `VS_CATALOG/VS_SCHEMA/VS_INDEX` constants. Add near the other tool code:

```python
import json
from agents import function_tool
from agent_server.lakebase_search import search_documents

@function_tool
def claims_document_search(query: str, claim_id: str = "") -> str:
    """Search the claims document knowledge base for relevant passages.

    Use for document/narrative questions: what happened, medical findings, injuries,
    correspondence, defense counsel, negotiation, coverage letters, investigation/SIU,
    damage descriptions.
    Args:
        query: Natural language search query.
        claim_id: Optional claim number to restrict results to one claim.
    """
    results = search_documents(query=query, claim_id=claim_id or None, top_k=10)
    if not results:
        return "No matching documents found."
    return json.dumps(results, default=str)
```

Register it on the agent in `create_orchestrator_agent(...)`: add `tools=[claims_document_search]` to the `Agent(...)` constructor. Keep `MODEL = "databricks-gpt-5-2"`. The instructions already say to cite `source_uri` — keep that; `search_documents` now returns `source_uri`.

- [ ] **Step 4: Run tests, verify pass**

Run: `cd adjuster-console && uv run pytest tests/test_agent_tool.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add adjuster-console/agent_server/agent.py adjuster-console/tests/test_agent_tool.py
git commit -m "Swap Vector Search MCP for in-process Lakebase document search tool

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 7: Lakebase-first data API with SQL-warehouse fallback

**Files:**
- Modify: `adjuster-console/agent_server/data_api.py`
- Test: `adjuster-console/tests/test_data_api_fallback.py`

**Interfaces:**
- Consumes: `agent_server.lakebase.pool`.
- Produces: `_query(sql_lb, sql_wh, params_lb=None, params_wh=None) -> list[dict]` — Lakebase when `pool.available`, else SQL warehouse; on Lakebase exception, fall back to SQL warehouse. Endpoints `/claims`, `/claims/{id}/docs`, `/claims/{id}/summary`, `/file` use it with both query dialects (`%s` for Lakebase, `:p0` for warehouse).

- [ ] **Step 1: Write failing tests for `_query` routing**

```python
# adjuster-console/tests/test_data_api_fallback.py
import os
os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", "fevm-classic-stable")
import agent_server.data_api as da

class FakePool:
    def __init__(self, available, rows=None, raises=False):
        self.available = available; self._rows = rows or []; self._raises = raises
    def query(self, sql, params=None):
        if self._raises: raise RuntimeError("lb down")
        return self._rows

def test_uses_lakebase_when_available(monkeypatch):
    monkeypatch.setattr(da, "lb", FakePool(True, rows=[{"x": 1}]))
    monkeypatch.setattr(da, "_sql_warehouse", lambda *a, **k: [{"x": "WH"}])
    assert da._query("lb sql", "wh sql") == [{"x": 1}]

def test_falls_back_when_unavailable(monkeypatch):
    monkeypatch.setattr(da, "lb", FakePool(False))
    monkeypatch.setattr(da, "_sql_warehouse", lambda *a, **k: [{"x": "WH"}])
    assert da._query("lb sql", "wh sql") == [{"x": "WH"}]

def test_falls_back_on_lakebase_error(monkeypatch):
    monkeypatch.setattr(da, "lb", FakePool(True, raises=True))
    monkeypatch.setattr(da, "_sql_warehouse", lambda *a, **k: [{"x": "WH"}])
    assert da._query("lb sql", "wh sql") == [{"x": "WH"}]
```

- [ ] **Step 2: Run tests, verify fail**

Run: `cd adjuster-console && uv run pytest tests/test_data_api_fallback.py -v`
Expected: FAIL (`lb` / `_sql_warehouse` / `_query` not defined).

- [ ] **Step 3: Edit `data_api.py`**

Rename the existing `_sql(...)` helper to `_sql_warehouse(...)` (same body). Add at top: `from agent_server.lakebase import pool as lb`. Add:

```python
def _query(sql_lb: str, sql_wh: str, params_lb: tuple | None = None, params_wh: list | None = None) -> list[dict]:
    if lb.available:
        try:
            return lb.query(sql_lb, params_lb)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Lakebase query failed, falling back to warehouse: %s", e)
    return _sql_warehouse(sql_wh, params_wh)
```

Convert each endpoint to call `_query` with an unqualified Lakebase SQL (`%s` params, tables unqualified since `search_path=claims`) and the existing qualified warehouse SQL (`:p0` params). For `/claims`, `/claims/{id}/summary`, `/claims/{id}/docs`, `/file` use the Lakebase SQL shapes from the spec/prototype; keep `/summary` columns (`loss_location, adjuster_name`) and `/chat/stream` unchanged.

- [ ] **Step 4: Run tests, verify pass**

Run: `cd adjuster-console && uv run pytest tests/test_data_api_fallback.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add adjuster-console/agent_server/data_api.py adjuster-console/tests/test_data_api_fallback.py
git commit -m "data_api: Lakebase-first reads with SQL-warehouse fallback

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 8: Initialize the pool on startup

**Files:**
- Modify: `adjuster-console/agent_server/start_server.py`

- [ ] **Step 1: Add non-blocking init**

After the data-router import, add:
```python
from agent_server.lakebase import pool as lakebase_pool  # noqa: E402
try:
    lakebase_pool.initialize()
except Exception:
    logging.getLogger(__name__).warning("Lakebase init failed; using SQL warehouse fallback", exc_info=True)
```
(Import `logging` at top if not already.)

- [ ] **Step 2: Smoke-run import**

Run: `cd adjuster-console && DATABRICKS_CONFIG_PROFILE=fevm-classic-stable uv run python -c "import agent_server.start_server as s; print('app:', s.app is not None)"`
Expected: prints `app: True` (pool init may log that it connected or fell back).

- [ ] **Step 3: Commit**

```bash
git add adjuster-console/agent_server/start_server.py
git commit -m "Initialize Lakebase pool on app startup (non-blocking)

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 9: Dependencies + bundle resources

**Files:**
- Modify: `adjuster-console/pyproject.toml`
- Modify: `adjuster-console/databricks.yml`

- [ ] **Step 1: Add deps**

In `pyproject.toml` dependencies: add `"psycopg[binary]>=3.1.0"`, change `"databricks-sdk>=0.30.0"` → `"databricks-sdk>=0.118.0"`.

- [ ] **Step 2: Update bundle**

In `databricks.yml` app `config.env`, add:
```yaml
- name: LAKEBASE_PROJECT_ID
  value: "insurance-claim"
- name: LAKEBASE_BRANCH_ID
  value: "production"
- name: LAKEBASE_ENDPOINT_ID
  value: "primary"
- name: LAKEBASE_DATABASE
  value: "databricks_postgres"
- name: LAKEBASE_SCHEMA
  value: "claims"
- name: EMBEDDING_ENDPOINT
  value: "databricks-gte-large-en"
```
Add to app `resources`:
```yaml
- name: 'lakebase'
  postgres:
    branch: 'projects/insurance-claim/branches/production'
    database: 'projects/insurance-claim/branches/production/databases/databricks_postgres'
    permission: 'CAN_CONNECT_AND_CREATE'
- name: 'embedding_model'
  serving_endpoint:
    name: 'databricks-gte-large-en'
    permission: 'CAN_QUERY'
```
**Remove** the Vector Search index resource (`gold_chunks_vs_index`). Keep Genie, python_exec, orchestrator model (`databricks-gpt-5-2`), landing volume, metadata-table grants, and the SQL warehouse (fallback). Confirm the exact DB resource path with `databricks postgres list-databases projects/insurance-claim/branches/production --profile fevm-classic-stable` (the DB id may be hyphenated).

- [ ] **Step 3: Update lock + validate bundle**

Run: `cd adjuster-console && uv lock && databricks bundle validate -t dev --profile fevm-classic-stable`
Expected: `uv lock` succeeds; bundle validation passes (no schema errors).

- [ ] **Step 4: Commit**

```bash
git add adjuster-console/pyproject.toml adjuster-console/uv.lock adjuster-console/databricks.yml
git commit -m "Add psycopg + Lakebase bundle resources; drop Vector Search index resource

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Phase 3 — Permissions + deploy

### Task 10: Deploy, grant SP, smoke-test

**Files:** none (ops task).

- [ ] **Step 1: Run the pipeline notebooks to populate Lakebase**

Run notebooks `07_lakebase_embed_sync` then `08_lakebase_metadata_sync` in the workspace (profile `fevm-classic-stable`). Confirm the verification cells pass (row counts match; sample queries return rows).

- [ ] **Step 2: Deploy the app (deploy-first, before local run)**

Run: `cd adjuster-console && databricks bundle deploy -t dev --profile fevm-classic-stable`
Then get the SP: `databricks apps get adjuster-console --profile fevm-classic-stable` → note `service_principal_client_id`.

- [ ] **Step 3: Grant the app SP read access on the `claims` schema**

Using the Lakebase psql flow (generate-database-credential as the project owner):
```sql
GRANT USAGE ON SCHEMA claims TO "<SP_CLIENT_ID>";
GRANT SELECT ON ALL TABLES IN SCHEMA claims TO "<SP_CLIENT_ID>";
ALTER DEFAULT PRIVILEGES IN SCHEMA claims GRANT SELECT ON TABLES TO "<SP_CLIENT_ID>";
```
Expected: `GRANT` / `ALTER DEFAULT PRIVILEGES` succeed.

- [ ] **Step 4: Smoke-test the deployed app**

Hit the app URL: `/api/claims` (list), `/api/claims/<id>/summary`, and `/api/chat/stream` with a document question. Expected: results return; app logs show "Lakebase pool initialized" (not the fallback warning); chat answer cites `source_uri` and the step shows "Searching claims documents".

---

## Phase 4 — Verify, tag, retire

### Task 11: Local verify, tag v2.0.0, retire revision/

**Files:**
- Delete: `revision/` (after verification)

- [ ] **Step 1: Local end-to-end run**

Run: `cd adjuster-console && uv run start-server --port 8000` (with `.env` → `fevm-classic-stable`, Lakebase env set). In another shell: `curl -s localhost:8000/api/claims`, `/api/claims/<id>/summary`, and a `POST /api/chat/stream` document query. Expected: metadata via Lakebase; chat answer cites `source_uri` rendered as clickable links in the UI at `http://localhost:8000`.

- [ ] **Step 2: Full test suite**

Run: `cd adjuster-console && uv run pytest -q`
Expected: all tests pass (history-normalization + the three new test files).

- [ ] **Step 3: Open PR, merge, tag**

Push `feat/v2-lakebase-pgvector`, open a PR, merge after review. Then:
```bash
git checkout main && git pull --ff-only origin main
git tag -a v2.0.0 -m "v2.0.0 — Lakebase pg-vector hybrid retrieval"
git push origin v2.0.0
gh release create v2.0.0 --title "v2.0.0 — Lakebase pg-vector hybrid retrieval" --verify-tag --latest --notes "..."
```

- [ ] **Step 4: Retire the prototype**

```bash
git rm -r revision/
git commit -m "Retire revision/ prototype now that V2 is live

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Self-review notes

- **Spec coverage:** search design (Task 2 schema/indexes, Task 5 query) ✓; pipeline-owned sync (Tasks 2,3) ✓; app wiring (Tasks 4–9) ✓; citations/source_uri (Task 5 SELECT + Task 6 instructions) ✓; SP permissions (Task 10) ✓; metadata fallback (Task 7) ✓; config values (Global Constraints) ✓; rollback (Task 11 via v1.1.0 tag) ✓.
- **Optimizations:** stored `chunk_tsv` (Task 2 table, Task 5 SQL, test asserts absence of per-row `to_tsvector(chunk_to_retrieve)`) ✓; indexes built after load (Task 2 Step 4) ✓.
- **Review Focus:** Lakebase-unavailable fallback (Task 7 tests) ✓; claim_id param order (Task 5 tests) ✓; empty BM25/vector (RRF FULL OUTER JOIN + `search_documents` returns [] + Task 6 "no documents" path) ✓; SP permission (Task 10) ✓.
