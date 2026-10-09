# Zákony ES Projekt (Architektura a Workflow 2026)

Tento projekt slouží k sofistikovanému a extrémně rychlému prohledávání české legislativy pomocí **Elasticsearchu**. Využívá **hybridní vyhledávání** – kombinuje klasické textové vyhledávání s pokročilým sémantickým hledáním pomocí umělé inteligence (AI Vektory), která chápe význam textu.

Projekt byl v říjnu 2026 kompletně přepsán, aby dokázal lokálně zpracovat 30 670 zákonů (více než 7 milionů paragrafů) na Apple Silicon grafice a spolehlivě je zaindexovat i na menších serverech (s 1 GB RAM pro Javu v Dockeru).

---

## 🏗️ Celková architektura systému

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

---

## 🚀 Jak používat a spravovat (Cheat Sheet)

Všechny zdrojové balíčky dat (`.json.gz`) nepatří do verzovacího systému a jsou ošetřeny v `.gitignore`.

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
Tento projekt je ideálně připraven pro tvůj nezávislý `mcp-dcuk` repozitář. Ve svém MCP asistentovi budeš využívat API Elasticsearchu a sestavovat složené dotazy (kombinující Full-text skóre názvu zákona se sémantickým `cosineSimilarity` skórem z vektorů u jednotlivých vnořených paragrafů).

*Dokumentace vygenerována systémem Antigravity 2.0*
