# JSON Pipeline (GLMF data)

## Co to dělá

Načítá data e-Sbírky ve formátu GLMF (Government MetaData Format) a vkládá je do Elasticsearchu.

## Data

| Soubor | Popis | Velikost |
|--------|-------|----------|
| 001PravniAktZneni.json.gz | Metadata zákonů (citace, název, rok, datum) | 176 MB |
| 003PravniAktZneniFragment.json.gz | Strom fragmentů (hierarchie, citace) | 1.2 GB |
| 004PravniAktFragment.json.gz | Typy fragmentů (Paragraf, Odstavec_Dc, Pozemek) | 529 MB |

## Jak to funguje

### Workflow

```
001 (metadata) ──→ parsování JSON → slovník zákonů {iri: metadata}
        │
        ▼
004 (typy) ────→ parsování JSON → SQLite databáze {fragment_id: (typ, text)}
        │
        ▼
003 (fragmente) ──→ streaming → grouping podle paragrafu → embedding → ES
```

### 3 fáze

1. **Načtení metadata (001)** — jednovláknově, parsování JSON → slovník {iri: metadata}
2. **Načtení typů (004)** — jednovláknově, parsování JSON → SQLite databáze
3. **Zpracování fragmentů (003)** — parallel processing:
   - Streaming JSON (ijson)
   - Grouping fragmentů podle paragrafu (číslo z hierarchie)
   - Kombinace textů z `Paragraf` + `Odstavec_Dc` typů
   - Embedding (SentenceTransformer)
   - Bulk insert do Elasticsearchu

## Aktuální stav

| Úkol | Stav | Poznámka |
|------|------|----------|
| Pipeline napsaná | ✅ HOTOVO | pipeline.py, ingest.py, test_small.py, run.sh |
| Data stažená | ✅ HOTOVO | 001, 003, 004 v data/ |
| Ingest běžel | ✅ HOTOVO | 2665 dokumentů v index-zakony-jsony |
| Skutečný text paragrafů | ❌ NE | Původní kód dělal jeden ES dokument na fragment |
| Grouping textů | ✅ PRIPRAVENO | pipeline.py má grouping logiku (Paragraf + Odstavec_Dc) |

### Problém s textem

**Co se stalo:** Původní pipeline.py dělal jeden ES dokument za každý fragment z 003.
Fragment `§ 1` měl jen `<var>§ 1.</var>` — chyběl skutečný text paragrafu.

**Řešení:** pipeline.py byl upraven (necommitnuté změny):
- Nový typ `Odstavec_Dc` přidán do VALID_TYPES
- `extract_paragraf_number()` extrahuje číslo paragrafu z hierarchie
- Fragmenty se grupují podle (law_iri, paragraf_number)
- Texty z `Paragraf` + `Odstavec_Dc` se kombinují do jednoho textu

## Použití

```bash
source .venv/bin/activate

# Full ingest
python scripts/ingest.py data/

# Dry run
python scripts/ingest.py --dry-run data/

# Test na malém datasetu (100 zákonů)
python scripts/test_small.py --n 100
```

## Struktura souborů

```
zpracovani-json/
├── data/                    # GLMF data (001, 003, 004 .gz)
├── 004_types.db             # SQLite databáze typů fragmentů (vytváří ingest)
├── state.json               # Checkpoint (co už bylo zpracováno)
├── test_ingest.py           # Testovací skript
└── README.md                # Tento soubor
```

## Data flow podrobně

### 001PravniAktZneni.json.gz

```json
{
  "typ": "právní-akt-znění",
  "iri": "esel-esb:eli/cz/sb/1993/1/2026-01-01",
  "akt-citace": "1/1993 Sb.",
  "akt-název-vyhlášený": "Ústava České republiky",
  "znění-dokument-id": 30123,
  "znění-ročník": 1993,
  "znění-datum-účinnosti-od": "1993-01-01",
  "znění-datum-účinnosti-do": null,
  "znění-je-zrušen": false
}
```

### 003PravniAktZneniFragment.json.gz

```json
{
  "typ": "právní-akt-znění-fragment",
  "iri": "esel-esb:eli/cz/sb/1993/1/2026-01-01/dokument/norma/par_1",
  "znění-fragment-citace": "§ 1",
  "znění-fragment-citace-text": "...",
  "znění-fragment-eli": "/eli/cz/sb/1993/1/2026-01-01/dokument/norma/par_1",
  "znění-fragment-hierarchie": "/2/1/",
  "znění-dokument-id": 30123,
  "právní-akt-fragment": {"fragment-id": 1234}
}
```

### 004PravniAktFragment.json.gz

```json
{
  "typ": "právní-akt-fragment",
  "fragment-id": 1234,
  "fragment-base-id": 1234,
  "fragment-text": "Česko je ústavní, demokratický...",
  "cis-esb-typ-fragmentu-položka": "Odstavec_Dc"
}
```

### Výstup do Elasticsearchu (po fixi)

```json
{
  "id_zakona": "esel-esb:eli/cz/sb/1993/1/2026-01-01",
  "akt_citace": "1/1993 Sb.",
  "akt_nazev": "Ústava České republiky",
  "rok": 1993,
  "datum_od": "1993-01-01",
  "datum_do": null,
  "je_zrusen": false,
  "sbírka": "sb",
  "paragrafy": [{
    "iris": "esel-esb:eli/cz/sb/1993/1/2026-01-01/dokument/norma/par_1",
    "eli": "/eli/cz/sb/1993/1/2026-01-01/dokument/norma/par_1",
    "citace": "§ 1",
    "text": "§ 1. Česko je ústavní, demokratický, právní a sociální stát. [kombinovaný text]",
    "hierarchie": "/2/1/",
    "fragment_id": 1234,
    "typ": "Paragraf",
    "vektor": [0.05, -0.12, 0.03, ...]
  }]
}
```
