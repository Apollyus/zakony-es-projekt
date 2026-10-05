#!/bin/bash
cd /Users/vojta-macbook/aaa_programovani/zakony-es-projekt/mcp-server
if [ -z "$ES_REMOTE_URL" ]; then
    export ES_REMOTE_URL="http://192.168.27.60:9200"
fi
exec .venv/bin/fastmcp run main.py:mcp
