#!/usr/bin/env python3
"""
Testovací skript pro zkušební ingest PDF zákonů.

Workflow:
  1. Stáhne prvních 10 zákonů (download-only režim)
  2. Ukončí skript s instrukcemi pro resume
  3. Po restartu pokračuje tam kde skončil

Použití:
  # Krok 1: Stáhnout první 10 zákonů
  python3 test_ingest.py

  # Krok 2: Spustit processing (nebo restartovat celý skript pro resume)
  python3 test_ingest.py --skip-download

  # Krok 3: Kontrola SQLite
  python3 test_ingest.py --check-db
"""

import argparse, os, sys, time, logging
from pathlib import Path

# --- Automaticky použij venv pokud je dostupný ---
_VENV_PYTHON = Path(__file__).parent.parent / ".venv" / "bin" / "python"
if not hasattr(sys, 'real_prefix') and _VENV_PYTHON.exists():
    # Pokud nejsme ve venv, zkus ho přidat do path
    _venv_site = _VENV_PYTHON.parent.parent / ".venv" / "lib"
    if not _venv_site.exists():
        _venv_site = Path(__file__).parent.parent / ".venv" / "lib"
    for p in sorted(_venv_site.glob("python3.*")):
        site_pkgs = p / "site-packages"
        if site_pkgs.exists():
            sys.path.insert(0, str(site_pkgs))
            break

# Přidat project root do path pro import pipeline.py a zpracovani_pdf_ingest
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from zpracovani_pdf_ingest import (
    download_phase,
    process_phase,
    PDFCheckpoint,
    INDEX_NAME,
    ES_HOST,
    EMBEDDING_MODEL,
)

log = logging.getLogger("test")


def check_es(es_url: str):
    from elasticsearch import Elasticsearch
    es = Elasticsearch([es_url], request_timeout=10)
    if es.ping():
        print("✅ Elasticsearch je dostupný")
        return es
    else:
        print("❌ Elasticsearch není dostupný na", es_url)
        sys.exit(1)


def cmd_download(args):
    """Stáhne prvních N zákonů."""
    checkpoint = PDFCheckpoint(args.state_db)
    try:
        print("\n" + "=" * 70)
        print("KROK 1: STAHOVÁNÍ PRVNÍCH 10 ZÁKONŮ")
        print("=" * 70 + "\n")

        stats = download_phase(args.data_dir, 10, args.min_year, checkpoint, log)

        print("\n" + "=" * 70)
        print("STAHOVÁNÍ HOTOVO!")
        print(f"  Staženo: {stats['success']}")
        print(f"  Skenu: {stats['scan']}")
        print(f"  Bez PDF: {stats['no_pdf']}")
        print(f"  Chyby: {stats['error']}")
        print(f"  Přeskočeno: {stats['skipped']}")
        print("=" * 70)

        print("\n📋 DALŠÍ KROKY:")
        print("  1. Spusť processing:")
        print("     python3 test_ingest.py --skip-download")
        print("")
        print("  2. Restartuj celý skript (resume):")
        print("     python3 test_ingest.py")
        print("")
        print("  3. Zkontroluj databázi:")
        print("     python3 test_ingest.py --check-db")

    finally:
        checkpoint.close()


def cmd_process(args):
    """Spustí processing stažených PDF."""
    es = check_es(args.es_url)

    checkpoint = PDFCheckpoint(args.state_db)
    try:
        print("\n" + "=" * 70)
        print("KROK 2: PROCESSING PDF → Elasticsearch")
        print("=" * 70 + "\n")

        # Vytvořit ES index
        from pipeline import create_es_index
        create_es_index(es, args.index)

        stats = process_phase(
            data_dir=args.data_dir,
            es_url=args.es_url,
            model=args.model,
            index_name=args.index,
            chunk_size=args.chunk_size,
            num_workers=args.workers,
            checkpoint=checkpoint,
            logger=log,
        )

        print("\n" + "=" * 70)
        print("PROCESSING HOTOVO!")
        print(f"  Zpracováno: {stats.get('processed', 0)}")
        print(f"  Paragrafů: {stats.get('paragraphs', 0)}")
        print(f"  Skenu: {stats.get('scan', 0)}")
        print(f"  Chyby: {stats.get('errors', 0)}")
        print("=" * 70)

    finally:
        checkpoint.close()


def cmd_resume(args):
    """Full resume — stáhne chybějící PDF + processing."""
    es = check_es(args.es_url)

    checkpoint = PDFCheckpoint(args.state_db)
    try:
        print("\n" + "=" * 70)
        print("RESUME — pokračování tam kde jste skončili")
        print("=" * 70 + "\n")

        # Stáhnout chybějící
        stats = download_phase(args.data_dir, 10, args.min_year, checkpoint, log)
        print(f"\n✅ Download hotovo: {stats['success']} staženo, {stats['skipped']} přeskočeno")

        # Processing
        from pipeline import create_es_index
        create_es_index(es, args.index)

        stats = process_phase(
            data_dir=args.data_dir,
            es_url=args.es_url,
            model=args.model,
            index_name=args.index,
            chunk_size=args.chunk_size,
            num_workers=args.workers,
            checkpoint=checkpoint,
            logger=log,
        )

        print("\n" + "=" * 70)
        print("RESUME HOTOVO!")
        print(f"  Zpracováno: {stats.get('processed', 0)}")
        print(f"  Paragrafů: {stats.get('paragraphs', 0)}")
        print("=" * 70)

    finally:
        checkpoint.close()


def cmd_check_db(args):
    """Zobrazí stav z SQLite databáze."""
    checkpoint = PDFCheckpoint(args.state_db)

    print("\n" + "=" * 70)
    print("STAV Z DATABASE")
    print("=" * 70 + "\n")

    # law_status
    rows = checkpoint.conn.execute("SELECT id_zakona, phase, paragraphs_count, pdf_path FROM law_status ORDER BY id_zakona").fetchall()

    if not rows:
        print("⚠️  Databáze je prázdná")
    else:
        print(f"{'ID':<20} {'Fáze':<15} {'Paragrafy':<10} {'PDF path'}")
        print("-" * 70)
        for row in rows:
            id_z, phase, para_count, pdf_path = row
            pdf_name = Path(pdf_path).name if pdf_path else "—"
            print(f"{id_z:<20} {phase:<15} {str(para_count or 0):<10} {pdf_name}")

    print(f"\nCelkem: {len(rows)} zákonů")

    # law_summary
    summary = checkpoint.conn.execute(
        "SELECT COUNT(*) FROM law_summary WHERE phase='done'"
    ).fetchone()
    done_count = summary[0] if summary else 0
    print(f"Zpracováno: {done_count}")

    scan_count = checkpoint.conn.execute(
        "SELECT COUNT(*) FROM law_summary WHERE phase='scan'"
    ).fetchone()
    scan_total = scan_count[0] if scan_count else 0
    print(f"Skenu (přeskočeno): {scan_total}")

    # phase breakdown
    phase_counts = checkpoint.conn.execute(
        "SELECT phase, COUNT(*) FROM law_status GROUP BY phase"
    ).fetchall()

    print("\nRozdělení podle fáze:")
    for phase, count in phase_counts:
        print(f"  {phase}: {count}")

    checkpoint.close()


def main():
    parser = argparse.ArgumentParser(
        description="Testovací skript pro zkušební ingest PDF zákonů",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Příklady:
  # Krok 1: Stáhnout prvních 10 zákonů
  python3 test_ingest.py

  # Krok 2: Spustit processing
  python3 test_ingest.py --skip-download

  # Full resume
  python3 test_ingest.py --resume

  # Kontrola databáze
  python3 test_ingest.py --check-db
        """
    )

    parser.add_argument("--data-dir", type=str, default=None,
                        help="Adresář pro PDF data")
    parser.add_argument("--es-url", default=ES_HOST, help="Elasticsearch URL")
    parser.add_argument("--model", default=EMBEDDING_MODEL, help="Embedding model")
    parser.add_argument("--index", default=INDEX_NAME, help="ES index name")
    parser.add_argument("--workers", type=int, default=2,
                        help="Počet parallel workers (výchozí: 2)")
    parser.add_argument("--chunk-size", type=int, default=10,
                        help="Chunk size (výchozí: 10)")
    parser.add_argument("--state-db", type=str, default=None,
                        help="Cesta k SQLite state databázi")
    parser.add_argument("--min-year", type=int, default=None,
                        help="Filtrovat zákony od roku (výchozí: žádný filtr)")
    parser.add_argument("--skip-download", action="store_true",
                        help="Pouze processing (přeskočit download)")
    parser.add_argument("--resume", action="store_true",
                        help="Full resume — download chybějících + processing")
    parser.add_argument("--check-db", action="store_true",
                        help="Zobrazit stav z databáze")

    args = parser.parse_args()

    # Defaultní cesty
    script_dir = Path(__file__).parent
    if not args.data_dir:
        args.data_dir = str(script_dir / "data")
    if not args.state_db:
        args.state_db = str(script_dir / "data" / "state.db")

    os.makedirs(args.data_dir, exist_ok=True)

    # Na začátku vždycky smaž stará data pro čistý test
    db_file = Path(args.state_db)
    if db_file.exists():
        db_file.unlink()
        log.info(f"Smazána stará state.db: {db_file}")

    pdf_dir = Path(args.data_dir)
    pdf_files = list(pdf_dir.glob("*.pdf"))
    if pdf_files:
        for f in pdf_files:
            f.unlink()
        log.info(f"Smazáno {len(pdf_files)} starých PDF souborů")

    # Spustit příkaz
    if args.check_db:
        cmd_check_db(args)
    elif args.resume:
        cmd_resume(args)
    elif args.skip_download:
        cmd_process(args)
    else:
        cmd_download(args)


if __name__ == "__main__":
    main()
