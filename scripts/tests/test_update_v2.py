"""Testy průběžné aktualizace (update_v2) nad miniaturními dumpy e-Sbírky, s falešným ES a embedderem."""

import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_v2_index as B  # noqa: E402
import update_v2 as U  # noqa: E402

D1, D2 = "2026-10-19", "2026-10-26"


# ── miniaturní dumpy ─────────────────────────────────────────────────────────
def law(citace, nazev, versions):
    """versions: [(doc_id, od, do, zmena, {cislo_par: text})]"""
    return {"citace": citace, "nazev": nazev, "versions": versions}


def write_dumps(d: Path, laws: list):
    v001, v003, v004, fid = [], [], [], [0]

    def frag(doc_id, iri, parent, hex_, typ, text):
        fid[0] += 1
        v003.append({"iri": iri, "znění-dokument-id": doc_id, "znění-fragment-předek": {"iri": parent} if parent else None,
                     "znění-fragment-hierarchie-hex": hex_, "právní-akt-fragment": {"fragment-id": fid[0]}})
        v004.append({"fragment-id": fid[0], "cis-esb-typ-fragmentu-položka": typ, "fragment-text": text})

    for lw in laws:
        num, year = lw["citace"].split()[0].split("/")
        for doc_id, od, do, zmena, pars in lw["versions"]:
            iri = f"esel-esb:eli/cz/sb/{year}/{num}/{od}"
            v001.append({"akt-citace": lw["citace"], "akt-název-vyhlášený": lw["nazev"], "znění-dokument-id": doc_id,
                         "iri": iri, "znění-datum-účinnosti-od": od, "znění-datum-účinnosti-do": do,
                         "cis-esb-typ-znění-položka": "KONSOL", "znění-datum-čas-poslední-změny": zmena})
            root = f"{iri}/dokument"
            frag(doc_id, root, None, "00", "Virtual_Document", None)
            frag(doc_id, f"{root}/norma", root, "01", "Virtual_Norma", None)
            for i, (n, text) in enumerate(sorted(pars.items()), 1):
                p = f"{root}/norma/par_{n}"
                frag(doc_id, p, f"{root}/norma", f"02{i:02d}", "Paragraf", f"§ {n}")
                frag(doc_id, f"{p}/odst_1", p, f"02{i:02d}01", "Odstavec_Dc", text)
    for name, data in (("001PravniAktZneni", v001), ("003PravniAktZneniFragment", v003),
                       ("004PravniAktFragment", v004)):
        with gzip.open(d / f"{name}.json.gz", "wt", encoding="utf-8") as f:
            json.dump({"položky": data}, f, ensure_ascii=False)


A = law("1/2000 Sb.", "Zákon o A", [(10, "2000-01-01", None, "t1", {1: "Text A jedna.", 2: "Text A dva."})])
B_OLD = (20, "2000-01-01", "2026-10-25", "t1", {1: "Společný text B.", 2: "Původní znění B dva."})
B_NEW = (21, "2026-10-26", None, "t1", {1: "Společný text B.", 2: "Nové znění B dva po novele."})
BLAW = law("2/2000 Sb.", "Zákon o B", [B_OLD, B_NEW])
D_V1 = law("4/2000 Sb.", "Zákon o D", [(40, "2000-01-01", None, "t1", {1: "Text D jedna.", 2: "Text D dva."})])

BEFORE = [A, BLAW, D_V1]
AFTER = [
    law("1/2000 Sb.", "Zákon o A", [(10, "2000-01-01", "2026-10-20", "t2", {1: "Text A jedna.", 2: "Text A dva."})]),
    BLAW,
    law("3/2026 Sb.", "Zákon o C", [(30, "2026-10-01", None, "t1", {1: "Úplně nový předpis C.", 2: "C druhý."})]),
    law("4/2000 Sb.", "Zákon o D", [(40, "2000-01-01", None, "t2", {1: "Text D jedna."})]),   # oprava: § 2 pryč
]


# ── falešný ES a embedder ────────────────────────────────────────────────────
class FakeIndices:
    def __init__(self, es):
        self.es = es

    def exists(self, index):
        self.es.check()
        return self.es.created

    def create(self, index, body):
        self.es.created = True

    def put_settings(self, index, body):
        self.es.check()

    def refresh(self, index):
        self.es.check()


class FakeES:
    def __init__(self):
        self.up, self.created, self.docs = True, False, {}
        self.indices = FakeIndices(self)

    def check(self):
        if not self.up:
            raise ConnectionError("VPN down")

    def options(self, **kw):
        return self

    def count(self, index):
        return {"count": len(self.docs)}

    def delete_by_query(self, index, query, **kw):
        self.check()
        c = query["bool"]["filter"][0]["term"]["akt_citace"]
        keep = set(query["bool"].get("must_not", [{}])[0].get("ids", {}).get("values", []))
        for i in [i for i, d in self.docs.items() if d["akt_citace"] == c and i not in keep]:
            del self.docs[i]


class FakeEmbedder:
    texts = []

    def __init__(self, device, mem_limit_gb):
        pass

    def plan(self, texts):
        return [(64, list(range(i, min(i + 3, len(texts))))) for i in range(0, len(texts), 3)]

    def encode(self, texts, bucket):
        FakeEmbedder.texts += texts
        return np.array([[int(hashlib.sha1(t.encode()).hexdigest()[:6], 16) % 997, 1, 2, 3] for t in texts],
                        dtype=np.float32)

    def gpu_mem_gb(self):
        return 1.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    monkeypatch.setattr(B, "DB_PATH", tmp_path / "v2.db")
    monkeypatch.setattr(B, "DUMP_DIR", dumps)
    monkeypatch.setattr(B, "PUSH_GRACE_S", 0)
    monkeypatch.setattr(B, "_stop", False)
    monkeypatch.setattr(B.ESPusher, "RETRY_AFTER", 0)
    monkeypatch.setattr(B, "Embedder", FakeEmbedder)
    FakeEmbedder.texts = []
    es = FakeES()

    def fake_bulk(client, actions, **kw):
        es.check()
        for a in actions:
            es.docs[a["_id"]] = a["_source"]
        return len(actions), []

    import elasticsearch
    import elasticsearch.helpers
    monkeypatch.setattr(elasticsearch, "Elasticsearch", lambda *a, **k: es)
    monkeypatch.setattr(elasticsearch.helpers, "bulk", fake_bulk)

    # plný build k D1
    write_dumps(dumps, BEFORE)
    B.stage_extract(D1)
    B.stage_units()
    assert B.stage_embed(max_priority=5, hours=1, device="mps", mem_limit_gb=12) == 0
    FakeEmbedder.texts = []
    write_dumps(dumps, AFTER)
    return es, dumps


def es_state(es):
    return sorted((d["akt_citace"], d["oznaceni"], d["zneni_iri"].rsplit("/", 1)[1], d["text"]) for d in es.docs.values())


EXPECTED = [
    ("2/2000 Sb.", "§ 1", "2026-10-26", "Společný text B."),
    ("2/2000 Sb.", "§ 2", "2026-10-26", "Nové znění B dva po novele."),
    ("3/2026 Sb.", "§ 1", "2026-10-01", "Úplně nový předpis C."),
    ("3/2026 Sb.", "§ 2", "2026-10-01", "C druhý."),
    ("4/2000 Sb.", "§ 1", "2000-01-01", "Text D jedna."),
]


# ── testy ────────────────────────────────────────────────────────────────────
def test_update_applies_new_changed_removed(env):
    es, _ = env
    assert len(es.docs) == 6                                         # A×2, B×2 (staré znění), D×2
    rc, summary = U.run_update(D2, hours=0.01)
    assert rc == 0
    assert [c for c, _ in summary["new"]] == ["3/2026 Sb."]
    assert dict(summary["changed"]) == {"2/2000 Sb.": "nové znění 2000-01-01 → 2026-10-26",
                                        "4/2000 Sb.": "oprava dat ve stejném znění"}
    assert summary["removed"] == ["1/2000 Sb."]
    assert es_state(es) == EXPECTED                                  # A pryč, B jen nové znění, D bez § 2


def test_only_changed_texts_are_embedded(env):
    U.run_update(D2, hours=0.01)
    embedded = sorted(t.split("\n", 1)[1] for t in FakeEmbedder.texts)
    # „Společný text B.“ a „Text D jedna.“ se převzaly z lokálních vektorů
    assert embedded == ["C druhý.", "Nové znění B dva po novele.", "Úplně nový předpis C."]


def test_second_run_is_noop(env):
    es, _ = env
    U.run_update(D2, hours=0.01)
    before, FakeEmbedder.texts = dict(es.docs), []
    rc, summary = U.run_update(D2, hours=0.01)
    assert rc == 0 and not summary["new"] and not summary["changed"] and not summary["removed"]
    assert FakeEmbedder.texts == [] and es.docs == before


def test_es_outage_keeps_old_version_until_next_run(env):
    es, _ = env
    old = dict(es.docs)
    es.up = False
    rc, summary = U.run_update(D2, hours=0.0005)
    assert rc == B.EXIT_INCOMPLETE and summary["es_pending_delete"] == 4
    assert es.docs == old                                            # žádný výpadek: staré znění dál v indexu
    es.up = True
    rc, summary = U.run_update(D2, hours=0.01)                       # plán už je prázdný, jen doposílá a dosmaže
    assert rc == 0 and not summary["changed"] and summary["es_pending_delete"] == 0
    assert es_state(es) == EXPECTED


def test_dry_run_changes_nothing(env):
    es, _ = env
    db_before, docs_before = B.DB_PATH.read_bytes(), dict(es.docs)
    rc, summary = U.run_update(D2, dry_run=True)
    assert rc == 0 and summary["removed"] == ["1/2000 Sb."] and len(summary["changed"]) == 2
    assert B.DB_PATH.read_bytes() == db_before and es.docs == docs_before and FakeEmbedder.texts == []


def test_db_is_consistent_after_update(env):
    U.run_update(D2, hours=0.01)
    con = sqlite3.connect(B.DB_PATH)
    laws = dict(con.execute("SELECT akt_citace, zneni_iri FROM laws"))
    assert set(laws) == {"2/2000 Sb.", "3/2026 Sb.", "4/2000 Sb."}
    assert laws["2/2000 Sb."].endswith("2026-10-26")
    assert con.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 5
    assert con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 5
    assert con.execute("SELECT COUNT(*) FROM units WHERE pushed = 0").fetchone()[0] == 0
    assert con.execute("SELECT value FROM meta WHERE key = 'extract_done'").fetchone()[0] == D2


def test_plan_changes_baseline_without_stored_timestamp():
    old = {"x": ("x", "N", "platny", 1, "iri1", "2000-01-01", "zakon", 0, None)}
    same = [("x", "N", "platny", 1, "iri1", "2000-01-01", "zakon", 0, "t9")]
    plan = U.plan_changes(old, same)
    assert not plan["changed"] and plan["baseline"] == same          # první běh po buildu: nic se nepřepočítá


def test_choose_versions_is_independent_of_dump_order():
    """Víc znění se stejným datem účinnosti: výběr nesmí záviset na pořadí v dumpu."""
    vs = [{"nazev": "Zákon X", "doc_id": d, "iri": f"iri/{d}", "od": "2026-07-17", "do": None,
           "typ_zneni": "KONSOL", "zmena": z} for d, z in ((2073307, "2026-07-02"), (2073325, "2026-07-08"), (6802, "2026-07-02"))]
    picks = {B.choose_versions({"x": order}, "2026-10-26")[0][0][3] for order in (vs, vs[::-1], vs[1:] + vs[:1])}
    assert picks == {2073325}                                        # nejnovější změna v datech
