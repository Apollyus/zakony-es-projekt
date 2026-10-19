"""
Generování syntetické golden sady: náhodné odstavce z indexu v2 (platné předpisy) -> LLM vytvoří
laickou otázku + referenční odpověď. Gold = (akt_citace, anchor z textu fragmentu).
"""

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Optional

from elasticsearch import Elasticsearch
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

PROMPT = """Jsi autor testovacích otázek pro vyhledávač v českých zákonech.

Zákon: {akt_nazev} ({akt_citace})
Text ustanovení:
\"\"\"{text}\"\"\"

Napiš JEDNU přirozenou otázku, kterou by mohl položit laik a na kterou tento text odpovídá.
Otázka NESMÍ obsahovat číslo zákona ani paragraf. Pak napiš krátkou referenční odpověď
vycházející pouze z textu. Pokud text nedává smysl samostatně (je to jen nadpis, výčet
bez kontextu apod.), vrať {{"question": null, "reference": null}}.

Odpověz pouze striktním JSON: {{"question": "...", "reference": "..."}}"""


def clean(t: str) -> str:
    t = re.sub(r"<[^>]+>", "", t or "")
    return re.sub(r"\s+", " ", t).strip()


def make_anchor(text: str, length: int = 80) -> str:
    if len(text) <= length:
        return text
    cut = text[:length]
    return cut[: cut.rfind(" ")] if " " in cut else cut


def sample_paragraphs(n: int, seed: int = 42) -> list:
    """Náhodně vybere n odstavců (150–1500 znaků) z platných předpisů v indexu v2, max 1 na předpis."""
    es = Elasticsearch([config.ES_URL], request_timeout=120)
    rng = random.Random(seed)
    body = {
        "size": n * 5,
        "_source": ["akt_citace", "akt_nazev", "text"],
        "query": {
            "function_score": {
                "query": {"bool": {"filter": [{"term": {"status": "platny"}}]}},
                "random_score": {"seed": seed, "field": "_seq_no"},
                "boost_mode": "replace",
            }
        },
    }
    res = es.search(index=config.V2_INDEX, body=body)
    out, seen_laws = [], set()
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        # max 1 odstavec na předpis, ať je sada pestrá
        if src.get("akt_citace") in seen_laws:
            continue
        # chunk v2 = odstavce § oddělené \n; otázka se dělá z jednoho odstavce
        frags = [clean(t) for t in (src.get("text") or "").split("\n")]
        frags = [t for t in frags if 150 <= len(t) <= 1500]
        if not frags:
            continue
        seen_laws.add(src.get("akt_citace"))
        out.append({"akt_citace": src.get("akt_citace"), "akt_nazev": src.get("akt_nazev", ""), "text": rng.choice(frags)})
        if len(out) >= n:
            break
    return out


def generate_qa(p: dict, llm) -> Optional[dict]:
    try:
        s = llm.invoke(PROMPT.format(**p)).content
        m = re.search(r"\{.*\}", s, re.S)
        data = json.loads(m.group(0)) if m else None
    except Exception as e:  # noqa: BLE001
        print(f"  LLM chyba: {e}")
        return None
    if not data or not data.get("question") or not data.get("reference"):
        return None
    return data


def main():
    ap = argparse.ArgumentParser(description="Syntetická golden sada pro RAGAS evaluaci")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "synthetic.jsonl"))
    args = ap.parse_args()

    llm = config.get_chat_llm(temperature=0.3)
    # vzorkujeme víc, protože část fragmentů LLM odmítne (nadpisy apod.)
    paragraphs = sample_paragraphs(int(args.n * 1.5) + 2, seed=args.seed)
    print(f"Navzorkováno {len(paragraphs)} fragmentů")

    records = []
    for p in tqdm(paragraphs, desc="Generuji QA"):
        qa = generate_qa(p, llm)
        if qa is None:
            continue
        records.append({
            "id": f"syn-{len(records) + 1:04d}",
            "user_input": qa["question"],
            "reference": qa["reference"],
            "gold": [{"akt_citace": p["akt_citace"], "anchor": make_anchor(p["text"])}],
            "reference_contexts": [p["text"]],
            "source": "synthetic",
            "akt_nazev": p["akt_nazev"],
        })
        if len(records) >= args.n:
            break

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Zapsáno {len(records)} záznamů do {args.out}")


if __name__ == "__main__":
    main()
