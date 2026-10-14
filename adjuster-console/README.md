# Adjuster Console (v1)

A Databricks App for insurance claims adjusters: a **2-pane console** (claim list, document /
image / transcript viewer on the left, grounded chat on the right) backed by an **OpenAI Agents
SDK** orchestrator. One FastAPI process serves the UI (`client/`), the viewer data APIs (`/api/*`),
and the agent.

This is the **v1.x line** (retrieval with Databricks AI Search / Vector Search), maintained on
`release/v1.1` and deployed as the app **`adjuster-console-v1`**. `main` is v2.x (Lakebase hybrid
search, conversation memory).

Built from the `agent-openai-agents-sdk-multiagent` app template (see `LICENSE` and `NOTICE`).
The knowledge base it reads is built by the [pipeline](../pipeline/notebooks/README.md); run that first.

## What the agent can use

| Tool | Backend | Used for |
|---|---|---|
| `claims_document_search` | **AI Search** `HYBRID` queries (vector + keyword) on the Delta Sync index `gold_chunks_vs_index`, via `VectorSearchRetrieverTool`, with a `claim_id` filter when the question is about one claim | What happened, medical findings, correspondence, photos (captions), call transcripts |
| Genie | **Genie space** "Insurance Claims and Payments" over `dim_claim` / `fact_payments`, via the managed Genie MCP server | Payments, totals, reserves, counts, comparisons |
| `python_exec` | `system.ai.python_exec` via the UC Functions MCP server, scoped to that one function | Ad-hoc math on retrieved values |

- **LLM:** the `databricks-gpt-5-2` Foundation Model serving endpoint (`MODEL` in `agent_server/agent.py`).
- **Why not the Vector Search MCP server:** it only takes a `query` and runs ANN without filters. Up to
  v1.1.0 the app used it; v1.1.1+ queries the index directly to get `HYBRID` and the `claim_id` filter.
- **Routing and citations:** the instructions in `agent_server/agent.py` tell the agent which tool to use
  and to end document answers with a `Sources:` line, which the UI turns into links that open the document.
- **Degraded mode:** if Genie or `python_exec` is unreachable at the start of a turn, the agent runs without
  it and says so. If AI Search fails, the tool returns `UNAVAILABLE: …` instead of an empty result.
- **No server-side memory:** the browser sends the conversation history with every turn, and it's lost on reload.

## Project layout

| Path | What it is |
|---|---|
| `agent_server/agent.py` | Tools, LLM client, instructions, `/invocations` handlers |
| `agent_server/data_api.py` | Viewer APIs (`/api/claims`, `/api/claims/{id}/docs`, `/api/claims/{id}/summary`, `/api/file`) and the UI chat endpoint (`POST /api/chat/stream`) |
| `client/index.html` | The single-page UI |
| `databricks.yml` | App config, env vars, and resource permissions (DABs) |
| `tests/` | Unit tests (`uv run --frozen python -m pytest`) |

## Configure for your workspace

The repo ships with the reference workspace's IDs. Replace them:

| File | What to set |
|---|---|
| `databricks.yml` | `experiment_id`, Genie `space_id`, SQL warehouse id (resource + `DATABRICKS_WAREHOUSE_ID`), the `fins_genai.claims_multimodal_kb` table/volume/index names, the LLM serving endpoint |
| `agent_server/agent.py` | `GENIE_SPACE_ID`, `VS_CATALOG` / `VS_SCHEMA` / `VS_INDEX`, `MODEL` |
| `agent_server/data_api.py` | `CATALOG`, `SCHEMA` (and the landing-Volume prefix) |

The index comes from the pipeline (`07_vector_search_index`, one-time create); the
`claims-kb-v1-index-sync` job keeps it in sync. If you recreate the index, redeploy the app so its
service principal gets `SELECT` on the new index.

## Run locally

```bash
uv run quickstart --profile <your-profile>   # auth, .env, MLflow experiment
uv run start-server --reload                 # http://localhost:8000
```

`VectorSearchRetrieverTool` doesn't accept CLI OAuth (U2M) credentials, so for local runs set a PAT in
`.env`, plus the warehouse id:

```bash
DATABRICKS_HOST=https://<your-workspace>.cloud.databricks.com
DATABRICKS_TOKEN=<personal-access-token>
DATABRICKS_WAREHOUSE_ID=<warehouse-id>
```

The deployed app authenticates as its service principal (M2M), so it needs no token.

```bash
uv run --frozen python -m pytest   # unit tests
uv run preflight                   # starts the server, sends a test request to /invocations
uv run agent-evaluate              # MLflow evaluation (edit agent_server/evaluate_agent.py)
```

## Deploy

```bash
databricks bundle validate --profile <your-profile>
databricks bundle deploy   --profile <your-profile>
databricks bundle run agent_openai_agents_sdk_multiagent --profile <your-profile>   # (re)starts the app
```

`bundle deploy` only uploads code and configures resources; `bundle run` is what restarts the app with
the new code. When deploying with DABs, `databricks.yml` (not `app.yaml`) defines the start command and env vars.
The v1 bundle (`adjuster_console_v1`) is separate from v2's, so both apps can live in one workspace.

- **`uv.lock` must resolve from public PyPI.** The Apps build can't reach private or internal package
  mirrors. If your local uv config points at one, rewrite the lock's URLs to `pypi.org` /
  `files.pythonhosted.org` before deploying.
- **App names can't be changed.** To rename, create a new app (new URL and service principal).

## Query the deployed agent

The UI uses `POST /api/chat/stream` (server-sent events) with the body `{"input": [...messages]}`.
The standard agent endpoint `/invocations` (Responses API) is also served. Calling a Databricks App
needs an **OAuth** token; PATs get a 302.

```bash
TOKEN=$(databricks auth token --profile <your-profile> -o json | jq -r .access_token)
curl -N -X POST <app-url>/api/chat/stream \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"input":[{"role":"user","content":"[Claim CLM-2026-04487] Who is the insurer?"}]}'
```

## Common issues

| Symptom | Fix |
|---|---|
| App is running old code after deploy | Run `databricks bundle run agent_openai_agents_sdk_multiagent` |
| `An app with the same name already exists` | The app was created outside DABs. Bind it: `databricks bundle deployment bind agent_openai_agents_sdk_multiagent <app-name> --auto-approve`, then deploy |
| Build fails downloading packages | `uv.lock` points at a private mirror; rewrite to public PyPI (above) |
| Document search returns `UNAVAILABLE` | Locally: missing PAT (above). Deployed: the index was recreated and the SP lost `SELECT`; redeploy |
| Index sync fails with `DIFFERENT_DELTA_TABLE_READ_BY_STREAMING_SOURCE` | `gold_chunks` was replaced rather than merged (`06` MERGEs in place since v1.1.1). Delete the index, re-run `07_vector_search_index`, then redeploy the app |
| 200 OK in logs but an error in the stream | Expected; the status is sent before streaming starts. Check the app logs |
