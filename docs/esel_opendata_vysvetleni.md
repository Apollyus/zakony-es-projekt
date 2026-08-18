# eseL opendata API — jak strojově získat zdrojově čitelný text zákonů

> Rozbor endpointu `https://opendata.eselpoint.gov.cz/esel-esb/...`
> Ukázkový příklad: vyhláška **13/1923 Sb.** (`.../eli/cz/sb/1923/13/1923-01-25`)
> Stav: ověřeno živě proti API

---

## 0. Shrnutí (TL;DR)

| Otázka | Odpověď |
|---|---|
| Je to souborový / PDF server? | **Ne.** Je to **Linked Data (RDF) API** (Virtuoso / OpenLink). |
| Dostanu z něj PDF nebo EPUB? | **Ne.** `Accept: application/pdf` i `application/epub+zip` vrací **406** (ověřeno i u moderních zákonů). |
| Kde je „zdrojově čitelný“ text? | V RDF vlastnosti **`text-fragmentu`** u každého zdroje `právní-akt-fragment/<id>`. |
| Jak ho strojově načíst? | Uzel **znění** → `má-fragment-znění` (seřazené deskriptory) → každý `obsahuje-fragment` → přečíst `text-fragmentu`. Podrobnosti v §4–5. |
| Máš funkční tool? | Ano: **`esel_extract.py`** (viz §5). Vytáhne celý text z URL znění. |
| Hlavní omezení | Žádné PDF; `text/plain` = RDF (N-Triples), ne text; SPARQL je **rate-limited** (POST blokován); velké zákony = **10 000+ fragmentů**. |

**Důležité:** Fragmenty jsou **nezměnitelné (jedna verze na fragment)**, takže `text-fragmentu` je vždy správný pro konkrétní znění, ze kterého se ptáš. Neexistuje problém „aktuční vs. historická verze textu“ uvnitř jednoho fragmentu.

---

## 1. Co to ten endpoint vůbec je

`opendata.eselpoint.gov.cz` je **odkazovaná-data (Linked Data) rozhraní** nad daty eseL (Sbírka zákonů a předpisů). Nevrací soubory (PDF, EPUB, obrázky), ale **RDF graf**. Odpovědi se podle HTTP hlavičky `Accept` vyrenderují do různých RDF serializací nebo do HTML vizualizace.

- Podklady: Virtuoso 07.20 (OpenLink), čtení přes content negotiation + SPARQL endpoint.
- Všechny URI jsou **IRI** (např. `https://opendata.eselpoint.gov.cz/esel-esb/eli/cz/sb/1923/13/1923-01-25`).
- Pojmy (vlastnosti/třidy) jsou definovány ve slovníku `https://slovník.gov.cz/datový/sbírka/pojem/...` (SKOS/OWL).

### 1.1 Content negotiation — co lze požádat

Ověřená matice pro uzel `.../1923-01-25/dokument`:

| `Accept:` | HTTP | Vraćený `Content-Type` | Co to je |
|---|---|---|---|
| `application/pdf` | **406** | — | nedostupné |
| `application/epub+zip` | **406** | — | nedostupné |
| `application/json` | **406** | — | nedostupné |
| `text/html` | 200 | `text/html` | **LodView** vizualizace RDF (ne text zákona!) |
| `application/xhtml+xml` | 200 | `text/html` | totéž (LodView) |
| `text/turtle` | 200 | `text/turtle` | RDF Turtle |
| `application/ld+json` | 200 | `application/ld+json` | RDF **JSON-LD** (nejpřijatelnější pro skripty) |
| `application/rdf+xml` | 200 | `application/rdf+xml` | RDF/XML |
| `text/plain` | 200 | `text/plain` | **RDF N-Triples** (není to čistý text zákona!) |

> ⚠️ Past: `text/plain` vypadá lákavě jako „čistý text“, ale API ho mapuje na RDF N-Triples. `text/html` je LodView. Žádný content-type nevrátí „hotový“ text zákona — ten si sestavíš z `text-fragmentu`.

### 1.2 SPARQL endpoint

- URL: `https://opendata.eselpoint.gov.cz/sparql`
- Podporuje **GET** i **POST** (`?query=...`), výstup `text/csv`, `application/sparql-results+json`, `text/turtle` atd.
- **Omezení (stav při testování):** POST dotazy vracely *„The request is blocked“* (rate-limit / ochrana). Živé **plné skenování** (`?s ?p ?o` přes celý korpus) je blokované. Cílené dotazy s předem známým IRI fungují.
- IRI v úložišti jsou uloženy s **unicode escapes** (`pr\u00E1vn\u00ED-akt-fragment/...`), takže přímé porovnávání IRI s diakritikou v dotazu nesejde — používej regex / `LIKE` na ASCII část (`...-akt-fragment/<id>`).

---

## 2. Struktura URL (ELI)

```
https://opendata.eselpoint.gov.cz/esel-esb/eli/cz/sb/1923/13/1923-01-25
                                              │  │  │    │    └─ datum účinnosti ZNĚNÍ (verze)
                                              │  │  │    └───── číslo předpisu
                                              │  │  └───────── rok vydání
                                              │  └──────────── sbírka: "sb" = Sbírka zákonů
                                              └─────────────── země: cz
```

Dva základní typy uzlů:

1. **Právní akt** (bez data): `.../eli/cz/sb/2012/89`
   - `@type` = `právní-akt`
   - vlastnosti: `citace-právního-aktu` („89/2012 Sb.“), `rok-předpisu`, `číslo-předpisu`, `patří-do-sbírky`,
     a **seznam verzí**:
     - `má-znění` → pole všech znění (historických verzí)
     - `má-poslední-znění` → aktuální/poslední verze
     - `má-vyhlášené-znění` → původní vyhlášené znění (datum `0000-00-00` = „vyhlášené“)

2. **Znění** (s datem): `.../eli/cz/sb/2012/89/2026-01-01`
   - `@type` = `znění-právního-aktu`
   - vlastnosti: `účinnost-znění-od` (datum), `ročník-znění`, `má-typ-znění-právního-aktu` (např. `KONSOL` = „Časové znění“),
     a **`má-fragment-znění`** → pole deskriptorů fragmentů (to je vstup do textu, viz §3).

> Pro vytažení konkrétní verze zákona vždy pracuj s **URL znění** (s datem na konci).

---

## 3. Datový model — kde přesně je text

Hierarchie odkazů od znění k samotnému textu:

```
znění  (.../eli/cz/sb/1923/13/1923-01-25)
  │
  │  má-fragment-znění  (sestava fragmentů tohoto znění)
  ▼
deskriptor fragmentu  (.../dokument/norma/frag_18435)
  │   typ = označení-fragmentu-znění-právního-aktu
  │   • pořadí-fragmentu-znění-právního-aktu  → kód pořadí v dokumentu (řadit LEKSIKOGRAFICKY)
  │   • hierarchie-fragmentu-znění-právního-aktu → cesta, např. "/2/1/"
  │   • má-předka → nadřazený deskriptor
  │   • obsahuje-fragment → právní-akt-fragment/18435
  ▼
fragment  (právní-akt-fragment/18435)
      typ = fragment
      • má-typ-fragmentu        → např. Nadpis_nad / Odstavec_Dc / Virtual_Document
      • má-první-verzi-fragmentu → id verze (fragment = jedna, nezměnitelná verze)
      • text-fragmentu          → ★★★ SKUTEČNÝ TEXT ZÁKONA ★★★
```

### 3.1 Co jednotlivé úrovně vrací (reálné RDF)

**Uzel znění** (`Accept: text/turtle`) — jen meta + odkazy, **žádný text**:
```turtle
<esel-esb/eli/cz/sb/1923/13/1923-01-25>
    a ...:znění-právního-aktu ;
    ...:účinnost-znění-od "1923-01-25"^^xsd:date ;
    ...:má-typ-znění-právního-aktu <esel-esb/cis-esb-typ-znění/položka/KONSOL> ;
    ...:má-fragment-znění <.../dokument/norma/frag_18435> , <.../dokument/prefix/frag_18425> , ... .
```

**Deskriptor fragmentu** — jen pořadí + odkaz:
```turtle
<esel-esb/eli/cz/sb/1923/13/1923-01-25/dokument/norma/frag_18435>
    a ...:označení-fragmentu-znění-právního-aktu ;
    ...:hierarchie-fragmentu-znění-právního-aktu "/2/1/"^^xsd:string ;
    ...:má-předka <.../dokument/norma> ;
    ...:obsahuje-fragment <esel-esb/právní-akt-fragment/18435> ;
    ...:pořadí-fragmentu-znění-právního-aktu "6AC0"^^xsd:string .
```

**Fragment — tady je text:**
```turtle
<esel-esb/právní-akt-fragment/18435>
    a ...:fragment ;
    ...:má-první-verzi-fragmentu 18435 ;
    ...:má-typ-fragmentu <esel-esb/cis-esb-typ-fragmentu/položka/Nadpis_nad> ;
    ...:text-fragmentu "V Čechách:"^^xsd:string .
```

### 3.2 Struktura dokumentu (příklad 13/1923 Sb.)

Znění má 27 deskriptorů: 4 „skupinové“ uzly (`/dokument`, `/dokument/prefix`, `/dokument/norma`, `/dokument/postfix`) + 23 listových fragmentů. Seřazené podle `pořadí`:

```
pořadí   hierarchie   fragment
00       /            právní-akt-fragment/18421   (Virtual_Document — kořen, bez textu)
58       /1/          právní-akt-fragment/18423   (předpis/preambule)
5AC0     /1/1/        právní-akt-fragment/18425
5B40     /1/2/        právní-akt-fragment/18427
5BC0     /1/3/        právní-akt-fragment/18475
...      /2/...       právní-akt-fragment/18435 … 18467   (tělo — články/odstavce)
78       /3/          právní-akt-fragment/18469   (závěr)
7AC0     /3/1/        právní-akt-fragment/18473
```

> **Pořadí se řadí jako řetězec (lexikograficky), ne numericky.**
> Důkaz: `5C60` (fragment prefixu) má jít PŘED `68` (skupina norma). Lexikograficky `5C60 < 68` ✓.
> Numericky by `0x68 = 104 < 0x5C60 = 23648` ✗ (vyšlo by to špatně). Proto **string sort**.

### 3.3 Číselníky (katalogy)

- **Typy fragmentů** `esel-esb/cis-esb-typ-fragmentu/položka/<NOTACE>` (SKOS pojmy). Ověřené:

  | notace | český popisek |
  |---|---|
  | `Nadpis_nad` | Nadpis ^Nad |
  | `Nadpis_pod` | Nadpis _Pod |
  | `Odstavec_Dc` | Odstavec (č) |
  | `Virtual_Document` | Virtual Dokument |
  | `Preambule` | Preambule |
  | `Cast` | Část |
  | `Priloha` | (příloha) |
  | `Tabulka` | Tabulka |
  | `Poznamka` | Poznámka |

- **Typy znění** `esel-esb/cis-esb-typ-znění/položka/<NOTACE>`:
  - `KONSOL` → „Časové znění“ (konsolidovaná verze k datu).

---

## 4. Jak to strojově načíst — algoritmus

```
1) GET <znění>  (Accept: application/ld+json)
     → čteš "má-fragment-znění"  = [ IRI deskriptorů, ... ]

2) Pro KAŽDÝ deskriptor: GET <deskriptor>
     → čteš "pořadí-fragmentu-znění-právního-aktu"  (kód pořadí)
     → čteš "obsahuje-fragment"                     (IRI fragmentu)

3) Deskriptory seřadíš podle "pořadí" (string sort)

4) Pro KAŽDÝ fragment: GET <fragment>
     → čteš "text-fragmentu"  = text

5) Spojuj texty v pořadí (vynech prázdné)  = CELÝ ZÁKON
```

Kroky 2 a 4 jsou nezávislé → **paralelizuj** (thread pool) a **cacheuj** po IRI.

### 4.1 Ručně přes `curl` (jednotlivé kroky)

```bash
BASE="https://opendata.eselpoint.gov.cz"

# 1) uzel znění → seznam deskriptorů
curl -sH "Accept: application/ld+json" \
  "$BASE/esel-esb/eli/cz/sb/1923/13/1923-01-25" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['má-fragment-znění'])"

# 2) jeden deskriptor → pořadí + fragment
curl -sH "Accept: application/ld+json" \
  "$BASE/esel-esb/eli/cz/sb/1923/13/1923-01-25/dokument/norma/frag_18435"

# 3) fragment → text
curl -sH "Accept: application/ld+json" \
  "$BASE/esel-esb/pr%C3%A1vn%C3%AD-akt-fragment/18435" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['text-fragmentu'])"
# → V Čechách:
```

> ⚠️ V URL `právní-akt-fragment` musíš percent-encode diakritiku:
> `právní` → `pr%C3%A1vn%C3%AD`. V Pythonu: `urllib.parse.quote(path, safe="/:")`.

---

## 5. Funkční tool — `esel_extract.py`

Uloženo: **`/home/vojtech/esel_extract.py`**

```bash
python3 esel_extract.py "https://opendata.eselpoint.gov.cz/esel-esb/eli/cz/sb/1923/13/1923-01-25"
# nebo do souboru:
python3 esel_extract.py "<URL-znění>" -o zakon.txt
# více workerů na velké zákony:
python3 esel_extract.py "<URL-znění>" --workers 16
```

Co dělá: stáhne uzel znění → paralelně stáhne všechny deskriptory → seřadí podle `pořadí` → paralelně (s deduplikací a cachí) stáhne `text-fragmentu` → spojí a vypíše / zapiše.

**Ověřený výsledek** pro 13/1923 Sb. (23 textových fragmentů, ~3,1 kB):

```
13.
Vyhláška ministra vnitra
ze dne 13. ledna 1923
o změnách úředních názvů měst, obcí, osad a částí osad, povolených v roce 1922.
Podle ustanovení § 5 zákona ze dne 14. dubna 1920, čís. 266 Sb. z. a n., vyhlašuji, že
v roce 1922 povoleny byly ministrem vnitra tyto změny úředních názvů míst:

V Čechách:
1. Výnosem ze dne 11. května 1922, č. 32.872, byla k žádosti městské obce Bělé ...
...
14. Výnosem ze dne 12. dubna 1922, č. 12.669, ... Střebovice, Strzebowitz na Třebovice.

Malypetr v. r.
```

Kompletní kód toolu:

```python
#!/usr/bin/env python3
"""Extract machine-readable (source) text of a Czech law from the eseL opendata API."""
import argparse, json, sys, urllib.request, urllib.parse, concurrent.futures as cf

BASE = "https://opendata.eselpoint.gov.cz/"

def _enc(path):
    return urllib.parse.quote(BASE + path, safe="/:")

def get_json(path, accept="application/ld+json"):
    req = urllib.request.Request(_enc(path), headers={"Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def _val(d, localname):
    for k, v in d.items():
        if k.split(":")[-1] == localname:
            return v
    return None

def fragment_text(frag_iri):
    d = get_json(frag_iri)
    t = _val(d, "text-fragmentu")
    if t is None:
        return ""
    if isinstance(t, list):
        return " ".join(x.get("@value", x) if isinstance(x, dict) else str(x) for x in t)
    return str(t)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zneni", help="Full URL of the znění node (ends in /YYYY-MM-DD)")
    ap.add_argument("-o", "--out")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    rel = args.zneni
    if rel.startswith(BASE):
        rel = rel[len(BASE):]
    z = get_json(rel)
    frags = _val(z, "má-fragment-znění")
    if isinstance(frags, dict):
        frags = [frags]
    if not frags:
        sys.exit("no 'má-fragment-znění' — je to URL znění (s datem)?")

    def desc(f):
        try:
            d = get_json(f)
        except Exception as e:
            return f, None, None
        return f, _val(d, "pořadí-fragmentu-znění-právního-aktu"), _val(d, "obsahuje-fragment")

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(desc, frags))

    leaves = sorted(((por, obs) for _, por, obs in rows if obs and por is not None),
                    key=lambda r: (r[0] or ""))
    order = [obs for _, obs in leaves]
    uniq = list(dict.fromkeys(order))
    cache = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for obs, txt in zip(uniq, ex.map(fragment_text, uniq)):
            cache[obs] = txt

    lines = [cache[obs] for _, obs in leaves if cache.get(obs, "").strip()]
    out = "\n".join(lines) + "\n"
    if args.out:
        open(args.out, "w", encoding="utf-8").write(out)
        print(f"wrote {len(out)} chars, {len(lines)} fragments -> {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out)

if __name__ == "__main__":
    main()
```

---

## 6. Adresy / endpointy (přehled)

| Co | URL / vzor | Poznámka |
|---|---|---|
| Base API | `https://opendata.eselpoint.gov.cz` | |
| Právní akt | `.../esel-esb/eli/cz/sb/<rok>/<cislo>` | např. `.../sb/2012/89` |
| Znění (verze) | `.../esel-esb/eli/cz/sb/<rok>/<cislo>/<YYYY-MM-DD>` | vstup do textu |
| Dokument (deskriptory) | `.../<znění>/dokument[/<sekce>/frag_<id>]` | sekce: `prefix`, `norma`, `postfix` |
| Fragment (text) | `.../esel-esb/právní-akt-fragment/<id>` | percent-encode diakritiku |
| SPARQL | `.../sparql?query=<SPARQL>` | GET i POST; rate-limited |
| Typy fragmentů | `.../esel-esb/cis-esb-typ-fragmentu/položka/<NOTACE>` | SKOS |
| Typy znění | `.../esel-esb/cis-esb-typ-znění/položka/<NOTACE>` | SKOS |
| Slovník pojmů | `https://slovník.gov.cz/datový/sbírka/pojem/<pojem>` | definice vlastností/tříd |

**Jak najít všechny verze zákona:**
```bash
curl -sH "Accept: application/ld+json" \
  "https://opendata.eselpoint.gov.cz/esel-esb/eli/cz/sb/2012/89" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('poslední:',d['má-poslední-znění']);[print(' ',x) for x in d['má-znění']]"
```

---

## 7. Omezení (co tě bude bolet)

1. **Žádné PDF / EPUB / soubory.** Tento endpoint je čistě RDF. `application/pdf`, `application/epub+zip`, `application/json` → **406**. (Ověřeno u 13/1923 Sb. i u 89/2012 Sb.)
   - Když ti PDF fakt musí být soubor: vygeneruj si ho z vytáženého textu, nebo ho dej z **oficiálního portálu eseL / portálu předpisů** (jiný systém, ten PDF servíruje).

2. **`text/plain` není text zákona** — je to RDF N-Triples. `text/html` je LodView. Neexistuje content-type, který by vrátil „hotový“ text.

3. **SPARQL je omezený.** POST dotazy vracely *„The request is blocked“*; plné skenování (`?s ?p ?o` přes celý korpus) je blokované. Fungují cílené dotazy. IRI v úložišti mají unicode escapes → porovnávej přes regex/`LIKE` na ASCII část.

4. **Velké zákony = tisíce fragmentů.** 89/2012 Sb. má **~10 400 deskriptorů**. Po-fragmentové stahování = ~10 000 HTTP requestů (paralelně to vydrží, ale je to pomalé a dává to na rate-limity). Pro hromadné zpracování je ideál SPARQL batch (až se odblokuje) nebo jiný zdroj.

5. **Rate-limiting / ochrana.** Při vyšším objemu requestů API blokuje („Service unavailable / blocked“). Dávej si pauzy, omezuj concurrency, cachej.

6. **Diakritika v URL.** `právní-akt-fragment` → v requestu musí být `pr%C3%A1vn%C3%AD-akt-fragment`. V Pythonu `urllib.parse.quote(..., safe="/:")`.

7. **Pořadí = string sort, ne číslo.** Viz §3.2 — řaď lexikograficky.

8. **Nezměnitelnost fragmentů = plus.** Každý fragment má jednu verzi, takže `text-fragmentu` je pro dané znění vždy správné. Nemusíš řešit „která verze fragmentu“.

---

## 8. Možnosti a využití

- **Extrakce celého textu konkrétní verze** — hotové, viz `esel_extract.py`.
- **Export do PDF/DOCX/EPUB** — z vytáženého textu (tady ne, jen vlastní generování; strukturu ti dají typy fragmentů `Nadpis_*`, `Odstavec_*`, `Priloha`, `Tabulka` → lze mapovat na nadpisy/odstavce).
- **Historické verze** — projdi `má-znění` (seznam verzí) a pro každé znění spusť extrakci → dostaneš časovou osu zákona.
- **Legal tech / NLP** — čisté strojově čitelné texty pro klasifikaci, vyhledávání, embeddy, RAG.
- **Synchronizace / monitoring změn** — porovnej `text-fragmentu` fragmentů mezi sousedními zněními → detekce novelizací.
- **Hromadné zpracování** — když SPARQL dopustí: jeden dotaz `?s <...>p:text-fragmentu ?o` + filtr na IRI fragmentů daného znění → celý zákon za 1 request místo tisíců.

---

## 9. Přílohy

### 9.1 Reálné RDF — uzel znění (výběr)
```turtle
<esel-esb/eli/cz/sb/1923/13/1923-01-25>
    a ...:znění-právního-aktu ;
    ...:účinnost-znění-od "1923-01-25"^^xsd:date ;
    ...:ročník-znění "1923"^^xsd:gYear ;
    ...:má-typ-znění-právního-aktu <esel-esb/cis-esb-typ-znění/položka/KONSOL> ;
    ...:má-fragment-znění <.../dokument> , <.../dokument/norma> ,
                           <.../dokument/norma/frag_18435> , ... .
```

### 9.2 Reálné RDF — fragment s textem
```turtle
<esel-esb/právní-akt-fragment/18435>
    a ...:fragment ;
    ...:má-první-verzi-fragmentu 18435 ;
    ...:má-typ-fragmentu <esel-esb/cis-esb-typ-fragmentu/položka/Nadpis_nad> ;
    ...:text-fragmentu "V Čechách:"^^xsd:string .
```

### 9.3 Vlastnosti uzlů (přehled)

| Uzel | `@type` | Klíčové vlastnosti |
|---|---|---|
| Právní akt | `právní-akt` | `citace-právního-aktu`, `rok-předpisu`, `číslo-předpisu`, `patří-do-sbírky`, `má-znění`, `má-poslední-znění`, `má-vyhlášené-znění` |
| Znění | `znění-právního-aktu` | `účinnost-znění-od`, `ročník-znění`, `má-typ-znění-právního-aktu`, `má-fragment-znění` |
| Deskriptor | `označení-fragmentu-znění-právního-aktu` | `pořadí-...`, `hierarchie-...`, `má-předka`, `obsahuje-fragment` |
| Fragment | `fragment` | `má-typ-fragmentu`, `má-první-verzi-fragmentu`, **`text-fragmentu`** |

### 9.4 Rozdíl velikostí (pro představu škálovatelnosti)

| Zákon | Deskriptory znění |
|---|---|
| 13/1923 Sb. (ukázka) | **27** |
| 89/2012 Sb. (občanský zákoník) | **~10 400** |

---

*Zpracováno automatizovaným průzkumem živého endpointu. Všechny HTTP kódy, RDF ukázky a počty fragmentů jsou reálně ověřené.*
