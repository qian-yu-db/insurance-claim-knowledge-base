"""Conversation memory (level 2): OpenAI Agents SDK Sessions persisted in Lakebase.

One session per adjuster per claim conversation. Session ids are always prefixed with the
signed-in user, so a client can never open another user's conversation. Storage is the
`AsyncDatabricksSession` layout (agent_sessions + agent_messages, ON DELETE CASCADE) in the
`agent_memory` schema, which the app's service principal creates and owns on first use.
"""
from __future__ import annotations

import logging
import os
import re
import time

from databricks_openai.agents import AsyncDatabricksSession
from sqlalchemy import text

from agent_server.lakebase import BRANCH_ID, PROJECT_ID

logger = logging.getLogger(__name__)

MEMORY_SCHEMA = os.environ.get("LAKEBASE_MEMORY_SCHEMA", "agent_memory")
RETENTION_DAYS = 30
_PURGE_INTERVAL_S = 24 * 3600
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_last_purge = 0.0


def scoped_session_id(user: str | None, client_session_id: str | None) -> str | None:
    """`<user>:<client id>`, or None if the client id is missing or malformed."""
    if not client_session_id or not _CLIENT_ID_RE.match(client_session_id):
        return None
    return f"{user or 'local-dev'}:{client_session_id}"


def open_session(session_id: str) -> AsyncDatabricksSession:
    """Session bound to the Lakebase project the app already uses (engine is cached per process)."""
    return AsyncDatabricksSession(
        session_id, project=PROJECT_ID, branch=BRANCH_ID, schema=MEMORY_SCHEMA,
    )


async def load_display_history(session_id: str) -> list[dict]:
    """User/assistant text turns for re-rendering the chat; tool calls and outputs are skipped."""
    items = await open_session(session_id).get_items()
    turns: list[dict] = []
    for it in items:
        role = it.get("role")
        if role not in ("user", "assistant"):
            continue
        content = it.get("content")
        if isinstance(content, list):  # assistant output items: [{"type": "output_text", "text": ...}]
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if content:
            turns.append({"role": role, "content": content})
    return turns


PURGE_SQL = (
    f"DELETE FROM {MEMORY_SCHEMA}.agent_sessions "
    f"WHERE updated_at < now() - interval '{RETENTION_DAYS} days'"
)


async def purge_expired_if_due(session: AsyncDatabricksSession) -> None:
    """Delete conversations idle for RETENTION_DAYS (messages cascade). At most once per 24h."""
    global _last_purge
    if time.monotonic() - _last_purge < _PURGE_INTERVAL_S:
        return
    _last_purge = time.monotonic()
    try:
        await session.get_items(limit=1)  # make sure the schema and tables exist
        async with session._engine.begin() as conn:  # SQLAlchemySession keeps its engine here
            result = await conn.execute(text(PURGE_SQL))
        logger.info("Purged %s conversation(s) older than %s days", result.rowcount, RETENTION_DAYS)
    except Exception:
        logger.warning("Conversation retention purge failed", exc_info=True)
