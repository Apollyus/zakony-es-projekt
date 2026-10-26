# MCP Server pro ES Zákony

Tento projekt je MCP (Model Context Protocol) server integrovaný s FastAPI pro prohledávání české legislativy nad Elasticsearch.
Je postaven na architektuře modulů (původně z `mcp-dcuk`), ze které byly odstraněny specifické moduly a přidán modul `ESZakony`.

## Funkce
Server poskytuje LLM modelům následující nástroje (tools):
- `search_laws_text`: Klasické BM25 textové vyhledávání nad paragrafy (ve v2 nad nadpisem, cestou a textem §).
- `search_laws_semantic`: Sémantické (vektorové) vyhledávání (kNN) nad paragrafy; ve v2 model `BAAI/bge-m3`.
- `get_law_detail`: Získání detailů zákona a všech jeho paragrafů podle ID (ve v2 i podle citace, např. `40/2009 Sb.`).

## Index v1 / v2
Backend se volí proměnnou `ES_INDEX` (rozhraní nástrojů je v obou případech stejné):

| `ES_INDEX` | schéma | vyhledávání | embedding |
|---|---|---|---|
| `zakony-esbirka-2026` (index **smazán** 19. 10. 2026, jen pro obnovu) | v1: zákon s nested fragmenty | BM25 / kNN + BM25 | `paraphrase-multilingual-mpnet-base-v2` |
| `zakony-v2` (**výchozí**) | v2: jeden dokument = jeden § aktuálního znění | BM25 přes nadpis+cestu+text / čisté kNN | `BAAI/bge-m3` |

Ve v2 má každý paragraf ve výsledku `citace` jako „§ 140 Vražda“ a navíc `cesta` (část › hlava › díl);
`get_law_detail` přijímá IRI (`esel-esb:eli/cz/sb/2009/40`) i citaci (`40/2009 Sb.`).
Schéma lze vynutit `ES_INDEX_SCHEMA=v1|v2`. Podrobnosti: `../docs/INDEX_V2.md`.

Ve v2 se výsledky přeřazují: přetisky „úplné znění“ se vyřadí, předpisy se statusem `neznamy` (jen vyhlášené
znění, platnost neověřena) nebo z doby před 1945 mají skóre ×0,94 (`V2_STALE_PENALTY`, 1.0 = vypnuto).
U každého předpisu ve výsledku je `status`.

Testy (mockovaný ES, bez sítě a modelu): `.venv/bin/python -m pytest -q`

## Řešení problémů

| projev | příčina | co udělat |
|---|---|---|
| první dotaz visí / klient (opencode) nic nevrátí | model bge-m3 v cache chybí nebo je poškozený (např. po úklidu disku) | zkontrolovat `du -sh ~/.cache/huggingface/hub/models--BAAI--bge-m3` – musí mít ~2,3 GB; jinak `rm -rf` té složky a spustit server znovu (stáhne se sám, potřebuje internet) |
| `Connection refused` / timeout na `192.168.27.60:9200` | není zapnutá VPN, nebo neběží ES | zapnout VPN; `curl http://192.168.27.60:9200/_cluster/health` |
| výsledky ze starého indexu | `ES_INDEX` nastavený jinde (klient, `.env`) | v logu při startu musí být `ESZakony: index zakony-v2, schéma v2` |
| opencode „Insufficient balance“ | došel kredit modelu, ne chyba MCP | jiný model (`opencode run --model …`) nebo dobít kredit |

`opencode mcp list` ukáže stav připojení (`✓ es-zakony connected`); první výpis po startu služby opencode může hlásit
„No MCP servers configured“, než se služba rozběhne – stačí zopakovat.

Výchozí index je `zakony-v2` (v kódu i v `run_mcp.sh`); při startu se do logu vypíše `ESZakony: index …, schéma …`.
`run_mcp.sh` navíc zapne `HF_HUB_OFFLINE=1`, pokud je model v cache – start pak nekontaktuje Hugging Face.

## Spuštění pro vývoj
Server umí běžet buď lokálně nebo na vzdáleném ES (určeno přes proměnné prostředí).

```bash
# 1. Instalace závislostí
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Nastavení proměnných prostředí (volitelné)
# Můžete vytvořit soubor .env, např.:
# ES_REMOTE_URL=http://192.168.27.60:9200   # výchozí
# ES_INDEX=zakony-v2            # výchozí, není třeba nastavovat
# EMBEDDING_MODEL=...           # výchozí podle schématu indexu

# 3. Spuštění serveru (run_mcp.sh nastaví ES_REMOTE_URL i ES_INDEX=zakony-v2)
./run_mcp.sh
# nebo ručně:
ES_INDEX=zakony-v2 python main.py
```

Server se spustí na portu 8000 a poskytne Swagger UI na http://localhost:8000/docs.

## Připojení klienta (např. Claude Desktop)
Do konfigurace MCP klienta přidáte:
```json
{
  "mcpServers": {
    "es-zakony": {
      "command": "/absolutni/cesta/k/mcp-server/run_mcp.sh"
    }
  }
}
```
nebo s explicitními proměnnými:
```json
{
  "mcpServers": {
    "es-zakony": {
      "command": "/absolutni/cesta/k/mcp-server/.venv/bin/python",
      "args": ["/absolutni/cesta/k/mcp-server/main.py"],
      "env": {
        "ES_REMOTE_URL": "http://192.168.27.60:9200",
        "ES_INDEX": "zakony-v2"
      }
    }
  }
}
```
První dotaz trvá déle (~4 s), stahuje/načítá se model bge-m3 (~2,3 GB); další dotazy ~0,2 s.
Při startu přes standardní MCP (např. Claude Desktop) sice FastMCP běží přes stdio (standardní chování FastMCP), 
ale uvicorn (FastAPI) poběží na pozadí, což je užitečné pro debuggování a SSE konektivitu.
