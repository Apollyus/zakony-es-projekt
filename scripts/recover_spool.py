#!/usr/bin/env python3
"""
Recovery: dozpracování zbylých spool chunků po pádu export_embeddings.py.

Načte chunk_*.jsonl z <shards>/spool_chunks/, postaví dokumenty (stejná
logika jako export — build_batch_docs), spočítá embeddingy na GPU a
dopíše je do nového shardu. IRIs označí do checkpointu, chunky smaže.

Použití:
    python3 recover_spool.py [cesta_k_shards_dir]
"""

import glob, gzip, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
log = logging.getLogger("recover")

import torch
import pipeline
from pipeline import Checkpoint, EMBEDDING_MODEL, build_batch_docs

SHARDS_DIR = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "embedding_shards")


def next_shard_path(shards_dir: str) -> str:
    nums = [
        int(m.group(1))
        for f in os.listdir(shards_dir)
        if (m := re.match(r"shard_(\d+)\.json\.gz$", f))
    ]
    idx = max(nums) + 1 if nums else 0
    return os.path.join(shards_dir, f"shard_{idx:05d}.json.gz"), idx


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    spool_dir = os.path.join(SHARDS_DIR, "spool_chunks")
    db_path = os.path.join(SHARDS_DIR, "004_types.db")
    laws_path = os.path.join(spool_dir, "laws_meta.json")

    chunks = sorted(
        f for f in glob.glob(os.path.join(spool_dir, "chunk_*.jsonl"))
    )
    if not chunks:
        log.info("Žádné spool chunky k dozpracování — nic k dělání.")
        return
    log.info(f"Chunků k dozpracování: {len(chunks)}")

    if not os.path.exists(laws_path):
        log.error(f"Chybí {laws_path} — nelze mapovat zákony!")
        sys.exit(1)

    # Metadána zákonů + checkpoint
    pipeline._init_worker(laws_path)
    os.chdir(SHARDS_DIR)  # Checkpoint i DB očekávají cwd = shards dir
    checkpoint = Checkpoint("export_state.json")

    # Model na GPU
    from sentence_transformers import SentenceTransformer
    eng = SentenceTransformer(EMBEDDING_MODEL)
    log.info(f"Model na zařízení: {eng.device}")

    shard_path, shard_idx = next_shard_path(SHARDS_DIR)
    log.info(f"Dokončuji do: {os.path.basename(shard_path)}")

    total_docs = 0
    total_iris = 0
    t0 = time.time()
    out_f = gzip.open(shard_path, "wt", encoding="utf-8")

    try:
        for i, chunk_path in enumerate(chunks, 1):
            docs, iris = build_batch_docs(chunk_path, db_path)

            if docs:
                texts = [p["text"] for d in docs for p in d["paragrafy"]]
                embs = eng.encode(texts, show_progress_bar=False, batch_size=32)
                k = 0
                for d in docs:
                    for p in d["paragrafy"]:
                        p["vektor"] = embs[k].tolist()
                        k += 1
                for d in docs:
                    out_f.write(json.dumps(d, ensure_ascii=False) + "\n")

            for iri in iris:
                checkpoint.mark_iri(iri)

            total_docs += len(docs)
            total_iris += len(iris)

            os.remove(chunk_path)
            checkpoint.save()

            log.info(f"[{i}/{len(chunks)}] {os.path.basename(chunk_path)}: "
                     f"{len(docs)} doků, {len(iris)} IRI (celkem {total_docs})")
    finally:
        out_f.close()

    size_mb = os.path.getsize(shard_path) / 1024**2
    log.info("=" * 70)
    log.info("RECOVERY HOTOV")
    log.info(f"  Dokumentů doplněno: {total_docs}")
    log.info(f"  IRI označeno:       {total_iris}")
    log.info(f"  Shard:              {os.path.basename(shard_path)} ({size_mb:.1f} MB)")
    log.info(f"  Čas:                {time.time() - t0:.0f} s")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
