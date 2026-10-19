"""
Retriever rozhraní pro RAGAS evaluaci: volá reálné MCP nástroje
a vlastní kNN přímo přes Elasticsearch client.
"""

import os
import re
import sys
from pathlib import Path

from elasticsearch import Elasticsearch

# ── Cesty: repo root + mcp-server/ ───────────────────────────────────────────
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
_mcp_parent = _repo_root / "mcp-server"
if str(_mcp_parent) not in sys.path:
    sys.path.insert(0, str(_mcp_parent))

# ── Konfigurace ──────────────────────────────────────────────────────────────
import config  # noqa: E402

os.environ.setdefault("ES_REMOTE_URL", config.ES_URL)

import modules.ESZakony.main as eszakony  # noqa: E402

# ── Fake MCP objekt ──────────────────────────────────────────────────────────
class _FakeMCP:
    """Nahrazuje MCP instanci; zachytí @mcp.tool() dekorátory."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *a, **kw):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator

# Jednorázová registrace nástrojů do fake MCP
_faker = _FakeMCP()
eszakony.register(_faker)


# ── Flatten výsledků ─────────────────────────────────────────────────────────
def _flatten(results_raw: list[dict]) -> list[dict]:
    """
    Rozplachtí výsledky z MCP nástrojů na uniformní formát:
        [{'akt_citace', 'citace', 'text', 'score', 'akt_nazev'}, ...]
    Duplikáty odstraněny podle (akt_citace, citace), order zachován.
    """
    seen = set()
    out = []
    for law in results_raw:
        akt_citace = law.get("akt_citace")
        for p in law.get("relevant_paragraphs", []):
            citace = p.get("citace")
            # citace je u většiny fragmentů prázdná -> dedupe i podle textu
            key = (akt_citace, citace, p.get("text"))
            if key not in seen:
                seen.add(key)
                out.append({
                    "akt_citace": akt_citace,
                    "citace": citace,
                    "text": p.get("text", ""),
                    "score": p.get("score"),
                    "akt_nazev": law.get("akt_nazev"),
                })
    return out


# ── retrieve() ───────────────────────────────────────────────────────────────
def retrieve(name: str, query: str, k: int = 5) -> list[dict]:
    """
    Vyvolá vyhledávání podle jména.

    Args:
        name: 'bm25', 'hybrid' nebo 'knn'.
        query: textový dotaz.
        k: počet výsledků.

    Returns:
        Seznam dict s klíči (akt_citace, citace, text, score).
    """
    tools = _faker.tools

    if name == "bm25":
        resp = tools["search_laws_text"](query=query, size=k)
        return _flatten(resp["results"])[: k * 3]

    if name == "hybrid":
        resp = tools["search_laws_semantic"](query=query, size=k)
        return _flatten(resp["results"])[: k * 3]

    if name == "knn":
        # Vlastní kNN přímo přes ES client (bez BM25 složky).
        es: Elasticsearch = eszakony.get_es_client()
        vector = eszakony.get_embedder().encode(query).tolist()

        body = {
            "knn": {
                "field": "paragrafy.vektor",
                "query_vector": vector,
                "k": k,
                "num_candidates": k * 10,
                "inner_hits": {"name": "knn_hits", "size": 3},
            },
            "size": k,
            "_source": {"excludes": ["paragrafy.vektor"]},
        }

        res = es.search(index=config.INDEX_NAME, body=body)

        out = []
        for hit in res["hits"]["hits"]:
            law_info = hit["_source"]
            akt_citace = law_info.get("akt_citace")
            if "inner_hits" in hit and "knn_hits" in hit["inner_hits"]:
                for p_hit in hit["inner_hits"]["knn_hits"]["hits"]["hits"]:
                    p_source = p_hit["_source"]
                    out.append({
                        "akt_citace": akt_citace,
                        "citace": p_source.get("citace"),
                        "text": p_source.get("text", ""),
                        "score": p_hit["_score"],
                        "akt_nazev": law_info.get("akt_nazev"),
                    })
        # Dedupe podle (akt_citace, citace), zachovat pořadí
        seen = set()
        deduped = []
        for item in out:
            key = (item["akt_citace"], item["citace"], item["text"])
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        return deduped[: k * 3]

    if name in _EXTRA:
        return _EXTRA[name](query, k)

    raise ValueError(
        f"Neznámý retriever: {name!r}. K dispozici: {RETRIEVERS}"
    )


# ── v1 omezený na pilotní univerzum zákonů (férové srovnání s v2) ───────────
import json  # noqa: E402
from functools import lru_cache  # noqa: E402

PILOT_DIR = Path(__file__).resolve().parent / "data" / "pilot"
V2_INDEX = config.V2_INDEX            # výchozí plný index zakony-v2 (pilot zakony-v2-pilot smazán)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from v2_common import search_text_of  # noqa: E402
V2_EMBED_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


@lru_cache(maxsize=1)
def _universe() -> list:
    return json.load(open(PILOT_DIR / "universe.json", encoding="utf-8"))["universe"]


def _v1_hits(res: dict) -> list:
    out = []
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        for name, ih in (hit.get("inner_hits") or {}).items():
            for p_hit in ih["hits"]["hits"]:
                out.append({"akt_citace": src.get("akt_citace"), "akt_nazev": src.get("akt_nazev"),
                            "citace": p_hit["_source"].get("citace"), "text": p_hit["_source"].get("text", ""),
                            "score": p_hit["_score"]})
    return out


def _v1_u(mode: str, query: str, k: int) -> list:
    """Stejné dotazy jako MCP server, jen s filtrem na pilotní univerzum."""
    es = eszakony.get_es_client()
    flt = {"terms": {"akt_citace": _universe()}}
    text_q = {"bool": {"should": [
        {"match": {"akt_nazev": query}},
        {"nested": {"path": "paragrafy", "query": {"match": {"paragrafy.text": query}},
                    "inner_hits": {"name": "text_hits", "size": 10, "_source": ["paragrafy.citace", "paragrafy.text"]}}},
    ], "filter": [flt]}}
    body = {"size": k, "_source": ["akt_citace", "akt_nazev"]}
    if mode in ("knn", "hybrid"):
        body["knn"] = {"field": "paragrafy.vektor", "query_vector": eszakony.get_embedder().encode(query).tolist(),
                       "k": k, "num_candidates": k * 10, "filter": flt,
                       "inner_hits": {"name": "knn_hits", "size": 10, "_source": ["paragrafy.citace", "paragrafy.text"]}}
    if mode in ("bm25", "hybrid"):
        body["query"] = text_q
    out, seen = [], set()
    for c in _v1_hits(es.search(index=config.INDEX_NAME, body=body)):
        key = (c["akt_citace"], c["text"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out[: k * 3]


# ── v2: § jednotky (zakony-v2) ─────────────────────────────────────────
@lru_cache(maxsize=1)
def _v2_embedder():
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(V2_EMBED_MODEL, device="mps")
    m.max_seq_length = 512
    return m


@lru_cache(maxsize=1)
def _reranker():
    from sentence_transformers import CrossEncoder
    # na MPS dochází paměť (GPU sdílí jiné procesy) -> default CPU
    return CrossEncoder(RERANK_MODEL, device=os.getenv("RERANK_DEVICE", "cpu"), max_length=512)


_V2_FILTER = [{"bool": {"must_not": [
    {"term": {"status": "zruseny"}},
    # "úplné znění ..." = historické přetisky jiných předpisů, ne platné právo
    {"match_phrase": {"akt_nazev": "úplné znění"}},
]}}]
_V2_SOURCE = {"excludes": ["vektor"]}


def _v2_doc(hit: dict) -> dict:
    s = hit["_source"]
    return {"akt_citace": s["akt_citace"], "akt_nazev": s["akt_nazev"],
            "citace": " ".join(x for x in (s.get("oznaceni"), s.get("nadpis")) if x),
            "text": s["text"], "search_text": s.get("search_text") or search_text_of(s), "score": hit["_score"],
            "_id": hit["_id"], "status": s.get("status"), "rok": s.get("rok")}


def _v2_bm25(query: str, n: int) -> list:
    es = eszakony.get_es_client()
    body = {"size": n, "_source": _V2_SOURCE, "query": {"bool": {"must": [{"multi_match": {
        "query": query, "type": "most_fields",
        "fields": ["nadpis^3", "cesta^1.5", "akt_nazev^0.5", "text"]}}], "filter": _V2_FILTER}}}
    return [_v2_doc(h) for h in es.search(index=V2_INDEX, body=body)["hits"]["hits"]]


def _v2_knn(query: str, n: int) -> list:
    es = eszakony.get_es_client()
    vec = _v2_embedder().encode(query, normalize_embeddings=True).tolist()
    body = {"size": n, "_source": _V2_SOURCE, "knn": {"field": "vektor", "query_vector": vec, "k": n,
                                                      "num_candidates": max(100, n * 4), "filter": _V2_FILTER}}
    return [_v2_doc(h) for h in es.search(index=V2_INDEX, body=body)["hits"]["hits"]]


def _rrf(*lists, k0: int = 60) -> list:
    """Reciprocal Rank Fusion na straně klienta (ES basic licence)."""
    score, docs = {}, {}
    for lst in lists:
        for rank, d in enumerate(lst, 1):
            score[d["_id"]] = score.get(d["_id"], 0.0) + 1.0 / (k0 + rank)
            docs.setdefault(d["_id"], d)
    return [dict(docs[i], score=score[i]) for i in sorted(score, key=score.get, reverse=True)]


def _v2_hybrid(query: str, k: int) -> list:
    return _rrf(_v2_bm25(query, 50), _v2_knn(query, 50))[:k]


def _v2_rerank(query: str, k: int, pool: int = 30) -> list:
    cands = _rrf(_v2_bm25(query, 50), _v2_knn(query, 50))[:pool]
    scores = _reranker().predict([(query, c["search_text"]) for c in cands], batch_size=8)
    ranked = sorted(zip(cands, scores), key=lambda x: float(x[1]), reverse=True)
    return [dict(c, score=float(s)) for c, s in ranked[:k]]


# ── v2 + filtr nejistých/zastaralých předpisů (přepočet skóre nad top 50 z kNN) ─────────
_REPUB_RE = re.compile(r"úpln\w*\s+zn[eě]\w*")      # úplné znění / úplném znění / úplnom znení / úplné znenie


def stale_flags(c: dict) -> dict:
    nazev = (c.get("akt_nazev") or "").lower()
    return {"republication": bool(_REPUB_RE.search(nazev)),
            "neznamy": c.get("status") == "neznamy",
            "pre1945": bool(c.get("rok")) and c["rok"] < 1945}


def _v2_knn_filtered(query: str, k: int, rep: float, nez: float, old: float, pool: int = 50) -> list:
    out = []
    for c in _v2_knn(query, max(pool, k * 5)):
        f = stale_flags(c)
        mult = (rep if f["republication"] else 1.0) * (nez if f["neznamy"] else 1.0) * (old if f["pre1945"] else 1.0)
        if mult > 0:
            out.append(dict(c, score=c["score"] * mult))
    return sorted(out, key=lambda c: c["score"], reverse=True)[:k]


_FILTER_VARIANTS = {
    "v2-knn-f1": (0.0, 1.0, 1.0),
    "v2-knn-f2": (0.0, 0.97, 1.0),
    "v2-knn-f3": (0.0, 0.94, 1.0),
    "v2-knn-f4": (0.0, 0.94, 0.94),
    "v2-knn-f5": (0.0, 0.90, 0.90),
    "v2-knn-platny": (0.0, 0.0, 1.0),
}

_EXTRA = {
    "v1-bm25-u": lambda q, k: _v1_u("bm25", q, k),
    "v1-hybrid-u": lambda q, k: _v1_u("hybrid", q, k),
    "v1-knn-u": lambda q, k: _v1_u("knn", q, k),
    "v2-bm25": lambda q, k: _v2_bm25(q, k),
    "v2-knn": lambda q, k: _v2_knn(q, k),
    "v2-hybrid": _v2_hybrid,
    "v2-rerank": _v2_rerank,
    **{name: (lambda q, k, p=params: _v2_knn_filtered(q, k, *p)) for name, params in _FILTER_VARIANTS.items()},
}

# ── Konstanta ────────────────────────────────────────────────────────────────
RETRIEVERS = ("bm25", "hybrid", "knn")
ALL_RETRIEVERS = RETRIEVERS + tuple(_EXTRA)


# ── CLI: quick test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    query = "Z čeho se skládá Parlament České republiky?"
    k = 3

    for rname in RETRIEVERS:
        print(f"\n{'='*60}")
        print(f"Retriever: {rname} | query: {query}")
        print(f"{'='*60}")
        results = retrieve(rname, query, k=k)
        if not results:
            print("  (žádné výsledky)")
        for i, r in enumerate(results, 1):
            text_preview = (r["text"] or "")[:80]
            print(f"  [{i}] {r['akt_citace']} | {r['citace']}")
            print(f"      {text_preview}...")
