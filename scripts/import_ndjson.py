#!/usr/bin/env python3
"""
Fáze B offline importu: nasypání hotových shardů s embeddingy do Elasticsearch.

Běží na serveru. Je idempotentní — _id dokumentů je deterministické
a hotové shardy se značí sidecarem .done, takže import lze kdykoli
přerušit a spustit znovu.

Použití:
    python3 import_ndjson.py /cesta/k/embedding_shards
    python3 import_ndjson.py shards/ --force     # smazat a vytvořit index znovu
    python3 import_ndjson.py shards/ --reset     # ignorovat .done, importovat vše znovu
"""

import argparse, gzip, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
log = logging.getLogger("import")

from elasticsearch import Elasticsearch, helpers


def es_doc_id(src: dict) -> str:
    """Deterministické _id — totožné s přímým ingestem (pipeline.py)."""
    return f"{src['id_zakona']}::{src['paragrafy'][0]['iris']}"


def main():
    ap = argparse.ArgumentParser(description="Import NDJSON shardů do Elasticsearch")
    ap.add_argument("shards_dir", help="Adresář se shard_*.json.gz")
    ap.add_argument("--es-url", default="http://localhost:9200")
    ap.add_argument("--index", default="zakony")
    ap.add_argument("--bulk-size", type=int, default=50, help="Dokumentů na bulk request")
    ap.add_argument("--delay", type=float, default=0.5, help="Pauza mezi shardy (sekundy)")
    ap.add_argument("--force", action="store_true", help="Smaže a vytvoří index znovu")
    ap.add_argument("--reset", action="store_true", help="Importuje i už hotové shardy (.done)")
    args = ap.parse_args()
    
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    
    shards_dir = os.path.abspath(args.shards_dir)
    if not os.path.isdir(shards_dir):
        log.error(f"Adresář neexistuje: {shards_dir}")
        sys.exit(1)
    
    shards = sorted(
        f for f in os.listdir(shards_dir)
        if f.startswith("shard_") and f.endswith(".json.gz")
    )
    if not shards:
        log.error(f"Žádné shardy v {shards_dir}")
        sys.exit(1)
    
    es = Elasticsearch([args.es_url], request_timeout=120,
                       retry_on_timeout=True, max_retries=5)
    if not es.ping():
        log.error("ES nedostupný!")
        sys.exit(1)
    
    # Index — mapping vezmeme z pipeline, ať je vždy totožný
    import pipeline
    pipeline.create_es_index(es, args.index, force=args.force)
    
    done_total = 0
    err_total = 0
    t0 = time.time()
    
    for i, fname in enumerate(shards, 1):
        path = os.path.join(shards_dir, fname)
        done_marker = path + ".done"
        
        if os.path.exists(done_marker) and not args.reset:
            log.info(f"[{i}/{len(shards)}] {fname} — již hotový, přeskočen")
            continue
        
        actions = []
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                src = json.loads(line)
                actions.append({
                    "_index": args.index,
                    "_id": es_doc_id(src),
                    "_source": src,
                })
        
        success, errors = helpers.bulk(
            es, actions, chunk_size=args.bulk_size, raise_on_error=False
        )
        n_err = len(errors) if errors else 0
        done_total += success
        err_total += n_err
        
        if n_err == 0:
            open(done_marker, "w").close()  # sidecar: shard je kompletně v ES
        else:
            log.warning(f"{fname}: {n_err} chyb — .done NEukládám, zkusí se znovu")
            for e in errors[:3]:
                log.warning(f"  ukázka chyby: {e}")
        
        elapsed = time.time() - t0
        rate = done_total / elapsed if elapsed > 0 else 0
        log.info(f"[{i}/{len(shards)}] {fname}: {success} OK, {n_err} err "
                 f"(celkem {done_total}, {rate:.0f} dok/s)")
        
        if args.delay > 0:
            time.sleep(args.delay)
    
    # Ověření
    es.indices.refresh(index=args.index)
    count = es.count(index=args.index)["count"]
    
    log.info("=" * 70)
    log.info("IMPORT HOTOV")
    log.info(f"  Vloženo teď:   {done_total} doků ({err_total} chyb)")
    log.info(f"  Index '{args.index}': {count} dokumentů celkem")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
