#!/bin/bash
#
# Spustí ingest dat e-Sbírky do Elasticsearch
# Data se čtou přímo z .gz souborů (neextrahují se na disk)
#
# Použití:
#   ./run.sh                    # Spustí full ingest
#   ./run.sh --dry-run          # Ověří data bez vložení do ES
#   ./run.sh --es-url URL       # Jiná adresa ES
#   ./run.sh --force            # Přepíše zpracovaná data
#
# Požadavky:
#   - Elasticsearch běží na localhost:9200
#   - Python virtual env aktivní (nebo spusť z projektu)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Kontrola ES
echo "🔌 Kontroluji Elasticsearch..."
if ! curl -s http://localhost:9200/_cluster/health > /dev/null 2>&1; then
    echo "❌ Elasticsearch není dostupný na localhost:9200"
    echo "   Spusť: docker start es || docker run -d --name es -p 9200:9200 elasticsearch:8.15.0"
    exit 1
fi
echo "✅ Elasticsearch je dostupný"

# Aktivace virtual env
if [ ! -d ".venv" ]; then
    echo "❌ Virtual env neexistuje"
    echo "   Spusť: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

source .venv/bin/activate

# Spuštění ingest skriptu
echo "🚀 Spouštím ingest..."
DATA_DIR="data"
if [ ! -d "$DATA_DIR" ]; then
    echo "❌ Adresář $DATA_DIR neexistuje"
    exit 1
fi

# Sestavíme seznam .gz souborů
GZ_FILES=()
for f in "$DATA_DIR"/*.gz; do
    [ -f "$f" ] && GZ_FILES+=("$f")
done

if [ ${#GZ_FILES[@]} -eq 0 ]; then
    echo "❌ V $DATA_DIR nejsou žádné .gz soubory"
    exit 1
fi

python3 ingest.py "${GZ_FILES[@]}" "$@"
