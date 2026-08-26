#!/usr/bin/env python3
"""
Fáze A offline importu: výpočet embeddingů doma, BEZ Elasticsearch.

Používá totožnou logiku jako přímý ingest (pipeline.process_laws) —
sdílí funkce build_batch_docs() a iter_spool_chunks() — ale místo bulk
insertu do ES zapisuje hotové dokumenty (včetně vektorů) jako komprimované
NDJSON shardy, které se následně přenesou na server a naimportují
skriptem import_ndjson.py.

Výstup:
    <out>/shard_00000.json.gz     (každý řádek = 1 dokument ve formátu ES _source)
    <out>/export_state.json       (checkpoint IRI — pokračování po přerušení)
    <out>/004_types.db            (lokální SQLite typů, vznikne automaticky)

Použití (na domácím stroji s GPU):
    python3 export_embeddings.py /cesta/k/data --workers 4

Pokračování po přerušení: spusťte stejný příkaz — hotové IRIs se přeskočí.
"""

import argparse, gzip, json, os, shutil, sys, time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
log = logging.getLogger("export")

import pipeline
from pipeline import (
    Checkpoint, EMBEDDING_MODEL,
    categorize_files, find_json_files,
    load_001_metadata, load_004_to_sqlite,
    iter_spool_chunks, build_batch_docs,
)
from sentence_transformers import SentenceTransformer

# Embedding engine v worker procesu (lazy, jednou na proces)
_ENG = None


def _get_eng(model: str) -> SentenceTransformer:
    global _ENG
    if _ENG is None:
        _ENG = SentenceTransformer(model)
        log.info(f"Embedding model na zařízení: {_ENG.device}")
    return _ENG


def _init_export_worker(laws_path: str):
    """Načte metadata zákonů (stejně jako pipeline._init_worker)."""
    pipeline._init_worker(laws_path)


def _export_worker(args):
    """
    Worker: z spool chunku postaví dokumenty (sdílená logika s ingestem),
    spočítá embeddingy a vrátí hotové ES dokumenty.

    Args:
        args: (spool_path, db_path, model, encode_batch_size)

    Returns:
        (docs, processed_iris)  # docs = list ES _source dictů
    """
    spool_path, db_path, model, encode_bs = args
    
    batch_docs, processed_iris = build_batch_docs(spool_path, db_path)
    
    if batch_docs:
        eng = _get_eng(model)
        texts = [p["text"] for d in batch_docs for p in d["paragrafy"]]
        embeddings = eng.encode(texts, show_progress_bar=False, batch_size=encode_bs)
        
        i = 0
        for d in batch_docs:
            for p in d["paragrafy"]:
                p["vektor"] = embeddings[i].tolist()
                i += 1
    
    return batch_docs, processed_iris


class ShardWriter:
    """Sekvenční zápis dokumentů do komprimovaných NDJSON shardů."""
    
    def __init__(self, out_dir: str, docs_per_shard: int):
        self.out_dir = out_dir
        self.docs_per_shard = docs_per_shard
        self.shard_idx = 0
        self.n_in_shard = 0
        self.f = None
        self.total_docs = 0
        self.closed_shards = []
    
    def _path(self):
        return os.path.join(self.out_dir, f"shard_{self.shard_idx:05d}.json.gz")
    
    def write(self, doc: dict):
        if self.f is None:
            self.f = gzip.open(self._path(), "wt", encoding="utf-8")
            self.n_in_shard = 0
        self.f.write(json.dumps(doc, ensure_ascii=False) + "\n")
        self.n_in_shard += 1
        self.total_docs += 1
        
        if self.n_in_shard >= self.docs_per_shard:
            self._rotate()
    
    def _rotate(self):
        if self.f:
            self.f.close()
            self.closed_shards.append(self._path())
            log.info(f"Shard hotov: {os.path.basename(self._path())} "
                     f"({self.n_in_shard} doků, celkem {self.total_docs})")
        self.f = None
        self.shard_idx += 1
    
    def close(self):
        self._rotate()
        # poslední prázdný shard nechceme
        last = self._path()
        if os.path.exists(last) and os.path.getsize(last) == 0:
            pass  # gzip.open vytvoří soubor až při zápisu; prázdný by nevznikl
    
    def shards(self):
        return sorted(
            p for p in os.listdir(self.out_dir)
            if p.startswith("shard_") and p.endswith(".json.gz")
        )


def main():
    ap = argparse.ArgumentParser(description="Offline výpočet embeddingů do NDJSON shardů")
    ap.add_argument("inputs", nargs="+", help="Adresář/ soubory se vstupními .gz (001/003/004)")
    ap.add_argument("--out", default="embedding_shards", help="Výstupní adresář pro shardy")
    ap.add_argument("--model", default=EMBEDDING_MODEL)
    ap.add_argument("--workers", type=int, default=2, help="Paralelní workery")
    ap.add_argument("--chunk-size", type=int, default=100, help="Paragrafů na chunk")
    ap.add_argument("--encode-batch-size", type=int, default=64,
                    help="Batch pro GPU encoding (GPU snese 128-256)")
    ap.add_argument("--docs-per-shard", type=int, default=5000)
    ap.add_argument("--max-laws", type=int, default=0, help="Testovací limit zákona (0 = všechny)")
    ap.add_argument("--fresh", action="store_true",
                    help="Smaže výstupní adresář a začne od nuly")
    args = ap.parse_args()
    
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    
    out_dir = os.path.abspath(args.out)
    if args.fresh and os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    
    # Relativní pracovní soubory (spool_chunks/, 004_types.db) sletí do out_dir
    os.chdir(out_dir)
    
    json_files = find_json_files(args.inputs)
    if not json_files:
        log.error("Žádná vstupní data!")
        sys.exit(1)
    
    cat = categorize_files(json_files)
    checkpoint = Checkpoint("export_state.json")
    
    log.info("=" * 70)
    log.info("EXPORT EMBEDDINGŮ (offline fáze A)")
    log.info(f"Workery: {args.workers}, docs/shard: {args.docs_per_shard}")
    log.info("=" * 70)
    
    laws = load_001_metadata(cat["001"], checkpoint, max_items=args.max_laws)
    if not laws:
        log.error("Žádné zákony!")
        sys.exit(1)
    log.info(f"Zákony: {len(laws)}")
    
    didx = {}
    for law_iri, law_data in laws.items():
        did = law_data.get("znění_dokument_id")
        if did:
            didx[did] = law_iri
    
    db_path = "004_types.db"
    load_004_to_sqlite(cat["004"], db_path, checkpoint,
                       max_items=args.max_laws * 10 if args.max_laws > 0 else 0)
    
    spool_dir = "spool_chunks"
    if not os.path.exists(spool_dir):  # při obnovení nemažeme — nevadí, jen doroste
        shutil.rmtree(spool_dir, ignore_errors=True)
        os.makedirs(spool_dir)
    
    laws_path = os.path.join(spool_dir, "laws_meta.json")
    with open(laws_path, "w", encoding="utf-8") as f:
        json.dump(laws, f, ensure_ascii=False)
    
    max_003 = args.max_laws * 100 if args.max_laws > 0 else 0
    writer = ShardWriter(out_dir, args.docs_per_shard)
    
    total_chunks = 0
    total_paras = 0
    t0 = time.time()
    max_pending = args.workers * 2
    
    with ProcessPoolExecutor(
        max_workers=args.workers,
        initializer=_init_export_worker,
        initargs=(laws_path,),
    ) as executor:
        pending = {}
        
        def reap_done():
            nonlocal total_chunks
            done, _ = wait(list(pending.keys()), return_when=FIRST_COMPLETED)
            for fut in done:
                fut_spool = pending.pop(fut)
                try:
                    docs, iris = fut.result()
                    
                    for d in docs:
                        writer.write(d)
                    for iri in iris:
                        checkpoint.mark_iri(iri)
                    checkpoint.save()
                    
                    if os.path.exists(fut_spool):
                        os.remove(fut_spool)
                    
                    elapsed = time.time() - t0
                    rate = writer.total_docs / elapsed if elapsed > 0 else 0
                    log.info(f"Chunk {total_chunks}: {len(docs)} doků "
                             f"(celkem {writer.total_docs}, {rate:.0f} dok/s)")
                    total_chunks += 1
                except Exception as e:
                    log.error(f"Chunk selhal: {e}", exc_info=True)
                    raise
        
        for spool_file, n_paras in iter_spool_chunks(
            cat, didx, checkpoint, spool_dir,
            max_items=max_003, chunk_size=args.chunk_size,
        ):
            while len(pending) >= max_pending:
                reap_done()
            
            fut = executor.submit(_export_worker,
                                  (spool_file, db_path, args.model, args.encode_batch_size))
            pending[fut] = spool_file
            total_paras += n_paras
        
        while pending:
            reap_done()
    
    shutil.rmtree(spool_dir, ignore_errors=True)
    
    shards = writer.shards()
    size_mb = sum(os.path.getsize(os.path.join(out_dir, s)) for s in shards) / 1024**2
    
    log.info("=" * 70)
    log.info("EXPORT HOTOV")
    log.info(f"  Dokumentů:  {writer.total_docs}")
    log.info(f"  Shardů:     {len(shards)} ({size_mb:.0f} MB)")
    log.info(f"  Adresář:    {out_dir}")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
