import sqlite3
import json
import argparse
import threading
import queue
from tqdm import tqdm
from elasticsearch import Elasticsearch, helpers
from langchain_huggingface import HuggingFaceEmbeddings

DB_PATH = "data/esbirka_checkpoint.db"
ES_URL = "http://192.168.27.60:9200"
INDEX_NAME = "zakony-esbirka-2026"

def create_es_index(es):
    if not es.indices.exists(index=INDEX_NAME):
        print(f"Vytvářím nový index {INDEX_NAME}...")
        es.indices.create(
            index=INDEX_NAME,
            body={
                "mappings": {
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
            }
        )

def upload_worker(q, es):
    import time
    """
    Dočasně vypnuto odesílání do ES! 
    Vlákno pouze přijímá data z fronty a zahazuje je, 
    aby hlavní GPU vlákno mohlo běžet na 100 % rychlosti bez čekání na síť.
    (Vektory se ale pořád ukládají do lokální SQLite databáze).
    """
    while True:
        actions = q.get()
        if actions is None:
            break
        
        # Ignorujeme ES upload, abychom nezdržovali GPU síťovými timeouty.
        q.task_done()

def get_laws_to_process(conn, subset_mode=False):
    c = conn.cursor()
    if subset_mode:
        print("MÓD SUBSET: Vybírám pouze Trestní zákoník pro test...")
        c.execute("""
            SELECT iri, nazev, citace, rok FROM akty 
            WHERE nazev = 'Zákon trestní zákoník' OR citace = '40/2009 Sb.'
        """)
    else:
        print("MÓD FULL: Vybírám všechny zákony z DB od roku 1960...")
        c.execute("SELECT iri, nazev, citace, rok FROM akty WHERE rok >= 1960")
    return c.fetchall()

def process_and_push(subset_mode=False):
    es = Elasticsearch(
        [ES_URL],
        request_timeout=120,
        max_retries=3,
        retry_on_timeout=True
    )
    create_es_index(es)
    
    conn = sqlite3.connect(DB_PATH)
    laws = get_laws_to_process(conn, subset_mode)
    print(f"Celkem zákonů ke zpracování: {len(laws)}")
    
    print("Načítám lokální embedding model s Apple Silicon (MPS) akcelerací...")
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        model_kwargs={'device': 'mps'},
        encode_kwargs={'batch_size': 16}
    )
    
    c = conn.cursor()
    
    es_actions = []
    
    # Fronta pro paralelní běh sítě a GPU
    # Omezíme velikost fronty na 5, aby nám nedošla paměť RAM, kdyby byla síť příliš pomalá.
    upload_queue = queue.Queue(maxsize=5)
    worker = threading.Thread(target=upload_worker, args=(upload_queue, es), daemon=True)
    worker.start()
    
    # Průchod zákony
    for iri, nazev, citace, rok in tqdm(laws, desc="Zpracování zákonů"):
        # Ultra-rychlá kontrola: Má tento zákon ještě nějaké chybějící vektory?
        if not subset_mode:
            c.execute("SELECT COUNT(*) FROM paragrafy_s_vektory WHERE akt_iri = ? AND vektor IS NULL", (iri,))
            chybejici = c.fetchone()[0]
            if chybejici == 0:
                # Zákon už je kompletně spočítaný a uložený v DB, můžeme ho celý přeskočit
                continue
                
        # Vytáhneme všechny odstavce a fragmenty
        c.execute("SELECT id, oznaceni, text, vektor FROM paragrafy_s_vektory WHERE akt_iri = ? ORDER BY id", (iri,))
        fragments = c.fetchall()
        
        if not fragments:
            continue
            
        paragrafy_es = []
        updates = []
        
        texts_to_embed = []
        frag_indexes = []
        
        last_heading = ""
        for i, (f_id, oznaceni, text, vektor_json) in enumerate(fragments):
            if not text or len(text.strip()) < 5:
                continue
                
            # Pokud je text krátký a nevypadá jako samotný odstavec začínající číslem (např. <var>(1)</var>),
            # považujeme ho za nadpis části/paragrafu (např. "Vražda").
            if len(text.strip()) < 150 and not text.strip().startswith("<var>"):
                last_heading = text.strip()
                
            # Spojíme nadpis s textem
            full_text = f"{last_heading}\n{text.strip()}" if last_heading else text.strip()
            
            # Pokud jsme v subset_mode, chceme si vektor přepočítat nově s nadpisem, takže vektor_json ignorujeme
            if vektor_json and not subset_mode:
                vektor = json.loads(vektor_json)
                paragrafy_es.append({
                    "citace": oznaceni or "",
                    "text": full_text,
                    "vektor": vektor
                })
            else:
                texts_to_embed.append(full_text)
                frag_indexes.append((f_id, oznaceni))
                
        # Provedeme embeddings hromadně pro chybějící
        if texts_to_embed:
            vectors = embeddings.embed_documents(texts_to_embed)
            
            # 1. Připravíme pro uložení zpět do SQLite (Checkpoint)
            for j, (f_id, oznaceni) in enumerate(frag_indexes):
                v = vectors[j]
                updates.append((json.dumps(v), f_id))
                paragrafy_es.append({
                    "citace": oznaceni or "",
                    "text": texts_to_embed[j],
                    "vektor": v
                })
                
            # Uložíme vektory do SQLite pro příště
            conn.executemany("UPDATE paragrafy_s_vektory SET vektor = ? WHERE id = ?", updates)
            conn.commit()
            
        # Pošleme do ES pomocí rychlého dávkového Bulk API namísto po jedné žádosti
        CHUNK_SIZE = 500
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
            es_actions.append({
                "_index": INDEX_NAME,
                "_id": chunk_id,
                "_source": doc
            })
            
        # Odeslat batch do Elasticsearch jakmile máme max 2 chunků a s explicitním limitem 10MB
        if len(es_actions) >= 2:
            upload_queue.put(list(es_actions))
            es_actions.clear()
            
    # Odeslání zbytků
    if es_actions:
        upload_queue.put(list(es_actions))
        
    # Čekání na dokončení uploadů
    upload_queue.put(None)
    worker.join()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subset", action="store_true", help="Vytvoří jen Trestní zákoník pro test")
    args = parser.parse_args()
    
    process_and_push(args.subset)
    print("Export kompletně dokončen!")
