"""Testy nočního buildu v2 bez GPU, modelu i sítě (fake model, fake ES)."""

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_v2_index as B  # noqa: E402


# ── Embedder: pevné tvary dávek, jen GPU, restart při zaplnění paměti ─────────
class FakeTok:
    pad_token_id = 1

    def __call__(self, texts, truncation=True, max_length=512):
        return {"input_ids": [[0] * min(max_length, len(t)) for t in texts]}   # 1 znak = 1 token


class FakeModel:
    def __init__(self, oom=False):
        self.oom, self.shapes = oom, set()

    def encode(self, texts, batch_size=None, normalize_embeddings=True):
        if self.oom:
            raise RuntimeError("MPS backend out of memory (MPS allocated: 9 GiB)")
        longest = min(512, max(len(t) for t in texts))
        self.shapes.add((len(texts), min(512, max(64, -(-longest // 64) * 64))))
        return np.array([[len(t)] * 4 for t in texts], dtype=np.float32)


class FakeMPS:
    def __init__(self, used_gb=2.0):
        self.used = used_gb * 1e9

    def empty_cache(self):
        pass

    def driver_allocated_memory(self):
        return self.used


def make_embedder(oom=False, used_gb=2.0, limit_gb=12):
    e = object.__new__(B.Embedder)
    e.device, e.mem_limit, e.tok, e.model = "mps", limit_gb * 1e9, FakeTok(), FakeModel(oom)
    e.torch = type("T", (), {"mps": FakeMPS(used_gb)})()
    return e


def test_plan_uses_only_fixed_shapes_and_keeps_every_text_once():
    e = make_embedder()
    rng = np.random.RandomState(0)
    texts = ["x" * int(n) for n in rng.randint(5, 900, size=3000)]
    plan = e.plan(texts)
    assert sorted(i for _, idx in plan for i in idx) == list(range(3000))
    for bucket, idx in plan:
        assert bucket in (64, 128, 192, 256, 320, 384, 448, 512)
        assert len(idx) <= e.batch_size(bucket)
        assert all(e.bucket_of(min(512, len(texts[i]))) == bucket for i in idx)
    for bucket, idx in plan:
        v = e.encode([texts[i] for i in idx], bucket)
        assert v.shape == (len(idx), 4)                               # doplněk dávky se zahodí
        assert list(v[:, 0]) == [len(texts[i]) for i in idx]          # pořadí zachováno
    # MPS vidí jen 8 tvarů (velikost dávky × délkový koš), ne tisíce
    assert len(e.model.shapes) <= 8


def test_oom_raises_restart_instead_of_cpu():
    e = make_embedder(oom=True)
    with pytest.raises(B.GPURestart):
        e.encode(["abc"], 64)


def test_memory_limit_raises_restart():
    e = make_embedder(used_gb=13, limit_gb=12)
    with pytest.raises(B.GPURestart, match="limit"):
        e.encode(["abc"], 64)


def test_cpu_device_is_refused():
    with pytest.raises(SystemExit):
        B.Embedder("cpu", 12)


def test_other_runtime_errors_are_not_swallowed():
    e = make_embedder()
    e.model.encode = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("shape mismatch"))
    with pytest.raises(RuntimeError, match="shape mismatch"):
        e.encode(["a"], 64)


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
        def __init__(self, device, mem_limit_gb):
            self.calls = 0

        def plan(self, texts):
            return [(64, list(range(i, min(i + 7, len(texts))))) for i in range(0, len(texts), 7)]

        def encode(self, texts, bucket):
            self.calls += 1
            if FakeEmb.restart_after and self.calls > FakeEmb.restart_after:
                raise B.GPURestart("paměť GPU 13.0 GB > limit 12.0 GB")
            return np.array([[len(t), 0.5, -0.25, 1 / 3] for t in texts], dtype=np.float32)

        def gpu_mem_gb(self):
            return 2.0

    FakeEmb.restart_after = 0
    env_emb = FakeEmb

    monkeypatch.setattr(B, "Embedder", FakeEmb)
    monkeypatch.setattr(B, "_stop", False)
    con = B.db()
    docs = [{"akt_citace": "1/2000 Sb.", "text": f"text {i}", "search_text": f"hlavička | text {i}" + "x" * i}
            for i in range(30)]
    con.executemany("INSERT INTO units (id, akt_citace, priority, len, doc) VALUES (?,?,?,?,?)",
                    [(f"id{i}", "1/2000 Sb.", 0 if i < 20 else 8, len(d["search_text"]), json.dumps(d))
                     for i, d in enumerate(docs)])
    con.commit()
    es.emb = env_emb
    return es, con


def counts(con):
    return con.execute("SELECT SUM(embedded), SUM(pushed), (SELECT COUNT(*) FROM vectors) FROM units").fetchone()


def test_embed_stores_vectors_locally_and_pushes(env):
    es, con = env
    B.stage_embed(max_priority=5, hours=1, device="mps", mem_limit_gb=12)
    assert counts(con) == (20, 20, 20)                     # priorita 8 (smlouvy) přeskočena
    assert len(es.docs) == 20
    assert es.mapping["mappings"]["_source"]["excludes"] == ["vektor", "search_text"]
    v = es.docs["id3"]["vektor"]
    assert len(v) == 4 and abs(v[3] - 1 / 3) < 1e-3          # fp16 záloha → float32 v ES


def test_es_outage_does_not_stop_embedding_then_push_catches_up(env):
    es, con = env
    es.up = False
    # při výpadku embed čeká na ES až do časového limitu běhu -> krátký limit
    B.stage_embed(max_priority=5, hours=0.0005, device="mps", mem_limit_gb=12)
    assert counts(con) == (20, 0, 20)                      # vše embedované lokálně, nic v ES
    assert es.docs == {}
    es.up = True
    B.stage_push(max_priority=5, hours=1, resend_all=False, recreate=False)
    assert counts(con)[1] == 20 and len(es.docs) == 20


def test_push_recreate_restores_index_without_reembedding(env, monkeypatch):
    es, con = env
    B.stage_embed(max_priority=5, hours=1, device="mps", mem_limit_gb=12)
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
    B.stage_embed(max_priority=5, hours=1, device="mps", mem_limit_gb=12)
    B.stage_backup(str(tmp_path / "zaloha"))
    (bk,) = list((tmp_path / "zaloha").glob("v2-*.db")) if (tmp_path / "zaloha").is_dir() else [tmp_path / "zaloha"]
    c2 = sqlite3.connect(bk)
    assert c2.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 20
    assert c2.execute("SELECT COUNT(*) FROM units WHERE embedded = 1").fetchone()[0] == 20


def test_gpu_restart_exits_75_and_next_run_resumes(env):
    es, con = env
    es.emb.restart_after = 1                                 # po 1. dávce "dojde paměť GPU"
    assert B.stage_embed(max_priority=5, hours=1, device="mps", mem_limit_gb=12) == B.EXIT_RESTART
    first = counts(con)
    assert first[0] == 7 and first[2] == 7                   # 1. dávka uložená, nic se neztratilo
    es.emb.restart_after = 0
    assert B.stage_embed(max_priority=5, hours=1, device="mps", mem_limit_gb=12) == 0
    assert counts(con) == (20, 20, 20)


def test_time_limit_returns_incomplete(env):
    es, con = env
    assert B.stage_embed(max_priority=5, hours=0, device="mps", mem_limit_gb=12) == B.EXIT_INCOMPLETE
