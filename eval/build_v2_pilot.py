"""
Pilot indexu v2: jeden dokument = jeden § / článek (aktuální znění), s nadpisem
a cestou v zákoně (část › hlava › díl), dlouhé § rozdělené po odstavcích s opakovanou hlavičkou.

Vstupy (vytvořené extrakcí z 001/003/004 JSONů, viz eval/README.md):
    eval/data/pilot/universe.json   – vybrané zákony, aktuální znění, status
    eval/data/pilot/frag003.jsonl   – strom fragmentů aktuálních znění
    eval/data/pilot/frag004.jsonl   – texty a typy fragmentů

Použití:
    .venv/bin/python eval/build_v2_pilot.py units
    .venv/bin/python eval/build_v2_pilot.py index [--recreate]
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PILOT_DIR = EVAL_DIR / "data" / "pilot"
sys.path.insert(0, str(EVAL_DIR))
import config  # noqa: E402

sys.path.insert(0, str(EVAL_DIR.parent / "scripts"))
from v2_common import MAPPING, V2_EMBED_MODEL, clean, units_for_law  # noqa: E402

V2_INDEX = "zakony-v2-pilot"


# ── Stage 1: § jednotky ──────────────────────────────────────────────────────
def build_units() -> list:
    universe = json.load(open(PILOT_DIR / "universe.json", encoding="utf-8"))
    texts = {}
    for line in open(PILOT_DIR / "frag004.jsonl", encoding="utf-8"):
        d = json.loads(line)
        texts[d["frag_id"]] = (d["typ"], d["text"])

    by_law = defaultdict(list)
    for line in open(PILOT_DIR / "frag003.jsonl", encoding="utf-8"):
        r = json.loads(line)
        typ, text = texts.get(r["frag_id"], ("", None))
        r["typ"], r["text"] = typ, clean(text)
        by_law[r["akt_citace"]].append(r)

    docs = []
    for akt_citace, rows in by_law.items():
        meta = universe["current"][akt_citace]
        docs += units_for_law(akt_citace, meta["nazev"], universe["status"][akt_citace], meta["iri"], meta["od"], rows)
    return docs


# ── Stage 2: embedding + ES ──────────────────────────────────────────────────
def index_docs(docs: list, recreate: bool, resume: bool = False, batch: int = 32):
    from elasticsearch import Elasticsearch, helpers
    from sentence_transformers import SentenceTransformer
    from tqdm import tqdm

    import torch

    es = Elasticsearch([config.ES_URL], request_timeout=300)
    if es.indices.exists(index=V2_INDEX) and resume:
        done = {h["_id"] for h in helpers.scan(es, index=V2_INDEX, query={"query": {"match_all": {}}}, _source=False)}
        docs = [d for d in docs if d["_id"] not in done]
        print(f"Resume: {len(done)} už v indexu, zbývá {len(docs)}")
    else:
        if es.indices.exists(index=V2_INDEX):
            if not recreate:
                raise SystemExit(f"Index {V2_INDEX} už existuje (použij --recreate nebo --resume)")
            es.indices.delete(index=V2_INDEX)
        es.indices.create(index=V2_INDEX, body=MAPPING)

    model = SentenceTransformer(V2_EMBED_MODEL, device=os.getenv("EMBED_DEVICE", "mps"))
    # chunky mají max ~1800 znaků (~512 tokenů); delší kusy (tabulky) se pro embedding oříznou
    model.max_seq_length = 512
    # řazení podle délky = menší padding, rychlejší embedding
    docs = sorted(docs, key=lambda d: len(d["search_text"]))
    batches, i = [], 0
    while i < len(docs):
        bs = batch if len(docs[i]["search_text"]) < 1000 else max(4, batch // 4)
        batches.append(docs[i: i + bs])
        i += bs
    for part in tqdm(batches, desc="embed+index"):
        vecs = model.encode([d["search_text"] for d in part], normalize_embeddings=True, batch_size=len(part))
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        actions = [{"_index": V2_INDEX, "_id": d["_id"], "_source": {**{k: v for k, v in d.items() if k != "_id"},
                                                                      "vektor": v.tolist()}}
                   for d, v in zip(part, vecs)]
        helpers.bulk(es, actions, request_timeout=300)
    es.indices.refresh(index=V2_INDEX)
    print(f"Hotovo: {es.count(index=V2_INDEX)['count']} dokumentů v {V2_INDEX}")


def main():
    ap = argparse.ArgumentParser(description="Pilot index v2 (§ jednotky)")
    ap.add_argument("stage", choices=["units", "index"])
    ap.add_argument("--recreate", action="store_true")
    ap.add_argument("--resume", action="store_true", help="doindexovat jen chybějící dokumenty")
    args = ap.parse_args()
    units_path = PILOT_DIR / "units.jsonl"
    if args.stage == "units":
        docs = build_units()
        with open(units_path, "w", encoding="utf-8") as f:
            for d in docs:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        print(f"{len(docs)} jednotek z {len({d['akt_citace'] for d in docs})} zákonů -> {units_path}")
    else:
        docs = [json.loads(line) for line in open(units_path, encoding="utf-8")]
        index_docs(docs, args.recreate, args.resume)


if __name__ == "__main__":
    main()
