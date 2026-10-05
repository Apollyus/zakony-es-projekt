#!/usr/bin/env python3
"""
Export embeddingů z .embed-overlap.db do NDJSON shardů pro ES import.

Použití:
    python3 scripts/export_from_embed_db.py \
        --db zakony/.embed-overlap.db \
        --out embedding_shards

Výsledek: embedding_shards/shard_00000.json.gz, shard_00001.json.gz, ...
"""

import argparse, gzip, json, os, struct, sys, time, sqlite3
import logging

log = logging.getLogger("export")


def main():
    ap = argparse.ArgumentParser(
        description="Export embed-overlap.db → NDJSON shards pro ES"
    )
    ap.add_argument("--db", required=True, help="Cesta k .embed-overlap.db")
    ap.add_argument("--out", default="embedding_shards")
    ap.add_argument("--docs-per-shard", type=int, default=5000)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    # ── Načtení DB ──────────────────────────────────────────────────
    log.info(f"Načítám DB: {args.db}")
    t0 = time.time()

    conn = sqlite3.connect(args.db)
    law_count = conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0]
    para_count = conn.execute("SELECT COUNT(*) FROM paragrafy").fetchone()[0]
    log.info(f"Zákony: {law_count}, Paragrafy: {para_count}")

    # ── Rychlé načtení metadat ────────────────────────────────────
    law_meta = {}
    for row in conn.execute(
        "SELECT id_zakona, citace, nazev, rok, sbirka FROM laws"
    ):
        law_id, citace, nazev, rok, sbirka = row
        law_meta[law_id] = {
            "id_zakona": law_id,
            "akt_citace": citace,
            "akt_nazev": nazev,
            "rok": rok,
            "datum_od": None,
            "datum_do": None,
            "je_zrusen": False,
            "sbírka": sbirka,
        }
    log.info(f"Metadata: {len(law_meta)} zákonů")

    # ── Načtení všech paragrafů najednou ──────────────────────────
    log.info("Načítám paragrafy (bulk)...")
    rows = conn.execute(
        "SELECT id_zakona, idx, citace, text, vektor "
        "FROM paragrafy ORDER BY id_zakona, idx"
    ).fetchall()
    log.info(f"Paragrafy načteny: {len(rows)} za {time.time()-t0:.1f}s")

    # Skupování do {law_id: [(idx, citace, text, vektor)]}
    laws_dict: dict[str, list] = {}
    for law_id, idx, citace, text, vektor_blob in rows:
        vektor = list(struct.unpack("768f", vektor_blob))
        if law_id not in laws_dict:
            laws_dict[law_id] = []
        laws_dict[law_id].append({
            "idx": idx,
            "citace": citace,
            "text": text,
            "vektor": vektor,
        })

    conn.close()
    log.info(f"Hotovo za {time.time()-t0:.1f}s, {len(laws_dict)} zákonů")

    # ── Zápis shardů ───────────────────────────────────────────────
    docs_per_shard = args.docs_per_shard
    total_docs = 0
    total_paras = 0
    t_write = time.time()
    shard_idx = 0

    for law_id, paras in laws_dict.items():
        meta = law_meta[law_id]
        paragrafy = []
        for p in paras:
            paragrafy.append({
                "iris": "",
                "eli": "",
                "citace": p["citace"] or f"§ {paras.index(p)}",
                "text": p["text"],
                "hierarchie": "",
                "fragment_id": 0,
                "typ": "Paragraf",
                "vektor": p["vektor"],
            })

        doc = {
            "_source": {
                **meta,
                "paragrafy": paragrafy,
            }
        }

        total_docs += 1
        total_paras += len(paras)

        # Zápis do shardu
        shard_path = os.path.join(args.out, f"shard_{shard_idx:05d}.jsonl.gz")
        with gzip.open(shard_path, "wt", encoding="utf-8") as f:
            f.write(json.dumps(doc["_source"], ensure_ascii=False) + "\n")
        shard_idx += 1

        if total_paras % 100000 == 0:
            elapsed = time.time() - t_write
            log.info(f"{total_paras} paragrafů v {elapsed:.0f}s")

    elapsed = time.time() - t_write
    shard_files = sorted(
        f for f in os.listdir(args.out)
        if f.endswith(".jsonl.gz") or f.endswith(".json.gz")
    )
    size_mb = sum(
        os.path.getsize(os.path.join(args.out, s)) for s in shard_files
    ) / 1024**2

    log.info("=" * 70)
    log.info("EXPORT HOTOV")
    log.info(f"  Dokumentů:   {total_docs}")
    log.info(f"  Paragrafů:   {total_paras}")
    log.info(f"  Shardů:      {len(shard_files)}")
    log.info(f"  Velikost:    {size_mb:.0f} MB")
    log.info(f"  Čas:         {time.time()-t0:.1f}s")
    log.info(f"  Adresář:     {args.out}")
    log.info("=" * 70)

    print()
    print("→ Import do ES:")
    print(f"  python3 scripts/import_ndjson.py {args.out} --es-url http://<VZDÁLENÝ_ES>:9200")


if __name__ == "__main__":
    main()
