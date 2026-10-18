"""Unit testy modulu ESZakony s mockovaným Elasticsearch (bez sítě a bez modelu)."""

import importlib
from unittest.mock import MagicMock

import numpy as np
import pytest


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def load(monkeypatch, index: str):
    monkeypatch.setenv("ES_INDEX", index)
    monkeypatch.delenv("ES_INDEX_SCHEMA", raising=False)
    import modules.ESZakony.main as m
    m = importlib.reload(m)
    es = MagicMock()
    emb = MagicMock()
    emb.encode.return_value = np.zeros(4)
    monkeypatch.setattr(m, "get_es_client", lambda: es)
    monkeypatch.setattr(m, "get_embedder", lambda: emb)
    mcp = FakeMCP()
    m.register(mcp)
    return m, es, mcp.tools


def frag(citace, text, score=1.0):
    return {"_source": {"citace": citace, "text": text}, "_score": score}


def test_schema_detection(monkeypatch):
    m, _, _ = load(monkeypatch, "zakony-esbirka-2026")
    assert m.SCHEMA == "v1"
    m, _, _ = load(monkeypatch, "zakony-v2")
    assert m.SCHEMA == "v2"
    assert m.DEFAULT_MODEL == "BAAI/bge-m3"


def test_v1_semantic_keeps_fragments_with_empty_citace(monkeypatch):
    _, es, tools = load(monkeypatch, "zakony-esbirka-2026")
    es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [{
        "_source": {"id_zakona": "z", "akt_citace": "40/2009 Sb.", "akt_nazev": "TZ", "rok": 2009},
        "inner_hits": {
            "knn_hits": {"hits": {"hits": [frag("", "(1) Kdo jiného úmyslně usmrtí"), frag("", "(2) s rozmyslem")]}},
            "text_hits": {"hits": {"hits": [frag("", "(1) Kdo jiného úmyslně usmrtí"), frag("§ 140", "§ 140")]}},
        },
    }]}}
    res = tools["search_laws_semantic"]("vražda", size=5)
    texts = [p["text"] for p in res["results"][0]["relevant_paragraphs"]]
    # dřív by z prázdných citací zbyl jen první fragment
    assert texts == ["(1) Kdo jiného úmyslně usmrtí", "(2) s rozmyslem", "§ 140"]


def v2_hit(_id, citace, oznaceni, nadpis, text):
    return {"_id": _id, "_score": 1.0, "_source": {
        "id_zakona": "esel-esb:eli/cz/sb/2009/40", "akt_citace": citace, "akt_nazev": "Zákon trestní zákoník",
        "rok": 2009, "oznaceni": oznaceni, "nadpis": nadpis, "cesta": "ČÁST DRUHÁ", "text": text}}


def test_rrf_prefers_docs_in_both_lists(monkeypatch):
    m, _, _ = load(monkeypatch, "zakony-v2")
    a, b, c = v2_hit("a", "x", "§ 1", "", ""), v2_hit("b", "x", "§ 2", "", ""), v2_hit("c", "x", "§ 3", "", "")
    fused = m._rrf([a, b], [c, b])
    assert fused[0]["_id"] == "b"
    assert {h["_id"] for h in fused} == {"a", "b", "c"}


def test_v2_semantic_groups_paragraphs_by_law(monkeypatch):
    _, es, tools = load(monkeypatch, "zakony-v2")
    hits = [v2_hit("1", "40/2009 Sb.", "§ 140", "Vražda", "(1) Kdo jiného úmyslně usmrtí"),
            v2_hit("2", "40/2009 Sb.", "§ 141", "Zabití", "(1) Kdo jiného úmyslně usmrtí v silném rozrušení")]
    es.search.return_value = {"hits": {"total": {"value": 2}, "hits": hits}}
    res = tools["search_laws_semantic"]("vražda", size=5)
    assert len(res["results"]) == 1
    law = res["results"][0]
    assert law["akt_citace"] == "40/2009 Sb."
    assert [p["citace"] for p in law["relevant_paragraphs"]] == ["§ 140 Vražda", "§ 141 Zabití"]
    # BM25 i kNN dotaz, oba s filtrem na status
    bodies = [c.kwargs["body"] for c in es.search.call_args_list]
    assert any("knn" in b for b in bodies) and any("query" in b for b in bodies)


@pytest.mark.parametrize("ident,field", [("40/2009 Sb.", "akt_citace"), ("esel-esb:eli/cz/sb/2009/40", "id_zakona")])
def test_v2_law_detail_by_citation_or_iri(monkeypatch, ident, field):
    _, es, tools = load(monkeypatch, "zakony-v2")
    es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [v2_hit("1", "40/2009 Sb.", "§ 1", "", "t")]}}
    res = tools["get_law_detail"](ident)
    assert res["akt_citace"] == "40/2009 Sb." and res["paragrafy"][0]["text"] == "t"
    body = es.search.call_args.kwargs["body"]
    assert field in body["query"]["term"] and body["sort"][0]["poradi"]["order"] == "asc"
