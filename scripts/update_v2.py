"""
Průběžná aktualizace indexu v2 (zakony-v2) na znění účinná k danému datu – bez celého rebuildu.

Co dělá (každý krok je bezpečné opakovat):
    1. (--download) stáhne novější dumpy 001–004 z opendata.eselpoint.gov.cz
    2. z 001 vybere pro každý předpis znění k datu (stejná pravidla jako build) a porovná s DB:
         nový předpis | změněný (jiné znění, oprava dat ve stejném znění, změna statusu/metadat) | odstraněný
    3. z 003/004 načte fragmenty jen pro nové/změněné předpisy a postaví § jednotky
    4. vektory: § se stejným textem (sha1 search_text) převezme z lokální zálohy, embeduje jen nové texty
    5. ES bez výpadku: nejdřív pošle nové znění předpisu, teprve pak smaže dokumenty starého znění
       (při výpadku ES zůstane staré znění v indexu; příští běh doposílá a dosmaže – tabulka es_pending_delete)

Použití:
    .venv/bin/python scripts/update_v2.py --dry-run              # jen plán, nesahá na DB ani ES
    .venv/bin/python scripts/update_v2.py                        # aktualizace na dnešek z lokálních dumpů
    .venv/bin/python scripts/update_v2.py --download             # nejdřív stáhnout novější dumpy
    .venv/bin/python scripts/update_v2.py --date 2027-01-01

Návratový kód: 0 = hotovo, 3 = nedokončeno (ES nedostupný – spustit znovu), 75 = restart kvůli paměti GPU.
"""

import argparse
import email.utils
import fcntl
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_v2_index as B  # noqa: E402

log = logging.getLogger("v2.update")

DUMPS = ("001PravniAktZneni", "002PravniAkt", "003PravniAktZneniFragment", "004PravniAktFragment")
DUMP_URL = "https://opendata.eselpoint.gov.cz/datove-sady-esbirka/{name}.json.gz"


def text_hash(search_text: str) -> str:
    return hashlib.sha1(search_text.encode("utf-8")).hexdigest()


# ── schéma a příprava DB ─────────────────────────────────────────────────────
def ensure_schema(con: sqlite3.Connection):
    con.executescript("""
        -- vektory podle obsahu: § se stejným textem v novém znění převezme vektor bez embeddingu
        CREATE TABLE IF NOT EXISTS vec_by_hash (h TEXT PRIMARY KEY, v BLOB);
        -- předpisy, jejichž staré dokumenty v ES čekají na smazání
        -- (keep_zneni_iri = nové znění → smazat dokumenty předpisu mimo jeho aktuální § jednotky; NULL = smazat vše)
        CREATE TABLE IF NOT EXISTS es_pending_delete (akt_citace TEXT PRIMARY KEY, keep_zneni_iri TEXT);
        CREATE TABLE IF NOT EXISTS update_log (ts TEXT, date TEXT, summary TEXT);
    """)
    con.commit()


def backfill_hashes(con: sqlite3.Connection, batch: int = 20000):
    """Jednorázově: naplní vec_by_hash z vektorů, které už v DB jsou (po plném buildu)."""
    if con.execute("SELECT 1 FROM vec_by_hash LIMIT 1").fetchone():
        return
    total = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    if not total:
        return
    log.info("vec_by_hash: první naplnění z %d lokálních vektorů …", total)
    cur = con.execute("SELECT u.doc, v.v FROM units u JOIN vectors v ON v.id = u.id")
    n = 0
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            break
        con.executemany("INSERT OR IGNORE INTO vec_by_hash (h, v) VALUES (?, ?)",
                        [(text_hash(json.loads(doc)["search_text"]), v) for doc, v in rows])
        n += len(rows)
    con.commit()
    log.info("vec_by_hash: %d vektorů (%d unikátních textů)", n,
             con.execute("SELECT COUNT(*) FROM vec_by_hash").fetchone()[0])


# ── stažení dumpů ────────────────────────────────────────────────────────────
def download_dumps(dump_dir: Path) -> list:
    """Stáhne dumpy, které jsou na serveru novější než lokální (podle Last-Modified). Vrací stažené."""
    got = []
    for name in DUMPS:
        url, local = DUMP_URL.format(name=name), dump_dir / f"{name}.json.gz"
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
            remote_ts = email.utils.parsedate_to_datetime(r.headers["Last-Modified"]).timestamp()
            size = int(r.headers.get("Content-Length") or 0)
        if local.exists() and local.stat().st_mtime >= remote_ts:
            log.info("dump %s je aktuální", name)
            continue
        part = local.with_suffix(".gz.part")
        log.info("stahuji %s (%.0f MB) …", name, size / 1e6)
        with urllib.request.urlopen(url, timeout=600) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        if size and part.stat().st_size != size:
            part.unlink()
            raise RuntimeError(f"{name}: nekompletní stažení ({part.stat().st_size} z {size} B)")
        os.replace(part, local)
        os.utime(local, (remote_ts, remote_ts))
        got.append(name)
    return got


# ── plán změn ────────────────────────────────────────────────────────────────
def plan_changes(indexed: dict, target_rows: list) -> dict:
    """
    indexed: {akt_citace: řádek laws (LAW_COLS)} – současný stav DB/indexu
    target_rows: výstup choose_versions pro nové datum
    """
    plan = {"new": [], "changed": [], "removed": [], "baseline": []}
    target = {r[0]: r for r in target_rows}
    for c, row in target.items():
        old = indexed.get(c)
        if old is None:
            plan["new"].append((row, "nový předpis"))
            continue
        _, nazev, status, doc_id, iri, od, typ, prio, zmena = row
        if old[4] != iri:
            plan["changed"].append((row, f"nové znění {old[5]} → {od}"))
        elif old[3] != doc_id:
            plan["changed"].append((row, "nový dokument téhož znění (oprava v datech)"))
        elif old[8] and zmena and old[8] != zmena:
            plan["changed"].append((row, "oprava dat ve stejném znění"))
        elif (old[1], old[2], old[6], old[7]) != (nazev, status, typ, prio):
            plan["changed"].append((row, "změna statusu/metadat"))
        elif old[8] is None and zmena:
            plan["baseline"].append(row)            # první běh po plném buildu: jen zapsat čas změny
    plan["removed"] = sorted(set(indexed) - set(target))
    return plan


# ── provedení ────────────────────────────────────────────────────────────────
def rebuild_laws(con, rows: list, max_priority: int, device: str, mem_limit_gb: float) -> dict:
    """Nové/změněné předpisy: fragmenty → § jednotky → vektory (převzaté / nově embedované) → DB."""
    stats = {"chunks": 0, "reused": 0, "embedded": 0}
    if not rows:
        return stats
    old_docs = [r[0] for r in con.execute(
        f"SELECT doc_id FROM laws WHERE akt_citace IN ({','.join('?' * len(rows))})", [r[0] for r in rows])]
    con.executemany("DELETE FROM frag WHERE doc_id = ?", [(d,) for d in old_docs])
    need = B.extract_fragments(con, {r[3] for r in rows})
    B.extract_texts(con, need)

    units, todo = {}, {}                    # akt_citace -> [(id, doc, h, prio)], h -> search_text
    for c, nazev, status, doc_id, iri, od, typ, prio, _ in rows:
        units[c] = []
        for d in B.law_units(con, c, nazev, status, doc_id, iri, od):
            uid, h = d.pop("_id"), text_hash(d["search_text"])
            units[c].append((uid, d, h, prio))
            if prio <= max_priority:
                if con.execute("SELECT 1 FROM vec_by_hash WHERE h = ?", (h,)).fetchone():
                    stats["reused"] += 1
                else:
                    todo[h] = d["search_text"]
    stats["chunks"] = sum(len(u) for u in units.values())

    if todo:
        log.info("embedding %d nových textů (zbytek převzat z lokálních vektorů)", len(todo))
        emb = B.Embedder(device, mem_limit_gb)
        hashes, texts = list(todo), list(todo.values())
        for bucket, idx in emb.plan(texts):
            vecs = emb.encode([texts[i] for i in idx], bucket)
            con.executemany("INSERT OR REPLACE INTO vec_by_hash (h, v) VALUES (?, ?)",
                            [(hashes[i], np.asarray(v, dtype=np.float16).tobytes()) for i, v in zip(idx, vecs)])
            con.commit()                    # průběžně – po pádu se už spočítané převezmou
        stats["embedded"] = len(todo)

    # zápis do DB po předpisech; dokumenty v ES označit k (pře)poslání, staré znění k smazání
    for c, nazev, status, doc_id, iri, od, typ, prio, zmena in rows:
        old_ids = [r[0] for r in con.execute("SELECT id FROM units WHERE akt_citace = ?", (c,))]
        con.executemany("DELETE FROM vectors WHERE id = ?", [(i,) for i in old_ids])
        con.execute("DELETE FROM units WHERE akt_citace = ?", (c,))
        for uid, d, h, p in units[c]:
            vec = con.execute("SELECT v FROM vec_by_hash WHERE h = ?", (h,)).fetchone() if p <= max_priority else None
            con.execute("INSERT INTO units (id, akt_citace, priority, len, doc, embedded, pushed) VALUES (?,?,?,?,?,?,0)",
                        (uid, c, p, len(d["search_text"]), json.dumps(d, ensure_ascii=False), 1 if vec else 0))
            if vec:
                con.execute("INSERT OR REPLACE INTO vectors (id, v) VALUES (?, ?)", (uid, vec[0]))
        con.execute(f"INSERT OR REPLACE INTO laws ({B.LAW_COLS}, units_done) VALUES (?,?,?,?,?,?,?,?,?,1)",
                    (c, nazev, status, doc_id, iri, od, typ, prio, zmena))
        con.execute("INSERT OR REPLACE INTO es_pending_delete VALUES (?, ?)", (c, iri))
    con.commit()
    return stats


def remove_laws(con, citations: list):
    for c in citations:
        ids = [r[0] for r in con.execute("SELECT id FROM units WHERE akt_citace = ?", (c,))]
        con.executemany("DELETE FROM vectors WHERE id = ?", [(i,) for i in ids])
        con.execute("DELETE FROM units WHERE akt_citace = ?", (c,))
        doc = con.execute("SELECT doc_id FROM laws WHERE akt_citace = ?", (c,)).fetchone()
        if doc:
            con.execute("DELETE FROM frag WHERE doc_id = ?", (doc[0],))
        con.execute("DELETE FROM laws WHERE akt_citace = ?", (c,))
        con.execute("INSERT OR REPLACE INTO es_pending_delete VALUES (?, NULL)", (c,))
    con.commit()


def sync_es(con, max_priority: int, hours: float) -> bool:
    """Pošle čekající dokumenty a teprve potom smaže staré znění. False = ES nedostupný, zkusit příště."""
    pusher = B.ESPusher(con, max_priority)
    try:
        if not pusher.push_all(time.time() + hours * 3600):
            return False
        pending = con.execute("SELECT akt_citace, keep_zneni_iri FROM es_pending_delete").fetchall()
        if pending:
            pusher.es.indices.refresh(index=B.INDEX)       # nové znění viditelné dřív, než zmizí staré
        for c, keep in pending:
            q = {"bool": {"filter": [{"term": {"akt_citace": c}}]}}
            if keep:
                # nechat jen aktuální § jednotky – pokryje nové znění i opravu, která § ubrala
                ids = [r[0] for r in con.execute(
                    "SELECT id FROM units WHERE akt_citace = ? AND priority <= ?", (c, max_priority))]
                if ids:
                    q["bool"]["must_not"] = [{"ids": {"values": ids}}]
            pusher.es.delete_by_query(index=B.INDEX, query=q, conflicts="proceed", refresh=False)
            con.execute("DELETE FROM es_pending_delete WHERE akt_citace = ?", (c,))
            con.commit()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("ES: %s – staré dokumenty zůstávají, příští běh dokončí", str(e)[:200])
        return False
    finally:
        pusher.finish()


def dumps_mtime() -> float:
    """Stáří dumpu 001 (podle něj se vybírají znění) – čas poslední změny souboru (= Last-Modified při --download)."""
    return (B.DUMP_DIR / "001PravniAktZneni.json.gz").stat().st_mtime


def run_update(today: str, max_priority: int = 5, hours: float = 2.0, device: str = "mps",
               mem_limit_gb: float = 12.0, dry_run: bool = False, force: bool = False) -> tuple:
    """Vrací (návratový kód, souhrn)."""
    if not B.DB_PATH.exists():
        raise SystemExit("Chybí data/v2/v2.db – nejdřív plný build (scripts/run_v2_overnight.sh).")
    if dry_run:
        con = sqlite3.connect(f"file:{B.DB_PATH}?mode=ro", uri=True)
        cols = {r[1] for r in con.execute("PRAGMA table_info(laws)")}
        sel = B.LAW_COLS if "zneni_zmena" in cols else B.LAW_COLS.replace("zneni_zmena", "NULL")
    else:
        con = B.db()
        ensure_schema(con)
        sel = B.LAW_COLS
    built_for = (con.execute("SELECT value FROM meta WHERE key = 'extract_done'").fetchone() or [None])[0]
    if not built_for:
        raise SystemExit("DB nemá dokončený extract – nejdřív plný build.")
    if today < built_for:
        log.warning("cílové datum %s je dřív než současné %s – index se vrátí ke starším zněním", today, built_for)
    # Starší dumpy než ty, ze kterých se naposledy aktualizovalo, by předpisy vyhlášené mezitím vyhodnotily jako
    # „zrušené“ a smazaly je z indexu. Bez --force se nepokračuje.
    used = (con.execute("SELECT value FROM meta WHERE key = 'dumps_mtime'").fetchone() or [None])[0]
    current = dumps_mtime()
    if used and current < float(used) and not force:
        raise SystemExit(f"Dumpy v {B.DUMP_DIR} jsou starší ({date.fromtimestamp(current)}) než ty, ze kterých se "
                         f"naposledy aktualizovalo ({date.fromtimestamp(float(used))}) – nové předpisy by se smazaly. "
                         f"Použij --download (nebo V2_DUMP_DIR s novějšími dumpy); --force jen pokud je to záměr.")

    indexed = {r[0]: r for r in con.execute(f"SELECT {sel} FROM laws")}
    log.info("001: čtu znění, cílové datum %s (DB je k %s) …", today, built_for)
    target, skipped = B.choose_versions(B.read_versions(), today)
    plan = plan_changes(indexed, target)
    summary = {
        "date": today, "previous_date": built_for, "skipped": skipped,
        "new": [(r[0], why) for r, why in plan["new"]],
        "changed": [(r[0], why) for r, why in plan["changed"]],
        "removed": plan["removed"],
    }
    log.info("plán: %d nových, %d změněných, %d odstraněných předpisů", len(plan["new"]), len(plan["changed"]),
             len(plan["removed"]))
    for key in ("new", "changed"):
        for c, why in summary[key][:30]:
            log.info("  %-8s %-18s %s", key, c, why)
    for c in plan["removed"][:30]:
        log.info("  removed  %s", c)
    if dry_run:
        return 0, summary

    if plan["baseline"]:
        con.executemany("UPDATE laws SET zneni_zmena = ? WHERE akt_citace = ?", [(r[8], r[0]) for r in plan["baseline"]])
        con.commit()
    backfill_hashes(con)
    try:
        summary["vectors"] = rebuild_laws(con, [r for r, _ in plan["new"] + plan["changed"]],
                                          max_priority, device, mem_limit_gb)
    except B.GPURestart as e:
        log.warning("%s → spusť znovu (už spočítané vektory se převezmou)", e)
        return B.EXIT_RESTART, summary
    remove_laws(con, plan["removed"])
    con.execute("INSERT OR REPLACE INTO meta VALUES ('extract_done', ?)", (today,))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('dumps_mtime', ?)", (str(max(current, float(used or 0))),))
    con.commit()

    ok = sync_es(con, max_priority, hours)
    summary["es_done"] = ok
    summary["es_pending_delete"] = con.execute("SELECT COUNT(*) FROM es_pending_delete").fetchone()[0]
    summary["es_pending_push"] = con.execute(
        "SELECT COUNT(*) FROM units WHERE embedded = 1 AND pushed = 0 AND priority <= ?", (max_priority,)).fetchone()[0]
    con.execute("INSERT INTO update_log VALUES (datetime('now'), ?, ?)", (today, json.dumps(summary, ensure_ascii=False)))
    con.commit()
    log.info("hotovo: %s", {k: summary[k] for k in ("es_done", "es_pending_push", "es_pending_delete")}
             | {"vektory": summary.get("vectors")})
    return (0 if ok else B.EXIT_INCOMPLETE), summary


def main():
    ap = argparse.ArgumentParser(description="Průběžná aktualizace indexu v2")
    ap.add_argument("--date", default=date.today().isoformat(), help="datum účinnosti znění (výchozí dnešek)")
    ap.add_argument("--download", action="store_true", help="nejdřív stáhnout novější dumpy 001–004")
    ap.add_argument("--dry-run", action="store_true", help="jen vypsat plán změn, nesahat na DB ani ES")
    ap.add_argument("--max-priority", type=int, default=5, help="embedovat a indexovat priority <= N (jako build)")
    ap.add_argument("--force", action="store_true", help="pokračovat i se staršími dumpy, než byly použity naposledy")
    ap.add_argument("--hours", type=float, default=2.0, help="jak dlouho čekat na ES při výpadku")
    ap.add_argument("--device", default="mps", choices=["mps", "cuda"])
    ap.add_argument("--gpu-mem-limit", type=float, default=float(os.getenv("GPU_MEM_LIMIT_GB", "12")))
    args = ap.parse_args()

    (B.ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(B.ROOT / "logs" / "update_v2.log")])
    for noisy in ("elastic_transport", "urllib3", "sentence_transformers", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    signal.signal(signal.SIGINT, B._on_signal)
    signal.signal(signal.SIGTERM, B._on_signal)

    B.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = open(B.DB_PATH.parent / "update.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)      # žádné dva běhy aktualizace současně
    except BlockingIOError:
        raise SystemExit("Jiná aktualizace už běží (data/v2/update.lock).")

    if args.download:                                     # stažení dumpů nemění DB ani index → i při --dry-run
        got = download_dumps(B.DUMP_DIR)
        log.info("staženo: %s", got or "nic, dumpy jsou aktuální")
    rc, summary = run_update(args.date, args.max_priority, args.hours, args.device, args.gpu_mem_limit, args.dry_run,
                             args.force)
    (B.ROOT / "logs" / f"update_v2_{args.date}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    sys.exit(rc)


if __name__ == "__main__":
    main()
