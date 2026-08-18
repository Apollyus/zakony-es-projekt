# Elasticsearch - Technická Dokumentace

## INSTALACE
- Verze: 8.15.0
- Spuštění: Docker container
- Adresa: localhost:9200
- Konfigurace: výchozí (single node)

## INDEX: zakony

### Nastavení
- Shards: 1 (single node)
- Replicas: 0 (žádná redundance)

### Analyzátory
- `czech`: standard tokenizer + lowercase + asciifolding
  - asciifolding: převádí české znaky na ASCII (š→s, č→c, ř→r, ...)
  - Umožňuje vyhledávání "paragraf" i když uživatel napíše "paragraf"

### Mapping

```json
{
  "id_zakona": {
    "type": "keyword"
  },
  "akt_citace": {
    "type": "text"
  },
  "akt_nazev": {
    "type": "text",
    "fields": {
      "keyword": {
        "type": "keyword"
      }
    }
  },
  "rok": {
    "type": "integer"
  },
  "datum_od": {
    "type": "date"
  },
  "datum_do": {
    "type": "date"
  },
  "je_zrusen": {
    "type": "boolean"
  },
  "sbírka": {
    "type": "keyword"
  },
  "paragrafy": {
    "type": "nested",
    "properties": {
      "iris": {
        "type": "keyword"
      },
      "eli": {
        "type": "keyword"
      },
      "citace": {
        "type": "text"
      },
      "text": {
        "type": "text",
        "analyzer": "czech"
      },
      "hierarchie": {
        "type": "keyword"
      },
      "fragment_id": {
        "type": "integer"
      },
      "typ": {
        "type": "keyword"
      },
      "vektor": {
        "type": "dense_vector",
        "dims": 768,
        "index": true,
        "similarity": "cosine",
        "index_options": {
          "type": "int8_hnsw",
          "m": 16,
          "ef_construction": 100
        }
      }
    }
  }
}
```

## PROČ ELASTICSEARCH

### 1. Hybridní vyhledávání
ES umí kombinovat:
- **Keyword search** (přesná slova, filtry)
- **Vector search** (semantická podobnost)
- **Hybrid search** (kombinace obojího)

### 2. Nested dokumenty
Paragrafy jsou v `nested` poli:
- Každý paragraf je samostatný dokument uvnitř zákona
- Umožňuje filtrovat podle typu, citace, hierarchie
- Vyhledávání pouze v paragrafech daného typu ("Paragraf")

### 3. Vector index (HNSW)
- **HNSW** (Hierarchical Navigable Small World): algoritmus pro rychlý approximate nearest neighbor search
- **int8_hnsw**: 8-bit kvantizace (4× menší storage, ~1-2% ztráta kvality)
- **m=16**: každý uzel má 16 spojení (balance mezi rychlostí a přesností)
- **ef_construction=100**: kvalita indexu (vyšší = přesnější, ale pomalejší build)

### 4. Cosine similarity
- Měří úhel mezi vektory, ne vzdálenost
- Ideální pro normalizované embedding vektory
- Hodnota: -1 až 1 (1 = identický význam)

### 5. Skalabilita
- Funguje na single node (pro test)
- Lze rozšířit na cluster (více nodů, shards, replicas)
- Podpora pro:
  - Aggregace (statistiky)
  - Suggestions (doplnění)
  - Re-ranking (přeskupení výsledků)

## VZOREK DOKUMENTU (JSON GLMF)

```json
{
  "id_zakona": "esel-esb:eli/cz/sb/1918/8/0000-00-00",
  "akt_citace": "8/1918 Sb.",
  "akt_nazev": "Zákon, jímž se zrušuje zabavení jmění pro činy velezrādné.",
  "rok": 1918,
  "datum_od": "1918-11-04",
  "je_zrusen": false,
  "paragrafy": [
    {
      "iris": "esel-esb:eli/cz/sb/1918/8/0000-00-00/dokument/norma/par_1",
      "eli": "/eli/cz/sb/1918/8/0000-00-00/dokument/norma/par_1",
      "citace": "§ 1",
      "text": "§ 1. Účelem tohoto zákona je... [kombinovaný text z Paragraf + Odstavec_Dc fragmentů]",
      "hierarchie": "/2/1/",
      "fragment_id": 8,
      "typ": "Paragraf",
      "vektor": [-0.001, -0.113, -0.020, ...]
    }
  ]
}
```

## VZOREK DOKUMENTU (DOCX)

```json
{
  "id_zakona": "1993_1",
  "akt_citace": "1/1993 Sb.",
  "akt_nazev": "Ústava České republiky",
  "rok": 1993,
  "datum_od": null,
  "datum_do": null,
  "je_zrusen": false,
  "sbírka": "",
  "paragrafy": [
    {
      "citace": "§ 1",
      "text": "Česko je ústavní, demokratický, právní a sociální stát...",
      "typ": "DOCX",
      "vektor": [0.05, -0.12, 0.03, ...]
    }
  ]
}
```

> **Poznámka:** DOCX dokumenty mají jednodušší strukturu — nemají iris/eli/hierarchie/fragment_id.
> ID zákona se tvoří z roku a čísla (např. "1993_1"). Typ je vždy "DOCX".

## PRÍKLAD VYHLEDÁVÁNÍ

### Semantic search (vector)
```json
{
  "knn": {
    "field": "paragrafy.vektor",
    "query_vector": [0.1, 0.2, ...],
    "k": 10,
    "num_candidates": 100
  }
}
```

### Hybrid search (vector + keyword)
```json
{
  "query": {
    "nested": {
      "path": "paragrafy",
      "query": {
        "bool": {
          "must": [
            { "match": { "paragrafy.text": "velezrādné" }},
            { "match": { "paragrafy.typ": "Paragraf" }}
          ]
        }
      }
    }
  }
}
```

## AKTUÁLNÍ STATISTIKY (2026-08-18)

### JSON GLMF Pipeline
- Index: `index-zakony-jsony`
- Dokumenty: 2665 (test na 1000 zákonech)
- Status: DATA NAČTENÁ, ALE TEXT PARAGRAFŮ JE PRÁZDNÝ
- Problém: původní kód dělal jeden ES dokument na fragment, ne na paragraf
- Řešení: PRIPRAVENO — pipeline.py obsahuje grouping logiku (kombinace Paragraf + Odstavec_Dc)

### DOCX Pipeline
- Status: DOCX staženo (5 testovacích souborů), processing neimplementován
- Data: tests-and-previews/, tests-n-previews/

## ZDROJE DAT

| Způsob | Formát | Soubory | Velikost |
|--------|--------|---------|----------|
| JSON GLMF | JSON | 001, 003, 004 | 1.9 GB |
| DOCX | DOCX | Informativní znění | ~50 KB/test |

## DATOVÉ Soubory

### GLMF data (JSON)
- **001PravniAktZneni.json.gz** — metadata zákonů (citace, název, rok, datum)
- **003PravniAktZneniFragment.json.gz** — strom fragmentů (hierarchie, citace)
- **004PravniAktFragment.json.gz** — typy fragmentů (Paragraf, Odstavec_Dc, Pozemek)

### DOCX data
- Stahují se z e-Sbírky API (informativní znění)
- API endpoint: `/sbr-externi/stahni/informativni-zneni/{dokumentBaseId}/DOCX`
- Obsahují kompletní text zákona včetně novel
