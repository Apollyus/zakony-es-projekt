import sqlite3
import json
import threading
import queue
import time
from tqdm import tqdm
from elasticsearch import Elasticsearch, helpers

DB_PATH = "data/esbirka_checkpoint.db"
ES_URL = "http://192.168.27.60:9200"
INDEX_NAME = "zakony-esbirka-2026"
THREADS = 2  # Sníženo ze 4, aby se nepřeplňovala 1GB paměť Elasticsearch serveru

def upload_worker(q, es):
    while True:
        actions = q.get()
        if actions is None:
            break
        
        success = False
        retries = 0
        # Omezeno na 3 pokusy, víc nemá smysl, pokud server nestíhá CPU
        while not success and retries < 3:
            try:
                # Odesíláme data na server
                helpers.bulk(es, actions, max_chunk_bytes=10 * 1024 * 1024)
                success = True
            except Exception as e:
                retries += 1
                time.sleep(5)
                
        if not success:
            print("\nVarování: Dokument se nepodařilo odeslat (server nestíhá).")
            
        q.task_done()

def create_es_index(es):
    if not es.indices.exists(index=INDEX_NAME):
        print(f"Vytvářím nový index {INDEX_NAME} (nebo obnovuji smazaný)...")
        es.indices.create(
            index=INDEX_NAME,
            mappings={
                "properties": {
                    "akt_nazev": {"type": "text", "analyzer": "czech"},
                    "akt_citace": {"type": "keyword"},
                    "rok": {"type": "integer"},
                    "id_zakona": {"type": "keyword"},
                    "paragrafy": {
                        "type": "nested",
                        "properties": {
                            "citace": {"type": "keyword"},
                            "text": {"type": "text", "analyzer": "czech"},
                            "vektor": {
                                "type": "dense_vector",
                                "dims": 768,
                                "index": True,
                                "similarity": "cosine"
                            }
                        }
                    }
                }
            }
        )

def push_only():
    es = Elasticsearch(
        [ES_URL],
        request_timeout=300,  # Zvýšeno na 5 minut! Počítání HNSW grafů je extrémně pomalé.
        max_retries=2,
        retry_on_timeout=True
    )
    
    create_es_index(es)
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    print("Načítám seznam zákonů z databáze...")
    c.execute("SELECT iri, nazev, citace, rok FROM akty WHERE rok >= 1960")
    laws = c.fetchall()
    print(f"Celkem zákonů k odeslání: {len(laws)}")
    
    # Menší fronta, aby se hlavní vlákno nezaseklo příliš daleko od pomalých workerů
    upload_queue = queue.Queue(maxsize=10)
    
    workers = []
    for _ in range(THREADS):
        t = threading.Thread(target=upload_worker, args=(upload_queue, es), daemon=True)
        t.start()
        workers.append(t)
        
    pbar = tqdm(total=len(laws), desc="Odesílání do Elasticsearch")
    
    for iri, nazev, citace, rok in laws:
        c.execute("SELECT oznaceni, text, vektor FROM paragrafy_s_vektory WHERE akt_iri = ? AND vektor IS NOT NULL ORDER BY id", (iri,))
        fragments = c.fetchall()
        
        if not fragments:
            pbar.update(1)
            continue
            
        paragrafy_es = []
        for oznaceni, text, vektor_json in fragments:
            vektor = json.loads(vektor_json)
            paragrafy_es.append({
                "citace": oznaceni or "",
                "text": text,
                "vektor": vektor
            })
            
        # Zmenšeno na 50 paragrafů v jednom dokumentu, aby to nevyhodilo 1GB RAM
        CHUNK_SIZE = 50
        for chunk_idx in range(0, len(paragrafy_es), CHUNK_SIZE):
            chunk = paragrafy_es[chunk_idx : chunk_idx + CHUNK_SIZE]
            doc = {
                "akt_nazev": nazev,
                "akt_citace": citace,
                "rok": rok,
                "id_zakona": iri,
                "paragrafy": chunk
            }
            chunk_id = f"{iri}_chunk_{chunk_idx // CHUNK_SIZE}"
            
            action = {
                "_index": INDEX_NAME,
                "_id": chunk_id,
                "_source": doc
            }
            upload_queue.put([action])
            
        pbar.update(1)
        
    pbar.close()
    
    print("Všechna data načtena z databáze, čekám na dokončení sítě (fronta běží na pozadí)...")
    for _ in range(THREADS):
        upload_queue.put(None)
        
    for w in workers:
        w.join()
        
    print("Odesílání do Elasticsearch je kompletně hotové! 🎉")

if __name__ == "__main__":
    push_only()
