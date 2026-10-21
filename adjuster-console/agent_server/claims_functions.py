"""Structured claims tools: fixed UC SQL table functions, run on the app's SQL warehouse.

The functions live in `fins_genai.claims_multimodal_kb` (created by pipeline notebook 09). They are
called directly with the Statement Execution API rather than through the UC Functions MCP server:
measured on fevm-classic-stable, a warm call takes ~1 s here vs ~4 s via MCP, plus ~3.5 s per turn
for the MCP connect + list. UC still governs them (the app SP needs EXECUTE on each function).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from agents import function_tool
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem

logger = logging.getLogger(__name__)

FUNCTION_SCHEMA = "fins_genai.claims_multimodal_kb"
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
_MAX_WAIT_S = 60  # total, incl. the first 30 s wait; covers a cold serverless warehouse start
_w = WorkspaceClient()


def call_function(name: str, *args) -> str:
    """`SELECT * FROM <schema>.<name>(args…)`; returns JSON {columns, rows} or an `ERROR:` message."""
    if not WAREHOUSE_ID:
        return "ERROR: the structured claims data is not configured (DATABRICKS_WAREHOUSE_ID). Tell the user."
    placeholders = ", ".join(f":p{i}" for i in range(len(args)))
    params = [StatementParameterListItem(name=f"p{i}", value=str(v), type="INT" if isinstance(v, int) else "STRING")
              for i, v in enumerate(args)]
    start = time.monotonic()
    try:
        resp = _w.statement_execution.execute_statement(
            warehouse_id=WAREHOUSE_ID, statement=f"SELECT * FROM {FUNCTION_SCHEMA}.{name}({placeholders})",
            parameters=params, wait_timeout="30s",
        )
        deadline = start + _MAX_WAIT_S
        while resp.status.state.value in ("PENDING", "RUNNING") and time.monotonic() < deadline:
            time.sleep(1)
            resp = _w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state.value in ("PENDING", "RUNNING"):
            _w.statement_execution.cancel_execution(resp.statement_id)  # nobody will read it
            return ("ERROR: the structured claims data took too long to answer (the SQL warehouse may be "
                    "starting). Tell the user to try again in a minute.")
        state = resp.status.state.value
        if state != "SUCCEEDED":
            message = resp.status.error.message if resp.status.error else state
            return f"ERROR: {message}"
        rows = list(resp.result.data_array or []) if resp.result else []
        chunk = resp.result.next_chunk_index if resp.result else None
        while chunk is not None:  # large results arrive in several chunks
            part = _w.statement_execution.get_statement_result_chunk_n(resp.statement_id, chunk)
            rows.extend(part.data_array or [])
            chunk = part.next_chunk_index
    except Exception as e:
        logger.error("Claims function %s failed: %s", name, e)
        return f"ERROR: the structured claims data could not be reached ({e}). Tell the user."
    if not rows:
        return "No rows returned (no matching claim, or nothing recorded yet)."
    columns = [c.name for c in resp.manifest.schema.columns]
    return json.dumps({"columns": columns, "rows": rows})


async def _call(name: str, *args) -> str:
    return await asyncio.to_thread(call_function, name, *args)


@function_tool
async def claim_details(claim_id: str) -> str:
    """Claim header: carrier (insurer), peril, policy number, insured, claimant, date and location of
    loss, assigned adjuster, number of documents on file.

    Args:
        claim_id: Claim number, e.g. CLM-2026-04487 (or just 04487).
    """
    return await _call("claim_details", claim_id)


@function_tool
async def claim_payments(claim_id: str) -> str:
    """Every payment line on one claim, oldest first: date, transaction_type (indemnity / expense /
    defense_expense), payee, amount in USD, status (Paid, or negotiated but pending), source ledger file.

    Args:
        claim_id: Claim number, e.g. CLM-2026-04487 (or just 04487).
    """
    return await _call("claim_payments", claim_id)


@function_tool
async def claim_financials(claim_id: str) -> str:
    """Financial summary of one claim in USD: paid indemnity / expense / defense expense, total paid,
    pending (agreed but not yet paid), total claim cost (indemnity + all expenses, paid or pending),
    number of payment lines, largest single payment.

    Args:
        claim_id: Claim number, e.g. CLM-2026-04487 (or just 04487).
    """
    return await _call("claim_financials", claim_id)


@function_tool
async def payment_totals(group_by: str = "all") -> str:
    """Payment totals in USD across all claims, one row per group: number of claims, payment lines,
    total paid, pending, total claim cost.

    Args:
        group_by: One of all (one overall row), carrier, peril, adjuster, loss_year, transaction_type, status.
    """
    return await _call("payment_totals", group_by)


@function_tool
async def top_claims(metric: str = "total_paid", n: int = 5) -> str:
    """The n claims with the highest metric (USD), largest first, with insured, carrier and peril.

    Args:
        metric: One of total_paid, total_claim_cost, pending_amount.
        n: How many claims to return.
    """
    return await _call("top_claims", metric, n)


@function_tool
async def claim_notes(claim_id: str, last_n: int = 20) -> str:
    """Adjuster claim notes (activity log) for one claim, oldest first, limited to the most recent
    last_n: note_time (as recorded by the notes system), author (adjuster / manager / SIU) id and
    name, note text.

    Args:
        claim_id: Claim number, e.g. CLM-2026-04487 (or just 04487).
        last_n: Return only the most recent n notes.
    """
    return await _call("claim_notes", claim_id, last_n)


CLAIMS_FUNCTION_TOOLS = [claim_details, claim_payments, claim_financials, payment_totals, top_claims,
                         claim_notes]
