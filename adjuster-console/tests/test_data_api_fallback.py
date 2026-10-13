import os
os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", "fevm-classic-stable")
import agent_server.data_api as da

class FakePool:
    def __init__(self, available, rows=None, raises=False):
        self.available = available; self._rows = rows or []; self._raises = raises
    def query(self, sql, params=None):
        if self._raises: raise RuntimeError("lb down")
        return self._rows

def test_uses_lakebase_when_available(monkeypatch):
    monkeypatch.setattr(da, "lb", FakePool(True, rows=[{"x": 1}]))
    monkeypatch.setattr(da, "_sql_warehouse", lambda *a, **k: [{"x": "WH"}])
    assert da._query("lb sql", "wh sql") == [{"x": 1}]

def test_falls_back_when_unavailable(monkeypatch):
    monkeypatch.setattr(da, "lb", FakePool(False))
    monkeypatch.setattr(da, "_sql_warehouse", lambda *a, **k: [{"x": "WH"}])
    assert da._query("lb sql", "wh sql") == [{"x": "WH"}]

def test_falls_back_on_lakebase_error(monkeypatch):
    monkeypatch.setattr(da, "lb", FakePool(True, raises=True))
    monkeypatch.setattr(da, "_sql_warehouse", lambda *a, **k: [{"x": "WH"}])
    assert da._query("lb sql", "wh sql") == [{"x": "WH"}]
