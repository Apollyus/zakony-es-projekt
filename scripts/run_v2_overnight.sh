#!/usr/bin/env bash
# Noční build indexu v2 (zakony-v2). Bezpečné spustit znovu – hotové fáze/chunky se přeskočí.
#
#   bash scripts/run_v2_overnight.sh            # embedding max 10 h, priority 0–5 (bez mezinárodních smluv)
#   HOURS=7 MAX_PRIORITY=3 bash scripts/run_v2_overnight.sh   # jen platné předpisy
#   MAX_PRIORITY=9 bash scripts/run_v2_overnight.sh           # i mezinárodní smlouvy (Sb. m. s., +87 tis. chunků)
#   BACKUP_DIR=/Volumes/Zaloha bash scripts/run_v2_overnight.sh  # záloha DB na externí disk
#
# Průběh:  tail -f logs/build_v2.log      Stav:  .venv/bin/python scripts/build_v2_index.py stats
# Zastavení: Ctrl+C – rozdělaná dávka se dokončí a uloží; další spuštění naváže.
set -uo pipefail
cd "$(dirname "$0")/.."

HOURS="${HOURS:-10}"
MAX_PRIORITY="${MAX_PRIORITY:-5}"
DATE="${DATE:-$(date +%F)}"
MAX_RESTARTS="${MAX_RESTARTS:-100}"
PY=.venv/bin/python
B=scripts/build_v2_index.py
mkdir -p logs

STOPPED=0
trap 'STOPPED=1' INT TERM

echo "== $(date) | build v2: datum znění $DATE, limit ${HOURS} h, priority <= $MAX_PRIORITY"
# -i: nespát při nečinnosti, -s: nespát na síťovém napájení. NEZAVÍRAT víko – MacBook se uspí i s caffeinate.
caffeinate -is $PY $B extract --date "$DATE" || exit 1
caffeinate -is $PY $B units || exit 1

# Embedding běží jen na GPU, lokálně; každá dávka se nejdřív uloží do SQLite (záloha vektorů), pak jde do ES.
# Kód 75 = paměť GPU zaplněná → nový proces (čistá paměť GPU) naváže, kde předchozí skončil.
END=$(python3 -c "import time; print(int(time.time() + float('$HOURS') * 3600))")
RESTARTS=0
while :; do
  LEFT_H=$(python3 -c "print(max(0.0, ($END - $(date +%s)) / 3600))")
  caffeinate -is $PY $B embed --hours "$LEFT_H" --max-priority "$MAX_PRIORITY"
  RC=$?
  if [ "$RC" -eq 75 ] && [ "$STOPPED" -eq 0 ] && [ "$RESTARTS" -lt "$MAX_RESTARTS" ]; then
    RESTARTS=$((RESTARTS + 1))
    echo "== $(date) | restart embeddingu kvůli paměti GPU (#$RESTARTS)"
    sleep 5
    continue
  fi
  break
done

# dorovnání do ES (kdyby na konci nebyl ES dostupný) a záloha DB včetně vektorů -> data/v2/backups/
caffeinate -is $PY $B push --hours 1 --max-priority "$MAX_PRIORITY" || true
caffeinate -is $PY $B backup ${BACKUP_DIR:+--out "$BACKUP_DIR"} || echo "POZOR: záloha selhala"
$PY $B stats | tee logs/build_v2_stats.txt

if [ "$RC" -ne 0 ] || [ "$STOPPED" -ne 0 ]; then
  echo "== $(date) | NEDOKONČENO (kód embed $RC, restartů $RESTARTS) – stav viz tabulka výše."
  echo "   Index zakony-v2 je zatím neúplný, eval se nespouští. Pro pokračování spusť skript znovu."
  exit 3
fi

echo "== $(date) | embedding hotový (restartů $RESTARTS) → ranní srovnání v1 vs v2 (bez LLM)"
V2_INDEX=zakony-v2 $PY eval/run_eval.py --no-ragas \
  --retrievers bm25,hybrid,knn,v2-bm25,v2-knn,v2-hybrid \
  --out "eval/results/full-v1-vs-v2-$(date +%Y%m%d)" || echo "eval selhal (index je i tak hotový)"
echo "== $(date) | HOTOVO – index zakony-v2 je kompletní"
