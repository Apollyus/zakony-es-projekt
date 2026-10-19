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


def test_default_index_is_v2(monkeypatch):
    monkeypatch.delenv("ES_INDEX", raising=False)
    monkeypatch.delenv("ES_INDEX_SCHEMA", raising=False)
    import modules.ESZakony.main as m
    m = importlib.reload(m)
    assert m.INDEX_NAME == "zakony-v2" and m.SCHEMA == "v2" and m.DEFAULT_MODEL == "BAAI/bge-m3"


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
    # jediný dotaz: čisté kNN s filtrem na status, bez BM25 části
    bodies = [c.kwargs["body"] for c in es.search.call_args_list]
    assert len(bodies) == 1 and "knn" in bodies[0] and "query" not in bodies[0]
    assert bodies[0]["knn"]["k"] == 50 and bodies[0]["knn"]["filter"]          # pool kandidátů pro přeřazení


@pytest.mark.parametrize("ident,field", [("40/2009 Sb.", "akt_citace"), ("esel-esb:eli/cz/sb/2009/40", "id_zakona")])
def test_v2_law_detail_by_citation_or_iri(monkeypatch, ident, field):
    _, es, tools = load(monkeypatch, "zakony-v2")
    es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [v2_hit("1", "40/2009 Sb.", "§ 1", "", "t")]}}
    res = tools["get_law_detail"](ident)
    assert res["akt_citace"] == "40/2009 Sb." and res["paragrafy"][0]["text"] == "t"
    body = es.search.call_args.kwargs["body"]
    assert field in body["query"]["term"] and body["sort"][0]["poradi"]["order"] == "asc"


def test_v2_stale_laws_demoted_and_republications_dropped(monkeypatch):
    _, es, tools = load(monkeypatch, "zakony-v2")

    def hit(_id, citace, nazev, status, rok, score):
        return {"_id": _id, "_score": score, "_source": {
            "id_zakona": "x", "akt_citace": citace, "akt_nazev": nazev, "rok": rok, "status": status,
            "oznaceni": "§ 1", "nadpis": "", "cesta": "", "text": "t"}}

    es.search.return_value = {"hits": {"total": {"value": 4}, "hits": [
        hit("a", "42/1970 Sb.", "Zákoník práce", "neznamy", 1970, 0.90),           # starý, jen vyhlášené znění
        hit("b", "123/1920 Sb.", "Řád volení do poslanecké sněmovny", "platny", 1920, 0.89),
        hit("c", "70/1983 Sb.", "Občanský zákoník (úplné znění, jak vyplývá …)", "neznamy", 1983, 0.95),
        hit("d", "262/2006 Sb.", "Zákon zákoník práce", "platny", 2006, 0.87),
    ]}}
    res = tools["search_laws_semantic"]("dovolená", size=5)
    order = [law["akt_citace"] for law in res["results"]]
    assert order[0] == "262/2006 Sb."                          # platný předpis nahoře
    assert "70/1983 Sb." not in order                          # přetisk vyřazen
    assert res["results"][order.index("42/1970 Sb.")]["status"] == "neznamy"   # status je vidět
