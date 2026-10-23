# Databricks notebook source
# DBTITLE 1,Overview
# MAGIC %md
# MAGIC # Claims Agent Evaluation
# MAGIC
# MAGIC Runs `mlflow.genai.evaluate` over the UC evaluation dataset `fins_genai.claims_multimodal_kb.agent_eval_v2` against the Insurance Claims Assistant defined in the bundle's `agent_server/agent.py`.
# MAGIC
# MAGIC It follows `agent_server/evaluate_agent.py`: it imports the agent module so the `@invoke` handler gets registered, gets it with `get_invoke_function()`, and wraps it as a sync `predict_fn`. The difference is that it uses the single-turn labeled dataset (questions plus `expected_facts` / `expected_numbers` / `expected_tools`) in place of the conversation simulator.
# MAGIC
# MAGIC **Scorers**
# MAGIC * `Correctness`: response vs `expected_facts`
# MAGIC * `RelevanceToQuery`, `Safety`, `Completeness`
# MAGIC * `retrieval_groundedness`: `RetrievalGroundedness`, only on rows where document search ran (it scores "no" when nothing was retrieved)
# MAGIC * `tool_routing` (custom): the agent called every tool in `expected_tools`
# MAGIC * `numbers_match` (custom): every value in `expected_numbers` appears in the response (understands `$`, commas, `K`/`M`)
# MAGIC
# MAGIC The agent code comes from `agent_root` (by default the app's deployed bundle). The run logs that bundle's git
# MAGIC branch and commit, so results can be tied to a code version.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install -q "mlflow[databricks]>=3.10.0" "databricks-agents>=1.9.3" "databricks-openai>=0.13.0" "openai-agents>=0.4.1" "databricks-sdk>=0.70.0" "psycopg[binary]>=3.2" "psycopg-pool>=3.2" python-dotenv
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configuration
dbutils.widgets.text("agent_root", "/Workspace/Users/q.yu@databricks.com/.bundle/agent_openai_agents_sdk_multiagent/dev/files", "Agent project root (contains agent_server/)")
dbutils.widgets.text("eval_table", "fins_genai.claims_multimodal_kb.agent_eval_v2", "Evaluation dataset (UC)")
dbutils.widgets.text("experiment_id", "2456186449359403", "MLflow experiment ID")
dbutils.widgets.text("warehouse_id", "322702d284b4a804", "SQL warehouse for claims functions")
dbutils.widgets.text("categories", "", "Categories (comma-separated, blank = all)")
dbutils.widgets.text("limit", "0", "Max rows (0 = all)")
dbutils.widgets.text("run_name", "claims-agent-eval", "Run name")
dbutils.widgets.text("max_workers", "4", "Parallel eval workers")

AGENT_ROOT = dbutils.widgets.get("agent_root")
EVAL_TABLE = dbutils.widgets.get("eval_table")
EXPERIMENT_ID = dbutils.widgets.get("experiment_id")
WAREHOUSE_ID = dbutils.widgets.get("warehouse_id")
CATEGORIES = [c.strip() for c in dbutils.widgets.get("categories").split(",") if c.strip()]
LIMIT = int(dbutils.widgets.get("limit") or 0)
RUN_NAME = dbutils.widgets.get("run_name")
MAX_WORKERS = int(dbutils.widgets.get("max_workers") or 4)

print(f"agent_root={AGENT_ROOT}\neval_table={EVAL_TABLE}\nexperiment_id={EXPERIMENT_ID}\ncategories={CATEGORIES or 'all'} limit={LIMIT or 'all'} max_workers={MAX_WORKERS}")

# COMMAND ----------

# DBTITLE 1,Environment and agent import
import os, sys, logging
import mlflow

# Same env the app gets from databricks.yml. Must be set BEFORE importing agent_server
# (claims_functions reads DATABRICKS_WAREHOUSE_ID at import time).
os.environ["MLFLOW_TRACKING_URI"] = "databricks"
os.environ["MLFLOW_REGISTRY_URI"] = "databricks-uc"
os.environ["MLFLOW_EXPERIMENT_ID"] = EXPERIMENT_ID
os.environ["DATABRICKS_WAREHOUSE_ID"] = WAREHOUSE_ID
# Fewer concurrent rows: the default parallelism overloaded the agent (MCP/Lakebase/warehouse) and rows were dropped
os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = str(MAX_WORKERS)
# Lakebase search defaults (override here if needed):
# os.environ["LAKEBASE_PROJECT_ID"] = "insurance-claim"

mlflow.set_experiment(experiment_id=EXPERIMENT_ID)
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)

if AGENT_ROOT not in sys.path:
    sys.path.insert(0, AGENT_ROOT)

# Importing the module registers the @invoke handler (same as evaluate_agent.py)
from agent_server import agent  # noqa: F401
from mlflow.genai.agent_server import get_invoke_function

invoke_fn = get_invoke_function()
assert invoke_fn is not None, "No function registered with @invoke() found in agent_server.agent"
print("Loaded invoke handler:", invoke_fn.__name__, "| model:", agent.MODEL)

# Which code is under test: a bundle deploy writes its git info to <bundle>/state/metadata.json, next to files/.
import json
from pathlib import Path

_meta = Path(AGENT_ROOT).parent / "state" / "metadata.json"
AGENT_GIT = json.loads(_meta.read_text()).get("config", {}).get("bundle", {}).get("git", {}) if _meta.exists() else {}
print("Agent code:", AGENT_GIT.get("branch", "unknown"), "@", AGENT_GIT.get("commit", "unknown")[:7],
      "(the commit checked out at deploy; uncommitted changes are not recorded)")

# COMMAND ----------

# DBTITLE 1,Define predict_fn
import asyncio, contextvars, logging, threading, time, traceback
import concurrent.futures as cf
from mlflow.types.responses import ResponsesAgentRequest

logger = logging.getLogger("claims_eval")
PREDICT_TIMEOUT_S = 300

# A single background event loop runs every agent call. mlflow.genai.evaluate calls predict_fn
# from worker threads, and the async OpenAI/MCP clients should stay on one loop.
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()

# One entry per row whose agent call failed; shown after the evaluation run
predict_failures: list[dict] = []
_fail_lock = threading.Lock()


def _run(coro, timeout=PREDICT_TIMEOUT_S):
    """Run coro on the shared loop with the caller's contextvars (including the active MLflow span),
    so the agent's spans nest under the trace that evaluate opens for this row."""
    ctx = contextvars.copy_context()
    fut: cf.Future = cf.Future()
    holder = {}

    def _start():
        task = _loop.create_task(coro, context=ctx)
        holder["task"] = task

        def _done(t):
            if t.cancelled():
                fut.cancel()
            elif t.exception() is not None:
                fut.set_exception(t.exception())
            else:
                fut.set_result(t.result())

        task.add_done_callback(_done)

    _loop.call_soon_threadsafe(_start)
    try:
        return fut.result(timeout=timeout)
    except cf.TimeoutError:
        if "task" in holder:
            _loop.call_soon_threadsafe(holder["task"].cancel)
        raise TimeoutError(f"agent call exceeded {timeout}s")


def _summarize(response: dict) -> dict:
    """Pull the final assistant text and the names of the tools called out of the Responses output."""
    texts, tools = [], []
    for item in response.get("output", []):
        t = item.get("type")
        if t == "message" and item.get("role", "assistant") == "assistant":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("text"):
                    texts.append(c["text"])
        elif t == "function_call" and item.get("name"):
            tools.append(item["name"])
    return {"response": texts[-1] if texts else "", "tool_calls": tools}


def build_prompt(question: str, claim_id: str | None) -> str:
    """Same message format as the local run (v2-eval-efed14e): '[Claim <id>] <question>' when the row has a claim."""
    return f"[Claim {claim_id}] {question}" if claim_id else question


def predict_fn(question: str, claim_id: str = None, category: str = None, id: str = None) -> dict:
    # Inputs mirror the dataset's `inputs` keys.
    req = ResponsesAgentRequest(input=[{"role": "user", "content": build_prompt(question, claim_id)}])
    start = time.monotonic()
    try:
        resp = _run(invoke_fn(req)) if asyncio.iscoroutinefunction(invoke_fn) else invoke_fn(req)
        out = _summarize(resp.model_dump())
        if not out["response"] and not out["tool_calls"]:
            raise RuntimeError("agent returned no message and no tool calls")
        return out
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        elapsed = round(time.monotonic() - start, 1)
        logger.error("predict_fn failed for id=%s after %ss: %s", id, elapsed, err)
        with _fail_lock:
            predict_failures.append({
                "id": id, "category": category, "question": question,
                "seconds": elapsed, "error": err, "traceback": traceback.format_exc()[-2000:],
            })
        span = mlflow.get_current_active_span()
        if span is not None:
            span.set_attributes({"eval.error": err})
        # Re-raise so evaluate marks the row as errored instead of scoring an empty answer
        raise

# COMMAND ----------

# DBTITLE 1,Smoke test one question
print("Prompt:", build_prompt("What is the total claim cost of CLM-2026-04487 so far?", "CLM-2026-04487"))
out = predict_fn("What is the total claim cost of CLM-2026-04487 so far?", claim_id="CLM-2026-04487")
print("Tools:", out["tool_calls"])
print(out["response"])

# COMMAND ----------

# DBTITLE 1,Load evaluation dataset
from mlflow.genai.datasets import get_dataset

ds = get_dataset(name=EVAL_TABLE)
eval_df = ds.to_df()[["inputs", "expectations"]].copy()
eval_df["category"] = eval_df["inputs"].apply(lambda x: x.get("category"))

if CATEGORIES:
    eval_df = eval_df[eval_df["category"].isin(CATEGORIES)]
if LIMIT:
    eval_df = eval_df.head(LIMIT)

print(f"{len(eval_df)} rows")
display(eval_df["category"].value_counts().rename_axis("category").reset_index(name="n"))
eval_data = eval_df[["inputs", "expectations"]].reset_index(drop=True)

# COMMAND ----------

# DBTITLE 1,Scorers
import re
from mlflow.entities import Feedback
from mlflow.genai.scorers import (
    Completeness,
    Correctness,
    RelevanceToQuery,
    RetrievalGroundedness,
    Safety,
    scorer,
)


@scorer
def tool_routing(outputs, expectations):
    """Agent called every tool listed in expected_tools."""
    expected = (expectations or {}).get("expected_tools")
    if not expected:
        return None
    called = set((outputs or {}).get("tool_calls", []))
    missing = [t for t in expected if t not in called]
    return Feedback(
        value="yes" if not missing else "no",
        rationale=f"expected={expected}, called={sorted(called)}, missing={missing}",
    )


# Same parsing as agent_server/eval_scorers.py (kept here: the deployed bundle may predate that file).
# $1,234.56 · 132500 · $132.5K · 1.2M; the optional suffix is a multiplier. Dates like 2026-08-14 aren't
# read as negative numbers.
_NUMBER = re.compile(r"(?<![\w.])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?([kKmM])?(?![\w])")
_SCALE = {"k": 1e3, "m": 1e6}


def numbers_in(text: str) -> list[float]:
    values = []
    for whole, frac, suffix in _NUMBER.findall(text or ""):
        value = float(whole.replace(",", "") + (frac or ""))
        values.append(value * _SCALE.get(suffix.lower(), 1) if suffix else value)
    return values


def _number_present(expected: float, found: list[float]) -> bool:
    # exact to the cent; a rounded "$132.5K" style value within 0.5%
    return any(abs(v - expected) < 0.01 or (expected and abs(v - expected) / expected < 0.005) for v in found)


@scorer
def numbers_match(outputs, expectations):
    """Every value in expected_numbers appears in the response."""
    expected = (expectations or {}).get("expected_numbers")
    if not expected:
        return None
    found = numbers_in((outputs or {}).get("response", ""))
    missing = [n for n in expected if not _number_present(float(n), found)]
    return Feedback(
        value="yes" if not missing else "no",
        rationale=f"expected={expected}, missing={missing}",
    )


from mlflow.entities import SpanType

_groundedness = RetrievalGroundedness()


@scorer(name="retrieval_groundedness")
def retrieval_groundedness(trace):
    """RetrievalGroundedness, only when document search ran. On its own it scores "no" for answers that
    retrieved nothing (e.g. every UC-function answer), which isn't meaningful."""
    if not trace.search_spans(span_type=SpanType.RETRIEVER):
        return None
    result = _groundedness(trace=trace)
    if not isinstance(result, list):
        return result
    # one Feedback per retriever span: combine, so this scorer returns a single assessment per row
    grounded = all(str(f.value).lower() in ("yes", "true") for f in result)
    return Feedback(value="yes" if grounded else "no",
                    rationale=" | ".join(f.rationale or "" for f in result))


scorers = [
    Correctness(),            # uses expectations.expected_facts
    RelevanceToQuery(),
    Completeness(),
    Safety(),
    retrieval_groundedness,   # rows that used document search only
    tool_routing,
    numbers_match,
]

# COMMAND ----------

# DBTITLE 1,Run evaluation
predict_failures.clear()

with mlflow.start_run(run_name=RUN_NAME) as run:
    mlflow.log_params({
        "max_workers": MAX_WORKERS,
        "prompt_format": "[Claim <id>] <question>",
        "eval_table": EVAL_TABLE,
        "agent_model": agent.MODEL,
        "agent_root": AGENT_ROOT,
        "agent_git_branch": AGENT_GIT.get("branch", "unknown"),
        "agent_git_commit": AGENT_GIT.get("commit", "unknown"),
        "categories": ",".join(CATEGORIES) or "all",
        "n_rows": len(eval_data),
    })
    results = mlflow.genai.evaluate(
        data=eval_data,
        predict_fn=predict_fn,
        scorers=scorers,
    )

print("Run ID:", results.run_id)
display(spark.createDataFrame([(k, float(v)) for k, v in sorted(results.metrics.items())], ["metric", "value"]))

print(f"{len(predict_failures)} of {len(eval_data)} rows failed in predict_fn")
if predict_failures:
    import pandas as pd
    display(pd.DataFrame(predict_failures)[["id", "category", "seconds", "error", "question"]])

# COMMAND ----------

# DBTITLE 1,Per-category breakdown
import json
import pandas as pd

from mlflow.entities import Feedback as _Feedback

traces = mlflow.search_traces(run_id=results.run_id, locations=[EXPERIMENT_ID], return_type="list")

# Map a trace back to its dataset row: by the predict_fn inputs when present, else by the prompt text
by_prompt = {build_prompt(i["question"], i.get("claim_id")): i for i in eval_data["inputs"]}
by_question = {i["question"]: i for i in eval_data["inputs"]}


def _row_inputs(trace) -> dict:
    root = next((s for s in trace.data.spans if s.parent_id is None), None)
    ins = root.inputs if root is not None else None
    if isinstance(ins, dict) and "question" in ins:
        return by_question.get(ins["question"], ins)
    text = str(trace.data.request)
    for prompt, row in by_prompt.items():
        if prompt in text:
            return row
    for q, row in by_question.items():
        if q in text:
            return row
    return {}


rows = []
for t in traces:
    inputs = _row_inputs(t)
    for a in t.info.assessments:
        if not isinstance(a, _Feedback):
            continue  # skip expectations
        v = a.value
        score = 1.0 if v in ("yes", True) else 0.0 if v in ("no", False) else None
        rows.append({
            "id": inputs.get("id"),
            "category": inputs.get("category"),
            "question": inputs.get("question"),
            "scorer": a.name,
            "value": v,
            "score": score,
            "rationale": a.rationale,
        })

scores = pd.DataFrame(rows, columns=["id", "category", "question", "scorer", "value", "score", "rationale"])
print(f"{len(traces)} traces, {len(scores)} assessments, {scores['id'].isna().sum()} unmatched")
summary = (scores.dropna(subset=["score"])
           .pivot_table(index="category", columns="scorer", values="score", aggfunc="mean")
           .round(2).reset_index())
display(summary)

# COMMAND ----------

# DBTITLE 1,Failed rows
failures = scores[scores["score"] == 0.0].sort_values(["category", "id", "scorer"])
print(f"{len(failures)} failing assessments across {failures['id'].nunique()} questions")
display(failures[["category", "id", "question", "scorer", "rationale"]])

# COMMAND ----------

# DBTITLE 1,Inspect tool spans in traces
# Debug view: which tools each row's trace called, and how tool_routing scored it.
for t in traces:
    root = next(s for s in t.data.spans if s.parent_id is None)
    resp = root.outputs
    while isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except Exception:
            break
    resp = resp if isinstance(resp, dict) else {"raw": str(resp)[:200]}
    print("Q:", str(t.data.request)[:100])
    print("  span types:", sorted({str(s.span_type) for s in t.data.spans}))
    print("  TOOL spans:", [s.name for s in t.search_spans(span_type=SpanType.TOOL)])
    print("  root span:", root.name, "| outputs.tool_calls:", resp.get("tool_calls", resp.get("raw")))
    print("  tool_routing:", [(a.name, a.value, a.rationale) for a in t.info.assessments if a.name == "tool_routing"])