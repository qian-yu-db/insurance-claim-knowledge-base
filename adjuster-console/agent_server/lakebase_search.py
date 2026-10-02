"""Lakebase hybrid search: lakebase_ann (cosine) + lakebase_bm25, fused with RRF (k=60)."""
from __future__ import annotations
import logging, os
from typing import Any
import mlflow, mlflow.deployments
from mlflow.entities import Document
from agent_server.lakebase import pool as lb

logger = logging.getLogger(__name__)
EMBEDDING_ENDPOINT = os.environ.get("EMBEDDING_ENDPOINT", "databricks-gte-large-en")
SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "claims")
TABLE = "gold_chunks_embeddings"
BM25_INDEX = f"{SCHEMA}.idx_chunks_bm25"
RRF_K = 60
_deploy = None


def _embed(text: str) -> list[float]:
    global _deploy
    if _deploy is None:
        _deploy = mlflow.deployments.get_deploy_client("databricks")
    return _deploy.predict(endpoint=EMBEDDING_ENDPOINT, inputs={"input": [text]}).data[0]["embedding"]


def build_hybrid_sql(emb_str: str, claim_id: str | None, top_k: int, query_text: str = "") -> tuple[str, tuple]:
    candidate_k = top_k * 3
    bm25 = f"g.chunk_tsv <@> to_bm25query(to_tsvector('english', %s), '{BM25_INDEX}'::regclass)"
    if claim_id:
        vector_where = "WHERE g.claim_id = %s"
        text_and_claim = "AND g.claim_id = %s"
        params: tuple = (claim_id, query_text, query_text, claim_id, query_text)
    else:
        vector_where = ""
        text_and_claim = ""
        params = (query_text, query_text, query_text)
    sql = f"""
    WITH vector_cte AS (
        SELECT g.chunk_id, ROW_NUMBER() OVER (ORDER BY g.embedding <=> '{emb_str}'::vector ASC) AS vrank
        FROM {SCHEMA}.{TABLE} g {vector_where}
        ORDER BY g.embedding <=> '{emb_str}'::vector ASC LIMIT {candidate_k}
    ),
    text_cte AS (
        SELECT g.chunk_id, ROW_NUMBER() OVER (ORDER BY ({bm25}) ASC) AS trank
        FROM {SCHEMA}.{TABLE} g
        WHERE ({bm25}) < 0 {text_and_claim}
        ORDER BY ({bm25}) ASC LIMIT {candidate_k}
    ),
    rrf AS (
        SELECT COALESCE(v.chunk_id, t.chunk_id) AS chunk_id,
               COALESCE(1.0/({RRF_K}.0+v.vrank),0.0) + COALESCE(1.0/({RRF_K}.0+t.trank),0.0) AS rrf_score
        FROM vector_cte v FULL OUTER JOIN text_cte t ON v.chunk_id = t.chunk_id
    )
    SELECT g.chunk_id, g.doc_id, g.claim_id, g.doc_type, g.corpus, g.carrier, g.peril,
           g.chunk_to_retrieve, b.filename, b.source_uri, rrf.rrf_score
    FROM rrf JOIN {SCHEMA}.{TABLE} g ON rrf.chunk_id = g.chunk_id
    LEFT JOIN {SCHEMA}.bronze_file_registry b ON g.doc_id = b.doc_id
    ORDER BY rrf.rrf_score DESC LIMIT {top_k}"""
    return sql, params


def search_documents(query: str, claim_id: str | None = None, top_k: int = 10) -> list[dict]:
    if not lb.available:
        logger.warning("Lakebase unavailable for hybrid search")
        return []
    emb_str = "[" + ",".join(f"{v:.8f}" for v in _embed(query)) + "]"
    sql, params = build_hybrid_sql(emb_str, claim_id, top_k, query_text=query)
    try:
        with mlflow.start_span(name="hybrid_retriever", span_type="RETRIEVER") as span:
            span.set_inputs({"query": query, "claim_id": claim_id, "top_k": top_k})
            rows = lb.query(sql, params)
            results = [{
                "chunk_id": r.get("chunk_id"), "doc_id": r.get("doc_id"), "claim_id": r.get("claim_id"),
                "doc_type": r.get("doc_type"), "corpus": r.get("corpus"), "carrier": r.get("carrier"),
                "peril": r.get("peril"), "filename": r.get("filename"), "source_uri": r.get("source_uri"),
                "content": r.get("chunk_to_retrieve"), "relevance_score": float(r.get("rrf_score", 0.0)),
            } for r in rows]
            span.set_outputs([Document(id=r["chunk_id"], page_content=r["content"] or "",
                              metadata={k: v for k, v in r.items() if k not in ("content", "chunk_id")})
                              for r in results])
        return results
    except Exception as e:
        logger.error("Lakebase hybrid search failed: %s", e)
        return []
