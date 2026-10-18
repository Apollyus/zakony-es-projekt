# Index v2 (`zakony-v2`) – jeden dokument = jeden §

Nový návrh indexu, který nahrazuje `zakony-esbirka-2026` (v1). Vznikl na základě RAGAS evaluace
(`eval/`), která ukázala, že v1 v realistických dotazech skoro nenachází správná ustanovení.

## Proč v1 nefungoval

| Problém v1 | Dopad | Řešení ve v2 |
|---|---|---|
| Text je rozsekaný na fragmenty (odstavec, písmeno) **bez čísla § a bez nadpisu**; „§ 140“ i nadpis „Vražda“ jsou samostatné prázdné fragmenty | Žádný kus textu neobsahuje otázku i odpověď; 25–45 % výsledků jsou holé nadpisy | Jednotka = celý § / článek s hlavičkou `předpis \| ČÁST › HLAVA › Díl \| § 140 Vražda` |
| V indexu jsou **všechna znění i zrušené předpisy** (zákoníky z 60. let, přetisky „Úplné znění …“) | Zastaralé právo vytlačuje platné; vrací se neplatný text (zkušební doba 3 měsíce místo dnešních 4) | Jen znění účinné k datu buildu; zrušené předpisy a přetisky se vynechají |
| Embedding `paraphrase-multilingual-mpnet-base-v2` ořezává text na **128 tokenů** | Delší ustanovení nemají smysluplný vektor | `BAAI/bge-m3` (1024 dim, 512 tokenů na chunk) |
| MCP `search_laws_semantic` deduplikoval podle `citace`, která je u fragmentů prázdná | Z každého zákona zbyl 1 text + nadpisy `§ N` | Opraveno (deduplikace podle citace i textu); ve v2 se neřeší |
| BM25 boduje shodu tázacích slov s krátkými názvy předpisů („jaký“ ~ „jakým“) | Na otázku o vraždě vyhraje vyhláška o bažantnicích | BM25 přes nadpis^3, cestu^1,5, text, název jen ^0,5; hybrid přes RRF |

Kolik který bod přispěl, není změřeno zvlášť (ve v2 se změnilo vše najednou) – viz „Další kroky“.

## Výsledky pilotu (447 předpisů, stejné univerzum pro v1 i v2)

Ruční sada 15 realistických otázek, top 5:

| | správné ustanovení | MRR | správný předpis | RAGAS context precision | RAGAS context recall |
|---|---|---|---|---|---|
| v1 hybrid (dnešní MCP) | 13 % | 0,06 | 27 % | 0,06 | 0,07 |
| v2 kNN | 93 % | 0,85 | 100 % | – | – |
| v2 RRF + reranker | 93 % | 0,80 | 100 % | 0,82 | 0,93 |

Syntetická sada (15 zodpověditelných otázek): nejlepší v1 53 %, v2 hybrid/rerank 93 %.
Výhrady: malé univerzum (447 místo ~30 tis. předpisů), 30 otázek, část úprav v2 (filtr přetisků,
oprava reference u zkušební doby) proběhla po prohlédnutí výsledků → čísla na plném indexu budou nižší.
Detail: `eval/README.md`, výsledky `eval/results/`.

## Schéma dokumentu

```json
{
  "akt_citace": "40/2009 Sb.",                    // keyword
  "id_zakona": "esel-esb:eli/cz/sb/2009/40",      // keyword (IRI předpisu)
  "rok": 2009,
  "akt_nazev": "Zákon trestní zákoník",           // text, czech
  "typ_predpisu": "zakon",                        // ustavni_zakon|zakon|narizeni|vyhlaska|sdeleni|...
  "status": "platny",                             // platny | neznamy (jen vyhlášené znění)
  "zneni_iri": "esel-esb:eli/cz/sb/2009/40/2026-01-01",
  "ucinnost_od": "2026-01-01",
  "oznaceni": "§ 140",                            // keyword; prázdné u textu mimo § (přílohy, sdělení)
  "nadpis": "Vražda",                             // text, czech
  "cesta": "ČÁST DRUHÁ ZVLÁŠTNÍ ČÁST › HLAVA I … › Díl 1 Trestné činy proti životu",
  "poradi": 1234,                                 // pořadí chunku v předpisu
  "chunk_no": 0,                                  // dlouhé § se dělí po odstavcích (max 1800 znaků)
  "text": "(1) Kdo jiného úmyslně usmrtí …",
  "search_text": "hlavička + text (to se embeduje; neindexuje se, není v _source – viz search_text_of)",
  "vektor": "dense_vector 1024, int8_hnsw, cosine"  // není v _source (šetří disk)
}
```

`_id` = IRI § v daném znění + `#chunk`, např. `esel-esb:eli/cz/sb/2009/40/2026-01-01/dokument/norma/cast_2/hlava_1/dil_1/par_140#0`.

## Jak se určuje aktuální znění

Ze `001PravniAktZneni` se pro každý předpis vezme **konsolidované znění (`KONSOL`) účinné k `--date`**
(`účinnost-od ≤ datum ≤ účinnost-do`). Příznak `znění-je-zrušen` je nespolehlivý (např. 40/1964 Sb. ho má
`false`, i když poslední znění skončilo 2013), proto se rozhoduje podle dat:

- existuje účinné KONSOL znění → `platny`,
- všechna KONSOL znění skončila → **zrušený, neindexuje se** (15 399 předpisů),
- předpis nemá žádné KONSOL znění (hlavně sdělení, mezinárodní smlouvy) → poslední vyhlášené znění, `neznamy`,
- název začíná „Úplné znění …“ → historický přetisk, neindexuje se (324).

Strom fragmentů se staví z `003PravniAktZneniFragment` (rodič, pořadí `hierarchie-hex`, IRI s cestou
`/dokument/norma/cast_2/hlava_1/dil_1/par_140/odst_1`) a texty/typy z `004PravniAktFragment`
(`Paragraf`, `Clanek`, `Nadpis_pod`, `Odstavec_Dc`, `Pismeno_Lb`, …). Logika je v `scripts/v2_common.py`.

## Build (přes noc)

```bash
bash scripts/run_v2_overnight.sh                          # priority 0–5, embedding max 10 h
HOURS=7 MAX_PRIORITY=3 bash scripts/run_v2_overnight.sh   # jen platné předpisy
MAX_PRIORITY=9 bash scripts/run_v2_overnight.sh           # i mezinárodní smlouvy (Sb. m. s.)
```

Skript postupně spustí `scripts/build_v2_index.py`:

| fáze | co dělá | čas | výstup |
|---|---|---|---|
| `extract` | 001/003/004 → SQLite | ~5 min | `data/v2/v2.db` (laws, frag, text) |
| `units` | § jednotky | ~10 min | tabulka `units` |
| `embed` | bge-m3 fp16 lokálně na MPS → vektor do SQLite → průběžně do ES | hodiny | tabulka `vectors`, index `zakony-v2` |
| `push` | dorovná do ES, co se během výpadku neodeslalo | minuty | index `zakony-v2` |
| `backup` | kopie celé DB včetně vektorů | ~1 min | `data/v2/backups/v2-YYYYMMDD.db` |
| eval | srovnání v1 vs v2 bez LLM | ~5 min | `eval/results/full-v1-vs-v2-*` |

Jak embedding probíhá: model běží **lokálně na Macu** (GPU, MPS). Po dávkách (desítky chunků): načíst ze SQLite →
zembedovat → **uložit vektor do SQLite** (`vectors`, fp16, 2 KB/chunk, celkem ~0,6 GB) → poslat dávku do ES →
označit `pushed`. Nic se nečeká „až na konec“, v ES přibývá průběžně.

**Zálohy a obnova:**
- vektory jsou v `data/v2/v2.db` (tabulka `vectors`) – nezávisle na ES serveru,
- na konci nočního běhu se udělá `backup` celé DB do `data/v2/backups/` (poslední 2; `BACKUP_DIR=` pro externí disk),
- **ztracený/poškozený index:** `build_v2_index.py push --recreate` (nebo `push`, když index neexistuje) –
  naplní ES z lokálních vektorů bez embeddingu (~170 dok./s, celý v2 ~30 min); ověřeno smazáním testovacího indexu,
- **nový server:** `ES_REMOTE_URL=… build_v2_index.py push --all`,
- **ztracená DB:** zkopírovat zálohu na `data/v2/v2.db`; texty/§ jednotky jdou i znovu vyrobit z `*.json.gz` (~4 min).

Proč je to tak malé oproti v1 (111 GB): v1 má 7,26 mil. vektorů uložených jako JSON text (~16,8 KB/vektor),
v2 ~304 tis. vektorů binárně ve fp16 (2 KB) – 24× méně vektorů (jen aktuální znění, odstavce sloučené do §)
a 8× menší vektor.

Vlastnosti nočního běhu:
- **obnovitelný** – stav je v SQLite (`units.embedded`), po pádu/Ctrl+C stačí skript spustit znovu,
- **priority** – nejdřív platné zákony (0), nařízení (1), vyhlášky (2), ostatní platné (3), pak `neznamy` (4–5)
  a nakonec mezinárodní smlouvy `Sb. m. s.` (8). Index je použitelný už po dokončení priorit 0–3,
- **paměť GPU** – dávky podle odhadu tokenů; při nedostatku paměti se dávka půlí, nakonec CPU,
- **výpadek ES/VPN** nezastaví embedding – vektory se ukládají lokálně, odeslání se zkouší každých 5 min,
  na konci se čeká na ES ještě aspoň 30 min, zbytek dorovná `push`,
- bulk po 200 dokumentech, `refresh_interval=-1` během buildu,
- `caffeinate` drží Mac vzhůru (na síťovém napájení); **víko nechat otevřené**, jinak se MacBook uspí.

Průběh: `tail -f logs/build_v2.log`, stav: `.venv/bin/python scripts/build_v2_index.py stats`.

Rychlost na M-series (24 GB): fp16 ~35 chunků/s na typickém chunku (fp32 ~13/s), dlouhé chunky pomaleji.

## Zdroje serveru (192.168.27.60)

| | hodnota |
|---|---|
| RAM | 4,1 GB celkem, heap 2,15 GB → ~2 GB page cache |
| disk | 103 GB, volných ~47 GB |
| v1 `zakony-esbirka-2026` | 24,2 GB disk, ~5 GB int8 vektorů (nevejde se do RAM) |
| v2 bez smluv (priority 0–5, ~304 tis. chunků) | odhad ~2–2,5 GB disk, ~0,35 GB „horkých“ dat (int8 vektory + HNSW graf) |
| v2 se smlouvami (~390 tis.) | odhad ~3 GB disk, ~0,45 GB |

Naměřeno na testovacím indexu: ~6,5 KB na dokument (vektor 5,2 KB = float32 + int8 + graf; `_source` bez vektoru
a `search_text` skoro nic). **Oba indexy současně se do RAM nevejdou** – v2 se dá postavit vedle v1 (nečinný
index RAM nezabírá), ale po ověření v2 je potřeba v1 smazat (a pilot `zakony-v2-pilot`, 0,3 GB).

## Přepnutí MCP serveru

MCP server rozhoduje o schématu podle názvu indexu (`mcp-server/modules/ESZakony/main.py`):

```bash
ES_INDEX=zakony-v2 python main.py      # v2: BM25 / RRF hybrid, bge-m3
python main.py                          # v1 (výchozí, zakony-esbirka-2026)
```

Rozhraní nástrojů je stejné (`search_laws_text`, `search_laws_semantic`, `get_law_detail`), výsledky jsou
seskupené podle předpisu; `citace` je ve tvaru „§ 140 Vražda“ a přibylo pole `cesta`. `get_law_detail`
ve v2 přijímá IRI i citaci („40/2009 Sb.“). Model bge-m3 (~2,3 GB) se stáhne při prvním dotazu.

## Deník potíží (říjen 2026)

Co se při evaluaci a stavbě v2 pokazilo a jak se to vyřešilo – ať je vidět, kde se v projektu ztrácel čas.

| Potíž | Projev | Řešení |
|---|---|---|
| Delegace na OpenCode (`etikos/qwen-3gpu`) | Z 11 běhů skončily čistě 2, 1 napsal kód a vypršel, 6 vypršelo (3 bez jediné akce), 2 spadly na kolizi `mktemp`; delegate skript nejde pouštět paralelně a ořezává textový výstup | Průzkum přes reporty zapisované do souboru, implementace ručně |
| LLM soudce Qwen3.5-35B-A3B za VPN | ~20–30 s na volání (přemýšlí do `reasoning_content`), proxy prakticky sekvenční; odmítá `chat_template_kwargs` | RAGAS jen na vzorku, rychlé deterministické metriky na všem; `content` je čistý JSON, takže parsování funguje |
| Gold podle čísla § nefungoval | Fragmenty ve v1 mají prázdnou `citace` | Shoda podle zákona + kotvy (fráze z textu) |
| Ruční gold byl zastaralý | Zkušební doba „3 měsíce“ z v1 – platné znění má 4 | Kotvy ověřovat proti aktuálnímu znění |
| `znění-je-zrušen` nespolehlivé | 40/1964 Sb. má `false`, ačkoli skončil 2013; vyhlášené znění (`VYHLZNE`) nemá konec účinnosti | Status podle dat účinnosti KONSOL znění |
| Přetisky „Úplné znění …“ | Staré zákoníky práce vytlačovaly platný | Vyřazeny z indexu |
| MPS OOM při embeddingu | Pád po ~54 % i po 72 % pilotu | fp16, dávky podle tokenů, půlení při OOM, CPU fallback, obnovitelnost |
| Reranker na MPS | 18/35 dotazů spadlo na paměť | Reranker na CPU (~9 s/dotaz), v produkci zatím ne |
| `_source` s vektory | 75 % velikosti pilotu | Vektory i `search_text` mimo `_source` |
| Málo RAM na ES serveru | v1 + v2 se nevejdou | Menší v2, smlouvy volitelné, v1 po ověření smazat |
| Žádná záloha vektorů v2 | ztráta indexu = hodiny embeddingu znovu | vektory lokálně v SQLite (fp16), fáze `push` a `backup` |
| `push --recreate` po smazání indexu zvenku poslal 0 dokumentů | DB si myslela, že je vše odesláno (fake test to nezachytil, reálný test ano) | když index neexistuje, posílá se vše; varování při nesouladu počtů |

## Další kroky

- Ablace: zjistit, kolik dělá jednotka §, filtr znění a model (pilot s mpnet; pilot bez filtru).
- Větší evaluační sada (ideálně otázky od uživatelů), oddělená od ladění.
- Reranker (`bge-reranker-v2-m3`) zlepšil MRR jen málo a na CPU stojí ~9 s/dotaz → zatím jen v eval.
- Pravidelný rebuild (nová znění) – stačí znovu spustit build s novým `--date` do nového indexu a přepnout alias.
- Historická znění: samostatný index, pokud bude potřeba hledat „jak to bylo k datu X“.
