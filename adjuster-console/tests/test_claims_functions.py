import os
os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", "fevm-classic-stable")
import json
from types import SimpleNamespace as NS

import agent_server.claims_functions as cf
from agent_server.data_api import _tool_label


def _resp(state="SUCCEEDED", rows=None, error=None, next_chunk=None):
    return NS(statement_id="s1", status=NS(state=NS(value=state), error=error),
              manifest=NS(schema=NS(columns=[NS(name="claim_id"), NS(name="total_paid")])),
              result=NS(data_array=rows, next_chunk_index=next_chunk))


class FakeStatements:
    def __init__(self, *responses):
        self.responses = list(responses); self.calls = []
    def execute_statement(self, **kw):
        self.calls.append(kw); return self.responses.pop(0)
    def get_statement(self, statement_id):
        return self.responses.pop(0)
    def get_statement_result_chunk_n(self, statement_id, chunk_index):
        return self.responses.pop(0)
    def cancel_execution(self, statement_id):
        self.cancelled = statement_id


def _patch(monkeypatch, fake):
    monkeypatch.setattr(cf, "_w", NS(statement_execution=fake))
    monkeypatch.setattr(cf, "WAREHOUSE_ID", "wh1")


def test_builds_parameterized_call(monkeypatch):
    fake = FakeStatements(_resp(rows=[["CLM-2026-04487", "12850.0"]]))
    _patch(monkeypatch, fake)
    out = json.loads(cf.call_function("top_claims", "total_paid", 3))
    call = fake.calls[0]
    assert call["statement"] == "SELECT * FROM fins_genai.claims_multimodal_kb.top_claims(:p0, :p1)"
    assert [(p.name, p.value, p.type) for p in call["parameters"]] == [
        ("p0", "total_paid", "STRING"), ("p1", "3", "INT")]  # values are bound, never interpolated
    assert out == {"columns": ["claim_id", "total_paid"], "rows": [["CLM-2026-04487", "12850.0"]]}


def test_waits_for_slow_statement(monkeypatch):
    monkeypatch.setattr(cf.time, "sleep", lambda s: None)
    _patch(monkeypatch, FakeStatements(_resp("PENDING"), _resp("RUNNING"), _resp(rows=[["x", "1"]])))
    assert json.loads(cf.call_function("claim_financials", "04487"))["rows"] == [["x", "1"]]


def test_sql_error_is_reported(monkeypatch):
    _patch(monkeypatch, FakeStatements(_resp("FAILED", error=NS(message="group_by must be one of: all"))))
    assert cf.call_function("payment_totals", "bogus") == "ERROR: group_by must be one of: all"


def test_unknown_claim_says_no_rows(monkeypatch):
    _patch(monkeypatch, FakeStatements(_resp(rows=None)))
    assert cf.call_function("claim_details", "CLM-2026-99999").startswith("No rows returned")


def test_unreachable_warehouse_is_reported(monkeypatch):
    class Down:
        def execute_statement(self, **kw):
            raise ConnectionError("warehouse down")
    _patch(monkeypatch, Down())
    assert cf.call_function("claim_notes", "04531", 20).startswith("ERROR: the structured claims data could not")


def test_reads_every_result_chunk(monkeypatch):
    first = _resp(rows=[["a", "1"]], next_chunk=1)
    _patch(monkeypatch, FakeStatements(first, NS(data_array=[["b", "2"]], next_chunk_index=None)))
    assert json.loads(cf.call_function("claim_notes", "04487", 500))["rows"] == [["a", "1"], ["b", "2"]]


def test_gives_up_and_cancels_when_too_slow(monkeypatch):
    clock = iter([0, 1000])  # start, then already past the deadline at the first check
    monkeypatch.setattr(cf.time, "monotonic", lambda: next(clock))
    fake = FakeStatements(_resp("PENDING"))
    _patch(monkeypatch, fake)
    out = cf.call_function("claim_financials", "04487")
    assert out.startswith("ERROR: the structured claims data took too long") and fake.cancelled == "s1"


def test_missing_warehouse_id_is_reported(monkeypatch):
    monkeypatch.setattr(cf, "WAREHOUSE_ID", "")
    assert "not configured" in cf.call_function("claim_details", "04487")


def test_step_labels():
    assert _tool_label("claim_financials") == "Looking up claim financials"
    assert _tool_label("claim_notes") == "Reading claim notes"
    assert _tool_label("claims_document_search") == "Searching claims documents"
