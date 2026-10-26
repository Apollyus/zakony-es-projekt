# Provoz: nasazení, aktualizace, kontrola, rollback

Provozní příručka pro index `zakony-v2`, jeho průběžnou aktualizaci (`scripts/update_v2.py`) a MCP server.
Jak kód funguje uvnitř: [`ARCHITEKTURA.md`](ARCHITEKTURA.md). Návrh indexu a výsledky: [`INDEX_V2.md`](INDEX_V2.md).

## Přehled: co kde běží

| komponenta | kde | poznámka |
|---|---|---|
| Elasticsearch 8.15 | `192.168.27.60:9200` (Docker, SSH `dcuk-vps`, data `/home/faltynek/.es-data`) | jen ve VPN; 4,1 GB RAM, index `zakony-v2` |
| lokální DB `data/v2/v2.db` | stroj, kde běží build/aktualizace (teď Mac) | **zdroj pravdy**: texty, § jednotky, vektory (fp16); ES jde z ní kdykoli znovu naplnit |
| build / aktualizace | totéž | potřebuje GPU (Apple Silicon `mps` nebo NVIDIA `cuda`), VPN a internet |
| MCP server | lokálně u klienta (`run_mcp.sh`, stdio), případně hostovaně (HTTP) | potřebuje model bge-m3 (CPU stačí) a VPN |

## Co je potřeba (stroj pro build a aktualizaci)

- **Síť:** VPN do `192.168.27.60` (ES) a internet (`opendata.eselpoint.gov.cz` pro dumpy, `huggingface.co` pro první stažení modelu).
- **GPU:** Apple Silicon (MPS) nebo NVIDIA (CUDA). Embedding na CPU skripty odmítnou (je řádově pomalejší).
- **Paměť:** embedding ve fp16 si na GPU vezme ~3 GB (pevné tvary dávek), proces s DB ~1 GB.
- **Disk (~20 GB):**

  | | velikost |
  |---|---|
  | `data/v2/v2.db` | ~5 GB (+ ~0,6 GB po první aktualizaci – tabulka `vec_by_hash`) |
  | `data/v2/backups/` | 2 × ~5 GB (drží poslední 2 automatické zálohy) |
  | dumpy `001–004*.json.gz` | ~1,9 GB |
  | model `BAAI/bge-m3` v `~/.cache/huggingface` | ~2,3 GB |

- **Python:** kořenové `.venv` (Python 3.9+, `pip install -r requirements.txt`; build, aktualizace, eval)
  a `mcp-server/.venv` (Python 3.12, `pip install -r mcp-server/requirements.txt`). Testy navíc `pytest`.
- **Konfigurace:** `.env` v kořeni repa (gitignorovaný):

  ```
  ES_REMOTE_URL=http://192.168.27.60:9200
  EVAL_LLM_BASE_URL=http://192.168.26.215:4000/v1     # jen pro RAGAS eval
  EVAL_LLM_API_KEY=...
  EVAL_LLM_MODEL=Qwen3.5-35B-A3B
  ```

- **DB:** `data/v2/v2.db` z plného buildu. Na novém stroji buď zkopírovat ze stávajícího (nebo ze zálohy),
  nebo postavit znovu (`bash scripts/run_v2_overnight.sh`, ~4 h).

### Proměnné prostředí

| proměnná | výchozí | k čemu |
|---|---|---|
| `ES_REMOTE_URL` | `http://192.168.27.60:9200` | adresa ES (build, aktualizace, eval, MCP) |
| `V2_INDEX` | `zakony-v2` | index, do kterého build/aktualizace zapisují (a který čtou retrievery `v2-*` v evalu) |
| `V2_DUMP_DIR` | kořen repa | kde leží dumpy 001–004 |
| `GPU_MEM_LIMIT_GB` | `12` | nad tímto limitem paměti MPS se embedding restartuje (kód 75) |
| `ES_INDEX` | `zakony-v2` | index pro MCP server |
| `V2_STALE_PENALTY` | `0.94` | znevýhodnění nejistých/starých předpisů v MCP (1.0 = vypnuto) |
| `HF_HUB_OFFLINE` | nastaví `run_mcp.sh` | offline načtení modelu, jen pokud jsou v cache celé váhy |

## První ostrý běh aktualizace

Předpoklad: zapnutá VPN, aktuální `main`, hotový plný build (`data/v2/v2.db`).

```bash
cd zakony-es-projekt && git pull

# 1) záloha DB (vektory i stav indexu) – nejlépe mimo Mac
.venv/bin/python scripts/build_v2_index.py backup --out /Volumes/Zaloha

# 2) čerstvé dumpy + plán změn (DB ani ES se nemění)
.venv/bin/python scripts/update_v2.py --download --dry-run
#    stav 26. 10. 2026: ~19 nových, ~72 změněných (z toho 62 jen poprvé – sjednocení výběru
#    mezi zněními se stejným datem účinnosti), 0 zrušených

# 3) aktualizace (dumpy už jsou stažené)
.venv/bin/python scripts/update_v2.py
echo $?          # 0 = hotovo; 3 / 75 viz „Co dělat při chybách“
```

Při prvním běhu se navíc jednorázově naplní tabulka `vec_by_hash` z existujících vektorů (~1–2 min)
a zapíše se čas poslední změny znění u všech předpisů. Celý běh trvá jednotky minut (čtení 003/004 ~3 min,
embedding jen nových textů).

## Kontrola po aktualizaci

```bash
# souhrn: co se změnilo, kolik vektorů převzato / embedováno, jestli je ES dorovnaný
cat logs/update_v2_$(date +%F).json
tail -50 logs/update_v2.log

# DB vs ES: počet chunků k indexaci (priorita <= 5) musí sedět s počtem dokumentů v ES
sqlite3 data/v2/v2.db "SELECT COUNT(*) FROM units WHERE priority <= 5"
curl -s http://192.168.27.60:9200/zakony-v2/_count
sqlite3 data/v2/v2.db "SELECT COUNT(*) FROM es_pending_delete"        # musí být 0
.venv/bin/python scripts/build_v2_index.py stats

# kvalita vyhledávání se nezhoršila (bez LLM, ~5 min; po #2 na ruční sadě hit 0,80 / MRR 0,80)
ES_INDEX=zakony-v2 V2_INDEX=zakony-v2 .venv/bin/python eval/run_eval.py --no-ragas \
  --retrievers bm25,hybrid,v2-knn-f4 --out eval/results/po-aktualizaci-$(date +%F)

# MCP vidí nový předpis
cd mcp-server && ES_REMOTE_URL=http://192.168.27.60:9200 .venv/bin/python -c "
import modules.ESZakony.main as m
class F:
    t = {}
    def tool(s, *a, **k): return lambda f: F.t.setdefault(f.__name__, f)
m.register(F()); d = F.t['get_law_detail']('181/2026 Sb.'); print(d.get('akt_nazev'), len(d.get('paragrafy', [])))"
```

## Rollback

Lokální DB je zdroj pravdy a ES se z ní dá kdykoli znovu naplnit **bez embeddingu** (~170 dokumentů/s,
celý index ~30 min). Rollback = vrátit DB ze zálohy a z ní znovu naplnit ES.

**Bez výpadku (doporučeno):** obnovit do nového indexu a přepnout MCP.

```bash
cp data/v2/v2.db data/v2/v2.db.pokazena                      # pro rozbor
cp /Volumes/Zaloha/v2-YYYYMMDD.db data/v2/v2.db              # záloha z kroku 1
V2_INDEX=zakony-v2-rollback .venv/bin/python scripts/build_v2_index.py push    # index neexistuje → pošle vše
# ověřit (kontroly výše s V2_INDEX/ES_INDEX=zakony-v2-rollback), pak v mcp-server/run_mcp.sh
# ES_INDEX=zakony-v2-rollback, restartovat klienty a starý index smazat
```

**Rychle, s výpadkem ~30 min:** `push --recreate` smaže index a naplní ho znovu ze zálohy.

```bash
cp /Volumes/Zaloha/v2-YYYYMMDD.db data/v2/v2.db
.venv/bin/python scripts/build_v2_index.py push --recreate
```

Rollback jen jednoho předpisu zatím není – buď celá DB, nebo počkat na opravu v datech a další aktualizaci.

> **Index v1 už obnovit nejde.** `zakony-esbirka-2026` byl 19. 10. 2026 smazán z ES a lokální
> `data/esbirka_checkpoint.db` (data a vektory v1) na disku už není (zmizel při úklidu disku do 26. 10.).
> Návrat k v1 by znamenal celou starou pipeline od nuly.

## Co dělat při chybách

| projev | příčina | co udělat |
|---|---|---|
| `update_v2.py` skončí kódem **3** | ES nedostupný (VPN, server) – staré znění zůstalo v indexu | zapnout VPN / ověřit ES, spustit znovu; doposílá a dosmaže (`es_pending_delete`) |
| kód **75** | došla paměť GPU při embeddingu | spustit znovu; spočítané vektory se převezmou (`vec_by_hash`) |
| „Jiná aktualizace už běží (data/v2/update.lock)“ | běží jiný proces aktualizace | počkat; zámek se uvolní sám s koncem procesu (soubor zůstává, to je v pořádku) |
| `nekompletní stažení` | přerušené stahování dumpu | spustit znovu s `--download`; rozpracovaný `.part` se přepíše, starý dump zůstal netknutý |
| „Embedding běží jen na GPU“ | stroj bez MPS/CUDA | spustit na stroji s GPU |
| „Chybí data/v2/v2.db“ / „DB nemá dokončený extract“ | chybí plný build | zkopírovat DB ze zálohy, nebo `bash scripts/run_v2_overnight.sh` |
| „cílové datum … je dřív než současné“ (varování) | `--date` do minulosti | v pořádku, pokud je to záměr – index se vrátí ke starším zněním |
| `DB je postavená pro datum …` (build) | `build_v2_index.py extract` s jiným datem | pro nové datum použít `update_v2.py`; `extract --force` jen pro plný rebuild |
| po aktualizaci v ES méně/více dokumentů než v DB | nedokončený běh nebo zásah do indexu zvenku | `update_v2.py` (dokončí čekající), případně `build_v2_index.py push --all` |
| MCP visí, vrací starý index, chyby připojení | cache modelu, `ES_INDEX`, VPN | [`mcp-server/README.md` → Řešení problémů](../mcp-server/README.md#řešení-problémů) |

Logy: `logs/update_v2.log`, `logs/update_v2_<datum>.json`, `logs/build_v2.log`; historie běhů v tabulce
`update_log` (`sqlite3 data/v2/v2.db "SELECT ts, date FROM update_log"`).

## Pravidelný provoz

- **Aktualizace:** zatím ručně (`update_v2.py --download`), ideálně denně ráno – dumpy se na opendata
  přegenerují v noci (~02–05 h UTC). Plánovač (launchd / cron na stroji ve VPN), notifikace a eval jako
  automatická kontrola jsou fáze 2.
- **Zálohy:** `build_v2_index.py backup --out <externí disk>` aspoň po každé aktualizaci, která změnila hodně
  předpisů (typicky kolem 1. 1. a 1. 7.).
- **Plný rebuild:** jen při změně logiky § jednotek nebo mappingu – [`INDEX_V2.md`](INDEX_V2.md#aktualizace-na-nová-znění-rebuild).
- **Server ES:** po smazání v1 ~1,6 GB dostupné RAM; volitelné snížení heapu viz README.
