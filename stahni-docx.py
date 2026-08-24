#!/usr/bin/env python3
"""
Paralelní stahování DOCX (informativní znění) všech předpisů z e-Sbírky.

Seznam předpisů se načte z OpenData (002PravniAkt.json.gz), ten se stáhne
jednou a cachuje lokálně. Stav každého předpisu (baseId, stažení) se ukládá
do SQLite checkpointu — skript lze kdykoli přerušit a spustit znovu,
pokračuje tam, kde skončil.

Vzorek:
    python3 stahni-docx.py                          # všechno (~46 000 předpisů)
    python3 stahni-docx.py --limit 100              # jen prvních 100
    python3 stahni-docx.py --url "/sb/1993/1"       # konkrétní zákon
    python3 stahni-docx.py --workers 16             # víc vláken

Rychlost: adaptivní throttling — při chybách 429/5xx nebo timeoutech se
automaticky zpomalí, když server zvládá, zase zrychlí.
"""

import argparse
import gzip
import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

OPENDATA_URL = "https://opendata.eselpoint.gov.cz/datove-sady-esbirka/002PravniAkt.json.gz"
SBIRKA_BASE = "https://e-sbirka.gov.cz"
HTTP_TIMEOUT = 60
MAX_ATTEMPTS = 4


# =====================================================================
# Adaptivní throttling
# =====================================================================

class AdaptiveThrottle:
    """Sdílený limit rychlosti. Chyby ho zpomalí, úspěchy postupně zrychlí."""

    def __init__(self, start_delay: float = 0.08, min_delay: float = 0.02,
                 max_delay: float = 8.0):
        self.delay = start_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            d = self.delay
        time.sleep(d)

    def report_success(self):
        with self._lock:
            self.delay = max(self.min_delay, self.delay * 0.85)

    def report_throttle(self):
        with self._lock:
            self.delay = min(self.max_delay, max(self.delay * 2.0, 0.5))


THROTTLE = AdaptiveThrottle()


# =====================================================================
# HTTP pomůcky
# =====================================================================

def http_get(url, accept="application/json"):
    """GET s throttlingem. Vrací (data, None) nebo (None, chybová_zpráva)."""
    req = Request(url, headers={
        "Accept": accept,
        "User-Agent": "Mozilla/5.0 (compatible; ZakonyStahovac/2.0)",
    })
    THROTTLE.wait()
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
        THROTTLE.report_success()
        return body, None
    except HTTPError as e:
        if e.code in (429, 500, 502, 503, 504):
            THROTTLE.report_throttle()
            return None, f"HTTP {e.code} (throttle)"
        return None, f"HTTP {e.code}"
    except (URLError, TimeoutError, OSError) as e:
        THROTTLE.report_throttle()
        return None, f"síťová chyba: {e}"


def api_get_json(url):
    for attempt in range(MAX_ATTEMPTS):
        body, err = http_get(url)
        if body is not None:
            try:
                return json.loads(body), None
            except json.JSONDecodeError as e:
                return None, f"nevalidní JSON: {e}"
        if "throttle" not in (err or "") and attempt < MAX_ATTEMPTS - 1:
            # trvalá chyba (např. 404) — opakovat nemá smysl
            return None, err
        time.sleep(2 ** attempt * 2)  # exponenciální backoff: 2, 4, 8 s
    return None, err


def api_get_binary(url):
    for attempt in range(MAX_ATTEMPTS):
        body, err = http_get(url, accept="application/octet-stream")
        if body is not None:
            return body, None
        time.sleep(2 ** attempt * 2)
    return None, err


# =====================================================================
# Seznam předpisů z OpenData
# =====================================================================

def fetch_law_list(cache_dir: Path) -> list:
    """Stáhne (nebo použije cachovaný) 002PravniAkt.json.gz a načte předpisy."""
    gz_path = cache_dir / "002PravniAkt.json.gz"
    if not gz_path.exists():
        print(f"Stahuji seznam předpisů: {OPENDATA_URL}")
        body, err = api_get_binary(OPENDATA_URL)
        if body is None:
            sys.exit(f"Nelze stáhnout seznam předpisů: {err}")
        gz_path.write_bytes(body)

    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("položky", data.get("items", data if isinstance(data, list) else []))

    laws = []
    seen = set()
    for item in items:
        citace = item.get("akt-citace", "")
        sbirka = item.get("akt-sbírka-kód", "sb")
        rok = item.get("akt-rok-předpisu")
        cislo = item.get("akt-číslo-předpisu")
        nazev = item.get("akt-název-vyhlášený", "")
        if not citace or not rok or not cislo:
            continue
        key = f"/{sbirka}/{rok}/{cislo}"
        if key in seen:
            continue
        seen.add(key)
        laws.append({"citace": citace, "nazev": nazev, "key": key})
    print(f"Načteno {len(laws):,} unikátních předpisů")
    return laws


# =====================================================================
# SQLite checkpoint
# =====================================================================

class Checkpoint:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS status (
                key TEXT PRIMARY KEY,
                citace TEXT,
                nazev TEXT,
                base_id TEXT,
                phase TEXT NOT NULL DEFAULT 'pending',
                file_path TEXT,
                error_msg TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()
        self._lock = threading.Lock()

    def load_all(self) -> dict:
        cur = self.conn.execute("SELECT key, phase, base_id, file_path FROM status")
        return {r[0]: {"phase": r[1], "base_id": r[2], "file_path": r[3]} for r in cur.fetchall()}

    def update(self, key, phase, base_id=None, file_path=None, error_msg=None,
               citace=None, nazev=None):
        with self._lock:
            self.conn.execute("""
                INSERT INTO status (key, phase, base_id, file_path, error_msg, citace, nazev)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    phase=excluded.phase,
                    base_id=COALESCE(excluded.base_id, status.base_id),
                    file_path=COALESCE(excluded.file_path, status.file_path),
                    error_msg=excluded.error_msg,
                    citace=COALESCE(excluded.citace, status.citace),
                    nazev=COALESCE(excluded.nazev, status.nazev),
                    updated_at=CURRENT_TIMESTAMP
            """, (key, phase, base_id, file_path, error_msg, citace, nazev))
            self.conn.commit()


# =====================================================================
# Stažení jednoho DOCX (3 kroky API)
# =====================================================================

def get_base_id(key: str):
    encoded = quote(key, safe="")
    data, err = api_get_json(f"{SBIRKA_BASE}/sbr-cache/dokumenty-sbirky/{encoded}")
    if data is None:
        return None, err
    if "chyby" in data:
        popis = data["chyby"][0].get("popis", "?") if data["chyby"] else "?"
        return None, f"API chyba: {popis}"
    base_id = data.get("dokumentBaseId")
    if not base_id:
        return None, "bez dokumentBaseId"
    return str(base_id), None


def get_docx_info(base_id: str):
    data, err = api_get_json(
        f"{SBIRKA_BASE}/sbr-externi/stahni/informativni-zneni/{base_id}/DOCX")
    if data is None:
        return None, err
    if data.get("stavPozadavku") != "OK":
        return None, f"stav={data.get('stavPozadavku')}"
    docx_id = data.get("id")
    if not docx_id:
        return None, "bez download ID"
    return str(docx_id), None


def download_file(docx_id: str, output_path: Path):
    body, err = api_get_binary(f"{SBIRKA_BASE}/souborove-sluzby/soubory/{docx_id}")
    if body is None:
        return 0, err
    output_path.write_bytes(body)
    return len(body), None


def filename_from_citace(citace: str) -> str:
    nazev = citace.replace(" Sb.", "").replace("/", "_")
    return re.sub(r"[^\w.-]", "", nazev)


def process_law(law, state, output_dir: Path, cp: Checkpoint):
    """Vrátí (key, vysledek, hlaska) — vysledek: ok | skip | no_docx | error."""
    key = law["key"]
    st = state.get(key, {})
    out_path = output_dir / f"{filename_from_citace(law['citace'])}.docx"

    if st.get("phase") == "downloaded" and Path(st.get("file_path") or out_path).exists():
        return key, "skip", "již staženo"

    base_id = st.get("base_id")
    if not base_id:
        base_id, err = get_base_id(key)
        if not base_id:
            return key, "error", f"baseId: {err}"

    docx_id, err = get_docx_info(base_id)
    if not docx_id:
        cp.update(key, "no_docx", base_id=base_id, error_msg=err)
        return key, "no_docx", err

    size, err = download_file(docx_id, out_path)
    if size <= 0:
        cp.update(key, "error", base_id=base_id, error_msg=f"download: {err}")
        return key, "error", f"download: {err}"

    cp.update(key, "downloaded", base_id=base_id, file_path=str(out_path),
              citace=law.get("citace"), nazev=law.get("nazev"))
    return key, "ok", f"{size:,} B"


# =====================================================================
# Main
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="Paralelní stahování DOCX z e-Sbírky")
    parser.add_argument("--url", type=str,
                        help="konkrétní předpis (např. /sb/1993/1)")
    parser.add_argument("--limit", type=int, default=0,
                        help="max počet předpisů (0 = vše)")
    parser.add_argument("--min-year", type=int, default=0,
                        help="jen předpisy od daného roku")
    parser.add_argument("--workers", type=int, default=8,
                        help="počet paralelních vláken (výchozí: 8)")
    parser.add_argument("--retry-errors", action="store_true",
                        help="zkusit znovu i předpisy, které loni selhaly")
    parser.add_argument("--output", type=str, default="./zakony",
                        help="cíl pro stažené soubory (výchozí: ./zakony)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    cp = Checkpoint(output_dir / ".checkpoint.db")

    if args.url:
        laws = [{"citace": "", "nazev": "", "key": args.url.rstrip("/")}]
    else:
        laws = fetch_law_list(output_dir)
        if args.min_year:
            laws = [l for l in laws
                    if int(re.search(r"/(\d{4})/", l["key"]).group(1)) >= args.min_year]
            print(f"Filtr roku >= {args.min_year}: {len(laws):,} předpisů")
        if args.limit > 0:
            laws = laws[:args.limit]

    state = cp.load_all()
    if not args.retry_errors:
        laws = [l for l in laws if state.get(l["key"], {}).get("phase") != "error"]

    todo = [l for l in laws if state.get(l["key"], {}).get("phase") not in
            ("downloaded", "no_docx")]
    print(f"K dispozici: {len(laws):,}, zbývá stáhnout: {len(todo):,} "
          f"(staženo dříve: {len(laws) - len(todo):,})")

    stats = {"ok": 0, "skip": 0, "no_docx": 0, "error": 0}
    t0 = time.time()
    done_count = 0

    def log_progress(result):
        nonlocal done_count
        key, res, msg = result
        stats[res] += 1
        done_count += 1
        if res == "ok":
            print(f"[{done_count:,}/{len(todo):,}] OK   {key} ({msg})")
        elif res == "error":
            print(f"[{done_count:,}/{len(todo):,}] CHYBA {key}: {msg}", file=sys.stderr)
        if done_count % 200 == 0:
            elapsed = time.time() - t0
            rate = done_count / elapsed if elapsed else 0
            eta = (len(todo) - done_count) / rate if rate else 0
            print(f"--- {done_count:,}/{len(todo):,} | {rate:.1f} předpisů/s | "
                  f"ETA {eta/60:.0f} min | ok={stats['ok']} no_docx={stats['no_docx']} "
                  f"errors={stats['error']} ---")

    if args.url:
        _, res, msg = process_law(laws[0], state, output_dir, cp)
        print(f"{res.upper()}: {msg}")
        sys.exit(0 if res in ("ok", "skip") else 1)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process_law, law, state, output_dir, cp) for law in todo]
        for fut in as_completed(futures):
            log_progress(fut.result())

    elapsed = time.time() - t0
    print(f"\nHotovo za {elapsed/60:.1f} min: "
          f"{stats['ok']:,} staženo, {stats['skip']:,} přeskočeno, "
          f"{stats['no_docx']:,} bez informativního znění, {stats['error']:,} chyb")
    if stats["error"]:
        print("Chybné položky lze zopakovat: python3 stahni-docx.py --retry-errors")


if __name__ == "__main__":
    main()
