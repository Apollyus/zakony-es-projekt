# PDF/DOCX Pipeline

## ⚠️ PDF jsou skeny — nepoužíváme!

PDF ze státní sbírky jsou skeny papíru (obrázky, ne text). Nelze z nich extrahovat text pro embedding.

**Řešení:** Používáme **DOCX dokumenty** (informativní znění z e-Sbírky).

## Co to dělá

Stahuje zákony z e-Sbírky, extrahuje z nich text s paragrafy, vytváří embeddingy a vkládá do Elasticsearchu.

## Dva formáty, stejný workflow

| Formát | Zdroj | Textový? | Stav |
|--------|-------|----------|------|
| **PDF** | Overené znění | ❌ Skeny | Pozastaveno |
| **DOCX** | Informativní znění | ✅ Ano | Implementováno stahování, processing ne |

## Jak to funguje

### Fáze 1: Download (sequential)
1. Načíst seznam zákonů z OpenData API (`002PravniAkt.json.gz`)
2. Pro každý zákon:
   - Zkontrolovat SQLite checkpoint
   - Stáhnout PDF/DOCX z e-Sbírka API
   - Uložit do `data/`
   - Update SQLite: `phase="downloaded"`

### Fáze 2: Processing (parallel)
1. Načíst stažená PDF ze SQLite
2. Rozdělit na chunky (chunk_size = 100 zákonů)
3. ProcessPoolExecutor (workers = cores-1):
   - pdfplumber → extrakce textu
   - Regex `§ \d+` → paragrafy
   - SentenceTransformer → embedding (768 dims)
   - ES bulk insert
   - SQLite: `phase="done"`

## Aktuální stav

| Úkol | Stav | Poznámka |
|------|------|----------|
| Pipeline napsaná | ✅ HOTOVO | zpracovani_pdf_ingest.py |
| Testovací skript | ✅ HOTOVO | test_ingest.py |
| PDF staženo | ✅ HOTOVO | 104 PDF, 61 skenů |
| PDF processing | ❌ NE | Žádný "done" status v DB |
| DOCX stažení | ✅ HOTOVO | 5 testovacích souborů |
| DOCX parsing | ❌ NE | python-docx není v requirements |
| DOCX processing | ❌ NE | Implementace čeká na parsing |

## Použití

```bash
source .venv/bin/activate

# Plný workflow (download + processing)
python zpracovani-pdf-docx/zpracovani_pdf_ingest.py --limit 100

# Pouze processing (pokud jsou PDF už stažené)
python zpracovani-pdf-docx/zpracovani_pdf_ingest.py --skip-download --workers 4

# Pouze download
python zpracovani-pdf-docx/zpracovani_pdf_ingest.py --skip-process --limit 50

# DOCX stažení
python zpracovani-pdf-docx/stahni-docx.py --search "ústava" --limit 5
```

## Struktura souborů

```
zpracovani-pdf-docx/
├── data/                    # PDF soubory (stažené)
│   └── state.db             # SQLite checkpoint (stav každého zákona)
├── zpracovani_pdf_ingest.py # Hlavní skript (PDF download + processing)
├── stahni-docx.py           # Stažení DOCX z e-Sbírky
├── test_ingest.py           # Test na 10 zákonech
├── NOTEBOOK.md              # API notebook, testovací výsledky
├── PLAN.md                  # Architektura a plán implementace
└── README.md                # Tento soubor
```

## SQLite checkpoint (state.db)

### Tabulka: law_status

| Sloupec | Význam |
|---------|--------|
| id_zakona | ID zákona (rok_cislo) |
| phase | fáze (downloading, downloaded, done, error, scan) |
| pdf_path | cesta ke staženému PDF |
| paragraphs_count | počet paragrafů (po processingu) |
| error_msg | chybová hláška |

### Fáze:
- `downloaded` — PDF staženo, čeká na processing
- `done` — zpracováno a vloženo do ES
- `scan` — PDF je sken (přeskočeno)
- `error` — došlo k chybě

## Resume

Skript umí pokračovat tam, kde skončil:
- Pokud je zákon `downloaded`, přeskočí download
- Pokud je zákon `done`, přeskočí processing

Stačí spustit stejný příkaz znovu.

## Embedding

- Model: `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`
- Dimenze: 768
- Similarita: cosine
- Index: int8_hnsw (kvantizace, 4× menší storage)

## ES mapping

Nested paragrafy s vektory — viz [docs/ES_DOKUMENTACE.md](../docs/ES_DOKUMENTACE.md).
