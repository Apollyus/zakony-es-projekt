"""
Plný build indexu v2 (zakony-v2): § jednotky aktuálních znění všech předpisů.

Fáze (každá je obnovitelná – po pádu/Ctrl+C stačí spustit znovu):
    extract  001/003/004 *.json.gz -> SQLite data/v2/v2.db   (~5 min, bez GPU)
    units    § jednotky do tabulky units                     (~10 min, bez GPU)
    embed    bge-m3 (fp16, jen GPU) → vektory do SQLite (záloha) → průběžně do ES   (hodiny – přes noc)
             kód 0 = hotovo, 3 = nedokončeno (Ctrl+C/limit), 75 = restart kvůli paměti GPU
    push     pošle lokální vektory do ES (doplnění po výpadku, obnova, nový server) – bez embeddingu
    backup   konzistentní kopie DB včetně vektorů (data/v2/backups/ nebo --out)
    stats    stav a odhad zbývajícího času

Co se indexuje:
    - jen znění účinné k --date (konsolidované znění KONSOL); zrušené předpisy se vynechají,
    - předpisy jen s vyhlášeným zněním (VYHLZNE, hlavně sdělení/smlouvy) mají status 'neznamy',
    - přetisky "Úplné znění …" se vynechají.
Pořadí embeddingu podle priority (viz v2_common.priority): platné zákony → nařízení → vyhlášky →
ostatní platné → předpisy s neznámým statusem → mezinárodní smlouvy (Sb. m. s.).
Když se běh nestihne, je index použitelný už po dokončení priorit 0–3.

Použití:
    .venv/bin/python scripts/build_v2_index.py extract --date 2026-10-18
    .venv/bin/python scripts/build_v2_index.py units
    .venv/bin/python scripts/build_v2_index.py embed [--max-priority 5] [--hours 9]
    .venv/bin/python scripts/build_v2_index.py push [--all | --recreate]
    .venv/bin/python scripts/build_v2_index.py backup [--out /Volumes/Disk/zalohy]
    .venv/bin/python scripts/build_v2_index.py stats
"""

import argparse
import gzip
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import ijson
import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from v2_common import (  # noqa: E402
    MAPPING, V2_EMBED_MODEL, clean, is_republication, law_type, priority, units_for_law,
)

load_dotenv(ROOT / ".env")
ES_URL = os.getenv("ES_REMOTE_URL", "http://192.168.27.60:9200")
INDEX = os.getenv("V2_INDEX", "zakony-v2")
DB_PATH = ROOT / "data" / "v2" / "v2.db"
PUSH_GRACE_S = 1800   # po embeddingu čekat na ES (výpadek VPN) aspoň 30 min, než to vzdáme

log = logging.getLogger("v2")
_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True
    log.warning("Signál %s – dokončím rozdělanou dávku a skončím.", signum)


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS laws (
            akt_citace TEXT PRIMARY KEY, nazev TEXT, status TEXT, doc_id INTEGER, zneni_iri TEXT,
            ucinnost_od TEXT, typ TEXT, priority INTEGER, units_done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS frag (doc_id INTEGER, iri TEXT, parent TEXT, hex TEXT, frag_id INTEGER);
        CREATE TABLE IF NOT EXISTS text (frag_id INTEGER PRIMARY KEY, typ TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS units (
            id TEXT PRIMARY KEY, akt_citace TEXT, priority INTEGER, len INTEGER, doc TEXT,
            embedded INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        -- lokální záloha vektorů (fp16, 1024 × 2 B); ES jde kdykoli znovu naplnit fází push
        CREATE TABLE IF NOT EXISTS vectors (id TEXT PRIMARY KEY, v BLOB);
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(units)")}
    if "pushed" not in cols:  # embedded = vektor uložený lokálně, pushed = dokument je v ES
        con.execute("ALTER TABLE units ADD COLUMN pushed INTEGER DEFAULT 0")
        con.execute("CREATE INDEX IF NOT EXISTS idx_units_push ON units(embedded, pushed, priority)")
        con.commit()
    return con


def items(fn: str):
    with gzip.open(ROOT / fn, "rb") as f:
        yield from ijson.items(f, "položky.item")


# ── extract ──────────────────────────────────────────────────────────────────
def stage_extract(today: str):
    con = db()
    done = dict(con.execute("SELECT key, value FROM meta").fetchall())
    if done.get("extract_done") == today:
        log.info("extract už hotový pro datum %s (smaž data/v2/v2.db pro nový běh)", today)
        return
    con.executescript("DELETE FROM laws; DELETE FROM frag; DELETE FROM text; DELETE FROM units;")

    log.info("001: čtu znění …")
    by_law = {}
    for it in items("001PravniAktZneni.json.gz"):
        by_law.setdefault(it.get("akt-citace"), []).append({
            "nazev": it.get("akt-název-vyhlášený"), "doc_id": it.get("znění-dokument-id"), "iri": it.get("iri"),
            "od": it.get("znění-datum-účinnosti-od"), "do": it.get("znění-datum-účinnosti-do"),
            "typ_zneni": it.get("cis-esb-typ-znění-položka"),
        })
    rows, skipped = [], {"zruseny": 0, "uplne_zneni": 0}
    for c, vs in by_law.items():
        kon = [v for v in vs if v["typ_zneni"] == "KONSOL"]
        if kon:
            live = [v for v in kon if v["od"] and v["od"] <= today and (v["do"] is None or v["do"] >= today)]
            if not live:
                skipped["zruseny"] += 1
                continue
            v, status = max(live, key=lambda v: v["od"]), "platny"
        else:
            v, status = max(vs, key=lambda v: v["od"] or ""), "neznamy"
        if is_republication(v["nazev"]):
            skipped["uplne_zneni"] += 1
            continue
        typ = law_type(clean(v["nazev"]))
        rows.append((c, v["nazev"], status, v["doc_id"], v["iri"], v["od"], typ, priority(status, typ, c)))
    con.executemany("INSERT INTO laws (akt_citace, nazev, status, doc_id, zneni_iri, ucinnost_od, typ, priority) "
                    "VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    log.info("001: %d předpisů k indexaci, vynecháno %s", len(rows), skipped)

    want = {r[3] for r in rows}
    log.info("003: čtu strom fragmentů (~2 min) …")
    need, buf, n = set(), [], 0
    for it in items("003PravniAktZneniFragment.json.gz"):
        d = it.get("znění-dokument-id")
        if d not in want:
            continue
        fid = (it.get("právní-akt-fragment") or {}).get("fragment-id")
        need.add(fid)
        buf.append((d, it["iri"], (it.get("znění-fragment-předek") or {}).get("iri"),
                    it.get("znění-fragment-hierarchie-hex"), fid))
        if len(buf) >= 50000:
            con.executemany("INSERT INTO frag VALUES (?,?,?,?,?)", buf)
            n += len(buf)
            buf = []
    con.executemany("INSERT INTO frag VALUES (?,?,?,?,?)", buf)
    n += len(buf)
    con.execute("CREATE INDEX IF NOT EXISTS idx_frag_doc ON frag(doc_id)")
    con.commit()
    log.info("003: %d fragmentů", n)

    log.info("004: čtu texty fragmentů (~1 min) …")
    buf, n = [], 0
    for it in items("004PravniAktFragment.json.gz"):
        fid = it.get("fragment-id")
        if fid in need:
            buf.append((fid, it.get("cis-esb-typ-fragmentu-položka"), it.get("fragment-text")))
            if len(buf) >= 50000:
                con.executemany("INSERT OR REPLACE INTO text VALUES (?,?,?)", buf)
                n += len(buf)
                buf = []
    con.executemany("INSERT OR REPLACE INTO text VALUES (?,?,?)", buf)
    n += len(buf)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('extract_done', ?)", (today,))
    con.commit()
    log.info("004: %d textů. extract hotový.", n)


# ── units ────────────────────────────────────────────────────────────────────
def stage_units():
    con = db()
    laws = con.execute("SELECT akt_citace, nazev, status, doc_id, zneni_iri, ucinnost_od, priority FROM laws "
                       "WHERE units_done = 0 ORDER BY priority").fetchall()
    log.info("units: %d předpisů ke zpracování", len(laws))
    t0, total = time.time(), 0
    for i, (c, nazev, status, doc_id, iri, od, prio) in enumerate(laws, 1):
        rows = [{"iri": r[0], "parent": r[1], "hex": r[2], "typ": r[3] or "", "text": clean(r[4])}
                for r in con.execute("SELECT f.iri, f.parent, f.hex, t.typ, t.text FROM frag f "
                                     "LEFT JOIN text t ON t.frag_id = f.frag_id WHERE f.doc_id = ?", (doc_id,))]
        docs = units_for_law(c, nazev, status, iri, od, rows)
        con.executemany("INSERT OR REPLACE INTO units (id, akt_citace, priority, len, doc) VALUES (?,?,?,?,?)",
                        [(d.pop("_id"), c, prio, len(d["search_text"]), json.dumps(d, ensure_ascii=False))
                         for d in docs])
        con.execute("UPDATE laws SET units_done = 1 WHERE akt_citace = ?", (c,))
        total += len(docs)
        if i % 500 == 0:
            con.commit()
            log.info("units: %d/%d předpisů, %d chunků, %.0f s", i, len(laws), total, time.time() - t0)
    con.execute("CREATE INDEX IF NOT EXISTS idx_units_todo ON units(embedded, priority, len)")
    con.commit()
    log.info("units hotové: +%d chunků", total)


# ── embed ────────────────────────────────────────────────────────────────────
class GPURestart(Exception):
    """Paměť GPU je zaplněná – proces skončí s kódem EXIT_RESTART a launcher ho spustí znovu (zase na GPU)."""


EXIT_INCOMPLETE = 3   # zastaveno (Ctrl+C / časový limit), práce zbývá
EXIT_RESTART = 75     # restart kvůli paměti GPU


class Embedder:
    """
    bge-m3 ve fp16, výhradně na GPU (MPS/CUDA).

    MPS si pro každý nový tvar vstupu (velikost dávky × délka) nechává v paměti cache a neuvolní ji –
    s dávkami proměnlivé velikosti/délky paměť driveru rostla ~6 GB/min až do OOM. Proto pevné tvary:
    délka se zarovná na koš po 64 tokenech (64…512) a každý koš má pevnou velikost dávky
    (TOKEN_BUDGET // délka); neúplná dávka se doplní kopiemi. Celkem tedy jen 8 různých tvarů.
    """

    BUCKET = 64
    MAX_LEN = 512
    TOKEN_BUDGET = 8192

    def __init__(self, device: str, mem_limit_gb: float):
        import torch
        from sentence_transformers import SentenceTransformer
        if device == "cpu":
            raise SystemExit("Embedding běží jen na GPU (mps/cuda).")
        self.torch = torch
        self.device = device
        self.mem_limit = mem_limit_gb * 1e9
        self.model = SentenceTransformer(V2_EMBED_MODEL, device=device)
        self.model.max_seq_length = self.MAX_LEN
        self.model.half()
        self.tok = self.model[0].tokenizer
        self._pad_to_buckets(self.model[0])

    def _pad_to_buckets(self, transformer):
        """Tokenizace ST dorovná dávku na nejdelší text; my navíc na násobek BUCKET."""
        orig, torch, tok, bucket = transformer.tokenize, self.torch, self.tok, self.BUCKET

        def tokenize(texts, padding=True):
            f = orig(texts, padding=padding)
            length = f["input_ids"].shape[1]
            target = min(self.MAX_LEN, -(-length // bucket) * bucket)
            if target > length:
                f["input_ids"] = torch.nn.functional.pad(f["input_ids"], (0, target - length), value=tok.pad_token_id)
                f["attention_mask"] = torch.nn.functional.pad(f["attention_mask"], (0, target - length), value=0)
            return f
        transformer.tokenize = tokenize

    def bucket_of(self, n_tokens: int) -> int:
        return min(self.MAX_LEN, max(self.BUCKET, -(-n_tokens // self.BUCKET) * self.BUCKET))

    def batch_size(self, bucket: int) -> int:
        return max(1, self.TOKEN_BUDGET // bucket)

    def plan(self, texts: list) -> list:
        """Rozdělí texty na dávky pevného tvaru. Vrací [(bucket, [indexy])]; pořadí uvnitř koše zachováno."""
        lengths = self.tok(texts, truncation=True, max_length=self.MAX_LEN)["input_ids"]
        groups = {}
        for i, ids in enumerate(lengths):
            groups.setdefault(self.bucket_of(len(ids)), []).append(i)
        out = []
        for bucket in sorted(groups):
            idx, size = groups[bucket], self.batch_size(bucket)
            out += [(bucket, idx[j: j + size]) for j in range(0, len(idx), size)]
        return out

    def encode(self, texts: list, bucket: int):
        size = self.batch_size(bucket)
        padded = texts + [texts[-1]] * (size - len(texts))   # pevná velikost dávky
        try:
            v = self.model.encode(padded, batch_size=size, normalize_embeddings=True)
        except RuntimeError as e:
            if "out of memory" in str(e).lower():
                raise GPURestart(f"OOM na GPU: {str(e)[:150]}") from e
            raise
        if self.device == "mps":
            self.torch.mps.empty_cache()
            used = self.torch.mps.driver_allocated_memory()
            if used > self.mem_limit:
                raise GPURestart(f"paměť GPU {used / 1e9:.1f} GB > limit {self.mem_limit / 1e9:.1f} GB")
        return v[: len(texts)]

    def gpu_mem_gb(self) -> float:
        return self.torch.mps.driver_allocated_memory() / 1e9 if self.device == "mps" else 0.0


class ESPusher:
    """Posílá lokálně uložené vektory do ES. Výpadek ES/VPN nezastaví embedding – zkusí to znovu za 5 min."""

    RETRY_AFTER = 300

    def __init__(self, con: sqlite3.Connection, max_priority: int):
        from elasticsearch import Elasticsearch, helpers
        self.helpers = helpers
        self.con = con
        self.max_priority = max_priority
        self.es = Elasticsearch([ES_URL], request_timeout=300, retry_on_timeout=True, max_retries=2)
        self.ready = False
        self.next_try = 0.0
        self.pushed = 0

    def _ensure_index(self):
        if self.ready:
            return
        if not self.es.indices.exists(index=INDEX):
            body = json.loads(json.dumps(MAPPING))
            # vektory jen v HNSW, search_text jde zrekonstruovat (v2_common.search_text_of) – šetří disk i RAM
            body["mappings"]["_source"] = {"excludes": ["vektor", "search_text"]}
            self.es.indices.create(index=INDEX, body=body)
            log.info("Vytvořen index %s", INDEX)
        self.es.indices.put_settings(index=INDEX, body={"index": {"refresh_interval": "-1"}})
        self.ready = True

    def _pending(self, limit: int) -> list:
        return self.con.execute(
            "SELECT u.id, u.doc, v.v FROM units u JOIN vectors v ON v.id = u.id "
            "WHERE u.embedded = 1 AND u.pushed = 0 AND u.priority <= ? LIMIT ?", (self.max_priority, limit)).fetchall()

    def push_some(self, limit: int = 2000, batch: int = 200) -> bool:
        """Pošle až `limit` čekajících dokumentů. False = ES nedostupný (zkusí se později)."""
        if time.time() < self.next_try:
            return False
        try:
            self._ensure_index()
            rows = self._pending(limit)
            for i in range(0, len(rows), batch):
                part = rows[i: i + batch]
                actions = [{"_index": INDEX, "_id": rid, "_source": {
                    **json.loads(doc), "vektor": np.frombuffer(v, dtype=np.float16).astype(np.float32).tolist()}}
                    for rid, doc, v in part]
                ok, errors = self.helpers.bulk(self.es.options(request_timeout=300), actions,
                                               raise_on_error=False, max_retries=3)
                failed = {e.get("index", {}).get("_id") for e in errors}
                done = [(rid,) for rid, _, _ in part if rid not in failed]
                self.con.executemany("UPDATE units SET pushed = 1 WHERE id = ?", done)
                self.con.commit()
                self.pushed += len(done)
                if errors:
                    raise RuntimeError(f"{len(errors)} chyb v bulku, první: {str(errors[0])[:300]}")
            return True
        except Exception as e:  # noqa: BLE001
            self.ready = False
            self.next_try = time.time() + self.RETRY_AFTER
            log.warning("ES nedostupný / chyba bulku (%s) – embeduji dál lokálně, další pokus za %d min",
                        str(e)[:200], self.RETRY_AFTER // 60)
            return False

    def push_all(self, deadline: float):
        """Pošle všechno čekající; při výpadku čeká a zkouší znovu až do deadline."""
        while not _stop and time.time() < deadline:
            if not self._pending(1):
                return True
            if not self.push_some(limit=5000):
                time.sleep(min(self.RETRY_AFTER, max(0, self.next_try - time.time())))
        return not self._pending(1)

    def finish(self):
        try:
            if self.ready or self.es.indices.exists(index=INDEX):
                self.es.indices.put_settings(index=INDEX, body={"index": {"refresh_interval": "1s"}})
                self.es.indices.refresh(index=INDEX)
                log.info("V indexu %s je %d dokumentů", INDEX, self.es.count(index=INDEX)["count"])
        except Exception as e:  # noqa: BLE001
            log.warning("Nepodařilo se obnovit refresh_interval (ES nedostupný?): %s", str(e)[:200])


def stage_embed(max_priority: int, hours: float, device: str, mem_limit_gb: float) -> int:
    """
    Embedding lokálně do SQLite (záloha) + průběžné posílání do ES, když je dostupný.
    Návratový kód: 0 = vše hotovo, EXIT_INCOMPLETE = zastaveno/limit, EXIT_RESTART = restart kvůli paměti GPU.
    """
    con = db()
    todo = con.execute("SELECT COUNT(*) FROM units WHERE embedded = 0 AND priority <= ?", (max_priority,)).fetchone()[0]
    log.info("embed: %d chunků k embeddingu (priorita <= %d), zařízení %s, limit %.1f h, limit paměti GPU %.0f GB",
             todo, max_priority, device, hours, mem_limit_gb)
    pusher = ESPusher(con, max_priority)
    deadline = time.time() + hours * 3600
    t0, done, last_log, rc = time.time(), 0, 0.0, 0
    try:
        emb = Embedder(device, mem_limit_gb) if todo else None
        while todo and not _stop and time.time() < deadline:
            rows = con.execute("SELECT id, doc, priority FROM units WHERE embedded = 0 AND priority <= ? "
                               "ORDER BY priority, len LIMIT 4000", (max_priority,)).fetchall()
            if not rows:
                break
            docs = [json.loads(r[1]) for r in rows]
            texts = [d["search_text"] for d in docs]
            for bucket, idx in emb.plan(texts):
                if _stop or time.time() >= deadline:
                    break
                vecs = emb.encode([texts[i] for i in idx], bucket)
                # nejdřív lokální záloha, teprve pak ES
                con.executemany("INSERT OR REPLACE INTO vectors (id, v) VALUES (?, ?)",
                                [(rows[i][0], np.asarray(v, dtype=np.float16).tobytes()) for i, v in zip(idx, vecs)])
                con.executemany("UPDATE units SET embedded = 1 WHERE id = ?", [(rows[i][0],) for i in idx])
                con.commit()
                done += len(idx)
                pusher.push_some()
                if time.time() - last_log > 60:
                    last_log = time.time()
                    rate = done / (time.time() - t0)
                    log.info("embed: %d/%d (%.1f %%) | %.1f chunků/s | priorita %d | GPU %.1f GB | v ES +%d | zbývá ~%.1f h",
                             done, todo, 100 * done / max(todo, 1), rate, rows[idx[-1]][2], emb.gpu_mem_gb(),
                             pusher.pushed, (todo - done) / max(rate, 1e-6) / 3600)
        left_embed = con.execute("SELECT COUNT(*) FROM units WHERE embedded = 0 AND priority <= ?",
                                 (max_priority,)).fetchone()[0]
        rc = EXIT_INCOMPLETE if left_embed else 0
        # zbytek do ES (při výpadku čeká až do deadline, nejméně 30 min); po Ctrl+C jen krátce
        pusher.push_all(max(deadline, time.time() + PUSH_GRACE_S) if not _stop else time.time() + 60)
    except GPURestart as e:
        log.warning("%s → ukončuji proces kvůli restartu na GPU (pokračuje se od posledního uloženého chunku)", e)
        pusher.push_some(limit=100000)
        rc = EXIT_RESTART
    finally:
        pusher.finish()
        left = con.execute("SELECT COUNT(*) FROM units WHERE embedded = 1 AND pushed = 0").fetchone()[0]
        log.info("embed konec: +%d chunků za %.1f h, do ES posláno %d, čeká na poslání %d, kód %d",
                 done, (time.time() - t0) / 3600, pusher.pushed, left, rc)
    return rc


def stage_push(max_priority: int, hours: float, resend_all: bool, recreate: bool):
    """Pošle lokálně uložené vektory do ES (doplnění po výpadku, obnova indexu, přesun na jiný server)."""
    con = db()
    pusher = ESPusher(con, max_priority)
    exists = pusher.es.indices.exists(index=INDEX)
    if recreate and exists:
        log.warning("Mažu index %s (--recreate)", INDEX)
        pusher.es.indices.delete(index=INDEX)
        exists = False
    if not exists:
        log.info("Index %s neexistuje → posílám všechny lokálně uložené vektory", INDEX)
        resend_all = True
    else:
        in_es = pusher.es.count(index=INDEX)["count"]
        marked = con.execute("SELECT COUNT(*) FROM units WHERE pushed = 1").fetchone()[0]
        if in_es < marked and not resend_all:
            log.warning("V indexu je %d dokumentů, ale DB jich eviduje %d jako odeslané – "
                        "pro úplnou obnovu spusť push --all", in_es, marked)
    if resend_all:
        con.execute("UPDATE units SET pushed = 0")
        con.commit()
    n = con.execute("SELECT COUNT(*) FROM units WHERE embedded = 1 AND pushed = 0 AND priority <= ?",
                    (max_priority,)).fetchone()[0]
    log.info("push: %d dokumentů k odeslání do %s", n, INDEX)
    try:
        pusher.push_all(time.time() + hours * 3600)
    finally:
        pusher.finish()
        log.info("push konec: odesláno %d", pusher.pushed)


def stage_backup(out: str):
    """Konzistentní kopie celé DB (lze i za běhu). Obnova = zkopírovat soubor zpět na data/v2/v2.db."""
    con = db()
    if out:
        target = Path(out).expanduser()
        if target.is_dir():
            target = target / f"v2-{date.today():%Y%m%d}.db"
    else:
        target = DB_PATH.parent / "backups" / f"v2-{date.today():%Y%m%d}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    dst = sqlite3.connect(target)
    con.backup(dst)
    dst.close()
    n = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    log.info("backup: %s (%.1f GB, %d vektorů) za %.0f s", target, target.stat().st_size / 1e9, n, time.time() - t0)
    # staré automatické zálohy: nechat poslední 2
    if not out:
        for old in sorted(target.parent.glob("v2-*.db"))[:-2]:
            old.unlink()
            log.info("backup: smazána stará záloha %s", old.name)


# ── stats ────────────────────────────────────────────────────────────────────
def stage_stats():
    con = db()
    print(f"DB: {DB_PATH}  index: {INDEX} @ {ES_URL}")
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    print(f"extract: {meta.get('extract_done', 'NE')}")
    print("předpisy (status, units hotové):",
          con.execute("SELECT status, SUM(units_done), COUNT(*) FROM laws GROUP BY status").fetchall())
    print(f"{'priorita':>8} {'chunků':>9} {'embed':>9} {'%':>6} {'v ES':>9}")
    for p, n, e, pu in con.execute("SELECT priority, COUNT(*), SUM(embedded), SUM(pushed) FROM units "
                                   "GROUP BY priority ORDER BY priority"):
        print(f"{p:>8} {n:>9} {e or 0:>9} {100 * (e or 0) / n:>6.1f} {pu or 0:>9}")
    nv = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    print(f"lokální záloha vektorů: {nv} ({nv * 2048 / 1e9:.2f} GB)")
    backups = sorted((DB_PATH.parent / "backups").glob("v2-*.db"))
    print("zálohy DB:", ", ".join(f"{b.name} ({b.stat().st_size / 1e9:.1f} GB)" for b in backups) or "žádné")


def main():
    ap = argparse.ArgumentParser(description="Plný build indexu v2 (zakony-v2)")
    ap.add_argument("stage", choices=["extract", "units", "embed", "push", "backup", "stats"])
    ap.add_argument("--date", default=date.today().isoformat(), help="datum účinnosti znění (extract)")
    ap.add_argument("--max-priority", type=int, default=5,
                    help="embedovat jen priority <= N (viz v2_common.priority); 8 = mezinárodní smlouvy")
    ap.add_argument("--hours", type=float, default=24.0, help="časový limit embeddingu")
    ap.add_argument("--device", default="mps", choices=["mps", "cuda"])
    ap.add_argument("--gpu-mem-limit", type=float, default=float(os.getenv("GPU_MEM_LIMIT_GB", "12")),
                    help="při překročení (GB paměti driveru MPS) se proces restartuje")
    ap.add_argument("--recreate", action="store_true", help="push: smazat index a naplnit ho znovu z lokálních vektorů")
    ap.add_argument("--all", action="store_true", help="push: poslat znovu všechno (např. na nový server)")
    ap.add_argument("--out", default="", help="backup: cílový soubor nebo adresář (výchozí data/v2/backups/)")
    args = ap.parse_args()

    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "build_v2.log")])
    for noisy in ("elastic_transport", "urllib3", "sentence_transformers", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    if args.stage == "extract":
        stage_extract(args.date)
    elif args.stage == "units":
        stage_units()
    elif args.stage == "embed":
        sys.exit(stage_embed(args.max_priority, args.hours, args.device, args.gpu_mem_limit))
    elif args.stage == "push":
        stage_push(args.max_priority, args.hours, args.all, args.recreate)
    elif args.stage == "backup":
        stage_backup(args.out)
    else:
        stage_stats()


if __name__ == "__main__":
    main()
