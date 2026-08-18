#!/usr/bin/env python3
"""
Pipeline pro zpracování legislativy.
Srdce ingest procesu — paralelní zpracování dat e-Sbírky do Elasticsearch.

Použití:
    from pipeline import process_laws
    process_laws(files, es_url="http://localhost:9200", num_workers=7, ...)
"""

import argparse, gzip, hashlib, json, logging, os, re, sqlite3, sys, time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Set, Tuple

import ijson
from elasticsearch import Elasticsearch, helpers
from sentence_transformers import SentenceTransformer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

INDEX_NAME = "zakony"
ES_HOST = "http://localhost:9200"
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
VALID_TYPES = {"Paragraf", "Odstavec_Dc", "Pozemek"}

# Fields pro každý typ souboru
F001 = {
    "typ","iri","akt-citace","akt-název-vyhlášený","akt-iri",
    "znění-id","znění-dokument-id","znění-datum-účinnosti-od",
    "znění-datum-účinnosti-do","znění-je-zrušen","znění-ročník",
    "cis-esb-sbírka-položka","cis-esb-podtyp-právní-akt",
    "právní-akt-znění-fragment",
}
F003 = {
    "typ","iri","znění-fragment-id","znění-fragment-předek",
    "znění-fragment-citace","znění-fragment-citace-text",
    "znění-fragment-označení-uzlu","znění-fragment-eli",
    "znění-fragment-url","znění-fragment-hierarchie",
    "znění-fragment-hierarchie-hex","znění-dokument-id",
    "právní-akt-fragment","právní-akt-odkaz","právní-akt-komentář-fragmentu",
}
F004 = {
    "typ","iri","fragment-id","fragment-base-id",
    "fragment-text","cis-esb-typ-fragmentu",
    "cis-esb-typ-fragmentu-položka",
}


def open_json(fp):
    """Otevře JSON nebo gzip soubor."""
    if fp.endswith(".gz"):
        return gzip.open(fp, "rt", encoding="utf-8")
    return open(fp, "r", encoding="utf-8")


def file_sha256(fp):
    """Vypočítá SHA256 souboru."""
    sha = hashlib.sha256()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


class Checkpoint:
    """Checkpoint pro uložení stavu (soubory + IRI).
    
    IRI se ukládají do setu pro O(1) lookup — kritické pro výkon!
    """
    
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.st = self._load()
        # Konverze iris na set pro O(1) lookup
        if isinstance(self.st.get("iris"), list):
            self.st["iris"] = set(self.st["iris"])
    
    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                return json.load(f)
        return {"files": {}, "iris": set()}
    
    def save(self):
        """Uloží checkpoint na disk."""
        tmp_file = self.state_file + ".tmp"
        # Konverze set -> list pro JSON
        data = {
            "files": self.st.get("files", {}),
            "iris": list(self.st.get("iris", set())),
        }
        with open(tmp_file, "w") as f:
            json.dump(data, f)
        os.replace(tmp_file, self.state_file)
    
    def file_done(self, filename: str) -> bool:
        return filename in self.st.get("files", {})
    
    def mark_file(self, filename: str, sha: str):
        self.st.setdefault("files", {})[filename] = sha
    
    def iri_done(self, iri: str) -> bool:
        # O(1) lookup — set místo listu!
        return iri in self.st.get("iris", set())
    
    def mark_iri(self, iri: str):
        self.st.setdefault("iris", set()).add(iri)


def stream_items(fp: str, item_type: str, fields: set, max_items: int = 0) -> dict:
    """Streamuje položky ze souboru."""
    count = 0
    with open_json(fp) as f:
        for item in ijson.items(f, "položky.item"):
            if item.get("typ") != item_type:
                continue
            yield {k: v for k, v in item.items() if k in fields}
            count += 1
            if max_items > 0 and count >= max_items:
                break


def load_001_metadata(files: List[str], checkpoint: Checkpoint, max_items: int = 0) -> Dict[str, dict]:
    """Načte metadata zákonů z 001. Vrací: {law_iri: metadata}"""
    laws = {}
    for fp in files:
        sha, bn = file_sha256(fp), os.path.basename(fp)
        if checkpoint.file_done(bn):
            log.info(f"Přeskočen zpracovaný soubor: {bn}")
            continue
        
        log.info(f"Načítám 001 metadata: {bn}")
        for item in stream_items(fp, "právní-akt-znění", F001, max_items):
            iri = item.get("iri", "")
            if not iri:
                continue
            
            did = item.get("znění-dokument-id")
            laws[iri] = {
                "iri": iri,
                "akt_citace": item.get("akt-citace", ""),
                "akt_nazev": item.get("akt-název-vyhlášený", ""),
                "znění_dokument_id": did,
                "rok": item.get("znění-ročník"),
                "datum_od": item.get("znění-datum-účinnosti-od"),
                "datum_do": item.get("znění-datum-účinnosti-do"),
                "je_zrusen": item.get("znění-je-zrušen", False),
                "sbírka": item.get("cis-esb-sbírka-položka", ""),
            }
        
        if max_items == 0:
            checkpoint.mark_file(bn, sha)
            log.info(f"001 hotovo: {len(laws)} zákonů")
    
    return laws


def load_004_to_sqlite(files: List[str], db_path: str, checkpoint: Checkpoint, max_items: int = 0):
    """Načte typy fragmentů z 004 do SQLite (disk-based)."""
    if os.path.exists(db_path):
        os.remove(db_path)
    
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE types (fid INTEGER PRIMARY KEY, typ TEXT, text TEXT)")
    
    for fp in files:
        sha, bn = file_sha256(fp), os.path.basename(fp)
        if checkpoint.file_done(bn):
            log.info(f"Přeskočen zpracovaný 004: {bn}")
            continue
        
        log.info(f"Načítám 004 typy do SQLite: {bn}")
        cnt = 0
        for item in stream_items(fp, "právní-akt-fragment", F004, max_items):
            fid = item.get("fragment-id")
            if fid is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO types VALUES (?,?,?)",
                    (fid, item.get("cis-esb-typ-fragmentu-položka", ""), item.get("fragment-text"))
                )
                cnt += 1
                if cnt % 50000 == 0:
                    conn.commit()
        conn.commit()
        
        if max_items == 0:
            checkpoint.mark_file(bn, sha)
        log.info(f"004 hotovo: {cnt} položek")
    
    conn.close()


def sqlite_lookup(db_path: str, fragment_ids: List[int]) -> Dict[int, Tuple[str, str]]:
    """Batch lookup fragment types from SQLite."""
    if not fragment_ids:
        return {}
    
    placeholders = ",".join(["?"] * len(fragment_ids))
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        f"SELECT fid, typ, text FROM types WHERE fid IN ({placeholders})",
        fragment_ids
    )
    result = {row[0]: (row[1] or "", row[2] or "") for row in cursor}
    conn.close()
    return result


def create_es_index(es: Elasticsearch, index_name: str):
    """Vytvoří ES index s mappingem."""
    if es.indices.exists(index=index_name):
        log.info(f"Index '{index_name}' již existuje, smažu...")
        es.indices.delete(index=index_name)
    
    mapping = {
        "settings": {
            "number_of_replicas": 0,
            "number_of_shards": 1,
        },
        "mappings": {
            "properties": {
                "id_zakona": {"type": "keyword"},
                "akt_citace": {"type": "text"},
                "akt_nazev": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "rok": {"type": "integer"},
                "datum_od": {"type": "date"},
                "datum_do": {"type": "date"},
                "je_zrusen": {"type": "boolean"},
                "sbírka": {"type": "keyword"},
                "paragrafy": {
                    "type": "nested",
                    "properties": {
                        "iris": {"type": "keyword"},
                        "eli": {"type": "keyword"},
                        "citace": {"type": "text"},
                        "text": {"type": "text", "analyzer": "czech"},
                        "hierarchie": {"type": "keyword"},
                        "fragment_id": {"type": "integer"},
                        "typ": {"type": "keyword"},
                        "vektor": {
                            "type": "dense_vector",
                            "dims": 768,
                            "index": True,
                            "similarity": "cosine",
                            "index_options": {
                                "type": "int8_hnsw",
                                "m": 16,
                                "ef_construction": 100,
                            },
                        },
                    },
                },
            },
        },
    }
    
    es.indices.create(index=index_name, body=mapping)
    log.info(f"Index '{index_name}' vytvořen")


def embed_and_bulk_insert(
    batch_docs: List[dict],
    es: Elasticsearch,
    eng: SentenceTransformer,
    index_name: str,
    bulk_batch_size: int = 50
) -> Tuple[int, List]:
    """Vytvoří embeddingy a bulk insert do ES. Vrací: (počet úspěšných, seznam chyb)"""
    if not batch_docs:
        return 0, []
    
    # Extract texts and create embeddings
    texts = []
    para_list = []
    for doc in batch_docs:
        for para in doc["paragrafy"]:
            texts.append(para["text"])
            para_list.append(para)
    
    if texts:
        embeddings = eng.encode(texts, show_progress_bar=False, batch_size=32)
        for para, emb in zip(para_list, embeddings):
            para["vektor"] = emb.tolist()
    
    # Build bulk actions
    actions = []
    for doc in batch_docs:
        for para in doc["paragrafy"]:
            actions.append({
                "_index": index_name,
                "_id": f"{doc['id_zakona']}::{para['iris']}",
                "_source": {
                    "id_zakona": doc["id_zakona"],
                    "akt_citace": doc["akt_citace"],
                    "akt_nazev": doc["akt_nazev"],
                    "rok": doc["rok"],
                    "datum_od": doc["datum_od"],
                    "datum_do": doc["datum_do"],
                    "je_zrusen": doc["je_zrusen"],
                    "sbírka": doc["sbírka"],
                    "paragrafy": [{
                        "iris": para["iris"],
                        "eli": para["eli"],
                        "citace": para["citace"],
                        "text": para["text"],
                        "hierarchie": para["hierarchie"],
                        "fragment_id": para["fragment_id"],
                        "typ": para["typ"],
                        "vektor": para["vektor"],
                    }],
                },
            })
    
    # Bulk insert
    success, errors = helpers.bulk(
        es, actions,
        chunk_size=bulk_batch_size,
        raise_on_error=False
    )
    return success, errors


def extract_paragraf_number(hierarchie: str) -> str:
    """Extrahuje cislo paragrafu z hierarchie (napr. '/2/1/1/' -> '1', '/2/3/' -> '3')."""
    if not hierarchie:
        return ""
    match = re.search(r'/2/(\d+)/', hierarchie + '/')
    if match:
        return match.group(1)
    return ""


def _worker_process_batch(args):
    """
    Worker funkce pro parallel processing.
    Zpracuje jednu batch grup (paragrafy) z 003.
    
    Každý worker si vytvoří vlastní embedding model a ES connection.
    
    Args:
        args: Tuple (batch_index, batch_groups, laws_dict, db_path, index_name, bulk_batch_size)
        batch_groups: List of (law_iri, paragraf_number, items)
    
    Returns:
        Tuple (batch_index, success_count, error_count, processed_iris)
    """
    batch_idx, batch_groups, laws, db_path, index_name, bulk_batch_size = args
    
    # Local embedding engine
    eng = SentenceTransformer(EMBEDDING_MODEL)
    
    # Local ES connection
    es = Elasticsearch([ES_HOST], request_timeout=60, retry_on_timeout=True, max_retries=3)
    
    batch_docs = []
    processed_iris = []
    
    for law_iri, paragraf_number, items in batch_groups:
        # Collect fragment IDs for this paragraf
        b_fids = []
        item_list = []
        for iri, item, liri in items:
            fid = item.get("právní-akt-fragment", {}).get("fragment-id")
            if fid is not None:
                b_fids.append(fid)
                item_list.append((iri, item, liri, fid))
        
        if not b_fids:
            continue
        
        # Lookup types in SQLite
        tmap = sqlite_lookup(db_path, b_fids)
        
        # Collect texts for this paragraf
        texts = []
        paragraf_eli = ""
        paragraf_hierarchie = ""
        paragraf_citace = ""
        fragment_id = None
        iris = []
        
        for iri, item, liri, fid in item_list:
            typ, text = tmap.get(fid, ("", ""))
            
            if typ not in VALID_TYPES:
                continue
            
            if not text or len(text.strip()) < 5:
                text = item.get("znění-fragment-citace-text", "")
                if not text or len(text.strip()) < 5:
                    continue
            
            if typ == "Paragraf":
                texts.insert(0, text.strip())
            else:
                texts.append(text.strip())
            
            if not paragraf_eli:
                paragraf_eli = item.get("znění-fragment-eli", "")
                paragraf_hierarchie = item.get("znění-fragment-hierarchie", "")
                paragraf_citace = item.get("znění-fragment-citace", "")
                fragment_id = item.get("znění-fragment-id")
            
            iris.append(iri)
        
        if not texts:
            continue
        
        law = laws[law_iri]
        combined_text = " ".join(texts)
        
        batch_docs.append({
            "id_zakona": law_iri,
            "akt_citace": law["akt_citace"],
            "akt_nazev": law["akt_nazev"],
            "rok": law["rok"],
            "datum_od": law["datum_od"],
            "datum_do": law["datum_do"],
            "je_zrusen": law["je_zrusen"],
            "sbírka": law["sbírka"],
            "paragrafy": [{
                "iris": iris[0] if iris else "",
                "eli": paragraf_eli,
                "citace": paragraf_citace or f"§ {paragraf_number}",
                "text": combined_text,
                "hierarchie": paragraf_hierarchie,
                "fragment_id": fragment_id,
                "typ": "Paragraf",
            }],
        })
        processed_iris.extend(iris)
    
    # Embed and insert
    success, errors = embed_and_bulk_insert(
        batch_docs, es, eng, index_name, bulk_batch_size
    )
    
    return batch_idx, success, len(errors), processed_iris


def process_laws(
    json_files: List[str],
    es_url: str = ES_HOST,
    model: str = EMBEDDING_MODEL,
    index: str = INDEX_NAME,
    batch_size: int = 50,
    chunk_size: int = 100,
    num_workers: int = 3,
    state_file: str = "state.json",
    dry_run: bool = False,
    max_laws: int = 0,
):
    """
    Hlavní funkce pro zpracování legislativy — parallel processing.
    
    Rozdělení práce:
    - 001 soubor se načte jako metadata zákonů (single-thread)
    - 003 soubor se streamuje a rozdělí na chunky (počet_jader - 1 workers)
    - Každý worker zpracuje chunk paralelně: lookup 004 + embedding + ES insert
    
    Args:
        json_files: Seznam cest ke JSON souborům (001, 003, 004)
        es_url: Elasticsearch URL
        model: Embedding model name
        index: Název ES indexu
        batch_size: Bulk insert batch size
        chunk_size: Počet položek na jeden parallel chunk (z 003)
        num_workers: Počet parallel workers (počet jader - 1)
        state_file: Cesta k checkpoint souboru
        dry_run: Pouze ověření bez vložení do ES
        max_laws: Maximální počet zákonů pro načtení (0 = všechny)
    
    Returns:
        Dict s statistikami (laws_count, total_docs, errors)
    """
    log.info("=" * 70)
    log.info("ZAKONY ES INGESTOR — parallel processing")
    log.info(f"Workers: {num_workers}, Chunk size: {chunk_size}")
    log.info("=" * 70)
    
    # Categorize files
    cat = categorize_files(json_files)
    for k, v in cat.items():
        if v:
            log.info(f"  {k}: {len(v)} souborů")
    
    if not cat["001"]:
        log.error("Chybí 001 soubor!")
        sys.exit(1)
    
    # Connect to ES
    es = Elasticsearch(
        [es_url],
        request_timeout=60,
        retry_on_timeout=True,
        max_retries=3
    )
    if not es.ping():
        log.error("ES nedostupný!")
        sys.exit(1)
    log.info("Připojeno k Elasticsearch")
    
    # Create index
    create_es_index(es, index)
    
    # Initialize checkpoint
    checkpoint = Checkpoint(state_file)
    
    # Load 001 (laws metadata) — single thread
    log.info("\nNačítám 001 metadata (zákony)...")
    laws = load_001_metadata(cat["001"], checkpoint, max_items=max_laws)
    if not laws:
        log.error("Žádné zákony!")
        sys.exit(1)
    log.info(f"Zákony: {len(laws)}")
    
    # Build dokument_id -> law_iri index
    didx = {}
    for law_iri, law_data in laws.items():
        did = law_data.get("znění_dokument_id")
        if did:
            didx[did] = law_iri
    log.info(f"dokument_id index: {len(didx)} záznamů")
    
    # Load 004 into SQLite
    db_path = "004_types.db"
    log.info(f"\nNačítám 004 typy do SQLite ({db_path})...")
    load_004_to_sqlite(cat["004"], db_path, checkpoint, max_items=max_laws * 10 if max_laws > 0 else 0)
    
    if dry_run:
        log.info("\nDRY RUN - hotovo!")
        return {"laws": len(laws), "dry_run": True}
    
    # Stream 003 and group by (law_iri, paragraf_number) to keep paragrafs intact
    log.info(f"\nStreamuji 003 a seskupuji podle paragrafů...")
    
    max_003 = max_laws * 100 if max_laws > 0 else 0
    
    groups = []
    current_key = None
    current_items = []
    total_processed = 0
    
    for fp in cat["003"]:
        sha, bn = file_sha256(fp), os.path.basename(fp)
        if checkpoint.file_done(bn):
            log.info(f"Přeskočen zpracovaný soubor: {bn}")
            continue
        
        log.info(f"Čtu 003: {bn}")
        for item in stream_items(fp, "právní-akt-znění-fragment", F003, max_003):
            iri = item.get("iri")
            if not iri or checkpoint.iri_done(iri):
                continue
            
            law_iri = didx.get(item.get("znění-dokument-id")) if item.get("znění-dokument-id") else None
            if not law_iri:
                continue
            
            paragraf_number = extract_paragraf_number(item.get("znění-fragment-hierarchie", ""))
            
            if not paragraf_number:
                continue
            
            key = (law_iri, paragraf_number)
            
            if key != current_key:
                if current_key is not None:
                    groups.append((current_key[0], current_key[1], current_items))
                current_key = key
                current_items = []
            
            current_items.append((iri, item, law_iri))
            total_processed += 1
            
            if total_processed % 10000 == 0:
                checkpoint.save()
        
        if not checkpoint.file_done(bn):
            checkpoint.mark_file(bn, sha)
    
    if current_items and current_key:
        groups.append((current_key[0], current_key[1], current_items))
    
    log.info(f"Seskupeno do {len(groups)} paragrafů, celkem {total_processed} položek")
    
    # Chunk groups (each group = one paragraf, never split across chunks)
    chunks = []
    current_chunk = []
    for group in groups:
        current_chunk.append(group)
        if len(current_chunk) >= chunk_size:
            chunks.append(current_chunk)
            current_chunk = []
    
    if current_chunk:
        chunks.append(current_chunk)
    
    log.info(f"Rozděleno na {len(chunks)} chunků")
    
    # Process chunks in parallel
    total_success = 0
    total_errors = 0
    total_docs = 0
    
    worker_args = [
        (idx, chunk, laws, db_path, index, batch_size)
        for idx, chunk in enumerate(chunks)
    ]
    
    log.info(f"Spouštím {len(worker_args)} chunků s {num_workers} workers...")
    
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(_worker_process_batch, args): args[0]
            for args in worker_args
        }
        
        for future in as_completed(futures):
            batch_idx = futures[future]
            try:
                success, errors, error_count, processed_iris = future.result()
                total_success += success
                total_errors += error_count
                total_docs += success
                
                # Mark IRIs as processed
                for iri in processed_iris:
                    checkpoint.mark_iri(iri)
                checkpoint.save()
                
                log.info(f"Chunk {batch_idx}: {success} OK, {error_count} err (celkem: {total_docs})")
            except Exception as e:
                log.error(f"Chunk {batch_idx} failed: {e}", exc_info=True)
                total_errors += 1
    
    log.info(f"\n{'='*70}")
    log.info(f"Hotovo!")
    log.info(f"  Zákony: {len(laws)}")
    log.info(f"  Dokumentů: {total_docs}")
    log.info(f"  Chyby: {total_errors}")
    log.info(f"{'='*70}")
    
    return {
        "laws": len(laws),
        "total_docs": total_docs,
        "errors": total_errors,
    }


def categorize_files(files: List[str]) -> Dict[str, List[str]]:
    """Roztřídí soubory podle typu (001, 003, 004, 007)."""
    result = {"001": [], "003": [], "004": [], "007": []}
    for f in files:
        bn = Path(f).name
        for k in result:
            if k in bn:
                result[k].append(f)
                break
    return result


def find_json_files(inputs: List[str]) -> List[str]:
    """Najde JSON soubory ze vstupních cest."""
    files = []
    for path in inputs:
        p = Path(path)
        if p.is_file():
            files.append(str(p))
        elif p.is_dir():
            for f in sorted(p.glob("*.json*")):
                files.append(str(f))
    return files


def main():
    """Hlavní entry point pro CLI."""
    parser = argparse.ArgumentParser(
        description="Import české legislativy do Elasticsearch"
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Cesty k JSON souborům nebo adresáře"
    )
    parser.add_argument("--es-url", default=ES_HOST, help="Elasticsearch URL")
    parser.add_argument("--model", default=EMBEDDING_MODEL, help="Embedding model")
    parser.add_argument("--index", default=INDEX_NAME, help="ES index name")
    parser.add_argument("--batch-size", type=int, default=50, help="Bulk batch size")
    parser.add_argument("--chunk-size", type=int, default=100, help="Parallel chunk size")
    parser.add_argument("--workers", type=int, default=3, help="Počet parallel workers (cores-1)")
    parser.add_argument("--state", default="state.json", help="Checkpoint file")
    parser.add_argument("--dry-run", action="store_true", help="Test without ES insert")
    
    args = parser.parse_args()
    
    # Find JSON files
    json_files = find_json_files(args.inputs)
    if not json_files:
        log.error("Nebyly nalezeny žádné JSON soubory!")
        sys.exit(1)
    
    log.info(f"Nalezeno {len(json_files)} JSON souborů")
    
    # Run pipeline
    stats = process_laws(
        json_files=json_files,
        es_url=args.es_url,
        model=args.model,
        index=args.index,
        batch_size=args.batch_size,
        chunk_size=args.chunk_size,
        num_workers=args.workers,
        state_file=args.state,
        dry_run=args.dry_run,
    )
    
    if args.dry_run:
        log.info("DRY RUN hotovo!")
    else:
        log.info(f"Stats: {stats}")


if __name__ == "__main__":
    main()
