import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_server import memory


def test_session_id_is_scoped_to_user():
    assert memory.scoped_session_id("a@x.com", "abc12345") == "a@x.com:abc12345"
    assert memory.scoped_session_id(None, "abc12345") == "local-dev:abc12345"


@pytest.mark.parametrize("bad", [None, "", "short", "has space 123", "b@x.com:abc12345", "x" * 65])
def test_malformed_client_ids_rejected(bad):
    # A client can't smuggle in another user's prefix ("b@x.com:...") or junk.
    assert memory.scoped_session_id("a@x.com", bad) is None


def test_purge_sql_uses_retention_window():
    assert "agent_sessions" in memory.PURGE_SQL
    assert f"interval '{memory.RETENTION_DAYS} days'" in memory.PURGE_SQL
    assert memory.RETENTION_DAYS == 30


def test_display_history_keeps_user_and_assistant_text(monkeypatch):
    items = [
        {"role": "user", "content": "[Claim CLM-1] what happened?"},
        {"type": "function_call", "name": "claims_document_search", "arguments": "{}"},
        {"type": "function_call_output", "output": "[...]"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "A rear-end collision."}]},
    ]

    class _S:
        async def get_items(self, limit=None):
            return items
    monkeypatch.setattr(memory, "open_session", lambda sid: _S())
    turns = asyncio.run(memory.load_display_history("u:abc12345"))
    assert turns == [
        {"role": "user", "content": "[Claim CLM-1] what happened?"},
        {"role": "assistant", "content": "A rear-end collision."},
    ]


# --- /api/chat/stream: what the agent is run with -----------------------------------------------

class _Result:
    final_output = "a2"

    async def stream_events(self):
        return
        yield


def _stream(monkeypatch, open_session):
    """Post one turn to /api/chat/stream and capture Runner.run_streamed's input/session."""
    import agents
    from agent_server import agent as agent_mod
    from agent_server import data_api

    captured = {}

    def fake_run_streamed(agent, input, session=None):
        captured.update(input=input, session=session)
        return _Result()

    async def no_servers(stack, servers):
        return [], []

    monkeypatch.setattr(agents.Runner, "run_streamed", staticmethod(fake_run_streamed))
    monkeypatch.setattr(agent_mod, "connect_healthy_mcp_servers", no_servers)
    monkeypatch.setattr(agent_mod, "build_mcp_servers", lambda: [])
    monkeypatch.setattr(data_api.memory, "open_session", open_session)
    monkeypatch.setattr(data_api.memory, "purge_expired_if_due", lambda s: asyncio.sleep(0))

    from fastapi import FastAPI
    app = FastAPI(); app.include_router(data_api.router)
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
               {"role": "user", "content": "q2"}]
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", headers={"x-forwarded-email": "a@x.com"},
                   json={"input": history, "custom_inputs": {"session_id": "abc12345"}})
        assert r.status_code == 200 and '"done"' in r.text
    return captured


def test_with_session_only_newest_turn_is_sent(monkeypatch):
    opened = []

    class _S:
        def __init__(self, sid): opened.append(sid)
        async def get_items(self, limit=None): return []
    cap = _stream(monkeypatch, _S)
    assert opened == ["a@x.com:abc12345"]
    assert cap["session"] is not None
    assert [m["content"] for m in cap["input"]] == ["q2"]


def test_session_store_down_falls_back_to_client_history(monkeypatch):
    class _Down:
        def __init__(self, sid): pass
        async def get_items(self, limit=None): raise ConnectionError("lakebase unreachable")
    cap = _stream(monkeypatch, _Down)
    assert cap["session"] is None
    assert [m["content"] for m in cap["input"]] == ["q1", "a1", "q2"]
