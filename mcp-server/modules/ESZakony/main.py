import os
import logging
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

depends_on = []

es_client = None
embedder = None
INDEX_NAME = "zakony"

def get_es_client():
    global es_client
    if es_client is None:
        es_url = os.getenv("ES_REMOTE_URL", "http://localhost:9200")
        logger.info(f"Connecting to Elasticsearch at {es_url}")
        es_client = Elasticsearch([es_url])
    return es_client

def get_embedder():
    global embedder
    if embedder is None:
        model_name = os.getenv("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
        logger.info(f"Loading embedding model {model_name}")
        embedder = SentenceTransformer(model_name)
    return embedder

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
        es = get_es_client()
        body = {
            "query": {
                "bool": {
                    "should": [
                        {"match": {"akt_nazev": query}},
                        {
                            "nested": {
                                "path": "paragrafy",
                                "query": {
                                    "match": {"paragrafy.text": query}
                                },
                                "inner_hits": {"size": 3}
                            }
                        }
                    ]
                }
            },
            "size": size,
            "_source": ["id_zakona", "akt_citace", "akt_nazev", "rok"]
        }
        res = es.search(index=INDEX_NAME, body=body)
        
        results = []
        for hit in res["hits"]["hits"]:
            law_info = hit["_source"]
            law_summary = {
                "id_zakona": law_info.get("id_zakona"),
                "akt_citace": law_info.get("akt_citace"),
                "akt_nazev": law_info.get("akt_nazev"),
                "rok": law_info.get("rok"),
                "relevant_paragraphs": []
            }
            if "inner_hits" in hit and "paragrafy" in hit["inner_hits"]:
                for p_hit in hit["inner_hits"]["paragrafy"]["hits"]["hits"]:
                    p_source = p_hit["_source"]
                    law_summary["relevant_paragraphs"].append({
                        "citace": p_source.get("citace"),
                        "text": p_source.get("text")
                    })
            results.append(law_summary)
            
        return {"total_hits": res["hits"]["total"]["value"], "results": results}

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
        es = get_es_client()
        emb = get_embedder()
        vector = emb.encode(query).tolist()
        
        body = {
            "query": {
                "nested": {
                    "path": "paragrafy",
                    "query": {
                        "knn": {
                            "paragrafy.vektor": {
                                "vector": vector,
                                "k": size
                            }
                        }
                    },
                    "inner_hits": {"size": 3}
                }
            },
            "size": size,
            "_source": ["id_zakona", "akt_citace", "akt_nazev", "rok"]
        }
        res = es.search(index=INDEX_NAME, body=body)
        
        results = []
        for hit in res["hits"]["hits"]:
            law_info = hit["_source"]
            law_summary = {
                "id_zakona": law_info.get("id_zakona"),
                "akt_citace": law_info.get("akt_citace"),
                "akt_nazev": law_info.get("akt_nazev"),
                "rok": law_info.get("rok"),
                "relevant_paragraphs": []
            }
            if "inner_hits" in hit and "paragrafy" in hit["inner_hits"]:
                for p_hit in hit["inner_hits"]["paragrafy"]["hits"]["hits"]:
                    p_source = p_hit["_source"]
                    law_summary["relevant_paragraphs"].append({
                        "citace": p_source.get("citace"),
                        "text": p_source.get("text"),
                        "score": p_hit["_score"]
                    })
            results.append(law_summary)
            
        return {"total_hits": res["hits"]["total"]["value"], "results": results}

    @mcp.tool()
    def get_law_detail(id_zakona: str) -> dict:
        """
        Get details and all paragraphs of a specific law by its ID.
        
        Args:
            id_zakona: The ID of the law (e.g. URI/IRI)
            
        Returns:
            The full document of the law.
        """
        es = get_es_client()
        body = {
            "query": {
                "term": {
                    "id_zakona": id_zakona
                }
            },
            # Exclude vektor to avoid massive payload
            "_source": {"excludes": ["paragrafy.vektor"]},
            "size": 1
        }
        res = es.search(index=INDEX_NAME, body=body)
        if res["hits"]["hits"]:
            return res["hits"]["hits"][0]["_source"]
        return {"error": "Law not found"}
