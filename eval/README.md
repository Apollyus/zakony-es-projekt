# Evaluace vyhledávání v zákonech (RAGAS)

Měří, jak dobře vyhledávání najde správný text zákona. Volá **skutečné** funkce MCP serveru
(`mcp-server/modules/ESZakony`) a varianty nad indexem v2. LLM soudce je Qwen na interním proxy za VPN.
Návrh indexu v2 a důvody: [`../docs/INDEX_V2.md`](../docs/INDEX_V2.md).

## Nastavení

`.env` v kořeni repa (je v `.gitignore`):

```
ES_REMOTE_URL=http://192.168.27.60:9200
EVAL_LLM_BASE_URL=http://192.168.26.215:4000/v1
EVAL_LLM_API_KEY=...
EVAL_LLM_MODEL=Qwen3.5-35B-A3B
```

Potřebuje VPN (ES i LLM). Závislosti navíc k `requirements.txt`:
`ragas`, `langchain-openai`, `langchain-huggingface`, `python-dotenv`, `pandas`, `tabulate`.

## Soubory

| Soubor | Co dělá |
|---|---|
| `config.py` | načte `.env`, továrny `get_chat_llm()`, `get_eval_llm()`, `get_eval_embeddings()` |
| `retrievers.py` | `retrieve(name, query, k)` – všechny varianty vyhledávání (tabulka níže) |
| `build_dataset.py` | syntetická golden sada: náhodné fragmenty z ES → Qwen napíše otázku + odpověď |
| `data/manual.jsonl` | 15 ručních otázek, gold kotvy ověřené proti ES (platné znění k 18. 10. 2026) |
| `data/manual_old.jsonl` | 3 kontrolní otázky na staré, ale platné zákony (99/1963, 586/1992, 2/1969) – hlídají, že filtr starých předpisů nepoškodí platné právo |
| `data/synthetic.jsonl` | 20 vygenerovaných otázek |
| `run_eval.py` | spustí vyhledávání (+ generování) a spočítá metriky → `results/<čas>/` |
| `extract_pilot_sources.py` | extrakce zdrojů pro pilot (001/003/004 → `data/pilot/`) |
| `build_v2_pilot.py` | pilot index `zakony-v2-pilot` (§ jednotky, bge-m3) |

## Retrievery

| Název | Index | Popis |
|---|---|---|
| `bm25`, `hybrid` | podle `ES_INDEX` | přímo MCP `search_laws_text` / `search_laws_semantic`; bez `ES_INDEX` v1, s `ES_INDEX=zakony-v2` měří **skutečné nástroje MCP nad v2** (tam je `hybrid` = čisté kNN + filtr starých předpisů) |
| `knn` | v1 | čisté kNN nad `paragrafy.vektor` |
| `v1-bm25-u`, `v1-hybrid-u`, `v1-knn-u` | v1 | stejné dotazy jako MCP, ale omezené na pilotní univerzum (férové srovnání s pilotem) |
| `v2-bm25` | v2 | multi_match nadpis^3, cesta^1,5, název^0,5, text |
| `v2-knn` | v2 | kNN bge-m3 |
| `v2-hybrid` | v2 | RRF z top 50 BM25 + top 50 kNN |
| `v2-rerank` | v2 | hybrid top 30 → `bge-reranker-v2-m3` (CPU, ~9 s/dotaz) |
| `v2-knn-f1` … `v2-knn-f5`, `v2-knn-platny` | v2 | kNN top 50 přeřazené filtrem starých předpisů: f1 = jen vyřadit přetisky, f2/f3 = + `neznamy` ×0,97/×0,94, **f4 = + před 1945 ×0,94 (v MCP)**, f5 = ×0,90, `platny` = vyřadit `neznamy` |

Index pro retrievery `v2-*` se volí proměnnou `V2_INDEX` – **výchozí je pilot `zakony-v2-pilot`**, pro plný index
vždy `V2_INDEX=zakony-v2`.

## Spuštění

```bash
# rychle, bez LLM: jen deterministické metriky (sekundy až minuty)
.venv/bin/python eval/run_eval.py --no-ragas

# pilot: v1 vs v2 na stejném univerzu
.venv/bin/python eval/run_eval.py --no-ragas \
  --retrievers v1-bm25-u,v1-hybrid-u,v1-knn-u,v2-bm25,v2-knn,v2-hybrid,v2-rerank

# plný index v2 vs plný v1
V2_INDEX=zakony-v2 .venv/bin/python eval/run_eval.py --no-ragas --retrievers bm25,hybrid,knn,v2-knn,v2-hybrid

# skutečné nástroje MCP nad v2 (to, co dostane LLM klient)
ES_INDEX=zakony-v2 .venv/bin/python eval/run_eval.py --no-ragas --retrievers bm25,hybrid

# varianty filtru starých předpisů
V2_INDEX=zakony-v2 .venv/bin/python eval/run_eval.py --no-ragas \
  --retrievers v2-knn,v2-knn-f1,v2-knn-f2,v2-knn-f3,v2-knn-f4,v2-knn-f5,v2-knn-platny

# RAGAS (LLM soudce) – pomalé, Qwen ~20–30 s na volání, proxy prakticky sekvenční
.venv/bin/python eval/run_eval.py --retrievers v1-hybrid-u,v2-rerank --datasets eval/data/manual.jsonl

# end-to-end RAG (Qwen odpovídá z kontextů)
.venv/bin/python eval/run_eval.py --retrievers v2-hybrid --with-generation --limit 5

# nová syntetická sada
.venv/bin/python eval/build_dataset.py --n 40 --seed 42
```

## Formát golden sady

```json
{"id": "man-0002", "user_input": "Jaký trest hrozí za vraždu?",
 "reference": "Kdo jiného úmyslně usmrtí, bude potrestán ...",
 "gold": [{"akt_citace": "40/2009 Sb.", "anchor": "Kdo jiného úmyslně usmrtí, bude potrestán odnětím svobody na deset až osmnáct let"}],
 "reference_contexts": ["..."], "source": "manual"}
```

Shoda s gold se určuje **zákonem + kotvou (frází z textu)**, ne číslem §: ve v1 mají fragmenty prázdnou
`citace` a „§ N“ je samostatný fragment. Nalezený kontext je správný, když sedí `akt_citace` a jeho text
obsahuje `anchor` (po odstranění tagů, normalizaci mezer a velikosti písmen). Kotva musí odpovídat
**platnému znění** – např. zkušební doba je od novely 2025 4 měsíce, ne 3.

## Metriky

| Metrika | Typ | Význam |
|---|---|---|
| `hit` | deterministická | gold fragment je v top-k (Hit@k) |
| `mrr` | deterministická | 1 / pořadí prvního gold fragmentu |
| `law_hit`, `law_mrr` | deterministická | v top-k je aspoň správný předpis |
| `empty_ctx_ratio` | deterministická | podíl kontextů, které jsou jen nadpis (`§ 30`, `ČÁST DRUHÁ`), < 40 znaků |
| `nejisty_ctx` | deterministická | podíl kontextů z nejistých předpisů (status `neznamy`, před 1945, přetisk „úplné znění“); jen retrievery `v2-*` (MCP cesta status do kontextu nepředává → NaN) |
| `latency_s` | deterministická | doba odezvy vyhledávání |
| `llm_context_precision_with_reference` | RAGAS | jsou relevantní kontexty nahoře (LLM hodnotí každý kontext) |
| `context_recall` | RAGAS | kolik z referenční odpovědi kontexty podporují |
| `faithfulness` | RAGAS, `--with-generation` | odpověď si nic nevymýšlí mimo kontexty |
| `answer_relevancy` | RAGAS, `--with-generation` | odpověď odpovídá na otázku |
| `factual_correctness` | RAGAS, `--with-generation` | shoda odpovědi s referencí |

Výstupy v `results/<čas>/`: `summary.md`, `summary.csv`, `summary_by_source.csv`,
`per_question.jsonl` (včetně nalezených kontextů a hodnocení soudce) a `config.json`.

## Výsledky na plném indexu (19. 10. 2026)

Top 5, hit / MRR / správný předpis (`results/full-v1-vs-v2-20261019`, `mcp-v2-filtr-20261019`, `filtr-stare-20261019`):

| | ruční (15) | kontrolní staré platné (3) | syntetické zodpověditelné (15) | nejisté kontexty |
|---|---|---|---|---|
| v1 MCP `search_laws_semantic` | 0,00 / 0,00 / 0,00 | – | 0,13 / 0,10 / 0,27 | – |
| v1 `knn` | 0,13 / 0,04 / 0,20 | – | 0,27 / 0,22 / 0,27 | – |
| v2 MCP `search_laws_text` | 0,53 / 0,32 / 0,80 | 0,00 / 0,00 / 0,67 | 0,07 / 0,07 / 0,13 | – |
| v2-knn (bez filtru) | 0,80 / 0,73 / 1,00 | 0,33 / 0,17 / 0,67 | 0,47 / 0,29 / 0,53 | 19 % |
| **v2 MCP `search_laws_semantic`** (kNN + filtr f4) | **0,80 / 0,80 / 1,00** | **0,67 / 0,23 / 1,00** | **0,47 / 0,28 / 0,53** | **4 %** |

Pokles proti pilotu (93 %) je daný ~70× větším počtem předpisů; syntetické otázky jsou bez názvu předpisu často
nejednoznačné. Minutá ruční otázka: „Kdo má právo volit do Poslanecké sněmovny?“ (čl. 18 Ústavy se nenajde).

## Výsledky pilotu (18. 10. 2026)

Ruční sada (15 otázek), top 5, stejné univerzum 447 předpisů:

| retriever | hit | mrr | law_hit | RAGAS precision | RAGAS recall |
|---|---|---|---|---|---|
| v1-hybrid-u | 0,13 | 0,06 | 0,27 | 0,06 | 0,07 |
| v1-knn-u | 0,27 | 0,19 | 0,73 | | |
| v2-bm25 | 0,67 | 0,49 | 1,00 | | |
| v2-knn | 0,93 | 0,85 | 1,00 | | |
| v2-hybrid | 0,73 | 0,65 | 1,00 | | |
| v2-rerank | 0,93 | 0,80 | 1,00 | 0,82 | 0,93 |

Syntetická sada, 15 otázek zodpověditelných ve v2 (5 vyřazeno: 2 zrušené předpisy, 1 poznámka pod čarou,
2 nestandardní dokumenty): nejlepší v1 0,53, v2-hybrid/rerank 0,93.

Poznámky:
- Výsledky v `results/` z doby před opravou deduplikace v MCP (`hybrid`, `results/norag-smoke`,
  `results/ragas-sample`) nejsou s novými přímo srovnatelné – `hybrid` teď vrací víc fragmentů.
- Filtr přetisků „Úplné znění …“ a oprava kotvy u zkušební doby proběhly po prohlédnutí výsledků;
  na plném indexu a nové sadě otázek budou čísla nižší.
