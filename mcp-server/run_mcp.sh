#!/bin/bash
cd /Users/vojta-macbook/aaa_programovani/zakony-es-projekt/mcp-server
if [ -z "$ES_REMOTE_URL" ]; then
    export ES_REMOTE_URL="http://192.168.27.60:9200"
fi
# index v2 (jeden dokument = jeden § aktuálního znění, bge-m3); původní: ES_INDEX=zakony-esbirka-2026
export ES_INDEX="${ES_INDEX:-zakony-v2}"
exec .venv/bin/fastmcp run main.py:mcp
