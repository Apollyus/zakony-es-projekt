# Zakony ES Projekt

Prohledávání české legislativy pomocí Elasticsearchu s hybridním vyhledáváním (keyword + vector/semantic search).

## ⚠️ Důležité: PDF vs DOCX

**PDF ze státní sbírky nejsou strojově čitelná** — jsou to skeny papíru (obrázky, ne text).
Nelze z nich extrahovat text pro embedding.

**Používáme DOCX** (informativní znění z e-Sbírky):
- ✅ Textové, lze extrahovat
- ✅ Obsahuje kompletní znění se všemi novelami
- ✅ Oficiální konsolidovaná verze e-Sbírky
- ❌ Není to pravě závazná verze (ale pro vyhledávání a RAG stačí)

## Způsoby zpracování dat

| Způsob | Formát | Zdroj | Stav | Data |
|--------|--------|-------|------|------|
| **JSON (GLMF)** | JSON | 001, 003, 004 | ⚠️ Data načtená, text prázdný | 1.9 GB v `zpracovani-json/data/` |
| **DOCX** | DOCX | e-Sbírka API | ⚠️ Staženo, processing ne | 5 testovacích souborů |
| **PDF** | PDF | e-Sbírka API | ❌ Pozastaveno (skeny) | 104 PDF (nepoužíváme) |

## Struktura projektu

```
zakony-es-projekt/
├── README.md                    # Tento soubor
├── pipeline.py                  # Sdílená knihovna (embedding + ES bulk insert)
├── requirements.txt             # Python závislosti
├── todo.md                      # TODO seznam (všechny způsoby)
├── .gitignore
│
├── docs/                        # Dokumentace
│   ├── ES_DOKUMENTACE.md        # Elasticsearch mapping, analyzátory, příklady
│   └── e-sbirka-api.md          # Kompletní API reference e-Sbírky
│
├── scripts/                     # CLI nástroje
│   ├── pipeline.py              # Sdílená knihovna (embedding + ES bulk insert)
│   ├── run.sh                   # Spouštěcí skript
│   ├── ingest.py                # CLI entry point (JSON pipeline)
│   └── test_small.py            # Test na malém datasetu
│
├── zpracovani-json/             # JSON GLMF pipeline
│   ├── data/                    # 001, 003, 004 (.gz soubory)
│   ├── 004_types.db             # SQLite databáze typů fragmentů
│   ├── state.json               # Checkpoint
│   ├── test_ingest.py           # Test
│   └── README.md                # Detail JSON pipeline
│
├── zpracovani-pdf-docx/         # PDF + DOCX pipeline (stejná data)
│   ├── data/                    # PDF soubory
│   ├── state.db                 # SQLite checkpoint
│   ├── zpracovani_pdf_ingest.py # Hlavní skript (PDF)
│   ├── stahni-docx.py           # Stažení DOCX
│   ├── test_ingest.py           # Test (PDF)
│   ├── NOTEBOOK.md              # API notebook, testovací výsledky
│   ├── PLAN.md                  # Architektura a plán
│   └── README.md                # Detail PDF/DOCX pipeline
│
└── tests-and-previews/          # Testovací DOCX soubory (5 souborů)
└── tests-n-previews/            # Testovací DOCX + HTML/JS (nepoužívat)
```

## Rychlý start

```bash
# Aktivace virtual env
source .venv/bin/activate

# JSON pipeline
python scripts/ingest.py zpracovani-json/data/
python scripts/test_small.py --n 100

# PDF pipeline (pozastaveno)
python zpracovani-pdf-docx/zpracovani_pdf_ingest.py --limit 100

# DOCX stažení
python zpracovani-pdf-docx/stahni-docx.py --search "ústava" --limit 5
```

## Elasticsearch

- URL: `http://localhost:9200`
- Index: `zakony` (nested paragrafy + vector search)
- Model: `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` (768 dims)
- Similarita: cosine, int8_hnsw

### Aktuální statistiky

| Index | Dokumenty | Zdroj | Stav |
|-------|-----------|-------|------|
| `index-zakony-jsony` | 2665 | JSON GLMF | Data načtená, text prázdný |
| `zakony` | 0 | — | Prázdný (nový) |
| `test-...` | různé | Testy | Lze smazat |

## Dokumentace

- [Elasticsearch mapping a analyzátory](docs/ES_DOKUMENTACE.md)
- [e-Sbírka API reference](docs/e-sbirka-api.md)
- [JSON pipeline detail](zpracovani-json/README.md)
- [PDF/DOCX pipeline detail](zpracovani-pdf-docx/README.md)
- [API Notebook (PDF strategie)](zpracovani-pdf-docx/NOTEBOOK.md)
- [Plán PDF pipeline](zpracovani-pdf-docx/PLAN.md)

## Použití

```bash
source .venv/bin/activate

# JSON pipeline
python scripts/ingest.py zpracovani-json/data/
python scripts/test_small.py --n 100

# PDF pipeline (pozastaveno)
python zpracovani-pdf-docx/zpracovani_pdf_ingest.py --limit 100

# DOCX stažení
python zpracovani-pdf-docx/stahni-docx.py --search "ústava" --limit 5
```
