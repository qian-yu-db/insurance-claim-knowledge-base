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
    assert hasattr(agent, "claims_document_search")
