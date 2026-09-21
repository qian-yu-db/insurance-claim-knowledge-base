# Databricks notebook source
# MAGIC %md
# MAGIC # 07 · Vector Search index (+ metadata sync, hybrid query)
# MAGIC
# MAGIC Creates a Delta Sync index over `gold_chunks`, syncing the **metadata columns** so they are available
# MAGIC as filters and returned facets, and embedding `chunk_to_embed` with the embedding endpoint.
# MAGIC Uses the Python SDK (the `vector-search-indexes query-index` CLI has a response-parsing bug).

# COMMAND ----------
# MAGIC %pip install -q databricks-vectorsearch
# MAGIC %restart_python

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
assert CFG["vs_endpoint"], "Set the 'vs_endpoint' widget to a Vector Search endpoint name."

# Metadata columns to sync (PK + embedding source are added automatically; list what you'll filter/return).
META_COLS = [
    "chunk_id", "doc_id", "chunk_position",
    "claim_id", "corpus", "doc_type", "modality", "content_source",
    "carrier", "peril", "loss_year", "date_of_loss",
    "policy_number", "insured_name", "claimant_name", "adjuster_name", "total_amount",
    "source_uri", "filename", "pii_masked", "ingested_at",
    "chunk_to_retrieve",   # returned to the caller/LLM
    "chunk_to_embed",      # embedding source
]

# COMMAND ----------
from databricks.vector_search.client import VectorSearchClient

vsc = VectorSearchClient(disable_notice=True)

# Ensure endpoint exists
existing_eps = {e["name"] for e in vsc.list_endpoints().get("endpoints", [])}
if CFG["vs_endpoint"] not in existing_eps:
    print("Creating endpoint (a few minutes)…")
    vsc.create_endpoint_and_wait(name=CFG["vs_endpoint"], endpoint_type="STANDARD")
print("Endpoint ready:", CFG["vs_endpoint"])

# COMMAND ----------
# Create the index if absent, else sync it.
try:
    idx = vsc.get_index(endpoint_name=CFG["vs_endpoint"], index_name=CFG["index_name"])
    print("Index exists — triggering sync.")
    idx.sync()
except Exception:
    print("Creating Delta Sync index (initial embed of all chunks)…")
    idx = vsc.create_delta_sync_index_and_wait(
        endpoint_name=CFG["vs_endpoint"],
        index_name=CFG["index_name"],
        source_table_name=CFG["t"]["gold"],
        primary_key="chunk_id",
        pipeline_type="TRIGGERED",
        embedding_source_column="chunk_to_embed",
        embedding_model_endpoint_name=CFG["embedding_endpoint"],
        columns_to_sync=META_COLS,
    )
print("Index ready:", CFG["index_name"])

# COMMAND ----------
# MAGIC %md
# MAGIC ### Metadata in action — filtered hybrid queries
# MAGIC `query_type="HYBRID"` = vector similarity + keyword/BM25 over the text; `filters={...}` uses the
# MAGIC synced metadata columns as hard predicates (equality, `IN` via list, ranges via `col >=` / `col <=`).

# COMMAND ----------
RETURN_COLS = ["chunk_id", "claim_id", "doc_type", "modality", "source_uri", "chunk_to_retrieve"]

def search(text, filters=None, k=3, query_type="HYBRID"):
    res = idx.similarity_search(query_text=text, columns=RETURN_COLS,
                                filters=filters or {}, num_results=k, query_type=query_type)
    for row in res.get("result", {}).get("data_array", []) or []:
        print(f"  [{row[2]} | {row[1]} | {row[3]}] {row[4].split('/')[-1]}")
        print(f"     -> {str(row[5])[:150]}")

# Q1: per-claim scope
print("Q1 — plaintiff demand, filtered to one claim:")
search("What is the plaintiff demand amount and who made it?",
       filters={"claim_id": "CLM-2026-04487"})

# Q2: filter by doc_type across claims (topic scoping = filter, not a separate index)
print("\nQ2 — medical findings, restricted to medical records:")
search("cervical and lumbar injury findings", filters={"doc_type": ["medical_record"]})

# Q3: range filter on a numeric metadata column + facet by modality
print("\nQ3 — damage description from image captions, recent losses:")
search("front-end collision damage severity",
       filters={"modality": "image", "loss_year >=": 2025})
