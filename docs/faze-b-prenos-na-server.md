# Fáze B: přenos embedding shardů na server a import do Elasticsearch

> Stav po fázi A (28. 8. 2026): **240 shardů, ~1 195 368 dokumentů, 13 GB**

## Kde jsou data (domácí PC `vojta-pc-linux`)

```
/home/vojtech/Documents/aaa_programovani/zakony-es-projekt/embedding_shards/
├── shard_00000.json.gz     # 5000 doků
├── shard_00001.json.gz
├── ...
├── shard_00239.json.gz     # poslední (368 doků, recovery)
├── 004_types.db            # SQLite (na server NENÍ potřeba)
├── export_state.json       # checkpoint (na server NENÍ potřeba)
└── spool_chunks/           # pracovní (na server NENÍ potřeba)
```

**Na server kopíruj jen `shard_*.json.gz`** (13 GB). Zbytek ignoruj.

## Co je na serveru potřeba

1. **Repozitář** (kvůli `scripts/import_ndjson.py` + `scripts/pipeline.py` — import si z něj bere mapping):
   ```bash
   git clone https://github.com/Apollyus/zakony-es-projekt.git
   ```
2. **Venv s elasticsearch balíčkem** — v repu `requirements.txt` (stačí i bez torch:
   `pip install elasticsearch>=8.0.0`)
3. **Elasticsearch 8.x** běžící (default `localhost:9200`)

---

## Přenos

`rsync` sám neví, kam posílat — `user@server` je placeholder, dosaď IP/hostname.
Server je dostupný jen přes VPN → přenos jde přes stroj s VPN.

### Varianta 1: Dvouskokový rsync (VPN stroj jako relay)

```bash
# KROK 1 — na VPN stroji (natáhne shardy z domácího PC):
rsync -avz --partial \
    vojtech@vojta-pc-linux:/home/vojtech/Documents/aaa_programovani/zakony-es-projekt/embedding_shards/ \
    ~/embedding_shards/
# (na vojta-pc musí běžet sshd: sudo apt install openssh-server)

# KROK 2 — na VPN stroji (pošle na server skrz VPN):
rsync -avz --partial ~/embedding_shards/ user@SERVER_IP:/home/faltynek/embedding_shards/
```

- `--partial` = po výpadku naváže, nic nedělá znovu
- Lze pustit průběžně i víckrát — done soubory se přepisují jen když se změnily

### Varianta 2: USB disk

13 GB se vejde kamkoliv. Zkopíruj adresář `embedding_shards/` a na cílovém
stroji umísti do `~/embedding_shards/`.

### Varianta 3: SSH jump (přímý rsync skrz VPN stroj)

Pokud VPN stroj vidí obě sítě, můžeš směrovat přímo z domácího PC:

```bash
rsync -avz --partial \
    -e "ssh -J user@VPN_STROJ" \
    /home/vojtech/Documents/aaa_programovani/zakony-es-projekt/embedding_shards/ \
    user@SERVER_IP:/home/faltynek/embedding_shards/
```

Ověř, že VPN stroj povoluje forwarding na server (port 22) a že máš SSH klíče
na oba skoky.

---

## Import do Elasticsearch (na serveru)

```bash
cd ~/zakony-pipeline    # adresář s funkčním venv
.venv/bin/python ~/Documents/zakony-es-projekt/scripts/import_ndjson.py \
    ~/embedding_shards/
```

Parametry:

| Přepínač | Význam |
|---|---|
| *(default)* | Cílí na index `zakony` |
| `--index NAME` | Jiný cílový index |
| `--force` | ⚠️ Smaže index a vytvoří nový |
| `--reset` | Přeimportuje i hotové shardy (ignoruje `.done`) |
| `--bulk-size N` | Dokumentů na bulk request (default 200) |
| `--es-url URL` | ES adresa (default `http://localhost:9200`) |

- **Idempotentní:** hotové shardy mají sidecar `.done`, přerušení/restart je bezpečné
- Chybující shard se `.done` neoznačí → příští běh ho zkusí celý znovu
- Při 1.2M doků počítej s desítkami minut (jen CPU + disk)

## Ověření výsledku

```bash
curl -s localhost:9200/zakony/_count
# očekáváno: ~1195368

curl -s "localhost:9200/zakony/_search?size=1&pretty" | head -40
```

## Troubleshooting

| Problém | Řešení |
|---|---|
| Import: mapping errors | Index má staré schéma → `--force` (smaže index!) |
| Málo místa na serveru | Po importu smaž shardy: `rm ~/embedding_shards/shard_*` |
| ES nedostupný | Zkontroluj `curl localhost:9200`, případně `--es-url` |

## Kontext

- Fáze A dokumentace: `docs/offline-import.md`
- Export skript: `scripts/export_embeddings.py` (checkpoint, obnovitelný)
- Recovery skript: `scripts/recover_spool.py` (dozpracování spool chunků po pádu)
- Import skript: `scripts/import_ndjson.py`
