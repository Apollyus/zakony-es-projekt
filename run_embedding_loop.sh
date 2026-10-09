#!/bin/bash

echo "Spouštím nekonečnou smyčku pro import zákonů."
echo "Pokud se proces přeruší (např. kvůli zavření notebooku a ztrátě sítě), automaticky se za 5 vteřin restartuje a naváže z databáze."
echo "Pro úplné ukončení stiskni CTRL+C dvakrát za sebou."

while true; do
    echo "----------------------------------------"
    echo "Spouštím embed_and_push.py..."
    .venv/bin/python scripts/embed_and_push.py
    
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 0 ]; then
        echo "Skript úspěšně doběhl do úplného konce!"
        break
    fi
    
    echo "Skript spadl nebo byl přerušen (Exit code: $EXIT_CODE). Restartuji za 5 vteřin..."
    sleep 5
done
