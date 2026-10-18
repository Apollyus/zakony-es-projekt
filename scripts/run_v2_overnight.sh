#!/usr/bin/env bash
# Noční build indexu v2 (zakony-v2). Bezpečné spustit znovu – hotové fáze/chunky se přeskočí.
#
#   bash scripts/run_v2_overnight.sh            # embedding max 10 h, priority 0–5 (bez mezinárodních smluv)
#   HOURS=7 MAX_PRIORITY=3 bash scripts/run_v2_overnight.sh   # jen platné předpisy
#   MAX_PRIORITY=9 bash scripts/run_v2_overnight.sh           # i mezinárodní smlouvy (Sb. m. s., +87 tis. chunků)
#   BACKUP_DIR=/Volumes/Zaloha bash scripts/run_v2_overnight.sh  # záloha DB na externí disk
#
# Průběh:  tail -f logs/build_v2.log      Stav:  .venv/bin/python scripts/build_v2_index.py stats
# Zastavení: Ctrl+C (nebo kill <PID>) – rozdělaná dávka se dokončí a uloží.
set -euo pipefail
cd "$(dirname "$0")/.."

HOURS="${HOURS:-10}"
MAX_PRIORITY="${MAX_PRIORITY:-5}"
DATE="${DATE:-$(date +%F)}"
PY=.venv/bin/python
mkdir -p logs

echo "== $(date) | build v2: datum znění $DATE, limit ${HOURS} h, priority <= $MAX_PRIORITY"
# -i: nespát při nečinnosti, -s: nespát na síťovém napájení. NEZAVÍRAT víko – MacBook se uspí i s caffeinate.
caffeinate -is $PY scripts/build_v2_index.py extract --date "$DATE"
caffeinate -is $PY scripts/build_v2_index.py units
# embedding běží lokálně; každá dávka se nejdřív uloží do SQLite (záloha vektorů), pak se pošle do ES.
# Při výpadku ES/VPN se embeduje dál a odeslání se zkouší každých 5 min.
EMBED_RC=0
caffeinate -is $PY scripts/build_v2_index.py embed --hours "$HOURS" --max-priority "$MAX_PRIORITY" || EMBED_RC=$?
# dorovnání do ES (kdyby na konci embeddingu nebyl ES dostupný); max 1 h
caffeinate -is $PY scripts/build_v2_index.py push --hours 1 --max-priority "$MAX_PRIORITY" || true
# záloha celé DB včetně vektorů -> data/v2/backups/ (drží poslední 2); BACKUP_DIR=/Volumes/... pro externí disk
caffeinate -is $PY scripts/build_v2_index.py backup ${BACKUP_DIR:+--out "$BACKUP_DIR"}
$PY scripts/build_v2_index.py stats | tee logs/build_v2_stats.txt

echo "== $(date) | ranní srovnání v1 (plný) vs v2 (plný), bez LLM"
V2_INDEX=zakony-v2 $PY eval/run_eval.py --no-ragas \
  --retrievers bm25,hybrid,knn,v2-bm25,v2-knn,v2-hybrid \
  --out "eval/results/full-v1-vs-v2-$(date +%Y%m%d)" || echo "eval selhal (index je i tak hotový)"
if [ "$EMBED_RC" -ne 0 ]; then
  echo "== $(date) | POZOR: embed skončil chybou ($EMBED_RC), viz logs/build_v2.log – stačí skript spustit znovu"
  exit "$EMBED_RC"
fi
echo "== $(date) | hotovo"
