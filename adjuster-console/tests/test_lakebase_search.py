from agent_server.lakebase_search import build_hybrid_sql


def test_no_claim_filter_param_order():
    q = "water damage"
    sql, params = build_hybrid_sql("[0.1,0.2]", None, top_k=10, query_text=q)
    assert params == (q, q, q)
    assert "FULL OUTER JOIN" in sql and "chunk_tsv <@> to_bm25query" in sql
    assert "to_tsvector('english', g.chunk_to_retrieve)" not in sql
    assert "LIMIT 30" in sql and sql.rstrip().endswith("LIMIT 10")


def test_claim_filter_adds_two_params():
    q = "water damage"
    sql, params = build_hybrid_sql("[0.1,0.2]", "CLM-1", top_k=5, query_text=q)
    assert params == ("CLM-1", q, q, "CLM-1", q)
    assert "g.claim_id = %s" in sql


def test_claim_filter_uses_exact_search():
    # The claim-scoped vector candidates come from a fenced subquery (exact scan), not the ANN index.
    sql, _ = build_hybrid_sql("[0.1,0.2]", "CLM-1", top_k=5, query_text="q")
    vector_cte = sql.split("text_cte")[0]
    assert "WHERE g.claim_id = %s OFFSET 0" in vector_cte


def test_search_failure_raises_unavailable(monkeypatch):
    import pytest
    from agent_server import lakebase_search as ls
    monkeypatch.setattr(type(ls.lb), "available", property(lambda self: True))
    monkeypatch.setattr(ls, "_embed", lambda text: (_ for _ in ()).throw(RuntimeError("429")))
    with pytest.raises(ls.SearchUnavailable):
        ls.search_documents("anything")


def test_pool_unavailable_raises(monkeypatch):
    import pytest
    from agent_server import lakebase_search as ls
    monkeypatch.setattr(type(ls.lb), "available", property(lambda self: False))
    with pytest.raises(ls.SearchUnavailable):
        ls.search_documents("anything")
