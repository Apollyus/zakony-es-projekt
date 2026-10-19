# MCP Server pro ES Zákony

Tento projekt je MCP (Model Context Protocol) server integrovaný s FastAPI pro prohledávání české legislativy nad Elasticsearch.
Je postaven na architektuře modulů (původně z `mcp-dcuk`), ze které byly odstraněny specifické moduly a přidán modul `ESZakony`.

## Funkce
Server poskytuje LLM modelům následující nástroje (tools):
- `search_laws_text`: Klasické BM25 textové vyhledávání nad paragrafy a názvy zákonů.
- `search_laws_semantic`: Sémantické (vektorové) vyhledávání (k-NN) nad paragrafy pomocí modelu `sentence-transformers`.
- `get_law_detail`: Získání detailů zákona a všech jeho paragrafů podle jeho ID.

## Index v1 / v2
Backend se volí proměnnou `ES_INDEX` (rozhraní nástrojů je v obou případech stejné):

| `ES_INDEX` | schéma | vyhledávání | embedding |
|---|---|---|---|
| `zakony-esbirka-2026` (výchozí) | v1: zákon s nested fragmenty | BM25 / kNN + BM25 | `paraphrase-multilingual-mpnet-base-v2` |
| `zakony-v2` (**aktuálně nastaveno v `run_mcp.sh`**) | v2: jeden dokument = jeden § aktuálního znění | BM25 přes nadpis+cestu+text / čisté kNN | `BAAI/bge-m3` |

Ve v2 má každý paragraf ve výsledku `citace` jako „§ 140 Vražda“ a navíc `cesta` (část › hlava › díl);
`get_law_detail` přijímá IRI (`esel-esb:eli/cz/sb/2009/40`) i citaci (`40/2009 Sb.`).
Schéma lze vynutit `ES_INDEX_SCHEMA=v1|v2`. Podrobnosti: `../docs/INDEX_V2.md`.

Ve v2 se výsledky přeřazují: přetisky „úplné znění“ se vyřadí, předpisy se statusem `neznamy` (jen vyhlášené
znění, platnost neověřena) nebo z doby před 1945 mají skóre ×0,94 (`V2_STALE_PENALTY`, 1.0 = vypnuto).
U každého předpisu ve výsledku je `status`.

Testy (mockovaný ES): `.venv/bin/python -m pytest -q`

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
# ES_INDEX=zakony-v2            # nový index (run_mcp.sh ho nastavuje sám; bez něj se použije zakony-esbirka-2026)
# EMBEDDING_MODEL=...           # výchozí podle schématu indexu

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
