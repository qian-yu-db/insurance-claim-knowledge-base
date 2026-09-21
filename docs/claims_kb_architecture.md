# Multi-Modal Claims Knowledge Base — Default Architecture

**Purpose:** batch-process a claim's mixed artifacts (email, PDF, Word, images, audio, video,
long-tail) into a searchable knowledge base an adjuster can query, plus browse the source documents.

This document describes the **minimum-complexity default** — the simplest design that still handles
multi-modal data correctly. Splits and expansions (multiple indexes, visual search, etc.) are
deliberately *out* of the default and listed at the end as options to add when a business driver
justifies them.

---

## Guiding principle

> **Normalize every modality to one text representation, unify into one index, and separate only
> when the embedding space or a governance boundary forces it.**

A vector index holds a single embedding space. So the design question for each modality is never
"does it need its own index?" but "**what text do I embed, and is it the same space as everything
else?**" Reduce PDFs, scans, emails, speech, and photos to text (OCR, transcript, caption,
extracted fields) and they all live in one index, scoped by metadata filters.

---

## Default architecture

```mermaid
flowchart TD
    subgraph LAND["① Landing (UC Volume)"]
      V["Raw claim artifacts<br/>pdf · docx · eml · images · audio · video · html/xml/csv/rtf"]
    end

    V --> BR["② Bronze — file registry<br/>doc_id · claim_id · source_uri · modality · route<br/><i>(lean: metadata only, no inlined bytes)</i>"]

    BR --> RT{"③ Modality router<br/>(by content type)"}

    RT -->|"pdf, docx, scanned images"| P1["ai_parse_document<br/>(text + OCR)"]
    RT -->|"eml, html, xml, csv, rtf"| P2["decode / strip<br/>+ LLM clean email body"]
    RT -->|"audio, video"| P3["ai_query → transcription<br/>(Whisper endpoint)*"]
    RT -->|"damage photos"| P4["ai_query → vision caption<br/>(multimodal chat model)*"]

    P1 & P2 & P3 & P4 --> SILVER["④ Unified content layer (Silver)<br/><b>one row per artifact, always text</b><br/>ai_classify → doc_type · ai_extract → claim#/policy#/parties/dates<br/>ai_mask → PII · confidence · provenance (uri, page, timestamp)"]

    SILVER --> CH["⑤ Chunk<br/>ai_prep_search (parsed) + 1-chunk (short text)"]
    CH --> IDX["⑥ ONE Vector Search index<br/>embed chunk_to_embed · filters: claim_id, doc_type, modality, corpus"]

    SILVER --> FACTS["Structured facts → Delta tables<br/>(ledgers, ACORD fields) for SQL / Genie"]

    IDX --> SERVE["⑦ Serving (later): retrieval + agent/UI<br/>claim-scoped Q&A + document browse"]
    FACTS --> SERVE

    classDef defer stroke-dasharray:5 5;
    class P3,P4 defer;
```

\* Dashed = requires an endpoint not yet stood up (transcription / vision). Everything else runs
today on a serverless SQL warehouse with built-in AI Functions.

---

## Why one index is the default

The adjuster's primary question is **"tell me about claim X"** — which spans doc types and
modalities. A single index with a `claim_id` filter answers it in one call and returns the FNOL,
the medical records, the emails, and the photo captions together. Sharding by topic or doc type
would fragment each claim across indexes and force query fan-out for the most common request.
Topic/doc-type scoping is a **metadata filter**, not a separate index.

## Modality handling in the default

| Modality | Representation for the index | Same text index? |
|---|---|---|
| PDF, DOCX | Parsed text | ✅ |
| Scanned document images (PNG/TIFF) | OCR text | ✅ |
| Email (.eml) | LLM-cleaned body text + headers | ✅ |
| HTML / XML / CSV / RTF | Decoded / stripped text | ✅ |
| Audio / video | Transcript (+ timestamps, diarization) | ✅ (as text) |
| Damage photos | Vision caption / description | ✅ (as text) |
| Structured (XLSX, ACORD fields, ledgers) | **Not embedded** — Delta table for SQL/Genie | ➖ side channel |

## Provenance & governance (built into Silver)

Every chunk keeps `source_uri`, `modality`, `page`/`timestamp`/`bbox`, `confidence`, and
`pii_masked` so answers cite the original artifact, the UI can deep-link (PDF page, audio position,
photo region), low-confidence transcripts/captions can be flagged for review, and PII is masked
*before* indexing.

---

## When to expand beyond the default

Add a **second index** only when one of these is true (otherwise use a filter):

1. **Different embedding space** — image-vector search on photos, multilingual, or domain-specialized embeddings.
2. **Native non-text similarity** — "find claims with visually similar damage."
3. **Governance / access / tenant / data-residency** boundary that row filters can't satisfy.
4. **Update cadence / pipeline isolation** — static reference corpus (SOW, guidelines, policies) vs. streaming claims.
5. **Scale / noisy-neighbor isolation** at large volume.

The most likely first expansions for this use case: a **reference-corpus** split (#4) and an
**image-vector index** for visual damage search (#1/#2) — both driven by customer requirements,
not by the technology. See the companion stakeholder document for the open questions.
