# Zakony ES Projekt — TODO

## 0. Index v2 (priorita, říjen 2026) — viz docs/INDEX_V2.md
- [x] RAGAS evaluace (eval/), golden sady: ruční 15 otázek, syntetická 20
- [x] Diagnóza v1: fragmenty bez §/nadpisu, všechna znění + zrušené předpisy, mpnet 128 tokenů, bug v deduplikaci MCP hybridu
- [x] Pilot zakony-v2-pilot (447 předpisů): správný § v top 5 13 % → 93 %
- [x] scripts/build_v2_index.py + scripts/run_v2_overnight.sh (obnovitelný noční build)
- [x] MCP: přepínání v1/v2 přes ES_INDEX, oprava deduplikace v1, unit testy
- [x] Noční build plného indexu zakony-v2 (303 795 chunků, 3,9 h, 0 restartů; mezinárodní smlouvy zatím ne)
- [x] Srovnání na plném indexu: ruční sada správný § 0 % (v1) → 80 % (v2 kNN), správný předpis 100 %
- [x] MCP přepnut na zakony-v2 (run_mcp.sh), search_laws_semantic = čisté kNN
- [ ] ES alias pro budoucí rebuildy
- [x] Filtr nejistých/zastaralých předpisů v MCP (přetisky ven, neznamy/před 1945 ×0,94): nejisté výsledky 19 % → 4 %
- [ ] Slovenské předpisy z doby federace ve výsledcích (460/1992 apod.)
- [ ] Ablace (jednotka § vs filtr znění vs model) a větší evaluační sada
- [ ] Rozhodnout o smazání zakony-esbirka-2026 (24 GB) a zakony-v2-pilot

## Setup
- [x] Docker + Elasticsearch 8.15.0 rozjeté na localhost:9200
- [x] Index `zakony` s nested mappingem (paragrafy s vektory)
- [x] Test data + search skript ~/zakony-search.sh (smazat)

## 1. JSON Pipeline (GLMF data)

### Hotovo
- [x] Vytvořit pipeline.py — sdílená knihovna (ijson streaming, embedding, ES bulk insert)
- [x] Vytvořit ingest.py — CLI entry point
- [x] Vytvořit test_small.py — test na malém datasetu
- [x] Vytvořit run.sh — spouštěcí skript
- [x] Vytvořit requirements.txt
- [x] Vytvořit state.json checkpoint
- [x] Stáhnout GLMF data (001, 003, 004) — 1.9 GB
- [x] Ingest běžel na datech (2665 dokumentů v index-zakony-jsony)

### Čeká na vyřešení
- [ ] **BUG FIX:** Text paragrafů je prázdný (`<var>§ X.</var>`) — chybí skutečný text *(ve v2 vyřešeno sloučením fragmentů pod §)*
  - Původní kód dělal jeden ES dokument na fragment, ne na paragraf
  - Řešení připraveno v pipeline.py (necommitnuté změny):
    - Nový typ `Odstavec_Dc` přidán do VALID_TYPES
    - `extract_paragraf_number()` extrahuje číslo paragrafu z hierarchie
    - Fragmenty se grupují podle (law_iri, paragraf_number)
    - Texty z `Paragraf` + `Odstavec_Dc` se kombinují do jednoho textu
  - **Akce:** Commitnout změny v pipeline.py a spustit ingest znovu
- [ ] Otestovat ingest na reálných datech (až bude text opraven)
- [ ] Ověřit hybrid search

## 2. Word Pipeline (DOCX — doporučené)

### Hotovo
- [x] Vytvořit stahni-docx.py (stažení DOCX z e-Sbírky API)
- [x] 5 DOCX testovacích souborů staženo (tests-and-previews/)
- [x] API dokumentace (docs/e-sbirka-api.md)

### Čeká na vyřešení
- [ ] Přidat `python-docx` do requirements.txt
- [ ] Implementovat parsing DOCX (extrakce textu a paragrafů)
- [ ] Implementovat processing pipeline (stejný jako PDF/JSON)
- [ ] Otestovat stažení DOCX → embedding → ES

## 3. PDF Pipeline (pozastaveno — skeny)

### Hotovo
- [x] Vytvořit zpracovani_pdf_ingest.py (2-fázový workflow)
- [x] Vytvořit test_ingest.py (test na 10 zákonech)
- [x] 104 PDF staženo, 61 skenů detekováno

### Pozastaveno
- [ ] **PDF nejsou strojově čitelná** — přechod na DOCX
- [ ] Processing nebyl spuštěn (žádný "done" status v DB)

## 4. Dokumentace

### Hotovo
- [x] ES_DOKUMENTACE.md — mapping, analyzátory, příklady search query
- [x] e-sbirka-api.md — kompletní API reference e-Sbírky
- [x] NOTEBOOK.md — API struktura, testovací výsledky
- [x] PLAN.md — architektura PDF pipeline
- [x] README.md — hlavní přehled projektu
- [x] README zpracovani-json/ — detail JSON pipeline
- [x] README zpracovani-pdf-docx/ — detail PDF/DOCX pipeline

### Čeká na vyřešení
- [ ] Aktualizovat README po dokončení DOCX pipeline

## Použití

### JSON pipeline
```bash
source .venv/bin/activate
python scripts/ingest.py zpracovani-json/data/
python scripts/test_small.py --n 100
```

### Word (DOCX) pipeline
```bash
source .venv/bin/activate
python zpracovani-pdf-docx/stahni-docx.py --search "ústava" --limit 5
# Až bude hotový parsing:
python zpracovani-pdf-docx/zpracovani_pdf_ingest.py --skip-download
```

### PDF pipeline (pozastaveno)
```bash
source .venv/bin/activate
python zpracovani-pdf-docx/zpracovani_pdf_ingest.py --limit 100
```
