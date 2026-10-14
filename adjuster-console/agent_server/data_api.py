"""Doc/claim/file APIs for the adjuster console viewer pane.

Backed by the claims KB tables (Lakebase Postgres primary, SQL warehouse fallback)
and the landing Volume (via Files API).
Runs as the app service principal when deployed; uses the .env profile locally.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from typing import AsyncGenerator

import mlflow
from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sse_starlette.sse import EventSourceResponse

from agent_server import memory
from agent_server.lakebase import pool as lb

logger = logging.getLogger(__name__)

CATALOG = "fins_genai"
SCHEMA = "claims_multimodal_kb"
VOLUME_PREFIX = f"/Volumes/{CATALOG}/{SCHEMA}/landing"          # security fence for file reads
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

_TEXT_EXTS = {"eml", "html", "xml", "rtf", "csv", "txt"}
_IMAGE_CT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "tiff": "image/tiff"}

router = APIRouter(prefix="/api")
_w = WorkspaceClient()


def _sql_warehouse(statement: str, params: list | None = None) -> list[dict]:
    """Run a query on the SQL warehouse and return rows as dicts."""
    if not WAREHOUSE_ID:
        raise HTTPException(500, "DATABRICKS_WAREHOUSE_ID is not configured")
    from databricks.sdk.service.sql import StatementParameterListItem

    resp = _w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=statement,
        parameters=[StatementParameterListItem(name=f"p{i}", value=str(v)) for i, v in enumerate(params or [])],
        wait_timeout="50s",
    )
    if resp.status and resp.status.state and resp.status.state.value not in ("SUCCEEDED",):
        raise HTTPException(502, f"query failed: {resp.status.error}")
    cols = [c.name for c in resp.manifest.schema.columns] if resp.manifest and resp.manifest.schema else []
    data = resp.result.data_array if resp.result and resp.result.data_array else []
    return [dict(zip(cols, row)) for row in data]


def _query(sql_lb: str, sql_wh: str, params_lb: tuple | None = None, params_wh: list | None = None) -> list[dict]:
    """Run against Lakebase if available, otherwise SQL warehouse.

    Falls back to SQL warehouse on any Lakebase error (missing schema, connection issues, etc.).
    """
    if lb.available:
        try:
            return lb.query(sql_lb, params_lb)
        except Exception as e:
            logger.warning("Lakebase query failed, falling back to warehouse: %s", e)
    return _sql_warehouse(sql_wh, params_wh)


@router.get("/claims")
def list_claims() -> list[dict]:
    """All claims (the header row per claim) for the claim picker."""
    return _query(
        sql_lb="""SELECT claim_id, carrier, peril, insured_name, claimant_name,
                         loss_year, date_of_loss, max_amount_seen, doc_count
                  FROM dim_claim ORDER BY claim_id""",
        sql_wh=f"""SELECT claim_id, carrier, peril, insured_name, claimant_name,
                          loss_year, date_of_loss, max_amount_seen, doc_count
                   FROM {CATALOG}.{SCHEMA}.dim_claim ORDER BY claim_id""",
    )


@router.get("/claims/{claim_id}/docs")
def list_claim_docs(claim_id: str) -> list[dict]:
    """Every viewable artifact for a claim (drives the document explorer).

    silver_documents can hold several rows per doc (parsed + vision_caption), so it
    is collapsed to one row per doc_id before the join to avoid duplicate entries.
    """
    return _query(
        sql_lb="""SELECT b.doc_id, b.filename, b.ext, b.modality, b.source_uri, b.size_bytes,
                         s.doc_type, s.content_source
                  FROM bronze_file_registry b
                  LEFT JOIN (SELECT doc_id, MAX(doc_type) AS doc_type, MIN(content_source) AS content_source
                             FROM silver_documents GROUP BY doc_id) s ON b.doc_id = s.doc_id
                  WHERE b.claim_id = %s AND b.route <> 'skip'
                  ORDER BY b.modality, b.filename""",
        sql_wh=f"""SELECT b.doc_id, b.filename, b.ext, b.modality, b.source_uri, b.size_bytes,
                          s.doc_type, s.content_source
                   FROM {CATALOG}.{SCHEMA}.bronze_file_registry b
                   LEFT JOIN (SELECT doc_id, MAX(doc_type) AS doc_type, MIN(content_source) AS content_source
                              FROM {CATALOG}.{SCHEMA}.silver_documents GROUP BY doc_id) s ON b.doc_id = s.doc_id
                   WHERE b.claim_id = :p0 AND b.route <> 'skip'
                   ORDER BY b.modality, b.filename""",
        params_lb=(claim_id,),
        params_wh=[claim_id],
    )


@router.get("/claims/{claim_id}/summary")
def claim_summary(claim_id: str) -> dict:
    """Quick summary stats for the claim detail/summary card."""
    rows = _query(
        sql_lb="""SELECT claim_id, carrier, peril, insured_name, claimant_name,
                         loss_year, date_of_loss, max_amount_seen, doc_count,
                         loss_location, adjuster_name
                  FROM dim_claim WHERE claim_id = %s""",
        sql_wh=f"""SELECT claim_id, carrier, peril, insured_name, claimant_name,
                          loss_year, date_of_loss, max_amount_seen, doc_count,
                          loss_location, adjuster_name
                   FROM {CATALOG}.{SCHEMA}.dim_claim WHERE claim_id = :p0""",
        params_lb=(claim_id,),
        params_wh=[claim_id],
    )
    return rows[0] if rows else {}


@router.get("/file")
def get_file(doc_id: str = Query(...)):
    """Return one document for the viewer, keyed by doc_id.

    - PDFs / images  → raw bytes (HEIC transcoded to JPEG for the browser)
    - text / email / structured / audio-video → the extracted text from silver_documents
    """
    rows = _query(
        sql_lb="SELECT source_uri, ext FROM bronze_file_registry WHERE doc_id = %s",
        sql_wh=f"SELECT source_uri, ext FROM {CATALOG}.{SCHEMA}.bronze_file_registry WHERE doc_id = :p0",
        params_lb=(doc_id,),
        params_wh=[doc_id],
    )
    if not rows:
        raise HTTPException(404, "doc not found")
    source_uri, ext = rows[0]["source_uri"], (rows[0]["ext"] or "").lower()
    path = source_uri.replace("dbfs:", "")
    if not path.startswith(VOLUME_PREFIX):                       # path-injection fence
        raise HTTPException(403, "path not permitted")

    # Text-bearing / non-image-doc modalities: serve the parsed text (cleaner than raw bytes).
    if ext in _TEXT_EXTS or ext in {"mp3", "m4a", "mov", "mp4", "3gp"}:
        t = _query(
            sql_lb="SELECT text_content FROM silver_documents WHERE doc_id = %s LIMIT 1",
            sql_wh=f"SELECT text_content FROM {CATALOG}.{SCHEMA}.silver_documents WHERE doc_id = :p0 LIMIT 1",
            params_lb=(doc_id,),
            params_wh=[doc_id],
        )
        text = (t[0]["text_content"] if t else None) or "(no extracted text for this file)"
        return PlainTextResponse(text)

    data = _w.files.download(path).contents.read()
    if ext == "pdf":
        return Response(data, media_type="application/pdf")
    if ext == "heic":                                            # browsers can't render HEIC → JPEG
        import pillow_heif
        from PIL import Image
        pillow_heif.register_heif_opener()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        return Response(buf.getvalue(), media_type="image/jpeg")
    if ext in _IMAGE_CT:
        return Response(data, media_type=_IMAGE_CT[ext])
    return Response(data, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Streaming chat with step visibility
# ---------------------------------------------------------------------------

@router.post("/chat/stream")
async def chat_stream(request: Request):
    """SSE endpoint that streams agent responses with step visibility.

    Retrieval-agnostic: it drives whatever tools the orchestrator agent is
    configured with (here: Genie + claims_document_search + python_exec).

    Emits events:
      - {"type": "step", "id": "...", "label": "...", "status": "active|done"}
      - {"type": "delta", "text": "..."}
      - {"type": "done"}
    """
    body = await request.json()
    messages = body.get("input", [])
    # Conversation memory: the server keys the Lakebase session on the signed-in user (header set
    # by Databricks Apps) plus the client's per-claim id. The client also sends its own history as a
    # fallback for when the session store is unreachable.
    user = request.headers.get("x-forwarded-email")
    session_id = memory.scoped_session_id(user, (body.get("custom_inputs") or {}).get("session_id"))

    async def event_generator() -> AsyncGenerator[dict, None]:
        from contextlib import AsyncExitStack

        from agents import Runner

        from agent_server.agent import (
            build_mcp_servers,
            connect_healthy_mcp_servers,
            create_orchestrator_agent,
        )
        from agent_server.history import normalize_history_items

        # Step 1: connecting
        yield {"event": "message", "data": json.dumps(
            {"type": "step", "id": "connect", "label": "Connecting to data sources", "status": "active"})}

        async with AsyncExitStack() as stack:
            servers, unavailable = await connect_healthy_mcp_servers(stack, build_mcp_servers())
            yield {"event": "message", "data": json.dumps(
                {"type": "step", "id": "connect", "label": "Connecting to data sources", "status": "done"})}

            agent = create_orchestrator_agent(servers, unavailable)
            normalized = normalize_history_items(messages)

            session = None
            run_input = normalized
            if session_id:
                try:
                    session = memory.open_session(session_id)
                    await session.get_items(limit=1)  # fail fast if the session store is unreachable
                    run_input = normalized[-1:]       # prior turns come from the session
                    asyncio.create_task(memory.purge_expired_if_due(session))
                except Exception:
                    logger.warning("Session memory unavailable; using client history", exc_info=True)
                    session, run_input = None, normalized

            # Step 2: thinking
            yield {"event": "message", "data": json.dumps(
                {"type": "step", "id": "think", "label": "Analyzing your question", "status": "active"})}

            # One trace per turn, tagged with the user and session so MLflow groups a conversation.
            with mlflow.start_span(name="chat_turn", span_type="AGENT") as turn_span:
                trace_meta = {"mlflow.trace.user": user or "local-dev"}
                if session_id:
                    trace_meta["mlflow.trace.session"] = session_id
                mlflow.update_current_trace(metadata=trace_meta)
                turn_span.set_inputs({"input": run_input, "memory": session is not None})

                result = Runner.run_streamed(agent, input=run_input, session=session)
                active_tool: str | None = None
                answering = False  # flips true on first text delta

                async for event in result.stream_events():
                    if event.type == "raw_response_event":
                        ed = event.data.model_dump()
                        evt_type = ed.get("type", "")

                        # Tool call started – emit a step
                        if evt_type == "response.output_item.added":
                            item = ed.get("item", {})
                            if item.get("type") == "function_call":
                                tool_name = item.get("name", "tool")
                                label = _tool_label(tool_name)
                                active_tool = tool_name
                                # Mark thinking done
                                yield {"event": "message", "data": json.dumps(
                                    {"type": "step", "id": "think", "label": "Analyzing your question", "status": "done"})}
                                yield {"event": "message", "data": json.dumps(
                                    {"type": "step", "id": f"tool_{tool_name}", "label": label, "status": "active"})}

                        # Text delta – stream to client
                        elif evt_type == "response.output_text.delta":
                            delta = ed.get("delta", "")
                            if delta:
                                # Close any active tool step
                                if active_tool:
                                    yield {"event": "message", "data": json.dumps(
                                        {"type": "step", "id": f"tool_{active_tool}",
                                         "label": _tool_label(active_tool), "status": "done"})}
                                    active_tool = None
                                # On the first answer text: close "thinking", open "composing".
                                # Runs whether or not a tool was called, so a direct
                                # (no-tool) answer still shows a consistent step sequence.
                                if not answering:
                                    yield {"event": "message", "data": json.dumps(
                                        {"type": "step", "id": "think", "label": "Analyzing your question", "status": "done"})}
                                    yield {"event": "message", "data": json.dumps(
                                        {"type": "step", "id": "respond", "label": "Composing response", "status": "active"})}
                                    answering = True
                                yield {"event": "message", "data": json.dumps(
                                    {"type": "delta", "text": delta})}

                    elif event.type == "run_item_stream_event":
                        if hasattr(event, "item") and event.item.type == "tool_call_output_item":
                            if active_tool:
                                yield {"event": "message", "data": json.dumps(
                                    {"type": "step", "id": f"tool_{active_tool}",
                                     "label": _tool_label(active_tool), "status": "done"})}
                                active_tool = None


            # Final done
            yield {"event": "message", "data": json.dumps(
                {"type": "step", "id": "respond", "label": "Composing response", "status": "done"})}
            yield {"event": "message", "data": json.dumps({"type": "done"})}

    return EventSourceResponse(event_generator())


@router.get("/chat/history")
async def chat_history(request: Request, session_id: str = Query(...)) -> list[dict]:
    """User/assistant turns of the caller's conversation, so the UI can restore it after a reload."""
    sid = memory.scoped_session_id(request.headers.get("x-forwarded-email"), session_id)
    if not sid:
        raise HTTPException(400, "invalid session_id")
    try:
        return await memory.load_display_history(sid)
    except Exception:
        logger.warning("Could not load conversation history", exc_info=True)
        return []


def _tool_label(tool_name: str) -> str:
    """Human-readable label for the step indicator."""
    name_lower = tool_name.lower()
    if "genie" in name_lower or "execute_query" in name_lower:
        return "Querying structured claims data"
    if any(k in name_lower for k in ("search", "vector", "retrieve", "vs_index", "chunk", "index")):
        return "Searching claims documents"
    if "python" in name_lower or "exec" in name_lower:
        return "Running calculations"
    return f"Calling {tool_name}"
