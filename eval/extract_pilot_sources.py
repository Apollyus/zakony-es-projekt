"""
Extrakce zdrojových dat pro pilot v2 ze souborů e-Sbírky (001/003/004 *.json.gz v kořeni repa).

1) 001 -> všechna znění (verze) zákonů            eval/data/pilot/zneni_all.jsonl
2) výběr univerza + aktuální znění + status       eval/data/pilot/universe.json
3) 003 -> strom fragmentů aktuálních znění         eval/data/pilot/frag003.jsonl
4) 004 -> texty a typy těchto fragmentů            eval/data/pilot/frag004.jsonl

Status: 'platny' = existuje konsolidované znění (KONSOL) účinné k --date,
'zruseny' = všechna KONSOL znění skončila, 'neznamy' = jen vyhlášené znění (VYHLZNE).

Použití:  .venv/bin/python eval/extract_pilot_sources.py [--random 400] [--date 2026-10-18]
"""

import argparse
import gzip
import json
import random
from collections import defaultdict
from datetime import date
from pathlib import Path

import ijson

ROOT = Path(__file__).resolve().parent.parent
PILOT_DIR = ROOT / "eval" / "data" / "pilot"
DATASETS = [ROOT / "eval" / "data" / "manual.jsonl", ROOT / "eval" / "data" / "synthetic.jsonl"]
# zrušení předchůdci a "úplná znění" – záměrně v univerzu jako distraktory
PREDECESSORS = {
    "40/1964 Sb.", "65/1965 Sb.", "140/1961 Sb.", "71/1967 Sb.", "141/1961 Sb.", "109/1965 Sb.", "32/1970 Sb.",
    "126/1994 Sb.", "85/2001 Sb.", "52/1989 Sb.", "70/1983 Sb.", "47/1992 Sb.", "28/1960 Sb.", "183/1960 Sb.",
    "7/2004 Sb.", "56/1981 Sb.", "38/1981 Sb.", "99/1963 Sb.", "94/1963 Sb.", "102/1963 Sb.",
}


def items(fn: str):
    with gzip.open(ROOT / fn, "rb") as f:
        yield from ijson.items(f, "položky.item")


def extract_versions() -> list:
    rows = []
    for it in items("001PravniAktZneni.json.gz"):
        rows.append({
            "akt_citace": it.get("akt-citace"), "akt_iri": it.get("akt-iri"), "nazev": it.get("akt-název-vyhlášený"),
            "doc_id": it.get("znění-dokument-id"), "iri": it.get("iri"),
            "od": it.get("znění-datum-účinnosti-od"), "do": it.get("znění-datum-účinnosti-do"),
            "zrusen": it.get("znění-je-zrušen"), "nikdy": it.get("znění-nikdy-nebyl-účinný"),
            "typ_zneni": it.get("cis-esb-typ-znění-položka"), "n_frag": len(it.get("právní-akt-znění-fragment") or []),
        })
    with open(PILOT_DIR / "zneni_all.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"001: {len(rows)} znění")
    return rows


def select_universe(rows: list, n_random: int, seed: int, today: str) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["akt_citace"]].append(r)
    gold = {g["akt_citace"] for p in DATASETS if p.exists()
            for line in p.open(encoding="utf-8") for g in json.loads(line)["gold"]}
    rand = set(random.Random(seed).sample(sorted(by), n_random))
    universe = sorted((gold | PREDECESSORS | rand) & set(by))

    current, status = {}, {}
    for c in universe:
        kon = [v for v in by[c] if v["typ_zneni"] == "KONSOL"]
        if kon:
            live = [v for v in kon if v["od"] and v["od"] <= today and (v["do"] is None or v["do"] >= today)]
            if live:
                current[c], status[c] = max(live, key=lambda v: v["od"]), "platny"
            else:
                status[c] = "zruseny"
        else:
            current[c], status[c] = max(by[c], key=lambda v: v["od"] or ""), "neznamy"
    u = {"universe": universe, "gold": sorted(gold), "status": status, "date": today,
         "current": {c: {k: v[k] for k in ("doc_id", "iri", "nazev", "od", "do")} for c, v in current.items()}}
    json.dump(u, open(PILOT_DIR / "universe.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"univerzum: {len(universe)} zákonů, aktuální znění: {len(current)}")
    return u


def extract_fragments(u: dict):
    want = {v["doc_id"]: c for c, v in u["current"].items()}
    need = set()
    with open(PILOT_DIR / "frag003.jsonl", "w", encoding="utf-8") as out:
        for it in items("003PravniAktZneniFragment.json.gz"):
            c = want.get(it.get("znění-dokument-id"))
            if c is None:
                continue
            fid = (it.get("právní-akt-fragment") or {}).get("fragment-id")
            need.add(fid)
            out.write(json.dumps({
                "akt_citace": c, "iri": it["iri"], "parent": (it.get("znění-fragment-předek") or {}).get("iri"),
                "oznaceni": it.get("znění-fragment-označení-uzlu-text") or it.get("znění-fragment-označení-uzlu"),
                "citace_text": it.get("znění-fragment-citace-text"), "hier": it.get("znění-fragment-hierarchie"),
                "hex": it.get("znění-fragment-hierarchie-hex"), "frag_id": fid,
            }, ensure_ascii=False) + "\n")
    print(f"003: {len(need)} fragmentů")
    n = 0
    with open(PILOT_DIR / "frag004.jsonl", "w", encoding="utf-8") as out:
        for it in items("004PravniAktFragment.json.gz"):
            if it.get("fragment-id") in need:
                n += 1
                out.write(json.dumps({"frag_id": it["fragment-id"], "typ": it.get("cis-esb-typ-fragmentu-položka"),
                                      "text": it.get("fragment-text")}, ensure_ascii=False) + "\n")
    print(f"004: {n} textů")


def main():
    ap = argparse.ArgumentParser(description="Extrakce zdrojů pro pilot index v2")
    ap.add_argument("--random", type=int, default=400, help="počet náhodných distraktorů")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--date", default=date.today().isoformat(), help="datum, ke kterému se hledá účinné znění")
    args = ap.parse_args()
    PILOT_DIR.mkdir(parents=True, exist_ok=True)
    u = select_universe(extract_versions(), args.random, args.seed, args.date)
    extract_fragments(u)


if __name__ == "__main__":
    main()
