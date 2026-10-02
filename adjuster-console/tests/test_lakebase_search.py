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
