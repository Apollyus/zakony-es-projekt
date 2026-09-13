#!/usr/bin/env python3
"""
Dvoufázová embedding pipeline pro stažené DOCX (informativní znění).

Výpočet embeddingů probíhá LOKÁLNĚ a výsledky se ukládají na disk —
Elasticsearch ani internet nejsou potřeba. Import do ES je samostatný
krok, který lze opakovat kdykoli bez přepočítávání.

Použití:
    python3 docx_embed.py parse  ./zakony    # Fáze A1: DOCX → paragrafy (SQLite)
    python3 docx_embed.py embed  ./zakony    # Fáze A2: paragrafy → vektory (SQLite)
    python3 docx_embed.py import ./zakony    # Fáze B:  vektory → Elasticsearch
    python3 docx_embed.py status ./zakony    # přehled pokroku

Každý dokument má stav: parsed → embedded → done. Přerušení nevadí,
při dalším spuštění pokračuje tam, kde skončil.

Testování bez GPU/modelu:
    python3 docx_embed.py embed ./zakony --fake-model   # náhodné vektory
"""

import argparse
import hashlib
import logging
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

# Stejné hodnoty jako v pipeline.py — bez importu (ten by táhl torch i ES)
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
ES_HOST = "http://localhost:9200"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

INDEX_NAME = "zakony-docx"
EMBED_DIM = 768

SECTION_PATTERNS = [
    (re.compile(r"^§\s*(\d+[a-zA-Z]?)\b"), "§ {}"),
    (re.compile(r"^[Čc]lánek\s+(\S+)"), "Článek {}"),
]
ROMAN = re.compile(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII)\.$")
CHUNK_CHARS = 2000


# =====================================================================
# Parsing DOCX → logické celky
# =====================================================================

def extract_units(texts: list, overlap: int = 0) -> list:
    """Rozdělí odstavce dokumentu na celky podle nadpisů (§ / Článek / římské)."""
    units, current_citace, current_lines = [], None, []

    def flush():
        text = "\n".join(current_lines).strip()
        if len(text) >= 30:
            units.append({"citace": current_citace or "Úvod", "text": text})

    for line in texts:
        matched = None
        for pattern, label in SECTION_PATTERNS:
            m = pattern.match(line)
            if m:
                matched = label.format(m.group(1))
                break
        if not matched and ROMAN.match(line):
            matched = f"Část {line[:-1]}"
        if matched:
            flush()
            current_citace, current_lines = matched, [line]
        else:
            current_lines.append(line)
    flush()

    result = []
    for u in units or [{"citace": "", "text": ""}]:
        result.extend(chunk_long_text(u["text"], citace=u["citace"],
                                      overlap=overlap))
    return [r for r in result if r["text"]]


def _overlap_tail(buf: str, overlap: int) -> str:
    """Konec buf (celé věty) o délce ~overlap znaků, jako začátek dalšího chunku."""
    if overlap <= 0:
        return ""
    tail = ""
    for s in reversed(re.split(r"(?<=[.!?])\s+", buf.strip())):
        if tail and len(tail) + len(s) + 1 > overlap:
            break
        tail = s + " " + tail
    return tail if len(tail.strip()) < len(buf.strip()) else ""


def chunk_long_text(text: str, citace: str = "", overlap: int = 0) -> list:
    """Dlouhé celky rozdělí na chunky po ~CHUNK_CHARS znacích.

    overlap > 0: každý další chunk začíná posledními větami předchozího
    chunku o celkové délce ~overlap znaků (klouzavé okno po větách).
    """
    if len(text) <= CHUNK_CHARS * 1.5:
        return [{"citace": citace or "Text", "text": text}]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, buf = [], ""
    for s in sentences:
        if buf and len(buf) + len(s) > CHUNK_CHARS:
            chunks.append(buf.strip())
            buf = _overlap_tail(buf, overlap)
        buf += s + " "
    if buf.strip():
        chunks.append(buf.strip())
    label = citace or "Text"
    return [{"citace": f"{label} ({i+1}/{len(chunks)})", "text": c}
            for i, c in enumerate(chunks)]


def docx_to_texts(path: Path) -> list:
    # docx jen pro parse fázi — embed/import bez wordů (a bez python-docx)
    from docx import Document
    doc = Document(str(path))
    return [t.strip() for t in (p.text for p in doc.paragraphs) if t.strip()]


def filename_to_meta(name: str):
    """Rozparsuje název na (id_zakona, rok, sbirka, cislo).

    Formáty vzniklé ze stahni-docx.py filename_from_citace:
      '1_1993.docx'         → sb,  cislo '1'     (key /sb/1993/1)
      'n100_1967.docx'      → sb,  cislo 'n100'  (key /sb/1967/n100)
      'o9_2002.docx'        → sb,  cislo 'o9'    (key /sb/2002/o9)
      '1_2000m.s..docx'     → sm,  cislo '1'     (key /sm/2000/1)
      'n1_2023m.s..docx'    → sm,  cislo 'n1'    (key /sm/2023/n1)
      'n1_1945Ú.l.I.docx'   → ul1, cislo 'n1'    (key /ul1/1945/n1)
      'n1_1953Ú.l..docx'    → ul0, cislo 'n1'    (key /ul0/1953/n1)
    """
    base = name[:-5] if name.endswith(".docx") else name
    m = re.match(r"^([a-zA-Z]?)(\d+)_(\d{4})(.*)$", base)
    if not m:
        return None, None, None, None
    prefix, cislo, rok, suffix = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    s = suffix.rstrip(".")
    if s.startswith("Ú.l"):
        sbirka = "ul1" if s.endswith("I") else "ul0"
    elif "m.s" in s:
        sbirka = "sm"
    else:
        sbirka = "sb"
    return f"{rok}_{prefix}{cislo}{s}", rok, sbirka, f"{prefix}{cislo}"


# =====================================================================
# SQLite úložiště (checkpoint + paragrafy + vektory)
# =====================================================================

class Store:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS laws (
                id_zakona TEXT PRIMARY KEY,
                phase TEXT NOT NULL DEFAULT 'parsed',
                docx_path TEXT,
                docx_sha256 TEXT,
                citace TEXT,
                nazev TEXT,
                rok INTEGER,
                sbirka TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS paragrafy (
                id_zakona TEXT,
                idx INTEGER,
                citace TEXT,
                text TEXT NOT NULL,
                vektor BLOB,
                PRIMARY KEY (id_zakona, idx)
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paragrafy_vektor "
            "ON paragrafy(id_zakona) WHERE vektor IS NULL")
        self.conn.commit()

    # --- fáze A1: parsing ---

    def law_state(self, id_zakona, sha=None):
        cur = self.conn.execute(
            "SELECT phase, docx_sha256 FROM laws WHERE id_zakona=?",
            (id_zakona,))
        row = cur.fetchone()
        if not row:
            return None
        return {"phase": row[0], "sha_matches": sha is None or row[1] == sha}

    def save_parsed(self, id_zakona, path, sha, citace, nazev, rok, sbirka, units):
        with self.conn:
            self.conn.execute("""
                INSERT INTO laws (id_zakona, phase, docx_path, docx_sha256,
                                  citace, nazev, rok, sbirka)
                VALUES (?, 'parsed', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id_zakona) DO UPDATE SET
                    phase='parsed', docx_path=excluded.docx_path,
                    docx_sha256=excluded.docx_sha256, citace=excluded.citace,
                    nazev=excluded.nazev, rok=excluded.rok,
                    sbirka=excluded.sbirka
            """, (id_zakona, str(path), sha, citace, nazev, rok, sbirka))
            self.conn.execute("DELETE FROM paragrafy WHERE id_zakona=?",
                              (id_zakona,))
            self.conn.executemany(
                "INSERT INTO paragrafy VALUES (?,?,?,?,NULL)",
                [(id_zakona, i, u["citace"], u["text"])
                 for i, u in enumerate(units)])

    def load_download_meta(self, data_dir: Path):
        """Názvy/citace ze stahovacího checkpointu (.checkpoint.db)."""
        meta = {}
        db = data_dir / ".checkpoint.db"
        if db.exists():
            try:
                mc = sqlite3.connect(db)
                meta = {r[0]: r for r in mc.execute(
                    "SELECT key, citace, nazev FROM status")}
                mc.close()
            except sqlite3.Error as e:
                log.warning("Nelze číst %s: %s", db, e)
        return meta

    # --- fáze A2: embedding ---

    def next_unembedded(self, batch_size: int):
        cur = self.conn.execute("""
            SELECT p.id_zakona, p.idx, p.text
            FROM paragrafy p JOIN laws l USING(id_zakona)
            WHERE p.vektor IS NULL AND l.phase != 'done'
            LIMIT ?
        """, (batch_size,))
        return cur.fetchall()

    def save_vectors(self, vectors: list):
        """vectors: [(id_zakona, idx, np.ndarray float32)]"""
        with self.conn:
            self.conn.executemany(
                "UPDATE paragrafy SET vektor=? WHERE id_zakona=? AND idx=?",
                [(np.asarray(v, dtype=np.float32).tobytes(), i, x)
                 for i, x, v in vectors])
            # dokumenty, které už nemají žádný neembeddovaný paragraf
            self.conn.execute("""
                UPDATE laws SET phase='embedded'
                WHERE phase='parsed' AND id_zakona NOT IN (
                    SELECT DISTINCT id_zakona FROM paragrafy WHERE vektor IS NULL)
            """)

    # --- fáze B: import ---

    def laws_to_import(self, limit: int):
        sql = "SELECT id_zakona, citace, nazev, rok, sbirka FROM laws WHERE phase='embedded'"
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def paragraphs_with_vectors(self, id_zakona):
        cur = self.conn.execute("""
            SELECT idx, citace, text, vektor FROM paragrafy
            WHERE id_zakona=? AND vektor IS NOT NULL ORDER BY idx
        """, (id_zakona,))
        return [(idx, citace, text, np.frombuffer(blob, dtype=np.float32))
                for idx, citace, text, blob in cur.fetchall()]

    def mark_done(self, ids):
        with self.conn:
            self.conn.executemany(
                "UPDATE laws SET phase='done' WHERE id_zakona=?",
                [(i,) for i in ids])

    # --- status ---

    def status(self):
        cur = self.conn.execute("""
            SELECT l.phase, COUNT(DISTINCT l.id_zakona), COUNT(p.id_zakona),
                   SUM(p.vektor IS NOT NULL)
            FROM laws l LEFT JOIN paragrafy p ON p.id_zakona = l.id_zakona
            GROUP BY l.phase
        """)
        total_vec = self.conn.execute(
            "SELECT COUNT(*), SUM(vektor IS NOT NULL) FROM paragrafy").fetchone()
        return cur.fetchall(), total_vec


def file_sha256(fp):
    sha = hashlib.sha256()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


# =====================================================================
# Fáze A1: parse
# =====================================================================

def cmd_parse(data_dir: Path, store: Store, overlap_pct: int = 0):
    files = sorted(data_dir.glob("*.docx"))
    if not files:
        log.error("Žádné DOCX v %s", data_dir)
        sys.exit(1)

    overlap = CHUNK_CHARS * overlap_pct // 100
    if overlap:
        log.info("Chunking s překryvem: %d %% (~%d znaků)", overlap_pct, overlap)
    meta = store.load_download_meta(data_dir)

    stats = {"parsed": 0, "skipped": 0, "error": 0}
    for fp in files:
        id_zakona, rok, sbirka, cislo = filename_to_meta(fp.name)
        if not id_zakona:
            log.warning("Přeskočen (nelze parsovat název): %s", fp.name)
            continue
        sha = file_sha256(fp)
        prev = store.law_state(id_zakona, sha)
        if prev and prev["phase"] in ("parsed", "embedded", "done") \
                and prev["sha_matches"]:
            stats["skipped"] += 1
            continue

        try:
            units = extract_units(docx_to_texts(fp), overlap=overlap)
        except Exception as e:
            log.error("Parse chyba %s: %s", fp.name, e)
            stats["error"] += 1
            continue

        key = f"/{sbirka}/{rok}/{cislo}"
        citace, nazev = (meta[key][1], meta[key][2]) if key in meta \
            else (f"{cislo}/{rok} Sb."
                  + (" m. s." if sbirka == "sm" else ""), "")

        store.save_parsed(id_zakona, fp, sha, citace, nazev, rok, sbirka, units)
        stats["parsed"] += 1
        if stats["parsed"] % 500 == 0:
            log.info("Parsing: %d hotovo (%d skip, %d chyb)",
                     stats["parsed"], stats["skipped"], stats["error"])

    log.info("Fáze A1 (parse) hotova: parsed=%d, skipped=%d, errors=%d",
             stats["parsed"], stats["skipped"], stats["error"])


# =====================================================================
# Fáze A2: embed (lokálně)
# =====================================================================

class FakeModel:
    """Testovací model — náhodné normalizované vektory."""

    def encode(self, texts, **kw):
        rng = np.random.default_rng(len(texts))
        vecs = rng.standard_normal((len(texts), EMBED_DIM)).astype(np.float32)
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def cmd_embed(data_dir: Path, store: Store, batch_paragrafs: int,
              fake_model: bool):
    todo = store.next_unembedded(1)
    if not todo:
        log.info("Nic k embeddování — všechno je hotové")
        return
    total = store.conn.execute(
        "SELECT COUNT(*) FROM paragrafy WHERE vektor IS NULL").fetchone()[0]
    log.info("Fáze A2: %d paragrafů k embeddování (%s)", total,
             "FAKE model — jen test!" if fake_model else EMBEDDING_MODEL)

    if fake_model:
        eng = FakeModel()
    else:
        from sentence_transformers import SentenceTransformer
        eng = SentenceTransformer(EMBEDDING_MODEL)

    done = 0
    while True:
        rows = store.next_unembedded(batch_paragrafs)
        if not rows:
            break
        vectors = eng.encode([r[2] for r in rows], show_progress_bar=False,
                             batch_size=32)
        store.save_vectors([(r[0], r[1], v) for r, v in zip(rows, vectors)])
        done += len(rows)
        rate_hint = f" ({done:,}/{total:,})" if total > len(rows) else ""
        log.info("Embedding: %d paragrafů hotových%s", done, rate_hint)

    log.info("Fáze A2 (embed) hotova — vektory jsou uložené lokálně")


# =====================================================================
# Fáze B: import do Elasticsearch
# =====================================================================

ES_MAPPING = {
    "settings": {"number_of_replicas": 0, "number_of_shards": 1},
    "mappings": {"properties": {
        "id_zakona": {"type": "keyword"},
        "akt_citace": {"type": "text"},
        "akt_nazev": {"type": "text",
                      "fields": {"keyword": {"type": "keyword"}}},
        "rok": {"type": "integer"},
        "sbírka": {"type": "keyword"},
        "paragrafy": {"type": "nested", "properties": {
            "citace": {"type": "text"},
            "text": {"type": "text", "analyzer": "czech"},
            "typ": {"type": "keyword"},
            "vektor": {"type": "dense_vector", "dims": EMBED_DIM,
                       "index": True, "similarity": "cosine",
                       "index_options": {"type": "int8_hnsw", "m": 16,
                                         "ef_construction": 100}},
        }},
    }},
}


def cmd_import(data_dir: Path, store: Store, es_url: str, index_name: str,
               batch_laws: int, limit: int):
    from elasticsearch import Elasticsearch, helpers

    laws = store.laws_to_import(limit)
    if not laws:
        log.info("Nic k importu — nejprve spusťte 'parse' a 'embed'")
        return
    log.info("Fáze B: %d dokumentů k importu do '%s'", len(laws), index_name)

    es = Elasticsearch([es_url], request_timeout=120,
                       retry_on_timeout=True, max_retries=5)
    if not es.ping():
        log.error("Elasticsearch nedostupný na %s!", es_url)
        sys.exit(1)
    if not es.indices.exists(index=index_name):
        # ES klient 9.x už nepřijímá body= — mapping se předává po částech
        es.indices.create(index=index_name,
                          settings=ES_MAPPING["settings"],
                          mappings=ES_MAPPING["mappings"])
        log.info("Index '%s' vytvořen", index_name)

    total = 0
    for start in range(0, len(laws), batch_laws):
        batch = laws[start:start + batch_laws]
        actions = []
        for id_zakona, citace, nazev, rok, sbirka in batch:
            for idx, cit, text, vec in store.paragraphs_with_vectors(id_zakona):
                actions.append({
                    "_index": index_name,
                    "_id": f"{id_zakona}::{idx}",
                    "_source": {
                        "id_zakona": id_zakona,
                        "akt_citace": citace,
                        "akt_nazev": nazev,
                        "rok": rok,
                        "sbírka": sbirka,
                        "paragrafy": [{
                            "citace": cit,
                            "text": text,
                            "typ": "DOCX",
                            "vektor": vec.tolist(),
                        }],
                    },
                })
        success, errors = helpers.bulk(es, actions, chunk_size=50,
                                       raise_on_error=False)
        store.mark_done([b[0] for b in batch])
        total += success
        log.info("Batch %d/%d: %d paragrafů vloženo (celkem %d)",
                 start // batch_laws + 1,
                 (len(laws) + batch_laws - 1) // batch_laws,
                 success, total)

    log.info("Fáze B (import) hotova: %d paragrafů v ES", total)


# =====================================================================
# Status
# =====================================================================

def cmd_status(store: Store):
    phases, (total_p, total_v) = store.status()
    print(f"{'fáze':<12}{'dokumentů':>12}{'paragrafů':>12}{'s vektorem':>13}")
    print("-" * 49)
    for phase, laws_n, paras_n, vecs_n in phases:
        print(f"{phase:<12}{laws_n:>12,}{paras_n:>12,}"
              f"{vecs_n:>13,}")
    print("-" * 49)
    print(f"{'CELKEM':<12}{'':>12}{total_p:>12,}{total_v:>13,}")


# =====================================================================
# Main
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="DOCX → lokální embeddingy → Elasticsearch")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("data_dir", help="adresář s DOCX (např. ./zakony)")
        p.add_argument("--db", default=None,
                       help="cesta k SQLite úložišti (default: <data_dir>/.embed.db)")

    p_parse = sub.add_parser("parse", help="DOCX → paragrafy")
    add_common(p_parse)
    p_parse.add_argument("--overlap", type=int, default=0, metavar="PCT",
                         help="překryv chunků v %% délky chunku (10 = ~200 znaků)")

    p_embed = sub.add_parser("embed", help="paragrafy → vektory (lokálně)")
    add_common(p_embed)
    p_embed.add_argument("--batch-paragrafs", type=int, default=64)
    p_embed.add_argument("--fake-model", action="store_true",
                         help="TEST: náhodné vektory místo modelu")

    p_import = sub.add_parser("import", help="vektory → Elasticsearch")
    add_common(p_import)
    p_import.add_argument("--es-url", default=ES_HOST)
    p_import.add_argument("--index", default=INDEX_NAME)
    p_import.add_argument("--batch-laws", type=int, default=20)
    p_import.add_argument("--limit", type=int, default=0)

    p_status = sub.add_parser("status", help="přehled pokroku")
    add_common(p_status)

    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        sys.exit(f"Adresář neexistuje: {data_dir}")
    db_path = Path(args.db) if args.db else data_dir / ".embed.db"
    store = Store(db_path)

    if args.cmd == "parse":
        cmd_parse(data_dir, store, args.overlap)
    elif args.cmd == "embed":
        cmd_embed(data_dir, store, args.batch_paragrafs, args.fake_model)
    elif args.cmd == "import":
        cmd_import(data_dir, store, args.es_url, args.index,
                   args.batch_laws, args.limit)
    elif args.cmd == "status":
        cmd_status(store)


if __name__ == "__main__":
    main()
