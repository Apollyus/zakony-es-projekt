import os
import re
import logging
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

depends_on = []

es_client = None
embedder = None

# v2 = "zakony-v2" (výchozí; jeden dokument = jeden §, jen aktuální znění, bge-m3),
# v1 = "zakony-esbirka-2026" (zákon s nested fragmenty; původní, na serveru smazán 19. 10. 2026).
# Schéma se odvodí z názvu indexu, lze vynutit ES_INDEX_SCHEMA=v1|v2.
INDEX_NAME = os.getenv("ES_INDEX", "zakony-v2")
SCHEMA = os.getenv("ES_INDEX_SCHEMA") or ("v2" if INDEX_NAME.startswith("zakony-v2") else "v1")
DEFAULT_MODEL = {"v1": "sentence-transformers/paraphrase-multilingual-mpnet-base-v2", "v2": "BAAI/bge-m3"}[SCHEMA]
logger.info(f"ESZakony: index {INDEX_NAME}, schéma {SCHEMA}, model {os.getenv('EMBEDDING_MODEL', DEFAULT_MODEL)}")


def get_es_client():
    global es_client
    if es_client is None:
        es_url = os.getenv("ES_REMOTE_URL", "http://192.168.27.60:9200")
        logger.info(f"Connecting to Elasticsearch at {es_url} (index {INDEX_NAME}, schema {SCHEMA})")
        es_client = Elasticsearch([es_url])
    return es_client


def get_embedder():
    global embedder
    if embedder is None:
        model_name = os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
        logger.info(f"Loading embedding model {model_name}")
        embedder = SentenceTransformer(model_name)
        if SCHEMA == "v2":
            embedder.max_seq_length = 512
    return embedder


# ── v1: zákon s nested fragmenty ─────────────────────────────────────────────
def _v1_law_summary(hit: dict) -> dict:
    law_info = hit["_source"]
    return {
        "id_zakona": law_info.get("id_zakona"),
        "akt_citace": law_info.get("akt_citace"),
        "akt_nazev": law_info.get("akt_nazev"),
        "rok": law_info.get("rok"),
        "relevant_paragraphs": [],
    }


def _v1_text_query(query: str, inner_hits_name: str = None) -> dict:
    nested = {"path": "paragrafy", "query": {"match": {"paragrafy.text": query}}, "inner_hits": {"size": 10}}
    if inner_hits_name:
        nested["inner_hits"]["name"] = inner_hits_name
    return {"bool": {"should": [{"match": {"akt_nazev": query}}, {"nested": nested}]}}


def _v1_search_text(query: str, size: int) -> dict:
    es = get_es_client()
    body = {
        "query": _v1_text_query(query),
        "size": size,
        "_source": ["id_zakona", "akt_citace", "akt_nazev", "rok"],
    }
    res = es.search(index=INDEX_NAME, body=body)

    results = []
    for hit in res["hits"]["hits"]:
        law_summary = _v1_law_summary(hit)
        if "inner_hits" in hit and "paragrafy" in hit["inner_hits"]:
            for p_hit in hit["inner_hits"]["paragrafy"]["hits"]["hits"]:
                p_source = p_hit["_source"]
                law_summary["relevant_paragraphs"].append({
                    "citace": p_source.get("citace"),
                    "text": p_source.get("text"),
                })
        results.append(law_summary)
    return {"total_hits": res["hits"]["total"]["value"], "results": results}


def _v1_search_semantic(query: str, size: int) -> dict:
    es = get_es_client()
    vector = get_embedder().encode(query).tolist()
    body = {
        "knn": {
            "field": "paragrafy.vektor",
            "query_vector": vector,
            "k": size,
            "num_candidates": size * 10,
            "inner_hits": {"name": "knn_hits", "size": 10},
        },
        "query": _v1_text_query(query, inner_hits_name="text_hits"),
        "size": size,
        "_source": ["id_zakona", "akt_citace", "akt_nazev", "rok"],
    }
    res = es.search(index=INDEX_NAME, body=body)

    results = []
    for hit in res["hits"]["hits"]:
        law_summary = _v1_law_summary(hit)
        if "inner_hits" in hit:
            # citace je u většiny fragmentů prázdná -> deduplikace podle citace i textu
            # (dřív se podle samotné citace zahodily skoro všechny fragmenty zákona)
            seen = set()
            for hit_type in ["knn_hits", "text_hits"]:
                if hit_type in hit["inner_hits"]:
                    for p_hit in hit["inner_hits"][hit_type]["hits"]["hits"]:
                        p_source = p_hit["_source"]
                        key = (p_source.get("citace"), p_source.get("text"))
                        if key not in seen:
                            seen.add(key)
                            law_summary["relevant_paragraphs"].append({
                                "citace": p_source.get("citace"),
                                "text": p_source.get("text"),
                                "score": p_hit["_score"],
                            })
        results.append(law_summary)
    return {"total_hits": res["hits"]["total"]["value"], "results": results}


def _v1_law_detail(id_zakona: str) -> dict:
    es = get_es_client()
    body = {
        "query": {"term": {"id_zakona": id_zakona}},
        # Exclude vektor to avoid massive payload
        "_source": {"excludes": ["paragrafy.vektor"]},
        "size": 1,
    }
    res = es.search(index=INDEX_NAME, body=body)
    if res["hits"]["hits"]:
        return res["hits"]["hits"][0]["_source"]
    return {"error": "Law not found"}


# ── v2: jeden dokument = jeden § aktuálního znění ────────────────────────────
V2_SOURCE = ["id_zakona", "akt_citace", "akt_nazev", "rok", "oznaceni", "nadpis", "cesta", "text", "status"]
V2_FILTER = [{"bool": {"must_not": [{"term": {"status": "zruseny"}}]}}]

# Nejisté/zastaralé předpisy, které data e-Sbírky nevedou jako zrušené (např. 42/1970 Sb. „Zákoník práce“
# má jen vyhlášené znění → status 'neznamy'; 123/1920 Sb. „platný“, protože konsolidace nikdy neskončila).
# Nevyřazují se (status 'neznamy' mají i nálezy ÚS a sdělení), jen se jim sníží skóre; přetisky „úplné znění“
# (i slovenské „úplnom znení“) se vyřadí. Změřeno na eval sadě: podíl nejistých výsledků 19 % → 4 %, správnost beze změny.
STALE_PENALTY = float(os.getenv("V2_STALE_PENALTY", "0.94"))
OLD_BEFORE_YEAR = 1945
POOL = 50
_REPUBLICATION_RE = re.compile(r"úpln\w*\s+zn[eě]\w*")


def _stale_multiplier(source: dict) -> float:
    if _REPUBLICATION_RE.search((source.get("akt_nazev") or "").lower()):
        return 0.0
    mult = 1.0
    if source.get("status") == "neznamy":
        mult *= STALE_PENALTY
    if source.get("rok") and source["rok"] < OLD_BEFORE_YEAR:
        mult *= STALE_PENALTY
    return mult


def _v2_rescore(hits: list) -> list:
    """Sníží skóre nejistým předpisům, vyřadí přetisky a přeřadí (nad poolem kandidátů)."""
    out = []
    for hit in hits:
        mult = _stale_multiplier(hit["_source"])
        if mult > 0:
            out.append(dict(hit, _score=hit["_score"] * mult))
    return sorted(out, key=lambda h: h["_score"], reverse=True)


def _v2_bm25_hits(query: str, n: int) -> list:
    body = {
        "size": n,
        "_source": V2_SOURCE,
        "query": {"bool": {
            "must": [{"multi_match": {"query": query, "type": "most_fields",
                                      "fields": ["nadpis^3", "cesta^1.5", "akt_nazev^0.5", "text"]}}],
            "filter": V2_FILTER,
        }},
    }
    return get_es_client().search(index=INDEX_NAME, body=body)["hits"]["hits"]


def _v2_knn_hits(query: str, n: int) -> list:
    vector = get_embedder().encode(query, normalize_embeddings=True).tolist()
    body = {
        "size": n,
        "_source": V2_SOURCE,
        "knn": {"field": "vektor", "query_vector": vector, "k": n,
                "num_candidates": max(100, n * 4), "filter": V2_FILTER},
    }
    return get_es_client().search(index=INDEX_NAME, body=body)["hits"]["hits"]


def _v2_group(hits: list, size: int) -> dict:
    """§ seskupené podle předpisu – stejný formát odpovědi jako v1; pořadí podle nejlepšího §."""
    laws = {}
    for hit in hits[:size]:
        s = hit["_source"]
        law = laws.setdefault(s["akt_citace"], {
            "id_zakona": s.get("id_zakona"),
            "akt_citace": s["akt_citace"],
            "akt_nazev": s.get("akt_nazev"),
            "rok": s.get("rok"),
            "status": s.get("status"),          # platny | neznamy (jen vyhlášené znění, platnost neověřena)
            "relevant_paragraphs": [],
        })
        law["relevant_paragraphs"].append({
            "citace": " ".join(x for x in (s.get("oznaceni"), s.get("nadpis")) if x),
            "cesta": s.get("cesta"),
            "text": s.get("text"),
            "score": hit["_score"],
        })
    return {"total_hits": len(hits), "results": list(laws.values())}


def _v2_law_detail(id_zakona: str) -> dict:
    es = get_es_client()
    field = "id_zakona" if id_zakona.startswith("esel-esb:") else "akt_citace"
    body = {
        "query": {"term": {field: id_zakona}},
        "_source": ["id_zakona", "akt_citace", "akt_nazev", "rok", "status", "zneni_iri", "ucinnost_od",
                    "oznaceni", "nadpis", "cesta", "text"],
        "sort": [{"poradi": {"order": "asc", "unmapped_type": "integer"}}],
        "size": 10000,
    }
    hits = es.search(index=INDEX_NAME, body=body)["hits"]["hits"]
    if not hits:
        return {"error": "Law not found"}
    first = hits[0]["_source"]
    return {
        **{k: first.get(k) for k in ("id_zakona", "akt_citace", "akt_nazev", "rok", "status", "zneni_iri", "ucinnost_od")},
        "paragrafy": [{"citace": " ".join(x for x in (h["_source"].get("oznaceni"), h["_source"].get("nadpis")) if x),
                       "cesta": h["_source"].get("cesta"), "text": h["_source"].get("text")} for h in hits],
    }


def register(mcp, registry=None):
    @mcp.tool()
    def search_laws_text(query: str, size: int = 10) -> dict:
        """
        Search for laws using standard full-text search (BM25) over paragraphs and law titles.

        Args:
            query: The search query in Czech.
            size: Number of results to return.

        Returns:
            A dictionary containing search results.
        """
        if SCHEMA == "v2":
            return _v2_group(_v2_rescore(_v2_bm25_hits(query, max(POOL, size * 5))), size)
        return _v1_search_text(query, size)

    @mcp.tool()
    def search_laws_semantic(query: str, size: int = 10) -> dict:
        """
        Search for laws using semantic vector search (k-NN) over paragraphs.

        Args:
            query: The search query in Czech.
            size: Number of results to return.

        Returns:
            A dictionary containing search results.
        """
        if SCHEMA == "v2":
            # čisté kNN (bge-m3): na evaluaci lepší než hybrid BM25+kNN přes RRF
            # (plný index, ruční sada: správný § v top 5 u 80 % vs 67 %)
            return _v2_group(_v2_rescore(_v2_knn_hits(query, max(POOL, size * 5))), size)
        return _v1_search_semantic(query, size)

    @mcp.tool()
    def get_law_detail(id_zakona: str) -> dict:
        """
        Get details and all paragraphs of a specific law by its ID.

        Args:
            id_zakona: The ID of the law (e.g. URI/IRI). With the v2 index also the citation (e.g. "40/2009 Sb.").

        Returns:
            The full document of the law.
        """
        if SCHEMA == "v2":
            return _v2_law_detail(id_zakona)
        return _v1_law_detail(id_zakona)
