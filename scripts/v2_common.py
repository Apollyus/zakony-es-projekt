"""
Sdílená logika indexu v2 (pilot i plný build).

Index v2: jeden ES dokument = jeden § / článek aktuálního znění předpisu, s nadpisem
a cestou v předpisu (část › hlava › díl). Dlouhé § se dělí po fragmentech (odstavcích)
na chunky do MAX_CHUNK_CHARS znaků; hlavička se opakuje v každém chunku.
"""

import re

V2_EMBED_MODEL = "BAAI/bge-m3"
V2_DIMS = 1024
MAX_CHUNK_CHARS = 1800

UNIT_RE = re.compile(r"^(par|cl)_")
CONTAINER_RE = re.compile(r"^(cast|hlava|dil|oddil|pododdil|kapitola|priloha)_")
# typy, které nejsou obsah (hlavička dokumentu, technické uzly, poznámky pod čarou)
SKIP_TYPES = {
    "Virtual_Document", "Virtual_Prefix", "Virtual_Postfix", "Virtual_Norma", "Prefix", "Prefix_Number",
    "Prefix_Type", "Prefix_Title", "Prefix_Date", "Prefix_Author", "Postfix", "Poznamka", "PPC",
}
HEADING_TYPES = {"Nadpis_pod", "Nadpis_nad", "Nadpis"}
LABEL_TYPES = {"Paragraf", "Clanek", "Cast", "Hlava", "Dil", "Oddil", "Pododdil", "Kapitola", "Priloha", "Hlavicka_priloha"}

MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "akt_citace": {"type": "keyword"},
            "id_zakona": {"type": "keyword"},
            "rok": {"type": "integer"},
            "poradi": {"type": "integer"},
            "akt_nazev": {"type": "text", "analyzer": "czech"},
            "typ_predpisu": {"type": "keyword"},
            "status": {"type": "keyword"},
            "zneni_iri": {"type": "keyword"},
            "ucinnost_od": {"type": "date"},
            "oznaceni": {"type": "keyword"},
            "nadpis": {"type": "text", "analyzer": "czech"},
            "cesta": {"type": "text", "analyzer": "czech"},
            "chunk_no": {"type": "integer"},
            "text": {"type": "text", "analyzer": "czech"},
            # hlavička + text, jen pro embedding; nevyhledává se v něm a v _source se neukládá (šetří disk)
            "search_text": {"type": "text", "index": False},
            "vektor": {
                "type": "dense_vector", "dims": V2_DIMS, "index": True, "similarity": "cosine",
                "index_options": {"type": "int8_hnsw", "m": 16, "ef_construction": 100},
            },
        }
    },
}


def clean(t: str) -> str:
    t = re.sub(r"<[^>]+>", "", t or "")
    return re.sub(r"\s+", " ", t).strip()


def law_type(nazev: str) -> str:
    n = (nazev or "").lower()
    for key, label in (("ústavní zákon", "ustavni_zakon"), ("ústava", "ustavni_zakon"), ("zákon", "zakon"),
                       ("nařízení", "narizeni"), ("vyhláška", "vyhlaska"), ("sdělení", "sdeleni"),
                       ("usnesení", "usneseni"), ("nález", "nalez"), ("opatření", "opatreni")):
        if n.startswith(key) or n.startswith("úplné znění") and key in n[:80]:
            return label
    return "jine"


def search_text_of(d: dict) -> str:
    """Rekonstrukce textu, který se embeduje (hlavička + text), z polí dokumentu."""
    head = " | ".join(x for x in [f"{d.get('akt_nazev')} ({d.get('akt_citace')})", d.get("cesta"),
                                  ((d.get("oznaceni") or "") + " " + (d.get("nadpis") or "")).strip()] if x)
    return head + "\n" + (d.get("text") or "")


def is_republication(nazev: str) -> bool:
    """'Úplné znění …' = historický přetisk jiného předpisu, ne samostatné platné právo."""
    return "úplné znění" in (nazev or "").lower()[:80]


def priority(status: str, typ: str, akt_citace: str) -> int:
    """Pořadí embeddingu: nejdřív platné zákony, nakonec mezinárodní smlouvy (Sb. m. s.)."""
    if "Sb. m. s." in akt_citace:
        return 8
    if status == "platny":
        return {"ustavni_zakon": 0, "zakon": 0, "narizeni": 1, "vyhlaska": 2}.get(typ, 3)
    return {"ustavni_zakon": 4, "zakon": 4}.get(typ, 5)


def rel_path(iri: str) -> list:
    tail = iri.split("/dokument", 1)[1] if "/dokument" in iri else ""
    return [s for s in tail.strip("/").split("/") if s]


def units_for_law(akt_citace: str, nazev: str, status: str, zneni_iri: str, ucinnost_od: str, rows: list) -> list:
    """
    rows: fragmenty jednoho znění, dicty s klíči iri, parent, hex, typ, text (text už vyčištěný).
    Vrací ES dokumenty (dict s '_id') bez vektoru.
    """
    nazev = clean(nazev)
    rows = sorted(rows, key=lambda r: r["hex"] or "")
    node = {r["iri"]: r for r in rows}

    # popisky kontejnerů (ČÁST DRUHÁ + nadpis) a nadpisy §
    label, heading = {}, {}
    for r in rows:
        if r["typ"] in LABEL_TYPES:
            label[r["iri"]] = r["text"]
        if r["typ"] == "Nadpis_pod" and r["parent"] and r["parent"] not in heading:
            heading[r["parent"]] = r["text"]

    units = {}          # unit_key -> dict
    order = []
    last_nad = {}       # parent iri -> poslední Nadpis_nad (platí pro následující §)
    for r in rows:
        if r["typ"] in SKIP_TYPES or "/dokument/prefix" in r["iri"] or not r["text"]:
            continue
        path = rel_path(r["iri"])
        if r["typ"] == "Nadpis_nad" and r["parent"]:
            last_nad[r["parent"]] = r["text"]
            continue
        # najdi §/článek v cestě (první výskyt; citované § v novelách patří k vnějšímu §)
        ui = next((i for i, s in enumerate(path) if UNIT_RE.match(s)), None)
        prefix = r["iri"].split("/dokument", 1)[0] + "/dokument"
        if ui is not None:
            key = prefix + "/" + "/".join(path[: ui + 1])
            container_segs = path[:ui]
            kind = "unit"
        else:
            ci = max((i for i, s in enumerate(path) if CONTAINER_RE.match(s)), default=None)
            if r["typ"] in LABEL_TYPES or (r["typ"] in HEADING_TYPES and r["parent"] in label):
                continue  # popisek/nadpis kontejneru – jde do cesty, ne do textu
            key = prefix + "/" + "/".join(path[: ci + 1]) + "#text" if ci is not None else prefix + "#text"
            container_segs = path[: ci + 1] if ci is not None else []
            kind = "text"
        if key not in units:
            crumbs, acc = [], prefix
            for s in container_segs:
                acc += "/" + s
                if acc in label:
                    crumbs.append((label[acc] + " " + heading.get(acc, "")).strip())
            unit_node = node.get(key.split("#")[0])
            parent = unit_node["parent"] if unit_node else None
            units[key] = {
                "oznaceni": label.get(key, "") if kind == "unit" else "",
                "nadpis": heading.get(key) or (last_nad.get(parent, "") if parent else ""),
                "cesta": " › ".join(c for c in crumbs if c),
                "pieces": [],
            }
            order.append(key)
        u = units[key]
        if r["iri"] == key or (r["typ"] == "Nadpis_pod" and r["parent"] == key):
            continue  # samotné "§ 140" a jeho nadpis jsou v hlavičce
        u["pieces"].append(r["text"])

    typ = law_type(nazev)
    id_zakona = zneni_iri.rsplit("/", 1)[0]          # esel-esb:eli/cz/sb/2009/40
    m = re.search(r"/(\d{4})/", id_zakona)
    rok = int(m.group(1)) if m else None
    docs = []
    for key in order:
        u = units[key]
        if not u["pieces"]:
            continue
        head = " | ".join(x for x in [f"{nazev} ({akt_citace})", u["cesta"],
                                      (u["oznaceni"] + " " + u["nadpis"]).strip()] if x)
        chunks, cur = [], []
        for p in u["pieces"]:
            if cur and sum(len(x) for x in cur) + len(p) > MAX_CHUNK_CHARS:
                chunks.append(cur)
                cur = []
            cur.append(p)
        if cur:
            chunks.append(cur)
        for ci, ch in enumerate(chunks):
            text = "\n".join(ch)
            docs.append({
                "_id": f"{key}#{ci}",
                "akt_citace": akt_citace,
                "id_zakona": id_zakona,
                "rok": rok,
                "poradi": len(docs),            # pořadí chunku v předpisu (pro get_law_detail)
                "akt_nazev": nazev,
                "typ_predpisu": typ,
                "status": status,
                "zneni_iri": zneni_iri,
                "ucinnost_od": ucinnost_od,
                "oznaceni": u["oznaceni"],
                "nadpis": u["nadpis"],
                "cesta": u["cesta"],
                "chunk_no": ci,
                "text": text,
                "search_text": head + "\n" + text,
            })
    return docs
