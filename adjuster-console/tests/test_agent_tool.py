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


def test_doc_search_tool_registered():
    from agent_server import agent
    a = agent.create_orchestrator_agent([], [])
    names = [getattr(t, "name", getattr(t, "__name__", "")) for t in a.tools]
    assert "claims_document_search" in names


def test_tool_reports_unavailable_not_empty(monkeypatch):
    from agent_server import agent
    from agent_server.lakebase_search import SearchUnavailable

    def boom(**kwargs):
        raise SearchUnavailable("down")
    monkeypatch.setattr(agent, "search_documents", boom)
    out = agent.run_document_search("x")
    assert out.startswith("UNAVAILABLE") and "No matching documents" not in out


def test_functions_mcp_scoped_to_python_exec():
    # A schema-level URL (/mcp/functions/system/ai) exposes every system.ai function as a tool.
    # The expected scope is pinned as a literal on purpose: that is the requirement under test.
    from agent_server import agent
    fn_urls = [u for u in (_server_url(s) for s in agent.build_mcp_servers()) if "/mcp/functions/" in u]
    assert len(fn_urls) == 1
    assert fn_urls[0].startswith("https://")  # host resolved (build_mcp_url returns "None/..." otherwise)
    assert fn_urls[0].endswith("/api/2.0/mcp/functions/system/ai/python_exec")


def test_orchestrator_uses_unity_gateway():
    from agents.models import _openai_shared
    from agent_server import agent
    a = agent.create_orchestrator_agent([], [])
    assert a.model == "system.ai.gpt-5-2"
    client = _openai_shared.get_default_openai_client()
    assert str(client.base_url).rstrip("/").endswith("/ai-gateway/mlflow/v1")
