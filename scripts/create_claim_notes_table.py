# /// script
# requires-python = ">=3.10"
# dependencies = ["databricks-sdk>=0.40"]
# ///
"""One-off: create the adjuster claim-notes Delta table from sample_data/claim_notes/claim_notes.csv.

Stands in for the customer's existing claim-notes table; it is NOT part of the pipeline. The
pipeline (nb 06) only reads the table, whose name is the `claim_notes_table` widget in 00_config.

    uv run scripts/create_claim_notes_table.py --profile <profile> --warehouse-id <id>
"""
import argparse
import csv
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem

CSV_PATH = Path(__file__).resolve().parents[1] / "sample_data/claim_notes/claim_notes.csv"
COLS = ("claim_id", "timestamp", "adjuster_id", "adjuster_name", "log")
BATCH = 20

parser = argparse.ArgumentParser()
parser.add_argument("--profile", required=True)
parser.add_argument("--warehouse-id", required=True)
parser.add_argument("--table", default="fins_genai.claims_multimodal_kb.claim_notes")
args = parser.parse_args()

w = WorkspaceClient(profile=args.profile)


def run(statement: str, params: dict | None = None) -> None:
    resp = w.statement_execution.execute_statement(
        warehouse_id=args.warehouse_id, statement=statement, wait_timeout="50s",
        parameters=[StatementParameterListItem(name=k, value=v) for k, v in (params or {}).items()],
    )
    if resp.status.state.value != "SUCCEEDED":
        raise RuntimeError(f"{resp.status.state.value}: {resp.status.error}")


rows = list(csv.DictReader(CSV_PATH.open(newline="", encoding="utf-8")))

run(f"""
CREATE OR REPLACE TABLE {args.table} (
  claim_id      STRING    COMMENT 'Claim number, e.g. CLM-2026-04487',
  timestamp     TIMESTAMP COMMENT 'When the note was written (adjuster local time)',
  adjuster_id   STRING    COMMENT 'Author id (adjuster, manager or SIU investigator)',
  adjuster_name STRING    COMMENT 'Author name',
  log           STRING    COMMENT 'Free-text note body'
)
COMMENT 'Adjuster claim notes / activity log (synthetic stand-in for the customer table)'
TBLPROPERTIES (delta.enableChangeDataFeed = true)""")

for i in range(0, len(rows), BATCH):
    batch = rows[i:i + BATCH]
    values = ", ".join(
        f"(:claim_id{j}, to_timestamp(:timestamp{j}), :adjuster_id{j}, :adjuster_name{j}, :log{j})"
        for j in range(len(batch)))
    params = {f"{c}{j}": r[c] for j, r in enumerate(batch) for c in COLS}
    run(f"INSERT INTO {args.table} VALUES {values}", params)

print(f"Loaded {len(rows)} notes into {args.table}")
