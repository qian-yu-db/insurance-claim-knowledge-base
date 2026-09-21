# Data Residency — where each asset lives

For a data-residency / security review, this is where every piece of data in this solution
physically resides: the **customer's own cloud storage** vs. **Databricks-managed** infrastructure.

## Summary

- **Customer's own cloud storage:** everything at rest — raw files (UC Volume), all Bronze/Silver/Gold
  Delta tables, and the structured facts (`dim_claim`, `fact_payments`).
- **Databricks-managed:** the Vector Search **index** (a synced, derived copy of the Gold chunks) and
  MLflow **traces**; plus all **compute** (serverless SQL, model serving, the app), which processes data
  transiently and stores none of it.

## Where each asset lives

| Asset | Location | Notes |
|---|---|---|
| Landing Volume (raw claim files) | **Customer cloud storage** | UC Volume → customer's S3/ADLS/GCS |
| Bronze / Silver / Gold Delta tables | **Customer cloud storage** | UC-governed Delta tables (`bronze_file_registry`, `silver_documents`, `gold_chunks`, …) |
| Structured facts (`dim_claim`, `fact_payments`) | **Customer cloud storage** | UC-governed Delta tables; queried by Genie |
| **Vector Search index** (`gold_chunks_vs_index`) | **Databricks-managed** | Derived copy of the Gold chunks (text + embeddings); served by the Vector Search endpoint |
| MLflow traces / experiment | **Databricks-managed control plane** | Agent request/response logs — can contain claim text |
| Serverless SQL warehouse, Model Serving (embeddings / LLM / `ai_transcribe`), Databricks App | **Databricks-managed compute** | Transient — reads customer data at query time, persists none |

## Clarifications that come up in reviews

1. **"UC managed table" ≠ "Databricks holds the data."** All Delta tables and Volumes — whether UC
   *managed* or *external* — physically reside in the **customer's own cloud account** (the metastore /
   catalog managed-storage location is a bucket in the customer's cloud, or an external location they own).
   Databricks governs access; it does not store the data.

2. **Serverless compute is transient, not storage.** The serverless SQL warehouse (pipeline + the app's
   data APIs), Model Serving endpoints, and the Databricks App run on Databricks-managed compute. They read
   the customer's data at query time and **persist nothing** (this app is stateless — no Lakebase). Temp
   spill during a query is Databricks-managed ephemeral storage, discarded afterward.

3. **MLflow traces are a second Databricks-side data location.** Traces logged by the agent app capture
   request/response content, which can include claim text and retrieved passages, and live in the
   Databricks-managed control plane. They can be scoped or disabled if residency requires it.

## Can the Vector Search index live in the customer's own storage?

**Not for the native served index.** In **Mosaic AI Vector Search**, the served index is a
**Databricks-managed** object regardless of configuration:

- **Endpoint type** (Standard vs Storage-Optimized) → changes scale / latency / cost, **not custody**.
- **Index type** (Delta Sync vs Direct Access) → changes how the index is populated, **not custody**.

There is no bring-your-own-bucket option to place the *served* index in the customer's cloud storage.

### What the customer *does* control

1. **Keep the source-of-truth vectors in the customer's storage** — use a **Delta Sync index with
   self-managed embeddings**: compute embeddings into a column of a **Delta table in the customer's
   storage**, then sync that table to the index. The vectors at rest are in the customer's storage; the
   index is a *derived, served copy* on the Databricks side. (This solution currently uses managed
   embeddings; switching to self-managed embeddings is a small pipeline change.)

2. **Full custody of the served vectors → use a vector store the customer owns.** If the queryable index
   itself must reside in the customer's account, don't use Mosaic AI Vector Search for serving — instead
   compute embeddings in Databricks (data stays in the customer's Delta tables) and export them to a vector
   store the customer runs (e.g. self-hosted `pgvector`, or a third-party vector DB in their account). The
   agent then queries that store via an external connection. Trade-off: you lose the native managed
   Delta-sync integration and operate the store yourself.

3. **Encryption / key custody as a middle ground.** If the requirement is control over *encryption* rather
   than physical location, discuss **customer-managed keys (CMK)** for Databricks managed services/storage
   with the account team — often this satisfies a security review even when the index is Databricks-managed.
   (Confirm CMK coverage for Vector Search with your Databricks account team.)

### Recommendation

For most deployments, keep the native Databricks-managed Vector Search index (best performance and the
least operational burden) and note that its **source of truth remains in the customer's storage** (the
Gold Delta table). Move to a self-managed / external vector store only when a strict residency requirement
forbids a Databricks-managed derived copy — and validate the specific compliance requirement with the
Databricks account team.

> Product capabilities change; confirm current Vector Search storage and CMK details with your Databricks
> account team / the official docs before committing to a compliance statement.
