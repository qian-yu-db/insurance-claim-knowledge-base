import json
import os
os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", "fevm-classic-stable")


def _server_url(s) -> str:
    """Return the URL from an McpServer (stored in params dict, not as .url attr)."""
    params = getattr(s, "params", {}) or {}
    return params.get("url", "") if isinstance(params, dict) else getattr(params, "url", "")


def test_no_vector_search_mcp():
    from agent_server import agent
    urls = [_server_url(s) for s in agent.build_mcp_servers()]
    assert not any("vector-search" in u for u in urls)
    assert any("genie" in u for u in urls)


def test_doc_search_tool_registered_and_hybrid():
    from agent_server import agent
    a = agent.create_orchestrator_agent([], [])
    names = [getattr(t, "name", getattr(t, "__name__", "")) for t in a.tools]
    assert "claims_document_search" in names
    assert agent.VS_QUERY_TYPE == "HYBRID"


class _FakeRetriever:
    def __init__(self, docs=None, exc=None):
        self.docs, self.exc, self.calls = docs or [], exc, []

    def execute(self, query, filters=None):
        self.calls.append((query, filters))
        if self.exc:
            raise self.exc
        return self.docs


def test_claim_id_becomes_filter(monkeypatch):
    from agent_server import agent
    fake = _FakeRetriever(docs=[{"page_content": "raw", "metadata": {
        "chunk_id": "c1", "claim_id": "CLM-1", "source_uri": "dbfs:/x.pdf", "chunk_to_retrieve": "text"}}])
    monkeypatch.setattr(agent, "_get_retriever", lambda: fake)
    out = json.loads(agent.run_document_search("injuries", "CLM-1"))
    (_, filters), = fake.calls
    assert [(f.key, f.value) for f in filters] == [("claim_id", "CLM-1")]
    assert out[0]["source_uri"] == "dbfs:/x.pdf" and out[0]["content"] == "text"


def test_no_claim_id_means_no_filter(monkeypatch):
    from agent_server import agent
    fake = _FakeRetriever()
    monkeypatch.setattr(agent, "_get_retriever", lambda: fake)
    assert agent.run_document_search("anything") == "No matching documents found."
    assert fake.calls[0][1] is None


def test_failure_reports_unavailable(monkeypatch):
    from agent_server import agent
    monkeypatch.setattr(agent, "_get_retriever", lambda: _FakeRetriever(exc=RuntimeError("503")))
    out = agent.run_document_search("x")
    assert out.startswith("UNAVAILABLE") and "No matching documents" not in out


def test_functions_mcp_scoped_to_python_exec():
    # A schema-level URL (/mcp/functions/system/ai) exposes every system.ai function as a tool.
    from agent_server import agent
    fn_urls = [u for u in (_server_url(s) for s in agent.build_mcp_servers()) if "/mcp/functions/" in u]
    assert fn_urls and all(u.rstrip("/").endswith("/mcp/functions/system/ai/python_exec") for u in fn_urls)
