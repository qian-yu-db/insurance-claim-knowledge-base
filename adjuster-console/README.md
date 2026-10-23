# Adjuster Console (v2)

A Databricks App for insurance claims adjusters: a **2-pane console** (claim list, document /
image / transcript viewer on the left, grounded chat on the right) backed by an **OpenAI Agents
SDK** orchestrator. One FastAPI process serves the UI (`client/`), the viewer data APIs (`/api/*`),
and the agent.

Built from the `agent-openai-agents-sdk-multiagent` app template (see `LICENSE` and `NOTICE`).
The knowledge base it reads is built by the [pipeline](../pipeline/notebooks/README.md); run that first.

## What the agent can use

| Tool | Backend | Used for |
|---|---|---|
| `claims_document_search` | In-process hybrid search in **Lakebase Postgres** (`claims.gold_chunks_embeddings`): vector ANN + BM25, fused with RRF. Exact scan when scoped to one claim. | What happened, medical findings, correspondence, photos (captions), call transcripts |
| `claim_details`, `claim_payments`, `claim_financials`, `payment_totals`, `top_claims`, `claim_notes` | Fixed **UC SQL table functions** in `fins_genai.claims_multimodal_kb` (pipeline nb 09), run on the app's SQL warehouse (`agent_server/claims_functions.py`), ~1 s per call | Claim header, payments, totals and comparisons, top claims, claim-note timelines |
| `python_exec` | `system.ai.python_exec` via the UC Functions MCP server, scoped to that one function | Ad-hoc math on retrieved values |

- **LLM:** `system.ai.gpt-5-2` through **Unity Gateway** (`AsyncDatabricksOpenAI(use_ai_gateway=True)`).
  It's a Unity Catalog model service; the app identity needs `EXECUTE` on it (`account users` have it by default).
- **Routing and citations:** the instructions in `agent_server/agent.py` tell the agent which tool to use
  and to end document answers with a `Sources:` line, which the UI turns into links that open the document.
- **Degraded mode:** if a tool is unreachable at the start of a turn, the agent runs without it and says so.
  If Lakebase search fails, the tool returns `UNAVAILABLE: …` instead of an empty result.

## Conversation memory

Each adjuster has **one ongoing conversation per claim**. It resumes after a page reload; **New chat**
starts a fresh one.

- Stored in Lakebase as OpenAI Agents SDK sessions (`AsyncDatabricksSession`): schema `agent_memory`
  (env `LAKEBASE_MEMORY_SCHEMA`), tables `agent_sessions` and `agent_messages`. The app's service
  principal creates and owns the schema on its first chat.
- The session id is `<signed-in user email>:<browser id>`. The server always adds the user prefix
  (from `X-Forwarded-Email`), so one user can't open another's conversation.
- Conversations idle for **30 days** are deleted (checked at most once a day, triggered by chat traffic).
- If the session store is unreachable, chat still works using the history the browser sends.
- MLflow traces carry `mlflow.trace.session` and `mlflow.trace.user`, so the trace list can **Group by session**.

## Project layout

| Path | What it is |
|---|---|
| `agent_server/agent.py` | Tools, LLM client, instructions, `/invocations` handlers |
| `agent_server/claims_functions.py` | The structured tools: calls the UC functions on the SQL warehouse |
| `agent_server/data_api.py` | Viewer APIs (`/api/claims`, `/api/claims/{id}/docs`, `/api/claims/{id}/summary`, `/api/file`) and the UI chat endpoints (`POST /api/chat/stream`, `GET /api/chat/history`) |
| `agent_server/lakebase.py`, `lakebase_search.py` | Lakebase connection pool (OAuth token refresh) and the hybrid search query |
| `agent_server/memory.py` | Session scoping, history restore, 30-day retention |
| `client/index.html` | The single-page UI |
| `databricks.yml` | App config, env vars, and resource permissions (DABs) |
| `tests/` | Unit tests (`uv run --frozen python -m pytest`) |

## Configure for your workspace

The repo ships with the reference workspace's IDs. Replace them:

| File | What to set |
|---|---|
| `databricks.yml` | `experiment_id`, SQL warehouse id (resource + `DATABRICKS_WAREHOUSE_ID`), the `fins_genai.claims_multimodal_kb` table/volume/function names, the Lakebase `postgres` resource and `LAKEBASE_*` env vars, `EMBEDDING_ENDPOINT` |
| `agent_server/agent.py` | `MODEL` |
| `agent_server/claims_functions.py` | `FUNCTION_SCHEMA` |
| `agent_server/data_api.py` | `CATALOG`, `SCHEMA` (and the landing-Volume prefix) |

After the first deploy, grant the app's service principal read access to the pipeline's Lakebase
`claims` schema (see step 3 of the [pipeline prerequisites](../pipeline/notebooks/README.md)). Get the
SP client id with `databricks apps get <app-name>` → `service_principal_client_id`.

## Run locally

```bash
uv run quickstart --profile <your-profile>   # auth, .env, MLflow experiment
uv run start-server --reload                 # http://localhost:8000
```

`.env.example` (copied to `.env` by quickstart) carries the Lakebase settings; set the warehouse id:

```bash
LAKEBASE_PROJECT_ID=insurance-claim
LAKEBASE_BRANCH_ID=production
LAKEBASE_ENDPOINT_ID=primary
LAKEBASE_DATABASE=databricks_postgres
LAKEBASE_SCHEMA=claims
LAKEBASE_MEMORY_SCHEMA=agent_memory_dev   # keep local conversations out of the app's schema
DATABRICKS_WAREHOUSE_ID=<warehouse-id>
```

> **Use a separate memory schema locally.** Whoever creates `agent_memory` owns it. If your local run
> creates it before the app does, the app's service principal can't write to it.

Locally there's no signed-in user header, so sessions are prefixed `local-dev:`.

```bash
uv run --frozen python -m pytest   # unit tests
uv run preflight                   # starts the server, sends a test request to /invocations
uv run agent-evaluate              # MLflow evaluation of the agent (see Evaluate below)
```

## Evaluate

`uv run agent-evaluate` scores the agent (in-process, the same code the app runs) on the evaluation
dataset `fins_genai.claims_multimodal_kb.agent_eval_v2`, an MLflow evaluation dataset in Unity Catalog.
Results are logged to the app's MLflow experiment as run `v2-eval-<git sha>`.

| Source | What it holds |
|---|---|
| `sample_data/eval/eval_questions.json` | Hand-written questions: document, structured (UC functions), timeline (claim notes), multi-source and out-of-scope, with expected facts, numbers and tools |
| `sample_data/eval/eval_synthetic.json` | Document questions generated with `databricks.agents.evals.generate_evals_df` (`scripts/generate_synthetic_evals.py`), then reviewed by hand |

Scorers: `Correctness` (expected facts), `RelevanceToQuery`, `RetrievalGroundedness`, `Safety`, plus
custom ones in `agent_server/eval_scorers.py`: `numbers_match`, `tool_routing`, `cites_sources`,
`retrieved_expected_doc` (search returned the source document) and `latency_s`.

```bash
uv run agent-evaluate --build-dataset            # after editing either JSON file
uv run agent-evaluate                            # full run
uv run agent-evaluate --category structured --limit 3
```

**In the workspace:** the notebook [`evaluation/agent_evaluation.py`](../evaluation/agent_evaluation.py) runs the
same evaluation interactively. Import it into the workspace and point its `agent_root` widget at a folder
that contains `agent_server/`, by default the app's deployed bundle files. Each run logs that bundle's git
branch and commit as parameters. Results go to the same experiment.

To refresh the synthetic questions after the documents change, rerun
`uv run scripts/generate_synthetic_evals.py --profile <profile> --warehouse-id <id>` from the repo root,
review its `eval_synthetic_candidates.json`, and save the kept rows as `eval_synthetic.json`.

## Deploy

```bash
databricks bundle validate --profile <your-profile>
databricks bundle deploy   --profile <your-profile>
databricks bundle run agent_openai_agents_sdk_multiagent --profile <your-profile>   # (re)starts the app
```

`bundle deploy` only uploads code and configures resources; `bundle run` is what restarts the app with
the new code. When deploying with DABs, `databricks.yml` (not `app.yaml`) defines the start command and env vars.

- **`uv.lock` must resolve from public PyPI.** The Apps build can't reach private or internal package
  mirrors. If your local uv config points at one, rewrite the lock's URLs to `pypi.org` /
  `files.pythonhosted.org` before deploying.
- **App names can't be changed.** To rename, create a new app (new URL and service principal) and redo the grants.

## Query the deployed agent

The UI uses `POST /api/chat/stream` (server-sent events). The body is
`{"input": [...messages], "custom_inputs": {"session_id": "<8-64 chars of A-Z a-z 0-9 _ ->"}}`.
Leave out `session_id` to skip memory.

The standard agent endpoint `/invocations` (Responses API) is also served. Calling a Databricks App
needs an **OAuth** token; PATs get a 302.

```bash
TOKEN=$(databricks auth token --profile <your-profile> -o json | jq -r .access_token)
curl -N -X POST <app-url>/api/chat/stream \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"input":[{"role":"user","content":"[Claim CLM-2026-04487] Who is the insurer?"}],
       "custom_inputs":{"session_id":"mytest0001"}}'
```

## Common issues

| Symptom | Fix |
|---|---|
| App is running old code after deploy | Run `databricks bundle run agent_openai_agents_sdk_multiagent` |
| `An app with the same name already exists` | The app was created outside DABs. Bind it: `databricks bundle deployment bind agent_openai_agents_sdk_multiagent <app-name> --auto-approve`, then deploy |
| Build fails downloading packages | `uv.lock` points at a private mirror; rewrite to public PyPI (above) |
| Document search returns `UNAVAILABLE` | The SP lacks the `claims` grants, or Lakebase Search isn't enabled (see the pipeline README) |
| `permission denied for schema agent_memory` | A user created the schema first. Drop it (this deletes stored conversations) and let the app recreate it |
| 200 OK in logs but an error in the stream | Expected; the status is sent before streaming starts. Check the app logs |
