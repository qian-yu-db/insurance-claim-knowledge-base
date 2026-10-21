# /// script
# requires-python = ">=3.10"
# dependencies = ["databricks-agents", "databricks-sdk>=0.40", "pandas"]
# ///
"""Generate candidate synthetic document questions for the V2 agent evaluation set.

Uses `databricks.agents.evals.generate_evals_df` over the KB's claim documents (silver_documents) and
claim notes (one document per claim). Writes candidates to a JSON file for review; the reviewed file is
what `build_eval_dataset.py` loads. Not part of the pipeline.

    uv run scripts/generate_synthetic_evals.py --profile <profile> --warehouse-id <id>
"""
import argparse
import json
import os
import re
from pathlib import Path

import pandas as pd
from databricks.agents.evals import generate_evals_df
from databricks.sdk import WorkspaceClient

OUT = Path(__file__).resolve().parents[1] / "sample_data/eval/eval_synthetic_candidates.json"
SCHEMA = "fins_genai.claims_multimodal_kb"
EVALS_PER_DOC = {"medical_record": 4}  # long documents; everything else gets 1 (claim notes: 3 per claim)

AGENT_DESCRIPTION = (
    "An assistant for insurance claims adjusters. It answers questions about specific auto claims from the "
    "claim file: documents, emails, call recordings, damage photos, payment data and adjuster notes. Each "
    "question is about one claim, identified by its claim number.")
QUESTION_GUIDELINES = """User persona: an experienced claims adjuster working the claim.
- Every question must name the claim number exactly as given in the document's first line (e.g. CLM-2026-04487).
- Ask about one concrete fact: an amount, date, party, injury, finding, decision or reason.
- Do not ask about file names, document formatting, email headers, phone numbers or addresses.
- Phrase questions the way an adjuster would type them in a chat box."""

parser = argparse.ArgumentParser()
parser.add_argument("--profile", required=True)
parser.add_argument("--warehouse-id", required=True)
args = parser.parse_args()
os.environ["DATABRICKS_CONFIG_PROFILE"] = args.profile  # generate_evals_df uses the default config
w = WorkspaceClient(profile=args.profile)


def query(sql: str) -> pd.DataFrame:
    r = w.statement_execution.execute_statement(warehouse_id=args.warehouse_id, statement=sql, wait_timeout="50s")
    if r.status.state.value != "SUCCEEDED":
        raise RuntimeError(r.status.error)
    rows = list(r.result.data_array or [])
    chunk = r.result.next_chunk_index
    while chunk is not None:
        part = w.statement_execution.get_statement_result_chunk_n(r.statement_id, chunk)
        rows.extend(part.data_array or [])
        chunk = part.next_chunk_index
    return pd.DataFrame(rows, columns=[c.name for c in r.manifest.schema.columns])


# One header line per document, so questions name the right claim (the pilot mixed claims up).
docs = query(f"""
SELECT concat('Claim: ', claim_id, ' · Document: ', coalesce(doc_type, 'other'), ' (', filename, ')\\n',
              text_content) AS content,
       source_uri AS doc_uri, claim_id, coalesce(doc_type, 'other') AS doc_type
FROM {SCHEMA}.silver_documents
WHERE corpus = 'claim' AND has_text AND length(text_content) > 200
UNION ALL
SELECT concat('Claim: ', claim_id, ' · Document: claim notes (adjuster activity log)\\n',
              array_join(sort_array(collect_list(chunk_to_retrieve)), '\\n\\n')),
       concat('claim_notes?claim_id=', claim_id), claim_id, 'claim_notes'  -- same id the search trace reports
FROM {SCHEMA}.gold_chunks WHERE doc_type = 'claim_notes' GROUP BY claim_id""")
print(f"{len(docs)} documents:", docs.groupby("doc_type").size().to_dict())

META_FACT = re.compile(r"^(the (answer|response)|the claim (referenced|number)|.*\bmust\b)", re.I)
candidates, dropped = [], 0
for _, d in docs.iterrows():
    n = 3 if d.doc_type == "claim_notes" else EVALS_PER_DOC.get(d.doc_type, 1)
    evals = generate_evals_df(pd.DataFrame([d[["content", "doc_uri"]]]), num_evals=n,
                              agent_description=AGENT_DESCRIPTION, question_guidelines=QUESTION_GUIDELINES)
    for _, e in evals.iterrows():
        question = e["inputs"]["messages"][-1]["content"]
        facts = [f for f in e["expectations"].get("expected_facts", []) if not META_FACT.match(f)]
        if d.claim_id[-5:] not in question or not facts:  # wrong / missing claim, or nothing checkable
            dropped += 1
            continue
        candidates.append({
            "inputs": {"question": question, "claim_id": d.claim_id},
            "expectations": {"expected_facts": facts,
                             "expected_retrieved_context": [{"doc_uri": d.doc_uri}]},
            "tags": {"category": "document", "source": "synthetic", "doc_type": d.doc_type},
        })

OUT.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
print(f"{len(candidates)} candidates written to {OUT} ({dropped} dropped by the automatic checks)")
