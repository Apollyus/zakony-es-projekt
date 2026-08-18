#!/usr/bin/env python3
"""
Test ingestu s malým datasetem.
Paralelní processing — workers * chunk_size položek najednou.

Použití:
    python3 test_small.py                        # 1000 zákonů
    python3 test_small.py --n 100                # 100 zákonů
    python3 test_small.py --dry-run              # jen ověření načítání
"""

import sys
import logging
import argparse
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from pipeline import process_laws, INDEX_NAME, ES_HOST

def main():
    parser = argparse.ArgumentParser(description="Test ingestu s malým datasetem")
    parser.add_argument("--n", type=int, default=1000, help="Počet zákonů na test")
    parser.add_argument("--dry-run", action="store_true", help="Jen ověření načítání")
    parser.add_argument("--es-url", default=ES_HOST, help="ES URL")
    parser.add_argument("--index", default="index-zakony-jsony", help="ES index")
    parser.add_argument("--data-dir", default="data", help="Složka s daty")
    parser.add_argument("--workers", type=int, default=3, help="Počet parallel workers")
    parser.add_argument("--chunk-size", type=int, default=50, help="Chunk size pro parallel processing")
    args = parser.parse_args()
    
    # Find files
    data_dir = Path(args.data_dir)
    files = sorted([str(f) for f in data_dir.glob("*.gz")])
    if not files:
        logging.error("Žádné .gz soubory v %s", args.data_dir)
        sys.exit(1)
    
    logging.info(f"Nalezeno {len(files)} souborů")
    
    if args.dry_run:
        logging.info(f"\nDRY RUN - test načítání prvních {args.n} zákonů\n")
        from pipeline import find_json_files, load_001_metadata, Checkpoint, load_004_to_sqlite, create_es_index
        from elasticsearch import Elasticsearch
        
        es = Elasticsearch([args.es_url], request_timeout=30)
        logging.info("Připojeno k ES")
        create_es_index(es, "test-dryrun")
        
        checkpoint = Checkpoint("test_state.json")
        laws = load_001_metadata(files, checkpoint, max_items=args.n)
        logging.info(f"Načteno {len(laws)} zákonů")
        
        db_path = "test_004_types.db"
        load_004_to_sqlite(files, db_path, checkpoint, max_items=args.n * 10)
        
        logging.info("\nDRY RUN PROSL!")
        return
    
    # Run pipeline with limits
    stats = process_laws(
        json_files=files,
        es_url=args.es_url,
        index=args.index,
        chunk_size=args.chunk_size,
        num_workers=args.workers,
        dry_run=False,
        max_laws=args.n,
    )
    
    logging.info(f"\nVýsledky:")
    logging.info(f"  Zákony: {stats.get('laws', 'N/A')}")
    logging.info(f"  Dokumentů: {stats.get('total_docs', 'N/A')}")
    logging.info(f"  Chyby: {stats.get('errors', 'N/A')}")


if __name__ == "__main__":
    main()
