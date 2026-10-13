# Databricks notebook source
# MAGIC %md
# MAGIC # 07b · Sync the Vector Search index
# MAGIC
# MAGIC Triggers a sync of the existing Delta Sync index on `gold_chunks` and waits for it to finish.
# MAGIC Used by the `claims-kb-v1-index-sync` job after the pipeline updates `gold_chunks`. Create the index
# MAGIC once with nb 07. This notebook uses only the preinstalled `databricks-sdk`; no `%pip install` needed.

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
import time
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
INDEX = CFG["index_name"]
TIMEOUT_S = 30 * 60

w.vector_search_indexes.sync_index(index_name=INDEX)
print(f"Sync triggered for {INDEX}")

# COMMAND ----------
# The status message reports a pending/running update until the sync finishes.
deadline = time.time() + TIMEOUT_S
time.sleep(30)
while True:
    status = w.vector_search_indexes.get_index(index_name=INDEX).status
    msg = (status.message or "").lower()
    print(f"ready={status.ready} rows={status.indexed_row_count} | {status.message}")
    if "failed" in msg:
        raise RuntimeError(f"Vector Search sync failed: {status.message}")
    if status.ready and not any(k in msg for k in ("pending", "in progress", "running", "syncing")):
        break
    if time.time() > deadline:
        raise TimeoutError(f"Vector Search sync did not finish within {TIMEOUT_S}s: {status.message}")
    time.sleep(30)

src = spark.table(CFG["t"]["gold"]).count()
print(f"Index rows: {status.indexed_row_count} | gold_chunks rows: {src}")
assert status.indexed_row_count == src, "Index row count does not match gold_chunks"
