import os
os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", "fevm-classic-stable")
from types import SimpleNamespace as NS

from mlflow.entities import SpanType

from agent_server.eval_scorers import (cites_sources, numbers_in, numbers_match, retrieved_expected_doc,
                                       tool_routing)


class FakeTrace:
    def __init__(self, tools=(), retrieved=()):
        self._spans = [NS(name=t, span_type=SpanType.TOOL, outputs=None) for t in tools]
        self._spans.append(NS(name="hybrid_retriever", span_type=SpanType.RETRIEVER,
                              outputs=[{"metadata": {"doc_uri": u}} for u in retrieved]))
    def search_spans(self, span_type=None):
        return [s for s in self._spans if s.span_type == span_type]


def test_numbers_in_reads_currency_and_suffixes():
    assert numbers_in("Paid $12,850.00; pending $132.5K; range $120k-$140k; 1.2M") == \
        [12850.0, 132500.0, 120000.0, 140000.0, 1200000.0]


def test_numbers_match():
    assert numbers_match(outputs="paid $12,850 and $132,500 pending",
                         expectations={"expected_numbers": [12850, 132500]}).value is True
    fb = numbers_match(outputs="paid $12,850", expectations={"expected_numbers": [12850, 132500]})
    assert fb.value is False and "132500" in fb.rationale
    assert numbers_match(outputs="x", expectations={"expected_facts": ["f"]}) is None  # not applicable


def test_tool_routing_allows_extra_tools():
    trace = FakeTrace(tools=["claim_financials", "claim_notes", "claims_document_search"])
    assert tool_routing(expectations={"expected_tools": ["claim_notes"]}, trace=trace).value is True
    assert tool_routing(expectations={"expected_tools": ["payment_totals"]}, trace=trace).value is False


def test_cites_sources_only_after_document_search():
    assert cites_sources(inputs={}, outputs="Paid $350.", trace=FakeTrace(tools=["claim_payments"])) is None
    used = FakeTrace(tools=["claims_document_search"])
    assert cites_sources(inputs={}, outputs="...\nSources: a.pdf", trace=used).value is True
    assert cites_sources(inputs={}, outputs="no citation", trace=used).value is False


def test_retrieved_expected_doc():
    trace = FakeTrace(tools=["claims_document_search"], retrieved=["dbfs:/a.pdf", "claim_notes?claim_id=X"])
    hit = {"expected_retrieved_context": [{"doc_uri": "claim_notes?claim_id=X"}]}
    miss = {"expected_retrieved_context": [{"doc_uri": "dbfs:/b.pdf"}]}
    assert retrieved_expected_doc(expectations=hit, trace=trace).value is True
    assert retrieved_expected_doc(expectations=miss, trace=trace).value is False
    assert retrieved_expected_doc(expectations={}, trace=trace) is None
