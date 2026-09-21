# Design Options & Scaling Decisions

The engineering companion to the architecture. This repo ships a **minimum-complexity default**; this
doc lays out the decisions you make when you expand it — index structure, scaling as data grows, adding
categories/modalities, and where structured data belongs. Each section gives the **default**, **when to
change it**, and the **trade-off**.

Related: [`claims_kb_architecture.md`](claims_kb_architecture.md) (diagram + design) ·
[`data_residency.md`](data_residency.md) (where data lives + Vector Search custody).

---

## 0. The guiding principle

> Reduce every file type to text, put it all in **one** Vector Search index, and separate into multiple
> indexes only when there is a clear business or technical reason.

A search index works on one *meaning representation* (text embeddings). So instead of an index per file
type, everything is normalized to text (parse / OCR / transcribe / caption), chunked, tagged with metadata,
and embedded into one index. Categories like document type or line of business are **filters**, not
separate indexes.

The current repo implements exactly this: `gold_chunks` → one Delta Sync index (`gold_chunks_vs_index`),
with structured facts (`dim_claim`, `fact_payments`) kept in Delta for Genie.

---

## 1. Index structure — one index vs. many

| Option | When to choose | Default | Trade-off |
|---|---|---|---|
| **Single unified index + metadata filters** | Same language, same retrieval semantics, one governance boundary | ✅ **Default** | Simplest to build/operate; all separation via filters |
| **Split: claims vs. reference material** | Reference (SOW, guidelines, policies) changes on a very different cadence and is shared across claims | Likely first expansion | Two sync pipelines; must union results at query time |
| **Split by line of business** | Auto vs. property vs. liability have genuinely different vocabulary/retrieval quality | Only if quality demands it | Duplicate ops; cross-LOB queries need fan-out |
| **Split by tenant / region** | Data isolation or residency that filtering cannot satisfy | Only if required | Per-tenant lifecycle; see [data_residency.md](data_residency.md) |
| **Split by sensitivity** | Access rules that row/column filters + UC grants cannot express | Only if compliance requires | Access-routing complexity |

**Categories ≠ indexes.** Document type, peril, carrier, loss year are handled by **filters** on one index
(see §4), not by new indexes. Add an index only when one of these is true:

1. A different **meaning representation** is needed — visual similarity search over images, a different
   language, or a specialized-domain embedding model.
2. A **governance / access / tenant / residency** boundary that simple filtering can't satisfy.
3. A very different **update frequency** (rarely-changing reference vs. constantly-arriving claims) where
   co-syncing is wasteful.

---

## 2. When the data size gets large

As claim volume grows, the decisions shift from "how many indexes" to "how do we size and feed one index."

### 2a. Do the vector-count math first

```
vectors ≈ documents × avg_chunks_per_document
```

Chunk size is the biggest lever. `ai_prep_search` semantic chunking typically yields a handful to a few
dozen chunks per document. Estimate before choosing an endpoint — e.g. 1M documents × 20 chunks ≈ 20M
vectors; 50M documents × 20 ≈ 1B vectors.

### 2b. Pick the endpoint tier for the scale

Databricks Vector Search endpoints (approximate; confirm current limits with your account team):

| Endpoint | Latency | Capacity (≈768-dim) | Relative cost | Use when |
|---|---|---|---|---|
| **Standard** | ~20–50 ms | ~320M vectors | higher | Real-time, low-latency retrieval |
| **Storage-Optimized** | ~300–500 ms | ~1B+ vectors | ~7× lower | Large corpora, cost-sensitive, latency-tolerant |

Rules of thumb:
- **< ~100M vectors and latency-sensitive** → Standard.
- **Hundreds of millions to billions, or cost-driven** → Storage-Optimized (also ~10–20× faster indexing).
- Note the **filter-syntax difference**: Standard uses dict-format `filters_json`; Storage-Optimized uses
  SQL-like `filters` strings. If you may migrate, keep filtering behind a thin helper.

### 2c. Control the vector count (before scaling the endpoint)

- **Chunk deliberately.** Larger, semantically-coherent chunks = fewer vectors + more context per hit;
  too-large hurts precision. Tune `ai_prep_search` rather than accepting defaults blindly.
- **Index only what's retrieved.** Keep `columns_to_sync` to the fields you actually return/filter on.
- **Don't index what isn't searched.** Ledgers/metrics stay in Delta for Genie (§5), not in the index.
- **Prune / TTL.** Closed or aged-out claims can be filtered, moved to a cold index, or dropped.

### 2d. Feed it incrementally

- Use **`TRIGGERED`** sync for periodic batch refresh (cheaper); **`CONTINUOUS`** only when near-real-time
  freshness is required.
- Make ingestion incremental (Auto Loader / streaming read of the landing volume + Change Data Feed on the
  Delta source) so you re-embed only new/changed rows — re-embedding the whole corpus is the usual cost trap.
- Embedding compute dominates cost at scale; batch it and avoid recomputing unchanged rows.

### 2e. Latency & cost budget

Retrieval latency = embed-the-query + vector search + (optional) reranking. If you add hybrid search or a
reranker, budget for it. At large scale, cost is driven by **embedding throughput** and **endpoint tier**,
not query volume — optimize those two first.

---

## 3. When there are more categories

"More categories" (new document types, perils, lines of business, sources) almost always means **more
metadata + filters**, not more indexes.

| Situation | Do this | New index? |
|---|---|---|
| New document type / peril / carrier / source | Add/populate a metadata column; filter on it | No |
| Users cite exact terms (claim #, policy #, codes, statute refs) | Turn on **hybrid search** (vector + BM25 keyword) | No |
| A category needs a range/inequality filter (e.g. loss year, amount) | Use a typed column (INT/DATE) and range filters | No |
| A category has genuinely different vocabulary and retrieval is poor | Consider a domain embedding model or a split index | Maybe |
| A category is another language | Multilingual embedding model, or a per-language index | Maybe |

The current pipeline already tags chunks with filterable metadata — reuse and extend these instead of
splitting: `claim_id`, `corpus`, `doc_type`, `modality`, `content_source`, `carrier`, `peril`,
`loss_year` (INT, for range filters), `source_uri`, `doc_id`. Adding a category = adding/curating one of
these columns.

---

## 4. Metadata & filtering strategy

Metadata is what lets one index behave like many. Design it up front:

- **Scoping filters** — `claim_id` (the "everything about claim X" query), `doc_type`, `modality`,
  `content_source` (parsed vs OCR vs caption vs transcript — a trust signal).
- **Range filters** — keep dates/years/amounts as typed columns (e.g. `loss_year INT`) so you can do
  `>= / <=`, not just equality.
- **Hybrid search** — combine embeddings with BM25 keyword scoring when queries carry exact identifiers
  (claim/policy numbers, error/statute codes). Available on Delta Sync and Direct Access indexes.
- **Only synced columns are queryable** — include every field you filter on or return in `columns_to_sync`.

---

## 5. Structured data does not belong in the index

Spreadsheets, payment ledgers, and form fields go into **Delta tables** (`dim_claim`, `fact_payments`)
and are answered by a **Genie space**, not the vector index. Semantic search over a payment ledger is
weak; exact aggregation ("total paid on claim X", "sum of reserves by peril") is a SQL job. The agent
routes: documents → Vector Search, facts/aggregation → Genie, ad-hoc math → `python_exec`.

Add structured facts to the index only when you need them as *retrievable narrative context*, and even
then prefer a generated text summary row over raw numbers.

---

## 6. Adding modalities

| Modality | How it enters the KB | Same text index? |
|---|---|---|
| Audio (calls, statements) | Transcribe (`ai_transcribe` — diarization + timestamps; or `ai_query` + Whisper) | ✅ Yes, as text |
| Video | Transcribe audio (+ optionally describe key frames) | ✅ Yes, as text |
| Photos — *ask about them* | Generate a written caption/description | ✅ Yes, as text |
| Photos — *find visually similar damage* | Image-embedding similarity search | ❌ No — needs a dedicated image index |
| Spreadsheets / form fields | Load to Delta for exact queries (§5) | ❌ No — reporting, not search |

The dividing line is the *meaning representation*: anything reducible to meaningful text joins the one
index; **visual similarity** is the main case that genuinely warrants a second (image) index.

---

## 7. Governance, residency, multi-tenant

- **Access control** — prefer Unity Catalog grants + metadata filters over separate indexes; split only
  when access rules can't be expressed as filters/grants.
- **Residency** — the served Vector Search index is Databricks-managed; the source vectors can stay in the
  customer's Delta table (self-managed embeddings), or use an external store for full custody. See
  [data_residency.md](data_residency.md).
- **Privacy** — apply PII masking (e.g. `ai_mask`) to transcripts and captions *before* indexing; define
  retention on both the source tables and the index.

---

## 8. Decision cheat-sheet

- **Default:** one index, everything-to-text, categories via metadata filters, structured facts in Delta.
- **Split the index** only for: a different meaning representation, a governance/residency boundary, or a
  very different update cadence.
- **Growing large?** Size the vector count (chunks × docs), pick Standard vs Storage-Optimized, sync
  incrementally, and control chunking before adding indexes.
- **More categories?** Add metadata + filters (and hybrid search for exact terms) — not new indexes.
- **Visual similarity or another language?** That's the real trigger for a second index.

> Vector Search limits, endpoint tiers, and pricing change over time — validate specifics with the
> Databricks account team / official docs before committing to a capacity or cost plan.
