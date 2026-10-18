import os
import logging
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

depends_on = []

es_client = None
embedder = None

# v1 = "zakony-esbirka-2026" (zákon s nested fragmenty), v2 = "zakony-v2" (jeden dokument = jeden §,
# jen aktuální znění, bge-m3). Schéma se odvodí z názvu indexu, lze vynutit ES_INDEX_SCHEMA=v1|v2.
INDEX_NAME = os.getenv("ES_INDEX", "zakony-esbirka-2026")
SCHEMA = os.getenv("ES_INDEX_SCHEMA") or ("v2" if INDEX_NAME.startswith("zakony-v2") else "v1")
DEFAULT_MODEL = {"v1": "sentence-transformers/paraphrase-multilingual-mpnet-base-v2", "v2": "BAAI/bge-m3"}[SCHEMA]


def get_es_client():
    global es_client
    if es_client is None:
        es_url = os.getenv("ES_REMOTE_URL", "http://localhost:9200")
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
RRF_K = 60


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


def _rrf(*hit_lists) -> list:
    """Reciprocal Rank Fusion na straně klienta (nativní RRF retriever vyžaduje placenou licenci)."""
    score, hits = {}, {}
    for hit_list in hit_lists:
        for rank, hit in enumerate(hit_list, 1):
            score[hit["_id"]] = score.get(hit["_id"], 0.0) + 1.0 / (RRF_K + rank)
            hits.setdefault(hit["_id"], hit)
    return [dict(hits[i], _score=score[i]) for i in sorted(score, key=score.get, reverse=True)]


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
            return _v2_group(_v2_bm25_hits(query, size), size)
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
            # hybrid: BM25 + kNN spojené přes RRF
            pool = max(50, size * 5)
            return _v2_group(_rrf(_v2_bm25_hits(query, pool), _v2_knn_hits(query, pool)), size)
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
