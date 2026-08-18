# Zakony ES Projekt — TODO

## Setup
- [x] Docker + Elasticsearch 8.15.0 rozjeté na localhost:9200
- [x] Index `zakony` s nested mappingem (paragrafy s vektory)
- [x] Test data + search skript ~/zakony-search.sh

## Ingest pipeline
- [x] Vytvořit ingest.py
  - [x] Čtení zip souboru → extrakce do /tmp
  - [x] ijson streaming na 001, 003, 004
  - [x] Propojení stromu z 003 podle hierarchie
  - [x] Join 001→003→004 podle iri/fragment-id
  - [x] Filtr na typy "Paragraf" s textem
  - [x] Batch embedding přes sentence-transformers
  - [x] Bulk insert do Elasticsearch
- [x] Vytvořit requirements.txt
- [x] Vytvořit state.json checkpoint (Checkpoint class)

## Data
- [ ] Stáhnout GLMF data ze státní sbírky
  - [ ] 001PravniAktZneni.json (4.3GB) — metadata zákonů
  - [ ] 003PravniAktZneniFragment.json.gz (1.2GB) — strom fragmentů
  - [ ] 004PravniAktFragment.json.gz (506MB) — typy fragmentů
  - [ ] 007PravniAktKonsolidacniVazba.json.gz (19MB) — konsolidace (volitelné)
- [ ] Umístit do data/

## Test
- [ ] Otestovat ingest na reálných datech
- [ ] Ověřit hybrid search

## Použití

```bash
# Aktivace venv
cd /home/faltynek/Documents/zakony-es-projekt
. .venv/bin/activate

# Dry run — ukáže co by se zpracovalo
python ingest.py --dry-run data/

# Reálný ingest
python ingest.py data/001PravniAktZneni.json 003PravniAktZneniFragment.json.gz 004PravniAktFragment.json.gz

# Nebo zip
python ingest.py data/zakony.zip

# S vlastními parametry
python ingest.py --es-url http://localhost:9200 --model paraphrase-multilingual-mpnet-base-v2 data/

# Force re-ingest
python ingest.py --force data/
```
