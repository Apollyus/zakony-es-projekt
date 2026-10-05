# MCP Server pro ES Zákony

Tento projekt je MCP (Model Context Protocol) server integrovaný s FastAPI pro prohledávání české legislativy nad Elasticsearch.
Je postaven na architektuře modulů (původně z `mcp-dcuk`), ze které byly odstraněny specifické moduly a přidán modul `ESZakony`.

## Funkce
Server poskytuje LLM modelům následující nástroje (tools):
- `search_laws_text`: Klasické BM25 textové vyhledávání nad paragrafy a názvy zákonů.
- `search_laws_semantic`: Sémantické (vektorové) vyhledávání (k-NN) nad paragrafy pomocí modelu `sentence-transformers`.
- `get_law_detail`: Získání detailů zákona a všech jeho paragrafů podle jeho ID.

## Spuštění pro vývoj
Server umí běžet buď lokálně nebo na vzdáleném ES (určeno přes proměnné prostředí).

```bash
# 1. Instalace závislostí
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Nastavení proměnných prostředí (volitelné)
# Můžete vytvořit soubor .env, např.:
# ES_REMOTE_URL=http://your-remote-server:9200
# EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-mpnet-base-v2

# 3. Spuštění serveru
python main.py
```

Server se spustí na portu 8000 a poskytne Swagger UI na http://localhost:8000/docs.

## Připojení klienta (např. Claude Desktop)
Do konfigurace MCP klienta přidáte:
```json
{
  "mcpServers": {
    "es-zakony": {
      "command": "python",
      "args": ["/absolutni/cesta/k/mcp-server/main.py"],
      "env": {
        "ES_REMOTE_URL": "http://your-remote-server:9200"
      }
    }
  }
}
```
Při startu přes standardní MCP (např. Claude Desktop) sice FastMCP běží přes stdio (standardní chování FastMCP), 
ale uvicorn (FastAPI) poběží na pozadí, což je užitečné pro debuggování a SSE konektivitu.
