"""Testy nočního buildu v2 bez GPU, modelu i sítě (fake model, fake ES)."""

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_v2_index as B  # noqa: E402


# ── Embedder: OOM → půlení dávky → CPU ───────────────────────────────────────
class FakeModel:
    def __init__(self, max_ok: int):
        self.max_ok, self.calls = max_ok, []

    def encode(self, texts, batch_size=None, normalize_embeddings=True):
        self.calls.append(len(texts))
        if len(texts) > self.max_ok:
            raise RuntimeError("MPS backend out of memory (MPS allocated: 9 GiB)")
        return np.array([[len(t)] * 4 for t in texts], dtype=np.float32)


def make_embedder(max_ok):
    e = object.__new__(B.Embedder)
    e.device, e.torch, e.model, e.cpu_model = "cpu", None, FakeModel(max_ok), None
    return e


def test_oom_halves_batch_and_keeps_order():
    e = make_embedder(max_ok=2)
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    v = e.encode(texts)
    assert v.shape == (5, 4)
    assert list(v[:, 0]) == [1, 2, 3, 4, 5]      # pořadí zachováno
    assert e.model.calls[0] == 5 and max(c for c in e.model.calls if c <= 2) <= 2


def test_oom_single_text_falls_back_to_cpu(monkeypatch):
    e = make_embedder(max_ok=0)
    cpu = FakeModel(max_ok=10)
    import sentence_transformers
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", lambda *a, **k: cpu)
    v = e.encode(["xyz"])
    assert v.shape == (1, 4) and cpu.calls == [1]


def test_other_runtime_errors_are_not_swallowed():
    e = make_embedder(max_ok=10)
    e.model.encode = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("shape mismatch"))
    with pytest.raises(RuntimeError, match="shape mismatch"):
        e.encode(["a"])


def test_token_batches_respect_budget():
    rows = [(str(i), "{}", 1800, 0) for i in range(40)]   # dlouhé chunky -> ~512 tokenů
    batches = B.token_batches(rows, budget=6000)
    assert sum(len(b) for b in batches) == 40
    assert all(len(b) * 512 <= 6000 for b in batches)


# ── embed + push s fake ES ───────────────────────────────────────────────────
class FakeIndices:
    def __init__(self, es):
        self.es = es

    def exists(self, index):
        self.es.check()
        return self.es.created

    def create(self, index, body):
        self.es.created, self.es.mapping = True, body

    def put_settings(self, index, body):
        self.es.check()

    def refresh(self, index):
        pass

    def delete(self, index):
        self.es.created, self.es.docs = False, {}


class FakeES:
    def __init__(self):
        self.up, self.created, self.docs, self.mapping = True, False, {}, None
        self.indices = FakeIndices(self)

    def check(self):
        if not self.up:
            raise ConnectionError("VPN down")

    def options(self, **kw):
        return self

    def count(self, index):
        return {"count": len(self.docs)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "DB_PATH", tmp_path / "v2.db")
    monkeypatch.setattr(B, "PUSH_GRACE_S", 0)
    monkeypatch.setattr(B.ESPusher, "RETRY_AFTER", 0)
    es = FakeES()

    def fake_bulk(client, actions, **kw):
        es.check()
        for a in actions:
            es.docs[a["_id"]] = a["_source"]
        return len(actions), []

    import elasticsearch
    import elasticsearch.helpers
    monkeypatch.setattr(elasticsearch, "Elasticsearch", lambda *a, **k: es)
    monkeypatch.setattr(elasticsearch.helpers, "bulk", fake_bulk)

    class FakeEmb:
        def __init__(self, device):
            pass

        def encode(self, texts):
            return np.array([[len(t), 0.5, -0.25, 1 / 3] for t in texts], dtype=np.float32)

    monkeypatch.setattr(B, "Embedder", FakeEmb)
    con = B.db()
    docs = [{"akt_citace": "1/2000 Sb.", "text": f"text {i}", "search_text": f"hlavička | text {i}" + "x" * i}
            for i in range(30)]
    con.executemany("INSERT INTO units (id, akt_citace, priority, len, doc) VALUES (?,?,?,?,?)",
                    [(f"id{i}", "1/2000 Sb.", 0 if i < 20 else 8, len(d["search_text"]), json.dumps(d))
                     for i, d in enumerate(docs)])
    con.commit()
    return es, con


def counts(con):
    return con.execute("SELECT SUM(embedded), SUM(pushed), (SELECT COUNT(*) FROM vectors) FROM units").fetchone()


def test_embed_stores_vectors_locally_and_pushes(env):
    es, con = env
    B.stage_embed(max_priority=5, hours=1, device="cpu")
    assert counts(con) == (20, 20, 20)                     # priorita 8 (smlouvy) přeskočena
    assert len(es.docs) == 20
    assert es.mapping["mappings"]["_source"]["excludes"] == ["vektor", "search_text"]
    v = es.docs["id3"]["vektor"]
    assert len(v) == 4 and abs(v[3] - 1 / 3) < 1e-3          # fp16 záloha → float32 v ES


def test_es_outage_does_not_stop_embedding_then_push_catches_up(env):
    es, con = env
    es.up = False
    # při výpadku embed čeká na ES až do časového limitu běhu -> krátký limit
    B.stage_embed(max_priority=5, hours=0.0005, device="cpu")
    assert counts(con) == (20, 0, 20)                      # vše embedované lokálně, nic v ES
    assert es.docs == {}
    es.up = True
    B.stage_push(max_priority=5, hours=1, resend_all=False, recreate=False)
    assert counts(con)[1] == 20 and len(es.docs) == 20


def test_push_recreate_restores_index_without_reembedding(env, monkeypatch):
    es, con = env
    B.stage_embed(max_priority=5, hours=1, device="cpu")
    es.docs.clear()
    es.created = False                                      # index ztracen (smazán zvenku)
    monkeypatch.setattr(B, "Embedder", None)                # embedding nesmí být potřeba
    B.stage_push(max_priority=5, hours=1, resend_all=False, recreate=False)
    assert len(es.docs) == 20
    es.docs.clear()                                         # --recreate nad existujícím indexem
    B.stage_push(max_priority=5, hours=1, resend_all=False, recreate=True)
    assert len(es.docs) == 20


def test_backup_is_consistent_copy(env, tmp_path):
    es, con = env
    B.stage_embed(max_priority=5, hours=1, device="cpu")
    B.stage_backup(str(tmp_path / "zaloha"))
    (bk,) = list((tmp_path / "zaloha").glob("v2-*.db")) if (tmp_path / "zaloha").is_dir() else [tmp_path / "zaloha"]
    c2 = sqlite3.connect(bk)
    assert c2.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 20
    assert c2.execute("SELECT COUNT(*) FROM units WHERE embedded = 1").fetchone()[0] == 20
