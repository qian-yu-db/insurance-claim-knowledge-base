"""Lakebase Postgres connection pool with OAuth token rotation (read-only queries)."""
from __future__ import annotations
import logging, os, threading, time
from contextlib import contextmanager
from typing import Any
import psycopg
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)
PROJECT_ID = os.environ.get("LAKEBASE_PROJECT_ID", "insurance-claim")
BRANCH_ID = os.environ.get("LAKEBASE_BRANCH_ID", "production")
ENDPOINT_ID = os.environ.get("LAKEBASE_ENDPOINT_ID", "primary")
DATABASE = os.environ.get("LAKEBASE_DATABASE", "databricks_postgres")
SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "claims")
_REFRESH_INTERVAL = 40 * 60

class LakebasePool:
    def __init__(self):
        self._w = WorkspaceClient()
        self._endpoint = ""; self._host = ""; self._user = ""; self._token = ""
        self._lock = threading.Lock(); self._initialized = False

    def _refresh_token(self) -> None:
        cred = self._w.postgres.generate_database_credential(endpoint=self._endpoint)
        with self._lock:
            self._token = cred.token

    def initialize(self) -> None:
        if self._initialized or not PROJECT_ID:
            if not PROJECT_ID:
                logger.warning("LAKEBASE_PROJECT_ID unset; using SQL warehouse fallback")
            return
        self._endpoint = f"projects/{PROJECT_ID}/branches/{BRANCH_ID}/endpoints/{ENDPOINT_ID}"
        self._host = self._w.postgres.get_endpoint(name=self._endpoint).status.hosts.host
        self._user = self._w.current_user.me().user_name
        self._refresh_token()
        def _loop():
            while True:
                time.sleep(_REFRESH_INTERVAL)
                try: self._refresh_token()
                except Exception: logger.exception("Lakebase token refresh failed")
        threading.Thread(target=_loop, daemon=True).start()
        self._initialized = True
        logger.info("Lakebase pool initialized (project=%s)", PROJECT_ID)

    @property
    def available(self) -> bool:
        return self._initialized

    @contextmanager
    def connection(self):
        with self._lock:
            token = self._token
        # scale-to-zero: compute wakes in ~100ms; retry once on connect failure
        try:
            conn = psycopg.connect(host=self._host, dbname=DATABASE, user=self._user,
                                   password=token, sslmode="require",
                                   options=f"-c search_path={SCHEMA},public")
        except psycopg.OperationalError:
            time.sleep(0.5)
            conn = psycopg.connect(host=self._host, dbname=DATABASE, user=self._user,
                                   password=token, sslmode="require",
                                   options=f"-c search_path={SCHEMA},public")
        try:
            yield conn
        finally:
            conn.close()

    def query(self, sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '30s'")
                cur.execute(sql, params)
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

pool = LakebasePool()
