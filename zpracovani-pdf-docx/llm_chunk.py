#!/usr/bin/env python3
"""LLM chunking zákonů přes Chonkie SlumberChunker + qwen-3gpu (etikos proxy).

Hybridní strategie (varianta A pro ES index):
  - celý korpus se rozdělí na §/Článek/římské celky (extract_units z
    docx_embed) — to je zdarma a pokryje ~99 % zákonů,
  - LLM (SlumberChunker hledá tématické hranice) dostane Jen celky delší
    než chunk_size * 1.5 (default > 3000 znaků) — měřeno na korpusu:
    311 celků v 223 souborech z 15 362 (≈ 968 LLM volání),
  - výsledek jde do SQLite checkpointu (tabulka `chunky`, typ='LLM'),
    importní fáze je pak spojí s paragrafy z .embed.db (typ='DOCX').

Běh:
  python3 llm_chunk.py ../../zakony-es-projekt/zakony            # full run
  python3 llm_chunk.py ../../zakony-es-projekt/zakony --limit-files 5
  python3 llm_chunk.py ../../zakony-es-projekt/zakony/n256_1980.docx --out chunky.json
  python3 llm_chunk.py ../../zakony-es-projekt/zakony --fresh    # od nuly

Checkpoint: <data_dir>/.llmchunk.db — přerušení (Ctrl-C) není problém,
další běh pokračuje tam, kde skončil. Konfigurace: .env v kořeni worktree
(LLM_BASE_URL, LLM_API_KEY, LLM_MODEL).

Pozn.: tokenizér SlumberChunkeru je 'character' — chunk_size i
candidate_size jsou v znacích, stejná konvence jako CHUNK_CHARS.
"""
import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# =====================================================================
# .env (bez python-dotenv — ať experiment netáhne další závislost)
# =====================================================================

def load_env() -> Optional[Path]:
    """Načte .env z CWD nebo kořene worktree; existující env nepřepisuje."""
    for cand in (Path.cwd() / ".env",
                 Path(__file__).resolve().parent.parent / ".env"):
        if cand.is_file():
            for line in cand.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
            return cand
    return None


# =====================================================================
# Genie: retry + fallback ze structured outputs na plain completion
# =====================================================================

from chonkie import SlumberChunker  # noqa: E402
from chonkie.genie.openai import OpenAIGenie  # noqa: E402


class SafeOpenAIGenie(OpenAIGenie):
    """OpenAIGenie odolný vůči qwen backendu a limitům proxy (60 RPM,
    100k TPM): retry s exponenciálním backoffem + jitter, generate_json
    umí spadnout z structured outputs na obyčejný completion. Thread-safe
    (čísítadla pod zámkem, OpenAI klient je sdílený)."""

    def __init__(self, *args, max_retries: int = 5, backoff: float = 2.0,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.max_retries = max_retries
        self.backoff = backoff
        self._lock = threading.Lock()
        # výkonnostní: DCUK backend "umí" response_format, ale plýtvá
        # reasoningem (minuty/volání) — textový režim je ~10× rychlejší
        self._json_broken = bool(os.environ.get("LLM_SKIP_JSON"))
        self.calls = 0
        self.json_ok = 0
        self.fallbacks = 0

    def _bump(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    def _chat(self, prompt: str) -> str:
        delay = self.backoff
        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._bump("calls")
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2500,
                )
                msg = resp.choices[0].message
                content = msg.content
                if content is None:
                    # reasoning model ořezaný na max_tokens dá text do
                    # reasoning_content — i ten může obsahovat odpověď
                    content = getattr(msg, "reasoning_content", None)
                if content:
                    return content
                last_err = ValueError("prázdná odpověď modelu")
                log.warning("Prázdná odpověď (pokus %d/%d)",
                            attempt, self.max_retries)
            except Exception as e:  # 429, timeout, 5xx — všechno retrypnout
                last_err = e
                log.warning("Volání LLM selhalo (pokus %d/%d): %s",
                            attempt, self.max_retries, e)
            if attempt < self.max_retries:
                # jitter proti synchronizovanému stádu u TPM/RPM limitu
                time.sleep(delay + random.uniform(0, delay / 2))
                delay *= 2
        raise RuntimeError(
            f"LLM neodpověděl po {self.max_retries} pokusech: {last_err}")

    def generate(self, prompt: str) -> str:
        return self._chat(prompt)

    def _clamp_split(self, prompt: str, split_index: int) -> int:
        """Omezí split_index na poslední ID pasáže v promptu + 1.

        SlumberChunker nízké hodnoty řeší sám (current_pos >= response
        → response = current_pos + 1), vysoké NE — odpověď nad rozsah
        shodí IndexError na splits[response-1] i
        cumulative_token_counts[response]. Odpověď nad poslední ID
        znamená „žádná změna tématu" → celé okno jako jeden chunk,
        což je přesně chování tiebreakeru ze šablony (Laguna např.
        odpovídá last+2 místo last+1).
        """
        ids = [int(m) for m in re.findall(r"^ID (\d+):", prompt, re.M)]
        if ids and split_index > ids[-1] + 1:
            log.debug("Clamp split_index %d → %d (poslední ID %d)",
                      split_index, ids[-1] + 1, ids[-1])
            return ids[-1] + 1
        return split_index

    def generate_json(self, prompt: str, schema) -> Dict[str, Any]:
        if not self._json_broken:
            try:
                out = super().generate_json(prompt, schema)
                self._bump("json_ok")
                out["split_index"] = self._clamp_split(
                    prompt, int(out["split_index"]))
                return out
            except Exception as e:
                with self._lock:
                    self._json_broken = True
                self._bump("fallbacks")
                log.info("Structured outputs nefungují (%s) — přepínám "
                         "trvale na textový režim", str(e)[:120])
        text = self._chat(
            prompt + "\n\nOdpověz POUZE číslem (hodnota split_index), "
            "bez jiného textu.")
        numbers = re.findall(r"\d+", text)
        if not numbers:
            raise ValueError(f"V odpovědi není číslo: {text[:200]!r}")
        return {"split_index": self._clamp_split(prompt, int(numbers[-1]))}


# =====================================================================
# SQLite checkpoint (vzor: Store v docx_embed.py)
# =====================================================================

class LLMStore:
    """Checkpoint full runu: laws (stav souboru) + chunky (typ='LLM')."""

    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS laws (
                id_zakona TEXT PRIMARY KEY,
                citace TEXT,
                rok INTEGER,
                sbirka TEXT,
                n_chunku INTEGER,
                status TEXT NOT NULL DEFAULT 'todo'
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chunky (
                id_zakona TEXT,
                idx INTEGER,
                citace TEXT,
                text TEXT NOT NULL,
                typ TEXT NOT NULL DEFAULT 'LLM',
                PRIMARY KEY (id_zakona, idx)
            )""")
        self.conn.commit()

    def law_status(self, id_zakona: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT status FROM laws WHERE id_zakona=?", (id_zakona,)
        ).fetchone()
        return row[0] if row else None

    def mark_scanned(self, id_zakona: str, citace: str, rok: int,
                     sbirka: str, status: str, n_chunku: int = 0) -> None:
        with self.conn:
            self.conn.execute("""
                INSERT INTO laws (id_zakona, citace, rok, sbirka, n_chunku,
                                  status)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(id_zakona) DO UPDATE SET
                    status=excluded.status
                    WHERE laws.status != 'done'
            """, (id_zakona, citace, rok, sbirka, n_chunku, status))

    def save_done(self, id_zakona: str, chunks: List[Tuple[str, str]]) -> None:
        """Uloží LLM chunky zákona a označí ho za hotový (atomicky)."""
        with self.conn:
            self.conn.execute("DELETE FROM chunky WHERE id_zakona=?",
                              (id_zakona,))
            self.conn.executemany(
                "INSERT INTO chunky VALUES (?,?,?,?,'LLM')",
                [(id_zakona, i, cit, text)
                 for i, (cit, text) in enumerate(chunks)])
            self.conn.execute(
                "UPDATE laws SET status='done', n_chunku=? WHERE id_zakona=?",
                (len(chunks), id_zakona))

    def stats(self) -> Tuple[int, int, int]:
        row = self.conn.execute("""
            SELECT SUM(status='done'), SUM(status='todo'),
                   COALESCE(SUM(n_chunku), 0)
            FROM laws""").fetchone()
        return (row[0] or 0, row[1] or 0, row[2] or 0)


# =====================================================================
# Chunkování
# =====================================================================

MIN_CHUNK_CHARS = 100  # pod tím chunk splyne se sousedem (zbytky tabulek)

# Vlastní šablona místo chonkie defaultu: právní text je tematicky
# homogenní, takže "find where the topic changes" uvádí modely do
# nerozhodnosti (smyčka → prázdná odpověď). Tiebreaker garantuje posun.
CUSTOM_TEMPLATE = (
    "Níže jsou pasáže z právního dokumentu (ID: text). Najdi index PRVNÍ "
    "pasáže, která začíná nové samostatné téma oproti předchozím. "
    "Pokud se téma v pasážích zásadně nemění, odpověz indexem "
    "bezprostředně následující pasáže. "
    "Odpověz POUZE jedním číslem (split_index).\n\n{passages}")


def _merge_tiny(chunks: List[str],
                min_chars: int = MIN_CHUNK_CHARS) -> List[str]:
    """Sloučí mikro-chunky se sousedem.

    LLM chunking v DOCX s tabulkami občas izoluje zbytky buněk/odrážek
    ("2.", nadpis sloupce) do chunků o pár znacích — chunk_long_text takové
    věci nevytváří, takže to srovnáme. Čísla v citacích se počítají až poté.
    """
    if not chunks:
        return chunks
    out = [chunks[0]]
    for c in chunks[1:]:
        if len(out[-1]) < min_chars:
            out[-1] += "\n" + c
        else:
            out.append(c)
    if len(out) > 1 and len(out[-1]) < min_chars:
        out[-2] += "\n" + out[-1]
        out.pop()
    return out


def read_input(path: Path) -> List[str]:
    """DOCX → odstavce (jako docx_to_texts) nebo TXT → řádky."""
    if path.suffix.lower() == ".docx":
        from docx import Document
        doc = Document(str(path))
        return [t.strip() for t in (p.text for p in doc.paragraphs)
                if t.strip()]
    return [line.strip() for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def chunk_whole(text: str, chunker: SlumberChunker) -> List[Dict[str, str]]:
    merged = _merge_tiny([c.text for c in chunker(text)])
    n = len(merged)
    return [{"citace": f"LLM ({i + 1}/{n})", "text": t}
            for i, t in enumerate(merged)]


# =====================================================================
# Full run: scan + paralelní LLM chunking s checkpointem
# =====================================================================

class CorpusChunker:
    """Sdružený stav full runu: genie + thread-local chunkery + statistiky."""

    def __init__(self, genie: SafeOpenAIGenie, chunk_size: int,
                 candidate_size: int):
        self.genie = genie
        self.chunk_size = chunk_size
        self.candidate_size = candidate_size
        self._local = threading.local()

    def chunker(self) -> SlumberChunker:
        # každý vlákno svou instanci (bez modelu — jen 'character' tokenizer)
        if not hasattr(self._local, "chunker"):
            self._local.chunker = SlumberChunker(
                genie=self.genie, chunk_size=self.chunk_size,
                candidate_size=self.candidate_size, verbose=False)
            self._local.chunker.template = CUSTOM_TEMPLATE
        return self._local.chunker


def scan_corpus(data_dir: Path, store: LLMStore, threshold: int
                ) -> List[Tuple[Path, str, str, int, str, List[Dict]]]:
    """Přegeneruje §-celky a vrátí soubory s dlouhými celky k LLM chunkingu.

    Stavy se zapisují do checkpointu: 'no_llm' (vše pod prahem — už se
    nescanuje), 'todo' (čeká na LLM), 'done' (hotovo). Restart tedy
    neparsuje znovu ani hotové, ani bezLLM soubory.
    """
    import docx_embed
    tasks = []
    n_scanned = 0
    for fp in sorted(data_dir.glob("*.docx")):
        id_zakona, rok, sbirka, cislo = docx_embed.filename_to_meta(fp.name)
        if not id_zakona:
            log.warning("Přeskočen (nelze parsovat název): %s", fp.name)
            continue
        prev = store.law_status(id_zakona)
        if prev in ("done", "no_llm"):
            continue
        try:
            units = docx_embed.extract_units(
                docx_embed.docx_to_texts(fp), overlap=0)
        except Exception as e:
            log.error("Parse chyba %s: %s", fp.name, e)
            continue
        n_scanned += 1
        citace = f"{cislo}/{rok} Sb." + (" m. s." if sbirka == "sm" else "")
        longs = [u for u in units if len(u["text"]) > threshold]
        if not longs:
            store.mark_scanned(id_zakona, citace, rok, sbirka, "no_llm")
            continue
        store.mark_scanned(id_zakona, citace, rok, sbirka, "todo")
        tasks.append((fp, id_zakona, citace, rok, sbirka, longs))
    log.info("Scan: %d souborů čerstvě nascarované, %d čeká na LLM",
             n_scanned, len(tasks))
    return tasks


def _is_list_like(text: str) -> bool:
    """Seznamy/přílohy (krátké řádky) nech na produkčním chunkingu — LLM tam
    nemá co hledat (žádný tématický posun → zacyklení)."""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 4:
        return False
    return len(text) / len(lines) < 120


def process_file(job: Tuple, corpus: CorpusChunker
                 ) -> Tuple[str, List[Tuple[str, str]]]:
    """Vlákno: LLM chunking všech dlouhých celků jednoho zákona."""
    fp, id_zakona, _citace, _rok, _sbirka, longs = job
    chunker = corpus.chunker()
    out: List[Tuple[str, str]] = []
    for u in longs:
        if _is_list_like(u["text"]):
            continue  # pokryto DOCX chunky z hlavní pipeline
        merged = _merge_tiny([c.text for c in chunker(u["text"])])
        n = len(merged)
        for i, t in enumerate(merged):
            out.append((f"{u['citace']} ({i + 1}/{n})", t))
    return id_zakona, out


def run_corpus(data_dir: Path, store: LLMStore, corpus: CorpusChunker,
               workers: int, limit_files: int) -> None:
    # práh výběru celků je fixní (3000 zn. ≈ jeden delší paragraf),
    # nezávislý na velikosti okna — menší okno = méně pasáží na volání
    threshold = 3000
    log.info("Scan korpusu %s (práh %d znaků)…", data_dir, threshold)
    tasks = scan_corpus(data_dir, store, threshold)
    if limit_files > 0:
        tasks = tasks[:limit_files]
        log.info("--limit-files: běží jen na %d souborech", limit_files)
    if not tasks:
        done, _, chunks = store.stats()
        log.info("Nic k LLM chunkingu (hotovo: %d zákonů, %d chunků)",
                 done, chunks)
        return

    total_calls_start = corpus.genie.calls
    t0 = time.monotonic()
    done_files, error_files = 0, 0
    total = len(tasks)
    log.info("Full run: %d zákonů, %d dlouhých celků, %d workers",
             total, sum(len(j[5]) for j in tasks), workers)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(process_file, job, corpus): job
                       for job in tasks}
            for fut in as_completed(futures):
                job = futures[fut]
                try:
                    id_zakona, chunks = fut.result()
                    store.save_done(id_zakona, chunks)
                    done_files += 1
                except Exception as e:
                    error_files += 1
                    log.error("Chyba u %s (zůstává 'todo' pro příště): %s",
                              job[1], e)
                if (done_files + error_files) % 5 == 0:
                    elapsed = time.monotonic() - t0
                    rate = (done_files + error_files) / elapsed * 60
                    eta = (total - done_files - error_files) / rate \
                        if rate else 0
                    calls = corpus.genie.calls - total_calls_start
                    log.info("Hotovo %d/%d (%.1f soub./min, ETA %.0f min) | "
                             "%d LLM volání, %d fallback",
                             done_files + error_files, total, rate, eta,
                             calls, corpus.genie.fallbacks)
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        log.warning("Přerušeno (Ctrl-C) — checkpoint je v DB, "
                    "další běh pokračuje.")

    done, todo, chunks = store.stats()
    log.info("Konec běhu: %d/%d souborů hotových, %d chyb | "
             "checkpoint: %d done / %d todo, %d LLM chunků",
             done_files, total, error_files, done, todo, chunks)


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM chunking zákonů (Chonkie SlumberChunker + qwen-3gpu)")
    parser.add_argument("vstup",
                        help="adresář s DOCX (full run) nebo jeden soubor "
                             "(náhled)")
    parser.add_argument("--mode", choices=["units", "whole"],
                        default="units",
                        help="jen pro jednotlivý soubor: units (§-celky + "
                             "LLM na dlouhé) nebo whole (celý text LLM)")
    parser.add_argument("--workers", type=int, default=12,
                        help="paralelní LLM volání (default 12; limit "
                             "proxy 60 RPM / 100k TPM)")
    parser.add_argument("--chunk-size", type=int, default=2000,
                        help="cílová délka chunku v znacích (default 2000)")
    parser.add_argument("--candidate-size", type=int, default=128,
                        help="velikost kandidáta pro LLM v znacích")
    parser.add_argument("--db", default=None,
                        help="cesta k checkpoint DB (default: "
                             "<data_dir>/.llmchunk.db)")
    parser.add_argument("--limit-files", type=int, default=0,
                        help="omezit full run na N souborů (test)")
    parser.add_argument("--fresh", action="store_true",
                        help="smaže checkpoint DB a začne od nuly")
    parser.add_argument("--out", default=None,
                        help="jen pro jeden soubor: výstupní JSON "
                             "([{citace, text}...])")
    args = parser.parse_args()

    env_path = load_env()
    base_url = os.environ.get("LLM_BASE_URL")
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if not (base_url and api_key and model):
        sys.exit("Chybí konfigurace: nastav LLM_BASE_URL, LLM_API_KEY a "
                 f"LLM_MODEL v .env ({env_path or 'worktree root'}) "
                 "nebo v env proměnných.")

    vstup = Path(args.vstup)
    if not vstup.exists():
        sys.exit(f"Cesta neexistuje: {vstup}")

    genie = SafeOpenAIGenie(model=model, base_url=base_url, api_key=api_key)
    corpus = CorpusChunker(genie, args.chunk_size, args.candidate_size)

    if vstup.is_file():
        # --- náhled nad jedním souborem (bez DB) ---
        log.info("LLM: %s @ %s | mode=%s, chunk_size=%d zn.",
                 model, base_url, args.mode, args.chunk_size)
        texts = read_input(vstup)
        if not texts:
            sys.exit("Vstup je prázdný.")
        t0 = time.monotonic()
        if args.mode == "whole":
            chunks = chunk_whole("\n".join(texts), corpus.chunker())
        else:
            import docx_embed
            threshold = int(args.chunk_size * 1.5)
            chunks = []
            for u in docx_embed.extract_units(texts, overlap=0):
                if len(u["text"]) <= threshold:
                    chunks.append({"citace": u["citace"],
                                   "text": u["text"]})
                    continue
                merged = _merge_tiny(
                    [c.text for c in corpus.chunker()(u["text"])])
                n = len(merged)
                for i, t in enumerate(merged):
                    chunks.append({"citace": f"{u['citace']} ({i + 1}/{n})",
                                   "text": t})
        elapsed = time.monotonic() - t0
        lens = [len(c["text"]) for c in chunks]
        log.info("Hotovo: %d chunků za %.1fs (průměr %d znaků)",
                 len(chunks), elapsed, sum(lens) // len(lens))
        log.info("LLM: %d volání (%d structured, %d fallback)",
                 genie.calls, genie.json_ok, genie.fallbacks)
        for i, c in enumerate(chunks):
            preview = re.sub(r"\s+", " ", c["text"])[:90]
            print(f"  [{i:>2}] {len(c['text']):>5} zn. | "
                  f"{c['citace']:<30} | {preview}…")
        if args.out:
            Path(args.out).write_text(
                json.dumps(chunks, ensure_ascii=False, indent=1),
                encoding="utf-8")
            log.info("Chunky uloženy: %s", args.out)
        return

    # --- full run nad adresářem ---
    if not vstup.is_dir():
        sys.exit(f"Nejde ani o soubor, ani o adresář: {vstup}")
    db_path = Path(args.db) if args.db else vstup / ".llmchunk.db"
    if args.fresh and db_path.exists():
        db_path.unlink()
        log.info("Checkpoint smazán (--fresh): %s", db_path)
    store = LLMStore(db_path)
    log.info("LLM: %s @ %s | chunk_size=%d zn., workers=%d, DB=%s",
             model, base_url, args.chunk_size, args.workers, db_path)
    run_corpus(vstup, store, corpus, args.workers, args.limit_files)


if __name__ == "__main__":
    main()
