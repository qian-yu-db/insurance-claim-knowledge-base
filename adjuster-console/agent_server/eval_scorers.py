"""Custom scorers for the V2 agent evaluation (see evaluate_agent.py).

Each returns None when it doesn't apply to a row (e.g. no expected numbers), so it isn't counted.
"""
from __future__ import annotations

import re

from mlflow.entities import Feedback, SpanType, Trace
from mlflow.genai.scorers import scorer

# $1,234.56 · 132500 · $132.5K · 1.2M — the optional suffix is a multiplier.
_NUMBER = re.compile(r"(?<![\w.])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?([kKmM])?(?![\w])")
_SCALE = {"k": 1e3, "m": 1e6}


def numbers_in(text: str) -> list[float]:
    values = []
    for whole, frac, suffix in _NUMBER.findall(text or ""):
        value = float(whole.replace(",", "") + (frac or ""))
        values.append(value * _SCALE.get(suffix.lower(), 1) if suffix else value)
    return values


def _matches(expected: float, found: list[float]) -> bool:
    # exact to the cent; a rounded "$132.5K" style value within 0.5%
    return any(abs(v - expected) < 0.01 or (expected and abs(v - expected) / expected < 0.005) for v in found)


def _tool_names(trace: Trace) -> set[str]:
    return {s.name for s in trace.search_spans(span_type=SpanType.TOOL)}


@scorer
def numbers_match(outputs, expectations) -> Feedback | None:
    """Every expected amount appears in the answer."""
    expected = (expectations or {}).get("expected_numbers")
    if not expected:
        return None
    found = numbers_in(str(outputs))
    missing = [n for n in expected if not _matches(float(n), found)]
    return Feedback(value=not missing,
                    rationale=f"missing {missing}" if missing else f"all of {expected} present")


@scorer
def tool_routing(expectations, trace: Trace) -> Feedback | None:
    """Every expected tool was called (other tools are allowed)."""
    expected = (expectations or {}).get("expected_tools")
    if not expected:
        return None
    called = _tool_names(trace)
    missing = [t for t in expected if not any(name.endswith(t) for name in called)]
    return Feedback(value=not missing, rationale=f"called {sorted(called)}; missing {missing}")


@scorer
def cites_sources(inputs, outputs, trace: Trace) -> Feedback | None:
    """Answers that used document search end with a Sources: line (the UI turns it into links)."""
    if "claims_document_search" not in _tool_names(trace):
        return None
    ok = "sources:" in str(outputs).lower()
    return Feedback(value=ok, rationale="has a Sources: line" if ok else "document search used, no Sources: line")


@scorer
def retrieved_expected_doc(expectations, trace: Trace) -> Feedback | None:
    """Document search returned the document the question was written from."""
    wanted = {c.get("doc_uri") for c in (expectations or {}).get("expected_retrieved_context") or []} - {None}
    if not wanted:
        return None
    got = set()
    for span in trace.search_spans(span_type=SpanType.RETRIEVER):
        for doc in span.outputs or []:
            got.add((doc.get("metadata") or {}).get("doc_uri"))
    hit = bool(wanted & got)
    return Feedback(value=hit, rationale=("retrieved " if hit else "not retrieved: ") + ", ".join(sorted(wanted)))


@scorer(aggregations=["mean", "median", "p90", "max"])
def latency_s(trace: Trace) -> float:
    """End-to-end turn time in seconds."""
    return round(trace.info.execution_duration / 1000, 2)
