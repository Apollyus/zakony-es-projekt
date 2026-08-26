# Offline import: embeddingy doma, import na serveru

Embedding výpočet je nejtěžší část pipeline. Ten poběží **doma na GPU**,
na server se přenesou jen hotové výsledky a tam se nasypou do Elasticsearch.

```
DOMA (GPU)                                SERVER
─────────────────                         ─────────────────────────────
data .gz ──> export_embeddings.py         embedding_shards/
             │  (001+004+003 parsing,     │
             │   grupování podle §,       ▼  rsync/scp
             │   GPU embeddingy)          │
             └──> shard_*.json.gz ────────> import_ndjson.py
                                                │
                                                ▼
                                          ES index "zakony"
```

Odhady pro celý dataset (~1,2 GB 003):
- **Výpočet doma**: hodiny místo dnů (GPU ~10-50× rychlejší než CPU)
- **Velikost shardů**: cca 4-8 GB komprimovaných
- **Přenos**: při uploadu 20 Mbit/s ~30-60 min, při 50 Mbit/s ~15-25 min
- **Import na serveru**: desítky minut (jen CPU + disk)

Obě fáze jsou **obnovitelné** — po přerušení prostě spusťte stejný příkaz znovu.

---

## 1. Příprava domácího stroje

### 1.1 Kód

```bash
git clone https://github.com/Apollyus/zakony-es-projekt.git
cd zakony-es-projekt
```

### 1.2 Virtualenv s CUDA torchem

DŮLEŽITÉ: torch s CUDA nainstalujte PRED requirements.txt, jinak pip
stáhne verzi jen s CPU podporou:

```bash
python3 -m venv .venv
source .venv/bin/activate

# torch s CUDA (upravte cu121/cu124 podle verze CUDA driveru, viz nvidia-smi)
pip install torch --index-url https://download.pytorch.org/whl/cu121

# zbytek závislostí (torch už je, pip ho nepřepíše)
pip install -r requirements.txt
```

Ověření GPU:
```bash
python3 -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# musí vypsat: True <název_vaší_karty>
```

### 1.3 Vstupní data

Potřebujete 3 soubory ze serveru (nebo přímo z e-Sbírky):

```bash
# na serveru jsou ve ~/zakony-pipeline/data/
scp server:zakony-pipeline/data/*.gz .
```

| Soubor | Velikost | Obsah |
|---|---|---|
| 001PravniAktZneni.json.gz | 177 MB | metadata zákonů |
| 003PravniAktZneniFragment.json.gz | 1,2 GB | fragmenty paragrafů |
| 004PravniAktFragment.json.gz | 530 MB | typy/texty fragmentů |

HuggingFace model (`paraphrase-multilingual-mpnet-base-v2`, ~1 GB)
se stáhne sám při prvním spuštění.

---

## 2. Fáze A doma: výpočet embeddingů

```bash
cd zakony-es-projekt
source .venv/bin/activate

python3 scripts/export_embeddings.py /cesta/k/datam --workers 4
```

Co potřebujete vědět:

- **`--workers`**: kolik paralelních procesů. Pro GPU stačí 2-4 — GPU je
  bottleneck, víc workerů jen konkuruje o VRAM.
- **`--encode-batch-size`**: 64 default, na kartách s ≥8 GB VRAM klidně 128-256.
- Výstup landuje do `embedding_shards/` (shardy + checkpoint `export_state.json`).
  Spool chunky a SQLite se vytvoří uvnitř tohoto adresáře — vše na jednom místě.
- **Přerušení (Ctrl+C, výpadek proudu...):** spusťte znovu tentýž příkaz.
  Hotové IRIs se ze checkpointu přeskočí. POZOR: pokud spadlo uprostřed
  chunku, ten jeden chunk se spočítá znovu — ale jeho dokumenty se jen
  zapíší do nového shardu; duplikace nevadí, import řeší deterministické _id.
- **Testovací běh:** přidejte `--max-laws 5 --fresh` — zpracuje prvních pár
  zákonů, trvá minuty.

Log průběhu vypadá takto:
```
Chunk 42: 31 doků (celkem 1337, 45 dok/s)
Shard hotov: shard_00000.json.gz (5000 doků, celkem 5000)
```

Když doběhne, v `embedding_shards/` budou soubory `shard_00000.json.gz`,
`shard_00001.json.gz`, ... plus `export_state.json`.

---

## 3. Přenos na server

```bash
rsync -avz --partial embedding_shards/ user@server:/home/faltynek/embedding_shards/
```

- `--partial` = po výpadku naváže, nedělá nic znovu
- lze pustit průběžně i během exportu (hotové shardy už se nemění),
  nakonec ještě jednou pro dokončení

---

## 4. Fáze B na serveru: import do ES

```bash
cd ~/zakony-pipeline    # tady je funkční venv
.venv/bin/python ~/Documents/zakony-es-projekt/scripts/import_ndjson.py \
    ~/embedding_shards/
```

- Default cílí na index **`zakony`** (`--index` pro změnu).
- Hotové shardy značí sidecar soubory `.done` — přerušení a restart je bezpečné.
- Chybující shard se neoznačí a příští běh ho zkusí celý znovu (idempotentní).
- `--force` smaže index a vytvoří nový (pozor — maže data!).
- `--reset` přeimportuje i hotové shardy.

Ověření výsledku:
```bash
curl -s localhost:9200/zakony/_count
curl -s "localhost:9200/zakony/_search?size=1&pretty" | head -40
```

---

## 5. Troubleshooting

| Problém | Řešení |
|---|---|
| `torch.cuda.is_available()` = False | torch byl nainstalován před CUDA verzí — reinstall dle 1.2 |
| GPU out of memory | snižte `--encode-batch-size` (např. 32) nebo `--workers 1` |
| Export spadl, chceme začít čistě | `--fresh` (maže embedding_shards/) |
| Import: mapping errors | index má staré schéma — `--force` (smaže index!) |
| Disk na serveru | shardy po importu smažte: `rm ~/embedding_shards/shard_*` |

## 6. Souvislosti

- `scripts/pipeline.py` — původní direct-streaming ingest (funguje, pomalejší,
  vhodný jako fallback). Nové skripty sdílí jeho jádro (`build_batch_docs`,
  `iter_spool_chunks`), takže logika dokumentů a grupování je totožná.
- `scripts/test_pipeline.py` — rychlý test direct pipeline na pár zákonech.
