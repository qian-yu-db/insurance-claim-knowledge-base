# V2 — Lakebase pg-vector hybrid retrieval for the Adjuster Console

**Date:** 2026-10-01
**Status:** Design — awaiting approval
**Target release:** `v2.0.0`
**Branch:** `feat/v2-lakebase-pgvector`

## Goal

Replace the Adjuster Console's document retrieval backend — currently Databricks
**Vector Search (AI Search)** via MCP — with **Lakebase (Autoscaling Postgres)
hybrid search**: `lakebase_ann` vector ANN + `lakebase_bm25` full-text, fused
with Reciprocal Rank Fusion (RRF). Metadata reads also move to Lakebase (with a
SQL-warehouse fallback). The branded UI and streaming chat from `v1.1.0` are
unchanged.

This is the next evolution of the existing `adjuster-console/` app (we evolve it
in place; `v1.0.0` and `v1.1.0` remain recoverable via their tags). The
`revision/adjuster-console-v2/` folder is the prototype this design is derived
from; it is retired once V2 lands.

## Locked decisions

| Decision | Choice |
|---|---|
| Document retrieval | **Full replace** with Lakebase hybrid (no AI Search fallback). Rollback = redeploy `v1.1.0`. |
| Metadata reads | Lakebase-first, **SQL-warehouse fallback** (same UC tables, identical rows). |
| Repo structure | Evolve `adjuster-console/` in place; tag `v2.0.0`; retire `revision/`. |
| Where indexing/sync runs | **Pipeline**, not the app. Embeddings → notebook `07`; metadata → notebook `08`. App carries no jobs. |
| Orchestrator model | Keep `databricks-gpt-5-2` (as in `v1.1.0`). |
| Lakebase instance | Project `insurance-claim` (`5bf96c8d-74c8-4782-88a8-c1c11f589485`), branch `production`, endpoint `primary`, db `databricks_postgres`, schema `claims`. |
| Embedding model | `databricks-gte-large-en` (1024-dim) — matches the current pipeline. |

**Principle:** the pipeline owns index/data refresh (re-run it when raw data
updates); the app owns agent orchestration and reads only.

## Verified facts (probed on the instance, 2026-10-01)

Available extensions on `insurance-claim`: `lakebase_vector` 1.1.1 (→ `lakebase_ann`),
`lakebase_text` 0.1.3 + `lakebase_tokenizer` 0.1.1 (→ `lakebase_bm25`,
`to_bm25query`), `vector` (pgvector) 0.8.0, `pg_trgm` 1.6. Postgres 17.11. None
installed yet. The native `lakebase_*` path is the documented optimization over
plain pgvector `hnsw`/`ivfflat`.

## Search design (with optimizations over the prototype)

The prototype's query *shape* already matches the documented Lakebase Search best
practice (`lakebase_ann` + `<=>`, `lakebase_bm25` + `to_bm25query` + `<@>`, RRF
k=60). We adopt it with two improvements grounded in the latest docs
([Lakebase Search](https://docs.databricks.com/aws/en/oltp/projects/lakebase-search),
[lakebase_vector](https://docs.databricks.com/aws/en/oltp/projects/lakebase-vector),
[lakebase_text](https://docs.databricks.com/aws/en/oltp/projects/lakebase-text)):

1. **Stored `TSVECTOR` column for BM25.** The prototype recomputes
   `to_tsvector('english', chunk_to_retrieve)` per row, three times per query.
   Instead: materialize a `chunk_tsv TSVECTOR` column at sync time, build the
   `lakebase_bm25` index on it, and have the query reference the column
   (`g.chunk_tsv <@> to_bm25query(to_tsvector('english', %s), '<idx>')`).

2. **Build vector + BM25 indexes AFTER loading data.** The prototype creates
   indexes then `TRUNCATE`+reloads, so IVF partitions and BM25 corpus statistics
   are computed on an empty table. Load first, then create (or `REINDEX`) the
   `lakebase_ann` and `lakebase_bm25` indexes.

3. **Tuning GUCs** (`lakebase_ann.probes/epsilon/prefilter`,
   `lakebase_bm25.default_limit`) are **out of scope** at the current corpus size
   (few claims; may use exact flat search). Noted for scale.

### `claims.gold_chunks_embeddings`
```
chunk_id TEXT PK, doc_id TEXT, chunk_position INT, claim_id TEXT, doc_type TEXT,
corpus TEXT, carrier TEXT, peril TEXT, loss_year INT,
chunk_to_retrieve TEXT, chunk_to_embed TEXT,
embedding vector(1024), chunk_tsv tsvector
```
Indexes (post-load): `lakebase_ann (embedding vector_cosine_ops)`,
`lakebase_bm25 (chunk_tsv)`, btree `(claim_id)`.

### Query (`lakebase_search.search_documents`)
On-the-fly query embedding via `databricks-gte-large-en`; vector CTE (`<=>` ASC,
`candidate_k = top_k*3`) + text CTE (`chunk_tsv <@> to_bm25query(...)` ASC) →
RRF (k=60) → join back to the table + `bronze_file_registry`. Wrapped in an
MLflow `RETRIEVER` span.

**Citations:** the `v1.1.0` UI makes citations clickable by matching
`source_uri`. So `search_documents` must **return `source_uri`** (join
`bronze_file_registry`) and the agent must keep citing `source_uri` — consistent
with `v1.1.0`, no UI change.

## Components

### Pipeline (`pipeline/notebooks/`)
- `00_config.py` — add Lakebase connection params (project/branch/endpoint/db/schema).
- **`07_*` (replaces the VS-index notebook)** — embed `gold_chunks.chunk_to_embed`
  via `databricks-gte-large-en`; create extensions + `claims.gold_chunks_embeddings`
  (incl. `chunk_tsv`); load; build indexes post-load. Replaces the Delta Sync VS
  index. The old notebook 07 (`gold_chunks_vs_index`) is removed.
- **`08_*` (new)** — sync `dim_claim`, `bronze_file_registry`, `silver_documents`
  into Lakebase `claims.*` for the app's data API.
- After load, **GRANT the app's service principal** read access on the `claims`
  schema (see Permissions).

### App (`adjuster-console/`)
- **NEW `agent_server/lakebase.py`** — `LakebasePool` (SDK endpoint resolution,
  OAuth token minting + background rotation, pooled psycopg3, read-only queries).
  Real project path; add scale-to-zero connection retry.
- **NEW `agent_server/lakebase_search.py`** — hybrid search per the design above
  (stored `chunk_tsv`, returns `source_uri`, MLflow retriever span).
- **MODIFY `agent_server/agent.py`** — remove the Vector Search MCP server; add the
  in-process `claims_document_search` tool backed by `lakebase_search`; keep Genie
  + `python_exec` MCP. Keep model `databricks-gpt-5-2`. Instructions keep citing
  `source_uri`.
- **MODIFY `agent_server/data_api.py`** — add the Lakebase pool + a Lakebase-first,
  SQL-warehouse-fallback `_query` wrapper; keep `/summary` and `/chat/stream`.
- **MODIFY `agent_server/start_server.py`** — `lakebase_pool.initialize()` on
  startup (non-blocking; warns and uses SQL fallback if unavailable).
- **MODIFY `pyproject.toml`** — add `psycopg[binary]>=3.1`, bump
  `databricks-sdk>=0.118.0` (for `w.postgres`). `sse-starlette` already present.
- **MODIFY `databricks.yml`** — add Lakebase env vars + the Postgres/Lakebase
  resource (app SP `CAN_CONNECT_AND_CREATE`/connect) + embedding-endpoint grant;
  **remove** the Vector Search index resource. **No jobs in the app bundle.**

### Data flow
```
UC gold_chunks            → pipeline 07 → Lakebase claims.gold_chunks_embeddings (ann + bm25)
UC dim_claim/bronze/silver → pipeline 08 → Lakebase claims.* metadata
App (read-only): data_api → metadata (Lakebase → SQL fallback)
                 agent.claims_document_search → gold_chunks_embeddings hybrid
```

## Permissions (Lakebase SP ownership — important)

The pipeline (run as a user/pipeline identity) creates and **owns** the `claims`
schema and tables. The deployed app's **service principal** has
`CAN_CONNECT_AND_CREATE` but **cannot read schemas it doesn't own**. So after the
pipeline populates Lakebase, we must grant the app SP:
```sql
GRANT USAGE ON SCHEMA claims TO "<SP_CLIENT_ID>";
GRANT SELECT ON ALL TABLES IN SCHEMA claims TO "<SP_CLIENT_ID>";
ALTER DEFAULT PRIVILEGES IN SCHEMA claims GRANT SELECT ON TABLES TO "<SP_CLIENT_ID>";
```
SP client id: `databricks apps get <APP_NAME>` → `service_principal_client_id`.
(The grant step runs after the pipeline load and after the app is deployed.)

## Phases

1. **Pipeline indexing** — update `00_config`, write `07` (embed) + `08` (metadata),
   run against `insurance-claim`; verify row counts and a sample hybrid query.
2. **App wiring** — `lakebase.py`, `lakebase_search.py`, `agent.py` retrieval swap,
   `data_api.py` fallback wrapper, `start_server.py` init, `pyproject.toml`,
   `databricks.yml`.
3. **Permissions + deploy** — deploy app; grant SP read on `claims`; smoke-test.
4. **Local test → verify → tag v2.0.0**; retire `revision/`.

## Testing

- Pipeline: after `07`/`08`, assert `gold_chunks_embeddings` count = `gold_chunks`
  count, metadata counts match UC, and a sample `search_documents(query, claim_id)`
  returns ranked results with `source_uri`.
- App (local, `fevm-classic-stable`): `/api/claims`, `/summary`, `/docs`, `/file`,
  and `/api/chat/stream` — confirm the document step runs through Lakebase (not VS),
  answers cite `source_uri`, and citations render as clickable links.
- Deployed: same checks via the app URL; confirm Lakebase pool initializes (not
  the SQL fallback) in logs.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| `lakebase_*` extensions unavailable | **Verified available** on the instance (above). |
| App SP cannot read `claims` schema | Explicit GRANT step after load (Permissions). Deploy-first ordering. |
| Indexes built on empty table (poor quality) | Build/`REINDEX` after load (optimization #2). |
| OAuth token 1h expiry | `LakebasePool` background rotation (ported). |
| Scale-to-zero cold start | Connection retry on wake (~100ms). |
| Local macOS DNS `getaddrinfo` on long host | Known Lakebase local-dev issue; use `hostaddr` workaround if hit. |
| Citations not clickable | `search_documents` returns `source_uri`; agent cites it (unchanged UI). |

## Out of scope

- Lakebase **synced tables** (managed Reverse ETL) for metadata — noted as the
  scale path; metadata uses a simple pipeline sync for now.
- ANN/BM25 tuning GUCs.
- Removing `revision/` happens only after V2 is verified.

## Rollback

`v1.1.0` is tagged and deployable; retrieval reverts to AI Search by redeploying it.
