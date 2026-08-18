#!/usr/bin/env python3
"""
Zpracování PDF zákonů — stahování + parallel processing do Elasticsearch.

Workflow ve dvou fázích:
  Fáze 1 (sequential download): Načte zákony z API, stáhne PDF, checkpoint do SQLite
  Fáze 2 (parallel processing): ProcessPoolExecutor workers — pdfplumber → embedding → ES

Použití:
    python3 zpracovani_pdf_ingest.py --limit 100
    python3 zpracovani_pdf_ingest.py --skip-download --workers 4
    python3 zpracovani_pdf_ingest.py --skip-process --data-dir ./pdf-data

"""

import argparse, gzip, io, json, logging, os, re, sqlite3, sys, time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# --- Import z pipeline.py ---
sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline import (
    embed_and_bulk_insert,
    create_es_index,
    EMBEDDING_MODEL,
    INDEX_NAME,
    ES_HOST,
)

# --- Konfigurace ---
OPENDATA_BASE = "https://opendata.eselpoint.gov.cz/datove-sady-esbirka/"
PRAVNI_AKT_URL = OPENDATA_BASE + "002PravniAkt.json.gz"
SBIRKA_BASE = "https://e-sbirka.gov.cz"
OVERENE_ZNENI_URL = SBIRKA_BASE + "/sbr-externi/stahni/overene-zneni/{document_id}"
SLOZKA_URL = SBIRKA_BASE + "/souborove-sluzby/soubory/{document_id}"

HTTP_TIMEOUT = 30
REQUEST_DELAY = 0.3
FILENAME_PATTERN = re.compile(r'Sb_(\d{4})_(\d+)_')
CITACE_PATTERN = re.compile(r'(\d+/\d{4}\s+Sb\.)')
PARAGRAPH_PATTERN = re.compile(r'§\s*(\d+)\s*[,.;]?\s*\n?(.*)', re.DOTALL)
PDF_INDEX_NAME = "zakony-pdf"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# =====================================================================
# SQLite checkpoint
# =====================================================================

class PDFCheckpoint:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS law_status (
                id_zakona TEXT PRIMARY KEY,
                phase TEXT NOT NULL DEFAULT 'not_started',
                pdf_path TEXT,
                pdf_sha256 TEXT,
                paragraphs_count INTEGER,
                error_msg TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS law_summary (
                id_zakona TEXT PRIMARY KEY,
                nazev TEXT,
                rok INTEGER,
                paragrafu_pocet INTEGER,
                phase TEXT,
                completed_at TIMESTAMP
            )
        """)
        self.conn.commit()

    def update_phase(self, id_zakona: str, phase: str, pdf_path: str = None,
                     paragraphs_count: int = None, error_msg: str = None):
        self.conn.execute("""
            INSERT INTO law_status (id_zakona, phase, pdf_path, paragraphs_count, error_msg)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id_zakona) DO UPDATE SET
                phase=excluded.phase,
                pdf_path=excluded.pdf_path,
                paragraphs_count=excluded.paragraphs_count,
                error_msg=excluded.error_msg,
                updated_at=CURRENT_TIMESTAMP
        """, (id_zakona, phase, pdf_path, paragraphs_count, error_msg))
        self.conn.commit()

    def get_laws_by_phase(self, phase: str) -> List[Tuple]:
        cur = self.conn.execute(
            "SELECT id_zakona, pdf_path, pdf_sha256, paragraphs_count, error_msg FROM law_status WHERE phase=?",
            (phase,)
        )
        return cur.fetchall()

    def update_summary(self, id_zakona: str, nazev: str, rok: int, paragrafu_pocet: int, phase: str):
        self.conn.execute("""
            INSERT INTO law_summary (id_zakona, nazev, rok, paragrafu_pocet, phase, completed_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id_zakona) DO UPDATE SET
                nazev=excluded.nazev,
                rok=excluded.rok,
                paragrafu_pocet=excluded.paragrafu_pocet,
                phase=excluded.phase,
                completed_at=CURRENT_TIMESTAMP
        """, (id_zakona, nazev, rok, paragrafu_pocet, phase))
        self.conn.commit()

    def close(self):
        self.conn.close()


# =====================================================================
# Stahování PDF (Fáze 1)
# =====================================================================

def fetch_json_gz(url: str, logger: logging.Logger) -> dict:
    logger.info(f"Stahuji data z: {url}")
    try:
        req = Request(url, headers={"Accept": "application/gzip"})
        with urlopen(req, timeout=HTTP_TIMEOUT) as response:
            raw_data = response.read()
        logger.info(f"Staženo {len(raw_data):,} bajtů")
    except (HTTPError, URLError) as e:
        logger.error(f"Chyba při stahování: {e}")
        raise

    with gzip.GzipFile(fileobj=io.BytesIO(raw_data)) as f:
        raw_json = f.read().decode("utf-8")

    return json.loads(raw_json)


def get_zneni_items(data: dict) -> list:
    if isinstance(data, dict):
        return data.get("položky", data.get("items", []))
    return data if isinstance(data, list) else []


def parse_law_key(citace: str) -> tuple:
    match = re.search(r"(\d{4})\s+Sb\.", citace)
    year = int(match.group(1)) if match else 0
    match_num = re.match(r"(\d+)", citace)
    number = int(match_num.group(1)) if match_num else 0
    return (year, number)


def load_laws(limit: int, min_year: int = None, logger: logging.Logger = None) -> list:
    data = fetch_json_gz(PRAVNI_AKT_URL, logger)
    items = get_zneni_items(data)
    if logger:
        logger.info(f"Načteno {len(items):,} právních aktů")

    laws = []
    for item in items:
        citace = item.get("akt-citace", "")
        rok = item.get("akt-rok-předpisu", 0)
        cislo = item.get("akt-číslo-předpisu", "0")
        nazev = item.get("akt-název-vyhlášený", "")
        zneni = item.get("právní-akt-znění", [])

        last_doc_id = None
        if zneni:
            last_zneni = zneni[-1]
            last_doc_id = last_zneni.get("znění-dokument-id")

        if last_doc_id:
            laws.append({
                "citace": citace,
                "rok": rok,
                "cislo": cislo,
                "nazev": nazev,
                "doc_id": last_doc_id,
                "key": parse_law_key(citace),
            })

    laws.sort(key=lambda x: x["key"])
    if logger:
        logger.info(f"Po platných zákonech s dokument-ID: {len(laws):,}")

    if min_year is not None:
        laws = [l for l in laws if l["rok"] >= min_year]
        if logger:
            logger.info(f"Filtrováno na roky >= {min_year}: {len(laws):,} zákonů")

    if limit > 0:
        laws = laws[:limit]
        if logger:
            logger.info(f"Omezeno na prvních {limit} zákonů")

    return laws


def get_pdf_download_url(doc_id: int, logger: logging.Logger) -> dict | None:
    url = OVERENE_ZNENI_URL.format(document_id=doc_id)
    try:
        req = Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; ZakonyStahovac/1.0)"
        })
        with urlopen(req, timeout=HTTP_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))

        if "chyby" in result:
            error = result["chyby"][0]
            logger.debug(f"API error pro doc_id={doc_id}: {error.get('kod', '?')} - {error.get('popis', '?')}")
            return None

        stav = result.get("stavPozadavku", "")
        if stav != "OK":
            logger.debug(f"stavPozadavku={stav} pro doc_id={doc_id}")
            return None

        return {
            "document_id": result.get("id"),
            "nazevDokumentu": result.get("nazevDokumentu", ""),
        }
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        logger.debug(f"Chyba API pro doc_id={doc_id}: {e}")
        return None


def download_pdf(document_uuid: str, output_path: Path, logger: logging.Logger) -> bool:
    url = SLOZKA_URL.format(document_id=document_uuid)
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ZakonyStahovac/1.0)"
        })
        with urlopen(req, timeout=HTTP_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            content = response.read()

        if "application/pdf" not in content_type:
            logger.debug(f"Ne-PDF content-type: {content_type} (pro {output_path.name})")
            return False

        output_path.write_bytes(content)
        return True
    except (HTTPError, URLError) as e:
        logger.debug(f"Chyba stahování PDF pro {output_path.name}: {e}")
        return False


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[\s]+', '_', name).strip()
    return name[:150]


def download_phase(data_dir: str, limit: int, min_year: int = None, checkpoint: PDFCheckpoint = None, logger: logging.Logger = None):
    logger.info("=" * 70)
    logger.info("FÁZE 1: DOWNLOAD — sequential stahování PDF")
    logger.info("=" * 70)

    laws = load_laws(limit, min_year, logger)
    if not laws:
        logger.error("Nebyly nalezeny žádné zákony!")
        sys.exit(1)

    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)

    stats = {"success": 0, "no_pdf": 0, "error": 0, "skipped": 0, "scan": 0, "resume": 0}
    failed_docs = []

    for i, law in enumerate(laws, 1):
        id_zakona = f"{law['rok']}_{law['cislo']}"
        phase = checkpoint.conn.execute(
            "SELECT phase FROM law_status WHERE id_zakona=?", (id_zakona,)
        ).fetchone()

        if phase and phase[0] == "downloaded":
            logger.info(f"[{i}/{len(laws)}] {law['citace']} — PŘESKOČENO (staženo)")
            stats["resume"] += 1
            continue

        checkpoint.update_phase(id_zakona, "downloading")

        logger.info(f"[{i}/{len(laws)}] {law['citace']} - {law['nazev'][:60]} (doc_id={law['doc_id']})")

        pdf_info = get_pdf_download_url(law["doc_id"], logger)

        if pdf_info is None:
            logger.info(f"  -> ŽÁDNÉ PDF (scan papíru / chybí v digitální podobě)")
            checkpoint.update_phase(id_zakona, "error", error_msg="no_pdf")
            stats["no_pdf"] += 1
            failed_docs.append({"citace": law["citace"], "doc_id": law["doc_id"], "reason": "no_pdf"})
            time.sleep(REQUEST_DELAY)
            continue

        pdf_filename = pdf_info.get("nazevDokumentu", f"{law['rok']}_{law['cislo']}.pdf")
        pdf_path = data_path / pdf_filename

        if pdf_path.exists():
            logger.info(f"  -> UŽ EXISTUJE (přeskakuji)")
            checkpoint.update_phase(id_zakona, "downloaded", pdf_path=str(pdf_path))
            checkpoint.update_summary(id_zakona, law["nazev"], law["rok"], 0, "downloaded")
            stats["skipped"] += 1
            time.sleep(REQUEST_DELAY)
            continue

        logger.info(f"  -> Stahuji PDF: {pdf_filename}")
        success = download_pdf(pdf_info["document_id"], pdf_path, logger)

        if success:
            size_kb = pdf_path.stat().st_size / 1024
            scan = is_scan_pdf(str(pdf_path), logger)
            if scan:
                logger.info(f"  -> SKEN (přeskakuji zpracování, velikost: {size_kb:.1f} KB)")
                checkpoint.update_phase(id_zakona, "scan", pdf_path=str(pdf_path))
                checkpoint.update_summary(id_zakona, law["nazev"], law["rok"], 0, "scan")
                stats["scan"] += 1
            else:
                logger.info(f"  -> ÚSPĚŠNÉ ({size_kb:.1f} KB)")
                checkpoint.update_phase(id_zakona, "downloaded", pdf_path=str(pdf_path))
                checkpoint.update_summary(id_zakona, law["nazev"], law["rok"], 0, "downloaded")
                stats["success"] += 1
        else:
            logger.info(f"  -> CHYBA (není PDF / stažení selhalo)")
            checkpoint.update_phase(id_zakona, "error", error_msg="download_failed")
            stats["error"] += 1
            failed_docs.append({"citace": law["citace"], "doc_id": law["doc_id"], "reason": "download_error"})

        time.sleep(REQUEST_DELAY)

    elapsed = time.time() - next((t for t in [time.time()] if True), time.time())
    logger.info("=" * 70)
    logger.info("SHRNUTÍ DOWNLOAD")
    logger.info(f"Celkem: {len(laws)}")
    logger.info(f"PDF staženo: {stats['success']}")
    logger.info(f"Skenu (přeskočeno): {stats['scan']}")
    logger.info(f"Bez PDF (skeny): {stats['no_pdf']}")
    logger.info(f"Chyby: {stats['error']}")
    logger.info(f"Přeskočeno (existuje): {stats['skipped']}")
    logger.info(f"Resume (už staženo): {stats['resume']}")
    logger.info("=" * 70)

    if failed_docs:
        failed_path = Path(data_dir) / "failed.json"
        failed_path.write_text(json.dumps(failed_docs, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Selhané zákony uloženy: {failed_path}")

    return stats


# =====================================================================
# PDF extrakce a parsování
# =====================================================================

def is_scan_pdf(pdf_path: str, logger: logging.Logger) -> bool:
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text and len(text.strip()) > 0:
                    return False
            return True
    except Exception as e:
        logger.warning(f"Nemohu detekovat sken {pdf_path}: {e}")
        return True


def extract_text_from_pdf(pdf_path: str, logger: logging.Logger) -> str | None:
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
    except Exception as e:
        logger.debug(f"pdfplumber selhal pro {pdf_path}: {e}")
        try:
            import pypdf
            reader = pypdf.PdfReader(pdf_path)
            pages = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
        except Exception as e2:
            logger.debug(f"pypdf selhal pro {pdf_path}: {e2}")
            return None


def extract_paragraphs(text: str, law_info: dict) -> List[dict]:
    chunks = []
    law_citace = f"{law_info['cislo']}/{law_info['rok']} Sb."

    lines = text.split('\n')
    current_para_text = []
    current_para_num = None

    for line in lines:
        match = re.match(r'^§\s*(\d+)\s*[,.;]?\s*$', line.strip())
        if match:
            if current_para_text and current_para_num:
                full_text = "§ " + current_para_num + " " + "\n".join(current_para_text).strip()
                chunks.append({
                    "citace": "§ " + current_para_num,
                    "text": full_text,
                    "typ": "PDF",
                    "id_zakona": f"{law_info['rok']}_{law_info['cislo']}",
                    "akt_citace": law_citace,
                    "akt_nazev": law_info.get("nazev", ""),
                    "rok": law_info["rok"],
                })
                current_para_text = []
            current_para_num = match.group(1)
        elif current_para_num:
            if line.strip():
                current_para_text.append(line)

    if current_para_text and current_para_num:
        full_text = "§ " + current_para_num + " " + "\n".join(current_para_text).strip()
        chunks.append({
            "citace": "§ " + current_para_num,
            "text": full_text,
            "typ": "PDF",
            "id_zakona": f"{law_info['rok']}_{law_info['cislo']}",
            "akt_citace": law_citace,
            "akt_nazev": law_info.get("nazev", ""),
            "rok": law_info["rok"],
        })

    return chunks


def parse_law_filename(filename: str) -> dict | None:
    match = FILENAME_PATTERN.search(filename)
    if match:
        rok = int(match.group(1))
        cislo = int(match.group(2))
        return {"rok": rok, "cislo": str(cislo)}
    return None


# =====================================================================
# Worker funkce pro parallel processing (Fáze 2)
# =====================================================================

def _process_pdf_worker(args):
    """
    Worker pro ProcessPoolExecutor.
    Zpracuje jeden zákon: pdfplumber → paragrafy → embedding → ES → SQLite update.
    
    Args:
        args: Tuple (id_zakona, pdf_path, law_info, es_url, index_name, model, db_path)
    
    Returns:
        Tuple (id_zakona, paragraphs_count, success, error_msg, is_scan)
    """
    id_zakona, pdf_path, law_info, es_url, index_name, model, db_path = args

    try:
        logger = logging.getLogger(f"worker-{id_zakona}")

        # PDF extrakce
        text = extract_text_from_pdf(pdf_path, logger)
        if not text:
            # Zkus detekovat sken
            try:
                scan = is_scan_pdf(pdf_path, logger)
            except Exception:
                scan = False
            return id_zakona, 0, False, "No text extracted", scan

        # Paragrafy
        paragraphs = extract_paragraphs(text, law_info)
        if not paragraphs:
            return id_zakona, 0, False, "No paragraphs found", False

        # Build batch doc
        batch_docs = [{
            "id_zakona": p["id_zakona"],
            "akt_citace": p["akt_citace"],
            "akt_nazev": p["akt_nazev"],
            "rok": p["rok"],
            "datum_od": None,
            "datum_do": None,
            "je_zrusen": False,
            "sbírka": "",
            "paragrafy": [{
                "citace": p["citace"],
                "text": p["text"],
                "typ": p["typ"],
                "vektor": None,
            } for p in paragraphs],
        }]

        # Embedding + ES insert
        eng = SentenceTransformer(model)
        es = Elasticsearch([es_url], request_timeout=60, retry_on_timeout=True, max_retries=3)

        success, errors = embed_and_bulk_insert(batch_docs, es, eng, index_name, bulk_batch_size=50)

        # SQLite update
        conn = sqlite3.connect(db_path)
        conn.execute("""
            INSERT INTO law_status (id_zakona, phase, paragraphs_count)
            VALUES (?, 'done', ?)
            ON CONFLICT(id_zakona) DO UPDATE SET
                phase='done',
                paragraphs_count=excluded.paragraphs_count,
                updated_at=CURRENT_TIMESTAMP
        """, (id_zakona, len(paragraphs)))
        conn.execute("""
            INSERT INTO law_summary (id_zakona, paragrafu_pocet, phase, completed_at)
            VALUES (?, ?, 'done', CURRENT_TIMESTAMP)
            ON CONFLICT(id_zakona) DO UPDATE SET
                paragrafu_pocet=excluded.paragrafu_pocet,
                phase='done',
                completed_at=CURRENT_TIMESTAMP
        """, (id_zakona, len(paragraphs)))
        conn.commit()
        conn.close()

        return id_zakona, len(paragraphs), success > 0, None, False

    except Exception as e:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            INSERT INTO law_status (id_zakona, phase, error_msg)
            VALUES (?, 'error', ?)
            ON CONFLICT(id_zakona) DO UPDATE SET
                phase='error',
                error_msg=excluded.error_msg,
                updated_at=CURRENT_TIMESTAMP
        """, (id_zakona, str(e)))
        conn.commit()
        conn.close()
        return id_zakona, 0, False, str(e), False


# =====================================================================
# Processing phase (Fáze 2)
# =====================================================================

def process_phase(data_dir: str, es_url: str, model: str, index_name: str,
                  chunk_size: int, num_workers: int, checkpoint: PDFCheckpoint,
                  logger: logging.Logger):
    logger.info("=" * 70)
    logger.info(f"FÁZE 2: PROCESSING — parallel (workers={num_workers}, chunk_size={chunk_size})")
    logger.info("=" * 70)

    # Načíst stažená PDF
    laws_to_process = checkpoint.get_laws_by_phase("downloaded")
    if not laws_to_process:
        logger.info("Žádná PDF ke zpracování.")
        return {"processed": 0, "scan": 0, "errors": 0}

    logger.info(f"PDF ke zpracování: {len(laws_to_process)}")

    # Rozdělit na chunky
    chunks = []
    for i in range(0, len(laws_to_process), chunk_size):
        chunks.append(list(laws_to_process[i:i + chunk_size]))

    logger.info(f"Rozděleno na {len(chunks)} chunků")

    # Parallel processing
    total_success = 0
    total_scan = 0
    total_errors = 0
    total_paragraphs = 0
    law_info_cache = {}

    for chunk_idx, chunk in enumerate(chunks):
        logger.info(f"\nChunk {chunk_idx + 1}/{len(chunks)} — {len(chunk)} zákonů")

        worker_args = []
        for id_zakona, pdf_path, pdf_sha256, para_count, error_msg in chunk:
            # Parse law info from id (rok_cislo)
            parts = id_zakona.split('_')
            if len(parts) == 2:
                law_info = {"rok": int(parts[0]), "cislo": parts[1]}
            else:
                law_info = {"rok": 0, "cislo": "0"}

            worker_args.append((id_zakona, pdf_path, law_info, es_url, index_name, model, checkpoint.db_path))

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(_process_pdf_worker, args): args[0]
                for args in worker_args
            }

            for future in as_completed(futures):
                batch_idx = futures[future]
                try:
                    result = future.result()
                    id_zakona, para_count, success, error_msg, is_scan = result
                    if success:
                        total_success += 1
                        total_paragraphs += para_count
                        logger.info(f"  {id_zakona}: OK ({para_count} paragrafů)")
                    elif is_scan:
                        total_scan += 1
                        logger.info(f"  {id_zakona}: SKEN (přeskočeno)")
                        # Update DB phase to scan
                        conn = sqlite3.connect(checkpoint.db_path)
                        conn.execute("""
                            INSERT INTO law_status (id_zakona, phase)
                            VALUES (?, 'scan')
                            ON CONFLICT(id_zakona) DO UPDATE SET
                                phase='scan',
                                updated_at=CURRENT_TIMESTAMP
                        """, (id_zakona,))
                        conn.commit()
                        conn.close()
                    else:
                        total_errors += 1
                        logger.error(f"  {id_zakona}: FAILED ({error_msg})")
                except Exception as e:
                    total_errors += 1
                    logger.error(f"  {batch_idx}: EXCEPTION ({e})")

    logger.info("=" * 70)
    logger.info("SHRNUTÍ PROCESSING")
    logger.info(f"Zpracováno: {total_success}")
    logger.info(f"Paragrafů celkem: {total_paragraphs}")
    logger.info(f"Skenu (přeskočeno): {total_scan}")
    logger.info(f"Chyby: {total_errors}")
    logger.info("=" * 70)

    return {"processed": total_success, "scan": total_scan, "paragraphs": total_paragraphs, "errors": total_errors}


# =====================================================================
# Main
# =====================================================================

def main():
    start_time = time.time()
    
    parser = argparse.ArgumentParser(
        description="Zpracování PDF zákonů — download + parallel processing do Elasticsearch"
    )
    parser.add_argument("--limit", type=int, default=100,
                        help="Počet zákonů ke stažení (0 = všechny, výchozí: 100)")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="Adresář pro PDF data (výchozí: ./zpracovani-pdf/data)")
    parser.add_argument("--es-url", default=ES_HOST, help="Elasticsearch URL")
    parser.add_argument("--model", default=EMBEDDING_MODEL, help="Embedding model")
    parser.add_argument("--index", default=PDF_INDEX_NAME, help="ES index name")
    parser.add_argument("--workers", type=int, default=3,
                        help="Počet parallel workers (výchozí: 3)")
    parser.add_argument("--chunk-size", type=int, default=50,
                        help="Chunk size pro parallel processing (výchozí: 50)")
    parser.add_argument("--skip-download", action="store_true",
                        help="Přeskočit download fázi (jen processing)")
    parser.add_argument("--skip-process", action="store_true",
                        help="Přeskočit processing fáze (jen download)")
    parser.add_argument("--state-db", type=str, default=None,
                        help="Cesta k SQLite state databázi (výchozí: ./zpracovani-pdf/data/state.db)")
    parser.add_argument("--min-year", type=int, default=None,
                        help="Filtrovat zákony od roku (výchozí: žádný filtr)")
    args = parser.parse_args()

    # Nastavit cesty
    script_dir = Path(__file__).parent
    data_dir = args.data_dir or str(script_dir / "data")
    state_db = args.state_db or str(script_dir / "data" / "state.db")

    os.makedirs(data_dir, exist_ok=True)

    log.info("=" * 70)
    log.info("ZPRACOVANI PDF ZAKONU — PDF Ingestor")
    log.info(f"Data dir: {data_dir}")
    log.info(f"State DB: {state_db}")
    log.info(f"ES URL: {args.es_url}")
    log.info(f"Workers: {args.workers}, Chunk size: {args.chunk_size}")
    log.info("=" * 70)

    # Inicializace checkpoint
    checkpoint = PDFCheckpoint(state_db)

    # --- Fáze 1: Download ---
    if not args.skip_download:
        download_stats = download_phase(data_dir, args.limit, args.min_year, checkpoint, log)
    else:
        log.info("PŘESKOČENA DOWNLOAD FÁZE")
        download_stats = {}

    # --- Fáze 2: Processing ---
    if not args.skip_process:
        log.info("\nPřipojuji k Elasticsearch...")
        from elasticsearch import Elasticsearch
        es = Elasticsearch([args.es_url], request_timeout=60, retry_on_timeout=True, max_retries=3)
        if not es.ping():
            log.error("ES nedostupný!")
            sys.exit(1)
        log.info("Připojeno k Elasticsearch")

        create_es_index(es, args.index)

        process_stats = process_phase(
            data_dir=data_dir,
            es_url=args.es_url,
            model=args.model,
            index_name=args.index,
            chunk_size=args.chunk_size,
            num_workers=args.workers,
            checkpoint=checkpoint,
            logger=log,
        )
    else:
        log.info("PŘESKOČENA PROCESSING FÁZE")
        process_stats = {}

    checkpoint.close()

    elapsed = time.time() - start_time
    log.info("\n" + "=" * 70)
    log.info("KONEC")
    if download_stats:
        log.info(f"Download: {download_stats}")
    if process_stats:
        log.info(f"Processing: {process_stats}")
    log.info(f"CELKOVÝ ČAS: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
