# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Config — shared parameters for the Claims KB workflow
# MAGIC
# MAGIC `%run ./00_config` from every stage notebook to inherit `CFG` and the `run_sql` helper.
# MAGIC All names are parameterized via widgets so the same workflow runs against any workspace/volume.
# MAGIC
# MAGIC **Pipeline map** (matches the architecture diagram):
# MAGIC `01 Bronze` → `02 Silver parse/text` + `03 Silver image caption` → `04 Silver documents (+metadata)`
# MAGIC → `05 Structured facts` → `06 Gold chunks` → `07 Vector Search index`.
# MAGIC
# MAGIC Audio/video transcription (`route='media_pending'`) is intentionally **out of scope** here — it is
# MAGIC handled by a separate custom solution that UNIONs transcripts into `silver_documents`.

# COMMAND ----------
dbutils.widgets.text("catalog", "fins_genai", "Catalog")
dbutils.widgets.text("schema", "claims_multimodal_kb", "Output schema")
dbutils.widgets.text("landing_path", "/Volumes/fins_genai/claims_multimodal_kb/landing/claims", "Landing volume path")
dbutils.widgets.text("claim_notes_table", "fins_genai.claims_multimodal_kb.claim_notes", "Adjuster claim notes table")
dbutils.widgets.text("embedding_endpoint", "databricks-gte-large-en", "Embedding endpoint")
dbutils.widgets.text("vision_endpoint", "databricks-llama-4-maverick", "Vision endpoint (image captions)")
dbutils.widgets.text("email_endpoint", "databricks-claude-haiku-4-5", "LLM endpoint (email body cleanup)")
dbutils.widgets.text("audio_endpoint", "whisper-large-v3", "Audio transcription endpoint (Whisper)")
dbutils.widgets.dropdown("pii_mask", "off", ["off", "sensitive_only"], "PII masking policy")
dbutils.widgets.text("lakebase_project_id", "insurance-claim", "Lakebase project id")
dbutils.widgets.text("lakebase_branch_id", "production", "Lakebase branch")
dbutils.widgets.text("lakebase_endpoint_id", "primary", "Lakebase endpoint")
dbutils.widgets.text("lakebase_database", "databricks_postgres", "Lakebase database")
dbutils.widgets.text("lakebase_schema", "claims", "Lakebase schema")

# COMMAND ----------
CFG = {k: dbutils.widgets.get(k) for k in (
    "catalog", "schema", "landing_path", "claim_notes_table", "embedding_endpoint",
    "vision_endpoint", "email_endpoint", "audio_endpoint", "pii_mask")}

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

C, S = CFG["catalog"], CFG["schema"]
CFG["fq"] = f"{C}.{S}"                                  # fully-qualified schema prefix
CFG["t"] = {                                            # table names
    "bronze":   f"{C}.{S}.bronze_file_registry",
    "parsed":   f"{C}.{S}.silver_parsed",
    "text":     f"{C}.{S}.silver_text",
    "captions": f"{C}.{S}.silver_image_captions",
    "audio":    f"{C}.{S}.silver_audio_transcripts",
    "docs":     f"{C}.{S}.silver_documents",
    "dim_claim":f"{C}.{S}.dim_claim",
    "fact_pay": f"{C}.{S}.fact_payments",
    "gold":     f"{C}.{S}.gold_chunks",
    "notes":    CFG["claim_notes_table"],               # customer-owned source table (read only)
}


def run_sql(sql: str):
    """Run a SQL statement and return the DataFrame (also displays row count)."""
    df = spark.sql(sql)
    return df


print("Config:")
for k, v in CFG.items():
    if k != "t":
        print(f"  {k:20} {v}")
print("  tables:")
for k, v in CFG["t"].items():
    print(f"    {k:10} {v}")

# COMMAND ----------
# MAGIC %md
# MAGIC ### Verify the required endpoints exist in this workspace
# MAGIC Foundation-model endpoints are workspace/date specific — confirm before running the AI-function stages.

# COMMAND ----------
from databricks.sdk import WorkspaceClient

_w = WorkspaceClient()
_needed = {CFG["embedding_endpoint"], CFG["vision_endpoint"], CFG["email_endpoint"], CFG["audio_endpoint"]}
_have = {e.name for e in _w.serving_endpoints.list()}
for n in sorted(_needed):
    print(("  ✅" if n in _have else "  ❌ MISSING"), n)
