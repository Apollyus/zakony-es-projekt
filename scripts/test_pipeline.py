#!/usr/bin/env python3
"""
Testovací běh pipeline na prvních N zákonech.

Používá totožnou logiku jako plný ingest (pipeline.process_laws), ale:
- omezený počet zákonů (--max-laws)
- vlastní ES index (default 'zakony-test') — nedotkne se produkčního
- vlastní checkpoint a SQLite DB v izolovaném adresáři test_run/
  (nepřepíše produkční state.json ani 004_types.db)

Použití:
    python3 test_pipeline.py                 # 10 zákonů do indexu zakony-test
    python3 test_pipeline.py --max-laws 3
    python3 test_pipeline.py --cleanup       # po testu smaže index i test_run/
"""

import argparse, json, os, shutil, sys
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import pipeline
from elasticsearch import Elasticsearch


def main():
    ap = argparse.ArgumentParser(description="Test pipeline na prvních N zákonech")
    ap.add_argument("--data-dir", default="/home/faltynek/zakony-pipeline/data",
                    help="Adresář se vstupními .gz soubory (001/003/004)")
    ap.add_argument("--es-url", default=pipeline.ES_HOST)
    ap.add_argument("--index", default="zakony-test", help="Testovací ES index")
    ap.add_argument("--max-laws", type=int, default=10, help="Počet zákonů k otestování")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--cleanup", action="store_true",
                    help="Po testu smazat testovací index i adresář test_run/")
    args = ap.parse_args()

    # Izolovaný pracovní adresář — všechny relativní cesty (004_types.db,
    # spool_chunks, state) sletí sem, produkční soubory zůstanou nedotčené.
    workdir = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "test_run"))
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    os.chdir(workdir)

    json_files = pipeline.find_json_files([args.data_dir])
    if not json_files:
        print(f"❌ Žádná data v {args.data_dir}")
        sys.exit(1)
    print(f"🧪 Test: {args.max_laws} zákonů -> index '{args.index}'")

    stats = pipeline.process_laws(
        json_files=json_files,
        es_url=args.es_url,
        index=args.index,
        num_workers=args.workers,
        state_file=os.path.join(workdir, "test_state.json"),
        max_laws=args.max_laws,
        force=True,
    )

    # ---- Ověření výsledku ----
    es = Elasticsearch([args.es_url], request_timeout=60)
    if not es.indices.exists(index=args.index):
        print("❌ FAIL: testovací index vůbec nevznikl!")
        sys.exit(1)

    # Bulk se nemusel ještě refreshnout (default interval 1 s)
    es.indices.refresh(index=args.index)

    count = es.count(index=args.index)["count"]
    laws_in_index = Counter()
    sample = None
    for hit in es.search(index=args.index, size=1000)["hits"]["hits"]:
        src = hit["_source"]
        laws_in_index[src["id_zakona"]] += 1
        if sample is None:
            sample = src

    print("\n" + "=" * 70)
    print(f"Vložených dokumentů (paragrafů): {count}")
    print(f"Zákonů v indexu: {len(laws_in_index)}")
    for iri, n in sorted(laws_in_index.items()):
        print(f"  {n:>3}x  {iri}")

    ok = True
    if count == 0:
        print("❌ FAIL: index je prázdný!")
        ok = False
    elif sample:
        emb = sample["paragrafy"][0].get("vektor", [])
        text_preview = (sample["paragrafy"][0]["text"] or "")[:120]
        print(f"\nUkázkový dokument:")
        print(f"  zákon:   {sample['id_zakona']}")
        print(f"  název:   {sample['akt_nazev']}")
        print(f"  citace:  {sample['paragrafy'][0]['citace']}")
        print(f"  text:    {text_preview}...")
        print(f"  vektor:  {len(emb)} dimenzí")
        if len(emb) != 768:
            print("❌ FAIL: embedding nemá 768 dimenzí!")
            ok = False

    if ok:
        print("\n✅ TEST PROŠEL")
    else:
        print("\n❌ TEST SELHAL")

    if args.cleanup:
        es.indices.delete(index=args.index, ignore=[400, 404])
        os.chdir("..")
        shutil.rmtree(workdir, ignore_errors=True)
        print("🧹 Uklizeno (index + test_run/)")
    else:
        print(f"\nIndex '{args.index}' a adresář test_run/ ponechány pro inspectci.")
        print(f"Smažeš je: python3 {__file__} --cleanup "
              f"--index {args.index}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
