"""
Konfigurace pro RAGAS evaluaci: LLM, embedder, konstanty.
Načítá hodnoty z .env (nikdy nehardcoduje API klíče).
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_huggingface import HuggingFaceEmbeddings

from ragas.llms import LangchainLLMWrapper
from ragas.embeddings import LangchainEmbeddingsWrapper

# ── Načtení .env (repo root = parent dir of eval/) ──────────────────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

# ── Konstanty ────────────────────────────────────────────────────────────────
ES_URL: str = os.getenv("ES_REMOTE_URL", "http://192.168.27.60:9200")
INDEX_NAME: str = "zakony-esbirka-2026"   # v1 – na serveru smazán 19. 10. 2026 (retrievery v1-* a knn jsou historické)
V2_INDEX: str = os.getenv("V2_INDEX", "zakony-v2")
EMBED_MODEL: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

EVAL_LLM_BASE_URL: str = os.getenv("EVAL_LLM_BASE_URL", "")
EVAL_LLM_API_KEY: str = os.getenv("EVAL_LLM_API_KEY", "")
EVAL_LLM_MODEL: str = os.getenv("EVAL_LLM_MODEL", "")

# ── Validace ─────────────────────────────────────────────────────────────────
_missing = []
for var, label in [
    (EVAL_LLM_BASE_URL, "EVAL_LLM_BASE_URL"),
    (EVAL_LLM_API_KEY,  "EVAL_LLM_API_KEY"),
    (EVAL_LLM_MODEL,    "EVAL_LLM_MODEL"),
]:
    if not var:
        _missing.append(label)

if _missing:
    raise RuntimeError(
        f"Chybějící proměnné prostředí: {', '.join(_missing)}. "
        "Zkontrolujte .env soubor v kořenu repozitáře."
    )

# ── Factory funkce ───────────────────────────────────────────────────────────

def get_chat_llm(temperature: float = 0.0) -> ChatOpenAI:
    """Vytvoří ChatOpenAI instanci pro evaluaci (směr k LLM-judge)."""
    return ChatOpenAI(
        base_url=EVAL_LLM_BASE_URL,
        api_key=EVAL_LLM_API_KEY,
        model=EVAL_LLM_MODEL,
        temperature=temperature,
        max_retries=3,
        timeout=180,
    )


def get_eval_llm():
    """Ragas-compatible LLM wrapper."""
    return LangchainLLMWrapper(get_chat_llm())


@lru_cache(maxsize=1)
def get_eval_embeddings():
    """Ragas-compatible embeddings wrapper (cached singleton)."""
    hf = HuggingFaceEmbeddings(model_name=EMBED_MODEL)
    return LangchainEmbeddingsWrapper(hf)
