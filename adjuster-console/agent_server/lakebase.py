"""Lakebase Postgres connection pool with OAuth token rotation (read-only queries)."""
from __future__ import annotations
import logging, os, threading, time
from typing import Any
import psycopg
from psycopg_pool import ConnectionPool
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)
PROJECT_ID = os.environ.get("LAKEBASE_PROJECT_ID", "insurance-claim")
BRANCH_ID = os.environ.get("LAKEBASE_BRANCH_ID", "production")
ENDPOINT_ID = os.environ.get("LAKEBASE_ENDPOINT_ID", "primary")
DATABASE = os.environ.get("LAKEBASE_DATABASE", "databricks_postgres")
SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "claims")
_REFRESH_INTERVAL = 40 * 60   # tokens expire after ~1 hour
_REFRESH_RETRY = 60           # retry soon after a failed refresh, before the token expires
_INIT_RETRY = 30              # min seconds between lazy init attempts after a failure
_CONN_MAX_LIFETIME = 45 * 60  # recycle connections well inside the token lifetime


class LakebasePool:
    def __init__(self):
        self._w = None
        self._endpoint = ""; self._host = ""; self._user = ""; self._token = ""
        self._lock = threading.Lock(); self._initialized = False
        self._last_init_attempt = 0.0
        self._pool: ConnectionPool | None = None

    def _refresh_token(self) -> None:
        cred = self._w.postgres.generate_database_credential(endpoint=self._endpoint)
        with self._lock:
            self._token = cred.token

    def _refresh_loop(self) -> None:
        delay = _REFRESH_INTERVAL
        while True:
            time.sleep(delay)
            try:
                self._refresh_token()
                delay = _REFRESH_INTERVAL
            except Exception:
                logger.exception("Lakebase token refresh failed; retrying in %ss", _REFRESH_RETRY)
                delay = _REFRESH_RETRY

    def current_token(self) -> str:
        with self._lock:
            return self._token

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._last_init_attempt = time.monotonic()
        if not PROJECT_ID:
            logger.warning("LAKEBASE_PROJECT_ID unset; using SQL warehouse fallback")
            return
        if self._w is None:
            self._w = WorkspaceClient()
        self._endpoint = f"projects/{PROJECT_ID}/branches/{BRANCH_ID}/endpoints/{ENDPOINT_ID}"
        self._host = self._w.postgres.get_endpoint(name=self._endpoint).status.hosts.host
        self._user = self._w.current_user.me().user_name
        self._refresh_token()
        cpool = ConnectionPool(
            connection_class=_TokenConnection, open=False,
            kwargs={"host": self._host, "dbname": DATABASE, "user": self._user, "sslmode": "require",
                    "options": f"-c search_path={SCHEMA},public -c statement_timeout=30s"},
            min_size=0, max_size=8, max_lifetime=_CONN_MAX_LIFETIME, max_idle=5 * 60,
            check=ConnectionPool.check_connection,  # drop connections broken by a compute restart
            configure=lambda conn: setattr(conn, "read_only", True),
        )
        cpool.open(wait=False)
        with self._lock:
            self._pool = cpool
            if not self._initialized:
                threading.Thread(target=self._refresh_loop, daemon=True).start()
            self._initialized = True
        logger.info("Lakebase pool initialized (project=%s)", PROJECT_ID)

    @property
    def available(self) -> bool:
        """True once initialized; retries a failed startup init lazily (rate-limited)."""
        if self._initialized:
            return True
        if time.monotonic() - self._last_init_attempt >= _INIT_RETRY:
            try:
                self.initialize()
            except Exception:
                logger.warning("Lakebase init retry failed", exc_info=True)
        return self._initialized

    def query(self, sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
        if not self.available:
            raise psycopg.OperationalError("Lakebase pool is not initialized")
        try:
            return self._run(sql, params)
        except psycopg.OperationalError:
            # scale-to-zero: compute wakes in ~100ms; retry once on a fresh connection
            time.sleep(0.5)
            return self._run(sql, params)

    def _run(self, sql: str, params: tuple | None) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]


class _TokenConnection(psycopg.Connection):
    """Injects the current OAuth token as the password each time the pool opens a connection."""
    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):
        kwargs["password"] = pool.current_token()
        return super().connect(conninfo, **kwargs)


pool = LakebasePool()
