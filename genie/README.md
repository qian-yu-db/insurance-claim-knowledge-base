# Genie space — structured claims facts

A **Genie space** over the structured tables (`dim_claim`, `fact_payments`) for business users who want
to ask ad-hoc questions in the Genie UI. This folder makes it reproducible.

> **The adjuster console no longer uses it** (since v2.2.0). A Genie call took 15–20 s per question, so
> the app now answers structured questions with fixed UC SQL table functions (pipeline nb 09), about 1 s
> per call. The functions encode the same payment semantics as this space's instructions.

Genie spaces are configured in the workspace (not a declarative bundle resource), so this folder captures
the space as an exported, portable spec you re-import into your own workspace.

## Contents

| File | What it is |
|---|---|
| `genie_agent.json` | The exported space config (`serialized_space`): tables, column settings, instructions, example Q→SQL, join spec, SQL snippets. Editable, version-controlled. |
| `create_space.sh` | Recreates the space from `genie_agent.json` via the Genie CLI (with catalog/schema remap). |

## Prerequisites

- **Tables:** `dim_claim` and `fact_payments` — produced by
  [`pipeline/notebooks/05_structured_facts.py`](../pipeline/notebooks/05_structured_facts.py). Run the
  pipeline first.
- A **Pro or Serverless SQL warehouse** (Genie requires `CAN USE`).
- **Databricks Assistant** enabled in the workspace.
- Databricks CLI **and `jq`**.

## Option A — recreate from the exported config (fast)

```bash
cd genie
PROFILE=<your-cli-profile> WAREHOUSE_ID=<your-sql-warehouse-id> ./create_space.sh
# If your catalog/schema differ from the reference:
#   CATALOG=my_catalog SCHEMA=my_schema PROFILE=... WAREHOUSE_ID=... ./create_space.sh
```

The script remaps `fins_genai.claims_multimodal_kb` → your `CATALOG.SCHEMA`, creates the space, and prints
the new `space_id`. **Copy that `space_id`** into:
- `adjuster-console/databricks.yml` → `resources` → `genie_space` → `space_id`
- `adjuster-console/agent_server/agent.py` → `GENIE_SPACE_ID`

To update an existing space instead of creating one:
`databricks genie update-space <SPACE_ID> --json "{\"serialized_space\": $(jq -c '.' genie_agent.json | jq -Rs '.')}"`

## Option B — build it in the UI (to understand it)

Create a new Genie space on your warehouse and configure it to match `genie_agent.json`:

1. **Data** — add the two tables: `<catalog>.<schema>.dim_claim` and `…​.fact_payments`. Enable **entity
   matching** on the name-like columns (`insured_name`, `claimant_name`, `adjuster_name`, `carrier`,
   `peril`, `loss_location`).
2. **Join** — `fact_payments.claim_id = dim_claim.claim_id` (many-to-one, "Payments to claim details").
3. **General instructions** — paste the `text_instructions` content (currency = USD; `transaction_type`
   values `indemnity` / `expense` / `defense_expense`; `status` values `Paid` / `Negotiated — pending
   release`; meanings of `max_amount_seen`, `doc_count`, `distinct_doc_types`).
4. **Example questions (SQL)** — add the four Q→SQL pairs from `genie_agent.json`, e.g.:
   - "What is the total paid amount by carrier?"
   - "How are payments distributed across transaction types?"
   - "Which claims have the highest total paid amount?"
   - "What is the total paid amount by peril type?"
5. **SQL snippets** — add the trusted snippets from the config as reusable query building blocks.

## Keeping the config in sync

If you tune the space in the UI, re-export so the repo stays authoritative:

```bash
databricks genie get-space <SPACE_ID> --include-serialized-space -o json --profile <profile> \
  | jq '.serialized_space | fromjson' > genie/genie_agent.json
```

> Note: `genie_agent.json` references the reference workspace's `fins_genai.claims_multimodal_kb` and the
> IDs are from that deployment — remap the catalog/schema (and use your own warehouse) for your workspace.
