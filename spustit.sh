#!/bin/bash
# Spustí ingest dat do ES + stahování DOCX dokumentů
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source venv/bin/activate

echo "============================================"
echo " 1/2 Ingest do Elasticsearch"
echo "============================================"
python3 ingest.py data/ --workers 3 --chunk-size 100

echo ""
echo "============================================"
echo " 2/2 Stahování DOCX"
echo "============================================"
python3 stahni-docx.py "$@"
