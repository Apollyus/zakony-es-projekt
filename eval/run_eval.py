"""
Evaluace vyhledávání v zákonech: deterministické metriky (Hit@k, MRR, ...) + RAGAS
(LLM judge = Qwen za VPN). Volitelně end-to-end RAG s generováním odpovědi.

Příklad:
    .venv/bin/python eval/run_eval.py --retrievers bm25,hybrid,knn --k 5 --limit 10
    .venv/bin/python eval/run_eval.py --with-generation
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
import config  # noqa: E402
import retrievers  # noqa: E402

DEFAULT_DATASETS = [EVAL_DIR / "data" / "manual.jsonl", EVAL_DIR / "data" / "manual_old.jsonl",
                    EVAL_DIR / "data" / "synthetic.jsonl"]

GEN_PROMPT = """Jsi právní asistent. Odpověz česky na otázku POUZE na základě uvedených úryvků ze zákonů.
Pokud úryvky odpověď neobsahují, řekni to. Uveď, ze kterého zákona odpověď vychází.

Úryvky:
{contexts}

Otázka: {question}
Odpověď:"""


# ── Pomocné funkce ───────────────────────────────────────────────────────────
def norm(t: str) -> str:
    t = re.sub(r"<[^>]+>", "", t or "")
    t = unicodedata.normalize("NFC", t).lower()
    return re.sub(r"\s+", " ", t).strip()


def load_dataset(paths, limit: int = 0) -> list:
    items = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            print(f"(přeskakuji, neexistuje: {p})")
            continue
        rows = [json.loads(line) for line in p.open(encoding="utf-8") if line.strip()]
        items.extend(rows[:limit] if limit else rows)
    return items


def is_gold(ctx: dict, gold: list) -> bool:
    """Fragment je relevantní, pokud je ze správného zákona a obsahuje anchor."""
    text = norm(ctx.get("text", ""))
    for g in gold:
        if ctx.get("akt_citace") == g["akt_citace"] and norm(g["anchor"]) in text:
            return True
    return False


def retrieval_metrics(ctxs: list, gold: list, k: int) -> dict:
    top = ctxs[:k]
    ranks = [i for i, c in enumerate(top, 1) if is_gold(c, gold)]
    gold_laws = {g["akt_citace"] for g in gold}
    law_ranks = [i for i, c in enumerate(top, 1) if c.get("akt_citace") in gold_laws]
    return {
        "hit": float(bool(ranks)),
        "mrr": 1.0 / ranks[0] if ranks else 0.0,
        "law_hit": float(bool(law_ranks)),
        "law_mrr": 1.0 / law_ranks[0] if law_ranks else 0.0,
        # podíl "prázdných" kontextů (jen nadpis typu '§ 30' / 'ČÁST DRUHÁ')
        "empty_ctx_ratio": (sum(len(norm(c.get("text", ""))) < 40 for c in top) / len(top)) if top else 1.0,
        "n_ctx": len(top),
        # podíl kontextů z nejistých předpisů (status neznamy / před 1945 / přetisk) – jen pro v2 (má status)
        "nejisty_ctx": (sum(any(retrievers.stale_flags(c).values()) for c in top) / len(top))
        if top and "status" in top[0] else float("nan"),
    }


def ctx_to_str(c: dict) -> str:
    head = f"{c.get('akt_nazev') or ''} ({c.get('akt_citace')})"
    if c.get("citace"):
        head += f" {c['citace']}"
    return f"{head}: {re.sub(r'<[^>]+>', '', c.get('text') or '').strip()}"


def generate_answer(llm, question: str, contexts: list) -> str:
    joined = "\n".join(f"[{i}] {c}" for i, c in enumerate(contexts, 1))
    return llm.invoke(GEN_PROMPT.format(contexts=joined, question=question)).content.strip()


# ── RAGAS ────────────────────────────────────────────────────────────────────
def run_ragas(rows: list, with_generation: bool, workers: int) -> pd.DataFrame:
    from ragas import EvaluationDataset, RunConfig, evaluate
    from ragas.metrics import (
        FactualCorrectness,
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )

    metrics = [LLMContextPrecisionWithReference(), LLMContextRecall()]
    if with_generation:
        metrics += [Faithfulness(), ResponseRelevancy(), FactualCorrectness()]

    samples = []
    for r in rows:
        s = {"user_input": r["user_input"], "retrieved_contexts": r["retrieved_contexts"] or ["(nic nenalezeno)"],
             "reference": r["reference"]}
        if with_generation:
            s["response"] = r["response"]
        samples.append(s)

    result = evaluate(
        dataset=EvaluationDataset.from_list(samples),
        metrics=metrics,
        llm=config.get_eval_llm(),
        embeddings=config.get_eval_embeddings(),
        run_config=RunConfig(max_workers=workers, timeout=600, max_retries=3),
        raise_exceptions=False,
        show_progress=True,
    )
    df = result.to_pandas()
    keep = [c for c in df.columns if c not in ("user_input", "retrieved_contexts", "reference", "response")]
    return df[keep]


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="RAGAS evaluace vyhledávání v zákonech")
    ap.add_argument("--retrievers", default=",".join(retrievers.RETRIEVERS))
    ap.add_argument("--k", type=int, default=5, help="počet kontextů (fragmentů) předaných k hodnocení")
    ap.add_argument("--datasets", nargs="*", default=[str(p) for p in DEFAULT_DATASETS])
    ap.add_argument("--limit", type=int, default=0, help="max otázek z každého datasetu (0 = vše)")
    ap.add_argument("--with-generation", action="store_true", help="vygenerovat odpověď a měřit i generování")
    ap.add_argument("--no-ragas", action="store_true", help="jen deterministické metriky (rychlé, bez LLM)")
    ap.add_argument("--workers", type=int, default=8, help="paralelních LLM volání v RAGAS")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    items = load_dataset(args.datasets, args.limit)
    names = [n.strip() for n in args.retrievers.split(",") if n.strip()]
    out_dir = Path(args.out) if args.out else EVAL_DIR / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Otázek: {len(items)} | retrievery: {names} | k={args.k} | výstup: {out_dir}")

    gen_llm = config.get_chat_llm() if args.with_generation else None

    # 1) Retrieval (+ generování)
    rows = []
    for name in names:
        for it in tqdm(items, desc=f"retrieval {name}"):
            t0 = time.time()
            try:
                ctxs = retrievers.retrieve(name, it["user_input"], k=args.k)
                err = ""
            except Exception as e:  # noqa: BLE001
                ctxs, err = [], str(e)
            latency = time.time() - t0
            row = {
                "retriever": name, "id": it["id"], "source": it.get("source", ""),
                "user_input": it["user_input"], "reference": it["reference"],
                "latency_s": round(latency, 3), "error": err,
                **retrieval_metrics(ctxs, it["gold"], args.k),
                "retrieved_contexts": [ctx_to_str(c) for c in ctxs[: args.k]],
                "retrieved_ids": [f"{c.get('akt_citace')} {c.get('citace') or ''}".strip() for c in ctxs[: args.k]],
                "gold": it["gold"],
            }
            if gen_llm is not None:
                try:
                    row["response"] = generate_answer(gen_llm, it["user_input"], row["retrieved_contexts"])
                except Exception as e:  # noqa: BLE001
                    row["response"] = f"(chyba generování: {e})"
            rows.append(row)

    df = pd.DataFrame(rows)

    # 2) RAGAS
    if not args.no_ragas:
        print("\n--- RAGAS (LLM judge: %s) ---" % config.EVAL_LLM_MODEL)
        ragas_df = run_ragas(rows, args.with_generation, args.workers)
        df = pd.concat([df.reset_index(drop=True), ragas_df.reset_index(drop=True)], axis=1)

    # 3) Výstupy
    df.to_json(out_dir / "per_question.jsonl", orient="records", lines=True, force_ascii=False)
    metric_cols = [c for c in df.columns if c in (
        "hit", "mrr", "law_hit", "law_mrr", "empty_ctx_ratio", "nejisty_ctx", "latency_s",
        "llm_context_precision_with_reference", "context_recall",
        "faithfulness", "answer_relevancy", "factual_correctness", "factual_correctness(mode=f1)")]
    summary = df.groupby(["retriever", "source"])[metric_cols].mean().round(3)
    overall = df.groupby("retriever")[metric_cols].mean().round(3)
    overall.insert(0, "n", df.groupby("retriever").size())
    summary.reset_index().to_csv(out_dir / "summary_by_source.csv", index=False)
    overall.reset_index().to_csv(out_dir / "summary.csv", index=False)

    md = [f"# Výsledky evaluace ({datetime.now():%Y-%m-%d %H:%M})", "",
          f"- otázek: {len(items)}, k = {args.k}, LLM judge: `{config.EVAL_LLM_MODEL}`",
          f"- generování: {'ano' if args.with_generation else 'ne'}", "",
          "## Celkem", "", overall.to_markdown(), "", "## Podle zdroje otázek", "", summary.to_markdown(), ""]
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    (out_dir / "config.json").write_text(json.dumps(vars(args), ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + overall.to_string())
    print(f"\nUloženo do {out_dir}")


if __name__ == "__main__":
    main()
