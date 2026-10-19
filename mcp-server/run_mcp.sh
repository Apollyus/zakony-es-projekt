#!/bin/bash
cd /Users/vojta-macbook/aaa_programovani/zakony-es-projekt/mcp-server
if [ -z "$ES_REMOTE_URL" ]; then
    export ES_REMOTE_URL="http://192.168.27.60:9200"
fi
# index v2 (jeden dokument = jeden § aktuálního znění, bge-m3); původní: ES_INDEX=zakony-esbirka-2026
export ES_INDEX="${ES_INDEX:-zakony-v2}"
# Model bge-m3 je v cache → nekontrolovat Hugging Face při každém startu (~25 HTTP požadavků;
# bez internetu, např. jen ve VPN, by start visel). Bez cache se model normálně stáhne.
HF_CACHE="${HF_HOME:-$HOME/.cache/huggingface}/hub/models--BAAI--bge-m3"
if [ -z "$HF_HUB_OFFLINE" ] && [ -d "$HF_CACHE/snapshots" ]; then
    export HF_HUB_OFFLINE=1
fi
exec .venv/bin/fastmcp run main.py:mcp
