# Zákony ES Projekt (Architektura a Workflow 2026)

Tento projekt slouží k prohledávání české legislativy (e-Sbírka) pomocí **Elasticsearchu** a zpřístupňuje ho
LLM modelům přes **MCP server**. Nabízí textové vyhledávání (BM25) a sémantické vyhledávání nad vektory
(model `BAAI/bge-m3`), které chápe význam dotazu.

Aktuální index `zakony-v2` obsahuje **jen platná znění** 22 301 předpisů (303 795 § jednotek, 2,15 GB) – z dumpů
e-Sbírky ze **5. 10. 2026**, znění účinná k **19. 10. 2026** (novely publikované po 5. 10. v něm nejsou; aktualizace viz
[`docs/INDEX_V2.md`](docs/INDEX_V2.md#aktualizace-na-nová-znění-rebuild)). Embedding běží
lokálně na GPU Apple Silicon, ES server má 4,1 GB RAM (heap 2,15 GB) – viz [`docs/INDEX_V2.md`](docs/INDEX_V2.md).

---

## ⭐ Aktuální stav (říjen 2026): přechod na index v2

Evaluace (RAGAS + deterministické metriky, `eval/`) ukázala, že původní index `zakony-esbirka-2026`
na realistických otázkách skoro nenachází správná ustanovení (správný § v top 5 u 13 % otázek).
Příčiny: text rozsekaný na fragmenty bez § a nadpisu, všechna historická znění a zrušené předpisy
v jednom indexu, slabý embedding model ořezávající na 128 tokenů a chyba v deduplikaci MCP hybridu.

Nový **index v2 (`zakony-v2`)**: jeden dokument = jeden § aktuálního znění s nadpisem a cestou v předpisu,
model `BAAI/bge-m3`, sémantické hledání čistým kNN. Plný index (303 795 chunků) je hotový: na realistických otázkách
správný § v top 5 u 80 % (v1: 0 %), správný předpis u 100 %.

| | Dokument |
|---|---|
| Návrh, důvody, schéma, build, přepnutí MCP | [`docs/INDEX_V2.md`](docs/INDEX_V2.md) |
| Evaluace a metriky | [`eval/README.md`](eval/README.md) |

**Noční build:**
```bash
bash scripts/run_v2_overnight.sh          # ~304 tis. chunků (bez mezinár. smluv), odhad 3–5 h
tail -f logs/build_v2.log                 # průběh
.venv/bin/python scripts/build_v2_index.py stats
```

**Průběžná aktualizace na nová znění** (bez rebuildu; vektory nezměněných § se převezmou):
```bash
.venv/bin/python scripts/update_v2.py --download --dry-run   # jen plán změn
.venv/bin/python scripts/update_v2.py --download             # stáhnout dumpy a aktualizovat na dnešek
```

**MCP server běží nad v2:** `mcp-server/run_mcp.sh` nastavuje `ES_INDEX=zakony-v2` (rozhraní nástrojů se nemění;
index v1 byl 19. 10. 2026 smazán).

Níže je popis původní pipeline v1 (`zakony-esbirka-2026`) – jen pro historii. **Index v1 byl 19. 10. 2026 smazán**
(24 GB; s v2 se nevešel do RAM serveru). Jeho data včetně vektorů jsou pořád v lokální `data/esbirka_checkpoint.db`
(111 GB) a `scripts/push_to_es.py` by ho uměl znovu nahrát (hodiny).

---

## 🖥️ Doporučená konfigurace serveru a hostovaný MCP

**Kdo potřebuje embedding model:** ES ne – ukládá hotové vektory a dělá jen kNN. Model `BAAI/bge-m3` potřebuje
ten, kdo převádí **dotaz** na vektor, tedy MCP server (lokálně tvůj Mac, u hostované varianty stroj, kde MCP běží).
Vektor dotazu musí být ze **stejného modelu** jako index.

### Současný ES server (`192.168.27.60`)

4,1 GB RAM (heap ES 2,15 GB, reálně využitý ~0,3 GB), 4 jádra, Ubuntu 20.04, ES v Dockeru (`docker run`, data
v `/home/faltynek/.es-data`, SSH alias `dcuk-vps`). Po smazání v1 (19. 10. 2026): jen index `zakony-v2`, cluster `green`,
~1,6 GB dostupné RAM, ~51 GB volného disku. **ES + MCP s modelem se sem nevejdou** (~4,05 GB potřeba na 4,1 GB
i se sníženým heapem, bez cache pro index).

### Doporučení

| varianta | RAM | CPU | volný disk | poznámka |
|---|---|---|---|---|
| **ES + MCP s modelem na jednom stroji** | **8 GB** | 4 jádra | ≥ 20 GB | ES heap 1–2 GB, MCP s bge-m3 ~2,2 GB, cache pro index ≥ 1 GB, systém ~0,5 GB; disk na index, druhý index při rebuildu, model a Docker obraz |
| jen ES (jako teď) | 4 GB | 2–4 jádra | ≥ 10 GB | stačí i současný heap; snížení na 1–1,5 GB přidá cache (viz níže) |
| jen MCP s modelem (samostatný stroj ve VPN) | ≥ 4 GB | 2–4 jádra | ~6 GB | model 2,3 GB + Python s torch (CPU) |
| MCP bez modelu (embedding jako služba) | ~0,5 GB | 1 jádro | ~1 GB | jen pokud LLM proxy za VPN začne nabízet `/v1/embeddings` s bge-m3 |

Změřeno (19. 10. 2026): MCP proces s bge-m3 na CPU **2,15 GB RAM**, ~43 ms na dotaz (CPU Macu, 4 vlákna; na serveru
odhadem stovky ms), index `zakony-v2` **2,15 GB** disku a ~0,35 GB „horkých“ dat, heap ES využitý ~0,3 GB.
Odhady: rezervy, disk pro Docker, rychlost na CPU serveru. Úspornou int8 verzi modelu se nepodařilo ověřit.

### Snížení heapu ES (volitelné)

Heap je nastavený proměnnou `ES_JAVA_OPTS` kontejneru (2 GB) – změna znamená **znovu vytvořit kontejner** se stejnými
parametry. Po smazání v1 to není nutné (v2 potřebuje ~0,35 GB cache, dostupných je ~1,6 GB). Postup na serveru:

```bash
ssh dcuk-vps
docker inspect elasticsearch      # opsat Env (ES_JAVA_OPTS, discovery.type, xpack.*…) a porty
docker stop elasticsearch && docker rename elasticsearch elasticsearch-old
docker run -d --name elasticsearch --restart unless-stopped -p 9200:9200 \
  -v /home/faltynek/.es-data:/usr/share/elasticsearch/data \
  -e <stejné proměnné jako dřív> -e ES_JAVA_OPTS="-Xms1g -Xmx1g" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.0
curl -s http://localhost:9200/_cat/indices        # zakony-v2 musí být zpět, 303 795 dokumentů
docker rm elasticsearch-old                         # až po ověření
```

### Hostovaný MCP

Server umí kromě stdio (`run_mcp.sh`, tak ho teď spouští opencode) i HTTP – ověřeno lokálně:

```bash
cd mcp-server && ES_REMOTE_URL=http://192.168.27.60:9200 ES_INDEX=zakony-v2 .venv/bin/python main.py
# poslouchá na 0.0.0.0:8000: MCP Streamable HTTP na /mcp/, SSE na /sse, Swagger na /docs
```

Klient (např. `opencode.jsonc`):

```jsonc
"es-zakony": { "type": "remote", "url": "http://<host>:8000/mcp/" }
```

Omezení a co dodělat před nasazením:
- stroj musí být **ve VPN** (vidět ES) – veřejný hosting bez vystavení ES nejde,
- **server nemá autentizaci** – kdo dosáhne na port 8000, může hledat; přidat token v hlavičce `Authorization`
  (u veřejného přístupu navíc TLS přes reverse proxy),
- `mcp-server/docker-compose.yml` obsahuje i Postgres, MariaDB, MSSQL, Grafanu a Prometheus z původního `mcp-dcuk` –
  pro tenhle server osekat jen na `fast-mcp`; obraz stavět s torch pro CPU (jinak několik GB CUDA knihoven)
  a model dát do svazku s cache, ať se 2,3 GB nestahují při každém startu,
- první dotaz po startu trvá ~4–5 s (načtení modelu), další desetiny sekundy.

## 🏗️ Architektura v1 (původní index `zakony-esbirka-2026`)

Celý proces (pipeline) je z důvodu bezpečnosti a rychlosti rozdělen do **3 nezávislých kroků**. Abychom zamezili ztrátě dat při pádu (nebo uspání) počítače, všechny mezivýpočty se ukládají do robustní lokální databáze `SQLite`.

### 1. Krok: Surový import (Lokální DB)
Zdrojová data z Ministerstva (gigabajtové e-Sbírka dumpy `*.json.gz`) se nejprve rozparsují a uloží do obyčejné textové SQLite databáze. 
- Databáze drží informace o Zákonech (metadata, název, rok) a v relaci k nim i jednotlivé Fragmenty/Paragrafy.
- **Skript:** `scripts/import_esbirka.py`

### 2. Krok: Výpočet AI Vektorů (GPU Apple Silicon)
Nejtěžší část práce. Skript prochází databázi paragraf po paragrafu a každý kousek textu (který je delší než 5 znaků) prožene přes AI model. 
- Využívá hardwarovou akceleraci pro Mac (tzv. `MPS` neboli Metal Performance Shaders), čímž běží grafika naplno.
- Použitý AI model: `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`
- Každý paragraf dostane matematický "vektor" o délce **768 dimenzí**, který popisuje jeho význam. Výsledek se ukládá zpět do stejné SQLite databáze (do sloupce `vektor` ve formátu JSON). 
- Pokud se počítač uspí, skript se při dalším spuštění okamžitě chytne tam, kde skončil, díky unikátnímu SQL indexu.
- **Skript:** `scripts/embed_and_push.py` (spouštěno přes `run_embedding_loop.sh` pro auto-restart)

### 3. Krok: Finální synchronizace do Elasticsearch
Jakmile je databáze kompletní (velikost může dosahovat přes 100 GB kvůli textovému JSON formátu čísel), data se plynule přesouvají po síti do produkčního serveru Elasticsearch.
- Odesílání probíhá paralelně ve **2 vláknech** s dávkami po **50 paragrafech** a HTTP timeoutem **5 minut**. Tím šetříme 1GB paměť v Dockeru.
- Skript si sám zakládá správné struktury (tzv. Mappings) v cílovém indexu, než začne nahrávat.
- **Skript:** `scripts/push_to_es.py`

---

## 💾 Elasticsearch Mapping a Triky

Data se do ES ukládají pod indexem `zakony-esbirka-2026`. Datová struktura (Mapping) v Elasticsearchu je kritická. 

Každý Zákon tvoří **JEDEN velký dokument**, který má jako vnořené (`nested`) pole seznam všech svých paragrafů.

### Schéma indexu:
```json
{
  "akt_nazev": "text (czech analyzer)",
  "akt_citace": "keyword",
  "rok": "integer",
  "id_zakona": "keyword",
  "paragrafy": [
    {
      "citace": "keyword",
      "text": "text (czech analyzer)",
      "vektor": "dense_vector (768 dimenzí, int8_hnsw)"
    }
  ]
}
```

### Proč se to občas zaseklo a jak je to vyřešeno?

Při vývoji jsme museli překonat masivní hardwarová specifika tvého ES Docker kontejneru (`192.168.27.60`):

1. **Nedostatek JVM Paměti (1 GB RAM)**
   - Algoritmus na sestavování 768-rozměrných HNSW grafů (navigačních map pro bleskové AI hledání) je extrémně náročný.
   - Původní 4 vlákna a dávky po 200 paragrafech zahlcovaly Java paměť, spouštěly nekonečnou Garbage Collection a shazovaly server (timeout). 
   - **Řešení:** Omezeno na 2 vlákna, dávky na 50, nastaven 5 minutový HTTP timeout v `push_to_es.py`. Server tak v klidu postupně přepočítává vektory, aniž by se zahltil.

2. **Bezpečnostní limit na volné místo disku (Disk Watermark)**
   - ES monitoruje celý hostitelský stroj. Pokud na něm zbývá < 10 % volného místa, ES uzamkne celý systém pro zápis a odmítne založit data. 
   - Vzhledem k tomu, že 30 670 zákonů ukrojí klidně dalších 20 GB, a server měl zrovna 4.6 GB volných, ES ihned vyhlásil `RED status`.
   - **Řešení:** Zasahovali jsme přes administrátorské REST API (`/_cluster/settings`) a překryli nativní parametry tak, aby ES ignoroval přeplněný disk až do **99 %**. 

3. **Chyby při dynamickém mapování (ES 8.x)**
   - Nová Python knihovna `elasticsearch>=8.0.0` potichu ignorovala parametr `body` při zakládání indexu. Založila ho prázdný a dynamicky mu vektory přiřadila jako klasický `[float]`, na čemž pak celý index havaroval.
   - **Řešení:** Vyměněno za přímý parametr `mappings={...}`. ES nyní explicitně ví, že sloupec vektor je AI model s 768 dimenzemi.

4. **Index běžel, ale vracel nesmysly (zjištěno evaluací, říjen 2026)**
   - Na otázku „Jaký trest hrozí za vraždu?“ vracel vyhlášku o bažantnicích; správný § v top 5 jen u 13 % otázek.
   - Příčiny: fragmenty bez § a nadpisu („§ 140“ i „Vražda“ jsou samostatné prázdné fragmenty), všechna historická znění
     a zrušené předpisy v jednom indexu (vracel i neplatné právo – zkušební dobu 3 místo 4 měsíců), model ořezávající
     na 128 tokenů, deduplikace v MCP hybridu podle prázdné citace.
   - **Řešení:** index v2 – jeden dokument = jeden § aktuálního znění, bge-m3, kNN. Viz [`docs/INDEX_V2.md`](docs/INDEX_V2.md).

5. **Server má jen 4,1 GB RAM (heap 2,15 GB)**
   - Na kNN je potřeba mít kvantované vektory v page cache (~2 GB volné). v1 jich má ~5 GB → nevejde se ani sám,
     v1 + v2 současně už vůbec ne. Disk (47 GB volných) problém není.
   - **Řešení:** v2 je ~10× menší (~0,35 GB „horkých“ dat), vektory ani `search_text` se neukládají do `_source`,
     mezinárodní smlouvy jsou volitelné. Index v1 i pilot smazány 19. 10. 2026 → ~1,6 GB dostupné RAM, cluster `green`.

6. **Embedding padal na nedostatek paměti GPU (MPS OOM)**
   - Dávky po 32 s nejdelšími chunky na konci fragmentovaly paměť.
   - První plný běh: po 14 min „OOM i pro 1 text“ a všechno přešlo na CPU. Příčina: MPS si pro **každý nový tvar dávky**
     nechává cache a neuvolní ji – paměť driveru rostla ~6 GB/min, ačkoli model potřebuje 1,76 GB.
   - **Řešení:** fp16 (2,6× rychlejší, vektory shodné na cos ≥ 0,9997), **pevné tvary dávek** (8 délkových košů s pevnou
     velikostí dávky → paměť stabilní ~3 GB), **jen GPU**; při překročení limitu se proces restartuje a naváže
     (stav v SQLite). Launcher po přerušení hlásí NEDOKONČENO, ne „hotovo“.

7. **Vektory v2 existovaly jen v ES**
   - Ztráta indexu by znamenala hodiny embeddingu znovu.
   - **Řešení:** každý vektor se nejdřív uloží lokálně do `data/v2/v2.db` (fp16, celkem ~0,6 GB), teprve pak jde do ES;
     `build_v2_index.py push` index obnoví bez embeddingu (~30 min), `backup` dělá kopii DB. Výpadek VPN/ES už embedding nezastaví.

8. **Po týdnu MCP visel při prvním dotazu**
   - Úklid disku na Macu promazal cache Hugging Face: soubor s vahami bge-m3 zůstal prázdný (0 B) a `run_mcp.sh`
     podle existence složky zapnul offline režim → model se nedal načíst ani stáhnout.
   - **Řešení:** offline režim jen při celých vahách (> 1 GB, test `mcp-server/tests/test_run_mcp_sh.py`);
     návod v [`mcp-server/README.md`](mcp-server/README.md#řešení-problémů).

---

## 🚀 Jak používat a spravovat (Cheat Sheet)

Všechny zdrojové balíčky dat (`.json.gz`) nepatří do verzovacího systému a jsou ošetřeny v `.gitignore`.

### Index v2 (aktuální)

```bash
bash scripts/run_v2_overnight.sh                        # build / pokračování buildu (obnovitelné)
.venv/bin/python scripts/build_v2_index.py stats        # stav: chunky, embedding, v ES, zálohy
.venv/bin/python scripts/build_v2_index.py push         # dorovnat do ES / obnovit smazaný index z lokálních vektorů
.venv/bin/python scripts/build_v2_index.py push --recreate   # smazat a znovu naplnit index (bez embeddingu, ~30 min)
.venv/bin/python scripts/build_v2_index.py backup --out /Volumes/Zaloha   # záloha DB včetně vektorů
.venv/bin/python scripts/update_v2.py --download         # průběžná aktualizace na dnešní znění
bash mcp-server/run_mcp.sh                              # MCP server nad zakony-v2
```

Nové datum znění (rebuild): `extract` s jiným datem bez `--force` odmítne zahodit hotové vektory – postup viz
[`docs/INDEX_V2.md`](docs/INDEX_V2.md#další-kroky).

### Index v1 (původní, MCP ho nepoužívá)

**Založení nebo přehrání všech dat z nuly:**
1. Aktivace prostředí: `source .venv/bin/activate`
2. Vymazání indexu na ES (pokud chceš čistý štít):
   ```bash
   curl -X DELETE "http://192.168.27.60:9200/zakony-esbirka-2026"
   ```
3. Odeslání lokálních vektorů do sítě:
   ```bash
   python scripts/push_to_es.py
   ```

**Průběžná práce na MCP serveru a UI:**
MCP server je v `mcp-server/` (moduly z `mcp-dcuk`, modul `ESZakony`); nástroje, přepínání v1/v2 a konfigurace
klienta viz [`mcp-server/README.md`](mcp-server/README.md).

## 🧪 Testy

Testy běží bez sítě, GPU i modelu (mockovaný Elasticsearch a model):

```bash
.venv/bin/pip install pytest                                   # jednorázově (není v requirements.txt)
.venv/bin/python -m pytest -q -p no:cacheprovider scripts/tests  # build v2 (pevné tvary dávek, restart GPU, výpadek ES,
                                                               # obnova push, záloha) + aktualizace (nový/změněný/zrušený
                                                               # předpis, převzetí vektorů, výpadek ES, idempotence)
cd mcp-server && .venv/bin/python -m pytest -q                 # MCP: v1/v2 schéma, deduplikace, kNN, filtr starých předpisů
```

Kvalita vyhledávání se neměří testy, ale evaluací nad skutečným ES (potřebuje VPN) – viz [`eval/README.md`](eval/README.md).

*Dokumentace vygenerována systémem Antigravity 2.0*
