import sqlite3
import gzip
import json
import os
import ijson
import time

DB_PATH = "data/esbirka_checkpoint.db"
os.makedirs("data", exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Tabulka pro základní info o zákonu
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS akty (
        iri TEXT PRIMARY KEY,
        nazev TEXT,
        citace TEXT,
        rok INTEGER
    )
    """)
    
    # Tabulka pro aktuálně platná znění zákona
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS zneni (
        zneni_doc_id INTEGER PRIMARY KEY,
        iri TEXT,
        akt_iri TEXT,
        FOREIGN KEY(akt_iri) REFERENCES akty(iri)
    )
    """)
    
    # Tabulka textů fragmentů (004)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fragment_text (
        fragment_iri TEXT PRIMARY KEY,
        text TEXT
    )
    """)
    
    # Vazební tabulka zneni -> fragment (003)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS zneni_fragment (
        zneni_doc_id INTEGER,
        fragment_iri TEXT,
        oznaceni TEXT,
        FOREIGN KEY(zneni_doc_id) REFERENCES zneni(zneni_doc_id),
        FOREIGN KEY(fragment_iri) REFERENCES fragment_text(fragment_iri)
    )
    """)
    
    # Výsledná textová tabulka
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS paragrafy_s_vektory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        akt_iri TEXT,
        oznaceni TEXT,
        text TEXT,
        vektor TEXT
    )
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_zneni_akt ON zneni(akt_iri)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_zf_zneni ON zneni_fragment(zneni_doc_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_zf_frag ON zneni_fragment(fragment_iri)")
    
    conn.commit()
    return conn

def parse_002_akty(conn):
    print("-> Parsování 002PravniAkt (Základní metadata zákonů)...")
    if not os.path.exists("002PravniAkt.json.gz"):
        print("Soubor 002PravniAkt.json.gz nenalezen! Stáhněte jej.")
        return

    cursor = conn.cursor()
    with gzip.open("002PravniAkt.json.gz", "rt", encoding="utf-8") as f:
        data = json.load(f)
        
    items = data.get("položky", [])
    count = 0
    for item in items:
        iri = item.get("iri")
        nazev = item.get("akt-název-vyhlášený")
        citace = item.get("akt-citace")
        rok = item.get("akt-rok-předpisu")
        if rok:
            try: rok = int(rok)
            except: rok = None
            
        if iri and nazev:
            cursor.execute("INSERT OR IGNORE INTO akty (iri, nazev, citace, rok) VALUES (?, ?, ?, ?)", 
                           (iri, nazev, citace, rok))
            count += 1
            
    conn.commit()
    print(f"Hotovo. Vloženo {count} unikátních zákonů.")

def parse_001_zneni(conn):
    print("-> Parsování 001PravniAktZneni (Verze a platnosti zákonů)...")
    if not os.path.exists("001PravniAktZneni.json.gz"):
        print("Soubor 001PravniAktZneni.json.gz nenalezen!")
        return

    cursor = conn.cursor()
    with gzip.open("001PravniAktZneni.json.gz", "rt", encoding="utf-8") as f:
        data = json.load(f)
        
    items = data.get("položky", [])
    count = 0
    
    # Budeme chtít jen aktuálně platná znění (zrušen = False, účinnost do = neomezeno)
    for item in items:
        iri = item.get("iri")
        akt_iri = item.get("akt-iri")
        zneni_doc_id = item.get("znění-dokument-id")
        zruseno = item.get("znění-je-zrušen", False)
        ucinnost_do = item.get("znění-datum-účinnosti-do")
        
        # Velmi zjednodušená logika pro platné znění
        if not zruseno and not ucinnost_do and zneni_doc_id and akt_iri:
            cursor.execute("INSERT OR IGNORE INTO zneni (zneni_doc_id, iri, akt_iri) VALUES (?, ?, ?)", 
                           (zneni_doc_id, iri, akt_iri))
            count += 1
            
    conn.commit()
    print(f"Hotovo. Vloženo {count} platných znění zákonů.")

def parse_004_fragment_texts(conn):
    print("-> Parsování 004PravniAktFragment (Texty) ...")
    if not os.path.exists("004PravniAktFragment.json.gz"):
        print("Soubor 004 nenalezen!")
        return
        
    cursor = conn.cursor()
    count = 0
    batch = []
    
    with gzip.open("004PravniAktFragment.json.gz", "rt", encoding="utf-8") as f:
        items = ijson.items(f, "položky.item")
        for item in items:
            iri = item.get("iri")
            text = item.get("fragment-text")
            # Uložíme jen ty, co opravdu obsahují text, abychom zbytečně neplnili DB null hodnotami (jako Virtual_Prefix atd)
            if iri and text:
                batch.append((iri, text))
                if len(batch) >= 10000:
                    cursor.executemany("INSERT OR IGNORE INTO fragment_text (fragment_iri, text) VALUES (?, ?)", batch)
                    batch = []
                    count += 10000
                    
    if batch:
        cursor.executemany("INSERT OR IGNORE INTO fragment_text (fragment_iri, text) VALUES (?, ?)", batch)
        count += len(batch)
        
    conn.commit()
    print(f"Hotovo. Vloženo {count} textů fragmentů.")

def parse_003_vazby(conn):
    print("-> Parsování 003PravniAktZneniFragment (Strukturální vazby) ...")
    if not os.path.exists("003PravniAktZneniFragment.json.gz"):
        print("Soubor 003 nenalezen!")
        return

    cursor = conn.cursor()
    
    cursor.execute("SELECT zneni_doc_id FROM zneni")
    platna_zneni = set([row[0] for row in cursor.fetchall()])
    
    batch = []
    count = 0

    with gzip.open("003PravniAktZneniFragment.json.gz", "rt", encoding="utf-8") as f:
        items = ijson.items(f, "položky.item")
        for item in items:
            zdoc_id = item.get("znění-dokument-id")
            if zdoc_id in platna_zneni:
                frag_dict = item.get("právní-akt-fragment", {})
                frag_iri = frag_dict.get("iri") if isinstance(frag_dict, dict) else None
                oznaceni = item.get("znění-fragment-označení-uzlu-text")
                
                if frag_iri:
                    batch.append((zdoc_id, frag_iri, oznaceni))
                    if len(batch) >= 20000:
                        cursor.executemany("INSERT INTO zneni_fragment (zneni_doc_id, fragment_iri, oznaceni) VALUES (?, ?, ?)", batch)
                        batch = []
                        count += 20000

    if batch:
        cursor.executemany("INSERT INTO zneni_fragment (zneni_doc_id, fragment_iri, oznaceni) VALUES (?, ?, ?)", batch)
        count += len(batch)

    conn.commit()
    print(f"Hotovo. Vloženo {count} vazeb.")

def build_final_texts(conn):
    print("-> Skládání hotových textů pro embedding...")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO paragrafy_s_vektory (akt_iri, oznaceni, text)
        SELECT z.akt_iri, zf.oznaceni, ft.text
        FROM zneni_fragment zf
        JOIN zneni z ON z.zneni_doc_id = zf.zneni_doc_id
        JOIN fragment_text ft ON ft.fragment_iri = zf.fragment_iri
    """)
    conn.commit()
    print("Hotovo, poskládáno.")

if __name__ == "__main__":
    start = time.time()
    conn = init_db()
    # parse_002_akty(conn) # Už proběhlo, můžeme přeskočit nebo zakomentovat
    # parse_001_zneni(conn) # Už proběhlo (změnili jsme ale schéma, takže to pustíme znova)
    parse_002_akty(conn)
    parse_001_zneni(conn)
    parse_004_fragment_texts(conn)
    parse_003_vazby(conn)
    build_final_texts(conn)
    print(f"Vše hotovo v čase: {time.time() - start:.2f} s")
