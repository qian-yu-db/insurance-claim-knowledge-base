# Modified from databricks/app-templates (agent-openai-agents-sdk-multiagent); see LICENSE and NOTICE.
"""
Insurance Claims Assistant — orchestrator agent for adjusters.

Mirrors the Agent Bricks supervisor (mas-63a9094a-endpoint) in code so the app
controls the response (notably: surfacing document citations for the viewer pane).

Tools:
  1. Genie space          → structured claims data (fact_payments, dim_claim) [MCP]
  2. claims_document_search → unstructured claims KB via in-process Lakebase hybrid search
  3. system.ai.python_exec → ad-hoc calculations [MCP, scoped to this one function]

Routing: structured → Genie, documents/narrative → claims_document_search, math → python_exec.
"""

import json
import logging
from contextlib import AsyncExitStack
from typing import AsyncGenerator

import mlflow
from agents import Agent, Runner, function_tool, set_default_openai_api, set_default_openai_client
from agents.tracing import set_trace_processors
from databricks_openai import AsyncDatabricksOpenAI
from databricks_openai.agents import McpServer
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)

from agent_server.history import normalize_history_items
from agent_server.lakebase_search import SearchUnavailable, search_documents
from agent_server.utils import (
    build_mcp_url,
    get_session_id,
    process_agent_stream_events,
)

# ---------------------------------------------------------------------------
# Resource configuration (fevm-classic-stable)
# ---------------------------------------------------------------------------
GENIE_SPACE_ID = "01f1b0d16247106aacc4750ce7e1b24f"          # "Insurance Claims and Payments"
PYTHON_EXEC_FN = "system.ai.python_exec"  # same securable as databricks.yml; MCP is scoped to this one
                                          # function (the schema URL would expose all of system.ai)
MODEL = "system.ai.gpt-5-2"                                   # orchestrator LLM (Unity Gateway model service)

# ---------------------------------------------------------------------------
# Client setup
# ---------------------------------------------------------------------------
set_default_openai_client(AsyncDatabricksOpenAI(use_ai_gateway=True))  # {host}/ai-gateway/mlflow/v1
set_default_openai_api("chat_completions")
set_trace_processors([])  # only use mlflow for trace processing
mlflow.openai.autolog()
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP servers (Databricks-hosted)
# ---------------------------------------------------------------------------


def build_mcp_servers() -> list[McpServer]:
    """The two hosted MCP tools the orchestrator routes between (document search is in-process)."""
    return [
        McpServer(
            url=build_mcp_url(f"/api/2.0/mcp/genie/{GENIE_SPACE_ID}"),
            name="Genie (structured claims data)",
        ),
        McpServer(
            url=build_mcp_url(f"/api/2.0/mcp/functions/{PYTHON_EXEC_FN.replace('.', '/')}"),
            name="Python exec",
        ),
    ]


async def connect_healthy_mcp_servers(
    stack: AsyncExitStack, servers: list[McpServer]
) -> tuple[list[McpServer], list[str]]:
    """Connect each MCP server and verify it can list its tools; drop any that fail.

    The Agents SDK lists tools lazily inside ``Runner.run``, so a server that connects
    but fails at list time (e.g. an unauthorized Genie space) would otherwise crash the
    whole request. We force the connectivity/authorization check here: healthy servers
    are kept; failures are dropped and their names returned so the orchestrator runs with
    whatever is available. Returns (healthy_servers, unavailable_names).
    """
    healthy: list[McpServer] = []
    unavailable: list[str] = []
    for server in servers:
        name = getattr(server, "name", "MCP server")
        try:
            connected = await stack.enter_async_context(server)
            await connected.list_tools()
            healthy.append(connected)
        except Exception:
            logger.warning("MCP server %r unavailable; continuing without it.", name, exc_info=True)
            unavailable.append(name)
    return healthy, unavailable


# ---------------------------------------------------------------------------
# In-process document search tool (replaces Vector Search MCP)
# ---------------------------------------------------------------------------


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
    return run_document_search(query, claim_id)


def run_document_search(query: str, claim_id: str = "") -> str:
    """Tool body, kept separate from the @function_tool wrapper so it can be tested directly."""
    try:
        results = search_documents(query=query, claim_id=claim_id or None, top_k=10)
    except SearchUnavailable:
        return ("UNAVAILABLE: the claims document search is temporarily unavailable. Do not conclude "
                "that documents are missing; tell the user document search could not be reached.")
    if not results:
        return "No matching documents found."
    return json.dumps(results, default=str)


# ---------------------------------------------------------------------------
# Orchestrator agent
# ---------------------------------------------------------------------------

INSTRUCTIONS = """You are the Insurance Claims Assistant for insurance adjusters. You help them \
investigate and analyze claims by combining structured claim data with the documents in the \
claims knowledge base.

## Routing
- **Structured data** (payments, totals, amounts paid, reserves, claim lookups, counts, trends, \
comparisons by carrier / peril / adjuster, financial breakdowns): use the **Genie** tools. They \
query fact_payments and dim_claim.
- **Documents / narrative** (what happened, medical findings, injuries, correspondence, defense \
counsel, negotiation, coverage letters, investigation/SIU, damage descriptions from photos, \
recorded-call transcripts): use the **Claims document search** (use claims_document_search) tool.
- **Calculations / ad-hoc math** on retrieved values: use **python_exec**.

## Scoping
When the user's question is about a specific claim, pass the claim number (e.g. CLM-2026-04487) to \
the tools so results are scoped to that claim.

## Citations (important for the app UI)
When you answer from the Claims document search tool, ALWAYS list the sources you used at the end \
under a "Sources:" line — include each passage's `source_uri`, `doc_type`, and `claim_id` when \
present in the tool result. The app uses these to open the exact document in the viewer pane. \
When you answer from Genie, note that the figures come from structured claim data.

Do not fabricate. If a needed data source is unavailable, say so briefly instead of guessing. If \
the request is ambiguous, ask a short clarifying question."""


def create_orchestrator_agent(
    mcp_servers: list[McpServer], unavailable_tools: list[str] | None = None
) -> Agent:
    instructions = INSTRUCTIONS
    if unavailable_tools:
        names = ", ".join(sorted(set(unavailable_tools)))
        instructions += (
            f"\n\nThese data sources are currently UNAVAILABLE (not authorized or unreachable "
            f"right now): {names}. If answering requires one of them, briefly tell the user it "
            "isn't available right now instead of guessing, and use whatever else you have."
        )
    return Agent(
        name="Insurance Claims Assistant",
        instructions=instructions,
        model=MODEL,
        mcp_servers=mcp_servers,
        tools=[claims_document_search],
    )


# ---------------------------------------------------------------------------
# MLflow Responses API handlers
# ---------------------------------------------------------------------------


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    if session_id := get_session_id(request):
        mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})
    async with AsyncExitStack() as stack:
        servers, unavailable = await connect_healthy_mcp_servers(stack, build_mcp_servers())
        agent = create_orchestrator_agent(servers, unavailable)
        messages = normalize_history_items([i.model_dump() for i in request.input])
        result = await Runner.run(agent, messages)
        return ResponsesAgentResponse(output=[item.to_input_item() for item in result.new_items])


@stream()
async def stream_handler(request: ResponsesAgentRequest) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    if session_id := get_session_id(request):
        mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})
    async with AsyncExitStack() as stack:
        servers, unavailable = await connect_healthy_mcp_servers(stack, build_mcp_servers())
        agent = create_orchestrator_agent(servers, unavailable)
        messages = normalize_history_items([i.model_dump() for i in request.input])
        result = Runner.run_streamed(agent, input=messages)
        async for event in process_agent_stream_events(result.stream_events()):
            yield event
