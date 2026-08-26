#!/usr/bin/env python3
"""
Full ingest legislativy (všechna data).
Paralelní processing — workers * chunk_size položek najednou.

Použití:
    python3 ingest.py data/001*.json.gz data/003*.json.gz data/004*.json.gz
    python3 ingest.py --workers 7 --chunk-size 100 data/
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

from pipeline import process_laws, find_json_files, INDEX_NAME, ES_HOST, EMBEDDING_MODEL

def main():
    parser = argparse.ArgumentParser(description="Full ingest legislativy do Elasticsearch")
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Cesty k JSON souborům nebo adresáře"
    )
    parser.add_argument("--es-url", default=ES_HOST, help="Elasticsearch URL")
    parser.add_argument("--model", default=EMBEDDING_MODEL, help="Embedding model")
    parser.add_argument("--index", default=INDEX_NAME, help="ES index name")
    parser.add_argument("--batch-size", type=int, default=50, help="Bulk batch size")
    parser.add_argument("--chunk-size", type=int, default=100, help="Parallel chunk size")
    parser.add_argument("--workers", type=int, default=3, help="Počet parallel workers (cores-1)")
    parser.add_argument("--state", default="state.json", help="Checkpoint file")
    parser.add_argument("--dry-run", action="store_true", help="Test without ES insert")
    
    args = parser.parse_args()
    
    # Find JSON files
    json_files = find_json_files(args.inputs)
    if not json_files:
        logging.error("Nebyly nalezeny žádné JSON soubory!")
        sys.exit(1)
    
    logging.info(f"Nalezeno {len(json_files)} JSON souborů")
    
    # Run pipeline
    stats = process_laws(
        json_files=json_files,
        es_url=args.es_url,
        model=args.model,
        index=args.index,
        batch_size=args.batch_size,
        chunk_size=args.chunk_size,
        num_workers=args.workers,
        state_file=args.state,
        dry_run=args.dry_run,
    )
    
    if args.dry_run:
        logging.info("DRY RUN hotovo!")
    else:
        logging.info(f"Stats: {stats}")


if __name__ == "__main__":
    main()
