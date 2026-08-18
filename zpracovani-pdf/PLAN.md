# Zpracování PDF zákonů — Plán implementace

## 1. Přehled projektu

### Cíl
Stáhnout zákony ze Sbírky zákonů ČR (e-sbirka.gov.cz) ve formátu PDF, extrahovat z nich text s paragrafy, vytvořit embeddingy a vložit do Elasticsearch pro semantické vyhledávání (RAG).

### Datový zdroj
- **E-sbírka.gov.cz** — oficiální platforma Ministerstva vnitra ČR
- **OpenData API:** `https://opendata.eselpoint.gov.cz/datove-sady-esbirka/002PravniAkt.json.gz`
- **PDF download:** `https://e-sbirka.gov.cz/souborove-sluzby/soubory/{uuid}`

### Právní status
Data ze Sbírky zákonů jsou **svobodná úřední díla** (§ 3 odst. 1 AZ). Žádné licence, poplatky ani omezení nejsou potřeba.

---

## 2. Architektura systému

### Workflow (dvoupásmový)

```
┌─────────────────────────────────────────────────────────────┐
│ FÁZE 1: DOWNLOAD (sequential)                               │
├─────────────────────────────────────────────────────────────┤
│ 1. Načíst seznam zákonů z OpenData API                      │
│ 2. Pro každý zákon:                                         │
│    - Zkontrolovat SQLite checkpoint                         │
│    - Stáhnout PDF z e-sbírka API                            │
│    - Uložit do data/                                        │
│    - Update SQLite: phase="downloaded"                      │
│ 3. Logovat statistiky (staženo, chyby, přeskočeno)          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ FÁZE 2: PROCESSING (parallel via ProcessPoolExecutor)       │
├─────────────────────────────────────────────────────────────┤
│ 1. Načíst stažená PDF ze SQLite                             │
│ 2. Rozdělit na chunky (chunk_size = 100 zákonů)             │
│ 3. ProcessPoolExecutor (workers = cores-1)                  │
│    Každý worker:                                            │
│    - pdfplumber → extrakce textu                            │
│    - Regex § → paragrafy                                    │
│    - SentenceTransformer → embedding                        │
│    - ES bulk insert                                         │
│    - SQLite: phase="done"                                   │
│ 4. Logovat statistiky (zpracováno, chyby, paragrafy)        │
└─────────────────────────────────────────────────────────────┘
```

### Data flow

```
002PravniAkt.json.gz ──→ seznam zákonů (citace, rok, cislo, doc_id)
                              │
                              ▼
e-sbirka API ───────────→ PDF soubory (.pdf)
                              │
                              ▼
pdfplumber ──────────────→ text + metadata
                              │
                              ▼
Regex § \d+ ─────────────→ paragrafy chunky (max 2000 zn.)
                              │
                              ▼
SentenceTransformer ────→ embedding vektory (768 dims)
                              │
                              ▼
Elasticsearch ───────────→ index zakony (nested paragrafy + vector)
```

---

## 3. SQLite checkpoint

### Tabulka: law_status

```sql
CREATE TABLE law_status (
    id_zakona TEXT PRIMARY KEY,
    phase TEXT NOT NULL,          -- downloading, downloaded, processing, done, error
    pdf_path TEXT,
    pdf_sha256 TEXT,
    paragraphs_count INTEGER,
    error_msg TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Fáze (phase):
| Hodnota | Význam |
|---------|--------|
| `downloading` | Zákony je právě stahováno |
| `downloaded` | PDF je stažené a uloženo |
| `processing` | Zákony je právě zpracováváno (Fáze 2) |
| `done` | PDF zpracováno + vloženo do ES |
| `error` | Došlo k chybě |

### Resume behavior:
- **Fáze 1 (download):** Pokud `phase="downloaded"`, přeskočit stahování
- **Fáze 2 (processing):** Pokud `phase="done"`, přeskočit zpracování
- **Fáze 2 (processing):** Pokud `phase="error"` nebo `"downloading"`, zpracovat znovu

---

## 4. PDF extrakce a parsování

### pdfplumber
- Nejlepší pro zachování formátování zákonů
- Extrahuje text s mezerami a novými řádky

### Regex pro paragrafy
```python
PARAGRAPH_PATTERN = re.compile(r'§\s*(\d+)\s*[,.;]?\s*\n?(.*)', re.DOTALL)
```
Chytne: `§ 1`, `§1,`, `§ 2;`, `§3.`

### Chunking
- Text mezi paragrafy = jeden chunk
- Maximální délka: 2000 znaků
- Pokud je chunk delší, rozdělit podle odstavců

### Metadata z PDF
- Filename: `Sb_2006_108_...pdf` → `108/2006 Sb.`
- Regex filename: `Sb_(\d{4})_(\d+)_`
- Citace: `(\d+/\d{4}\s+Sb\.)`

---

## 5. Elasticsearch

### Index: `zakony`
- Mapping viz `ES_DOKUMENTACE.md`
- Nested paragrafy s vektory
- dims: 768, similarity: cosine, int8_hnsw

### ES dokument (PDF paragraf)
```json
{
  "id_zakona": "89/2012 Sb.",
  "akt_nazev": "...",
  "rok": 2012,
  "datum_od": "2012-01-01",
  "paragrafy": [{
    "citace": "§ 1",
    "text": "text paragrafu...",
    "typ": "PDF",
    "vektor": [0.1, -0.2, ...]  // 768 dims
  }]
}
```

### Embedding model
- `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`
- 768 dimenze, podporuje češtinu

---

## 6. Kódy z pipeline.py které použijeme

### Importované funkce:
1. `embed_and_bulk_insert()` — vytvoření embeddingů + bulk insert do ES
2. `create_es_index()` — vytvoření ES indexu (upravit, aby nesmazal existující)
3. `EMBEDDING_MODEL`, `INDEX_NAME`, `ES_HOST` — konstanty

### Nepoužijeme:
- `ijson` streaming (to je pro GLMF JSON data)
- `ProcessPoolExecutor` worker `_worker_process_batch()` — vytvoříme vlastní
- `Checkpoint` (JSON-based) — použijeme SQLite

---

## 7. Requirements

```
elasticsearch>=8.0.0
sentence-transformers>=2.2.0
pdfplumber>=0.9.0
```

---

## 8. TODO Checklist

### [x] 1. Příprava
- [x] Vytvořit `zpracovani-pdf/zpracovani_pdf_ingest.py`
- [x] Smazat `stahovacka.py` (integrováno novým skriptem)
- [x] Update `requirements.txt` → přidat `pdfplumber`
- [x] Otestovat import z pipeline.py

### [x] 2. Fáze 1: Download
- [x] Implementovat `download_phase()`
  - [x] Načtení zákonů z OpenData API
  - [x] SQLite init (tabulka law_status)
  - [x] Sequential stahování PDF
  - [x] Checkpoint po každém zákoně
  - [x] Logování statistik

### [x] 3. Fáze 2: Processing
- [x] Implementovat `process_phase()`
  - [x] Načtení stažených PDF ze SQLite
  - [x] Rozdělení na chunky (chunk_size = 50)
  - [x] Parallel processing s `ProcessPoolExecutor`
  - [x] Worker funkce: pdfplumber → embedding → ES
  - [x] SQLite update po každém zákone
  - [x] Logování statistik

### [x] 4. PDF extrakce
- [x] Implementovat extrakci textu (pdfplumber)
- [x] Implementovat regex paragrafů: `§\s*(\d+)\s*[,.;]?`
- [x] Implementovat chunking (text mezi paragrafy)
- [x] Extrahovat metadata z filename/PDF

### [x] 5. CLI
- [x] argparse: `--limit N`, `--data-dir`, `--es-url`, `--workers`, `--chunk-size`
- [x] `--skip-download` (jen processing fáze)
- [x] `--skip-process` (jen download fáze)
- [x] `--state-db` (cesta k SQLite state databázi)

### [ ] 6. Testování
- [ ] Otestovat na 10 zákonech
- [ ] Otestovat resume po přerušení
- [ ] Ověřit ES ingest + search

---

## 9. Implementační detaily

### Soubor: `zpracovani-pdf/zpracovani_pdf_ingest.py`

**Struktura:**
```
1. Import z pipeline.py (embed_and_bulk_insert, create_es_index, konstanty)
2. PDFCheckpoint třída (SQLite)
3. download_phase() — sequential stahování PDF
4. extract_text_from_pdf() — pdfplumber + pypdf fallback
5. extract_paragraphs() — regex paragrafů
6. _process_pdf_worker() — worker pro ProcessPoolExecutor
7. process_phase() — parallel processing
8. main() — CLI entry point
```

**Importy z pipeline.py:**
- `embed_and_bulk_insert()` — embedding + ES bulk insert
- `create_es_index()` — vytvoření ES indexu
- `EMBEDDING_MODEL`, `INDEX_NAME`, `ES_HOST` — konstanty

**SQLite tabulky:**
- `law_status` — stav každého zákona (phase, pdf_path, paragraphs_count, error)
- `law_summary` — přehled zpracovaných zákonů (nazev, rok, pocet paragrafu)

**Resume logika:**
- Fáze 1: Pokud `phase="downloaded"`, přeskočit stahování
- Fáze 2: Pokud `phase="done"`, přeskočit zpracování

---

## 10. Použití

```bash
# Instalace dependencí
cd /home/faltynek/Documents/zakony-es-projekt
source .venv/bin/activate
pip install pdfplumber

# Plný workflow (download + processing)
python3 zpracovani-pdf/zpracovani_pdf_ingest.py --limit 100

# Pouze processing (pokud jsou PDF už stažené)
python3 zpracovani-pdf/zpracovani_pdf_ingest.py --skip-download --workers 4

# Pouze download
python3 zpracovani-pdf/zpracovani_pdf_ingest.py --skip-process --limit 50

# Vlastní ES URL a workers
python3 zpracovani-pdf/zpracovani_pdf_ingest.py --es-url http://localhost:9200 --workers 8

# Resume po přerušení — stačí spustit znovu, skript pokračuje tam kde skončil
python3 zpracovani-pdf/zpracovani_pdf_ingest.py --limit 100
```

---

## 11. Poznámky

### API e-sbírka
- `GET /sbr-externi/stahni/overene-zneni/{doc_id}` → PDF info
- `GET /souborove-sluzby/soubory/{uuid}` → PDF soubor
- Rate limit: 0.3s mezi požadavky

### Chybové stavy
- `NEPRIPRAVEN` API → zákon nemá PDF (sken papíru)
- HTTP error → zkusit znovu (max 3x)
- pdfplumber error → uložit do error_msg, zkusit pypdf jako fallback

### Výkon
- Download: ~3 sec/zakon (API latency)
- Processing: ~2-5 sec/zakon (embedding + ES)
- Parallel: workers * chunk_size = 3 * 50 = 150 zákonů najednou

---

*Plán vytvořen: 2025-08-14*
*Verze: 1.1*
*Skript vytvořen: 2025-08-14*
