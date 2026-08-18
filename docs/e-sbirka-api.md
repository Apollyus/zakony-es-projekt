# e-Sbírka API - Kompletní Reference

## Přehled

E-Sbírka (e-sbirka.gov.cz) je oficiální platforma Ministerstva vnitra ČR pro zveřejňování zákonů a dalších právních předpisů.

**Proč nás zajímá:**
- Poskytuje data ve více formátech: JSON (GLMF), PDF, DOCX
- Pro embeddování do Elasticsearchu potřebujeme textové dokumenty
- **PDF jsou skeny** (nelze textově extrahovat) → používáme **DOCX** (informativní znění)
- Alternativně: GLMF JSON data (001, 003, 004)

---

## Základní URL

| Zdroj | URL |
|-------|-----|
| Hlavní web | https://e-sbirka.gov.cz |
| OpenData API | https://opendata.eselpoint.gov.cz |
| REST API | https://e-sbirka.gov.cz/sbr-externi |
| Souborové služby | https://e-sbirka.gov.cz/souborove-sluzby |

---

## Datové sady OpenData (GLMF JSON)

Tyto soubory se stahují a zpracovávají JSON pipeline.

| Soubor | Popis | Počet záznamů | Velikost |
|--------|-------|---------------|----------|
| 001PravniAktZneni.json.gz | Metadata zákonů (citace, název, rok) | ~46 018 | 176 MB |
| 003PravniAktZneniFragment.json.gz | Strom fragmentů (hierarchie, citace) | ~46 018 | 1.2 GB |
| 004PravniAktFragment.json.gz | Typy fragmentů (Paragraf, Odstavec_Dc) | ~46 018 | 529 MB |
| 002PravniAkt.json.gz | Seznam právních aktů | ~46 018 | ~5 MB |
| 006PravniAktMetadata.json.gz | Metadata právních aktů | ~46 524 | |
| 010PravniAktBinarniSoubor.json.gz | Binární soubory (PDF, TIFF, JPEG, PNG) | ~45 860 | |
| 043PravniAktDigitalniReplika.json.gz | Digitální repliky (overená znění) | ~12 919 | |

### Struktura 001PravniAktZneni

```json
{
  "typ": "právní-akt-znění",
  "iri": "esel-esb:eli/cz/sb/1993/1/2026-01-01",
  "akt-citace": "1/1993 Sb.",
  "akt-název-vyhlášený": "Ústava České republiky",
  "akt-iri": "...",
  "znění-id": "...",
  "znění-dokument-id": 30123,
  "znění-datum-účinnosti-od": "1993-01-01",
  "znění-datum-účinnosti-do": null,
  "znění-je-zrušen": false,
  "znění-ročník": 1993,
  "cis-esb-sbírka-položka": "sb",
  "cis-esb-podtyp-právní-akt": "ZÁKON"
}
```

### Struktura 003PravniAktZneniFragment

```json
{
  "typ": "právní-akt-znění-fragment",
  "iri": "esel-esb:eli/cz/sb/1993/1/2026-01-01/dokument/norma/par_1",
  "znění-fragment-id": 1234,
  "znění-fragment-předek": "...",
  "znění-fragment-citace": "§ 1",
  "znění-fragment-citace-text": "...",
  "znění-fragment-eli": "/eli/cz/sb/1993/1/2026-01-01/dokument/norma/par_1",
  "znění-fragment-url": "...",
  "znění-fragment-hierarchie": "/2/1/",
  "znění-fragment-hierarchie-hex": "...",
  "znění-dokument-id": 30123,
  "právní-akt-fragment": {"fragment-id": 1234},
  "právní-akt-odkaz": "...",
  "právní-akt-komentář-fragmentu": "..."
}
```

### Struktura 004PravniAktFragment

```json
{
  "typ": "právní-akt-fragment",
  "iri": "...",
  "fragment-id": 1234,
  "fragment-base-id": 1234,
  "fragment-text": "Česko je ústavní, demokratický...",
  "cis-esb-typ-fragmentu": "Odstavec_Dc",
  "cis-esb-typ-fragmentu-položka": "Odstavec_Dc"
}
```

### Typy fragmentů (004)

| Typ | Popis |
|-----|-------|
| `Paragraf` | Nadpis paragrafu (např. "§ 1") |
| `Odstavec_Dc` | Text paragrafu (odstavec) |
| `Pozemek` | Pozemek (specifický typ) |

> **Klíčové:** Text paragrafu je rozdělen mezi `Paragraf` (nadpis) a `Odstavec_Dc` (obsah).
> Pipeline.py je kombinuje do jednoho textu.

---

## API Endpointy pro DOCX/PDF

### 1. Získání dokumentBaseId

**Endpoint:** `GET /sbr-cache/dokumenty-sbirky/{staleUrl}`

**Příklad:**
```
GET /sbr-cache/dokumenty-sbirky/%2Fsb%2F1993%2F1%2F2026-01-01
```

**Response:**
```json
{
  "dokumentBaseId": 30123,
  "staleUrl": "/sb/1993/1/2026-01-01",
  "nazev": "Ústava České republiky"
}
```

**Poznámky:**
- `staleUrl` musí být URL-encoded (lomitka → `%2F`)
- Vrací `dokumentBaseId` který se používá v dalších API

### 2. Získání DOCX download ID (INFORMATIVNÍ ZNĚNÍ)

**Endpoint:** `GET /sbr-externi/stahni/informativni-zneni/{dokumentBaseId}/DOCX`

**Příklad:**
```
GET /sbr-externi/stahni/informativni-zneni/30123/DOCX
```

**Response:**
```json
{
  "pozadavekId": "599536af-e908-421b-82de-1e0ada2cb365",
  "id": "fbb1a045-9278-496b-bf2c-b00af999eb64",
  "stavPozadavku": "OK",
  "nazevDokumentu": "Sb_1993_1_IZ.docx"
}
```

**Poznámky:**
- Vrací `id` pro stažení souboru (ne `pozadavekId`)
- `stavPozadavku` může být "OK" nebo jiná hodnota
- `nazevDokumentu` je název výsledného DOCX souboru

### 3. Stažení DOCX souboru

**Endpoint:** `GET /souborove-sluzby/soubory/{id}`

**Response:** Binární data DOCX souboru (Content-Type: application/octet-stream)

### 4. Search API

**Endpoint:** `POST /sbr-cache/jednoducha-vyhledavani`

**Request Body:**
```json
{
  "text": "ústava",
  "stranka": 1,
  "pocetZaznamuNaStrance": 10
}
```

**Response:**
```json
{
  "pocetCelkem": 46025,
  "seznam": [
    {
      "staleUrl": "/sb/2012/235",
      "nazev": "Ústavní zákon o změnách státních hranic...",
      "kodDokumentuSbirky": "235/2012 Sb.",
      "stavDokumentuSbirky": "AKTUALNE_PLATNY",
      "datum": "2012-07-04"
    }
  ]
}
```

---

## API Endpointy pro PDF (pozastaveno)

### Stahování PDF (overené znění)

1. `GET /sbr-externi/stahni/overene-zneni/{znění-dokument-id}`
   - Vrací JSON s informací o PDF
   - `stavPozadavku` může být "OK" nebo "NEPRIPRAVEN"

2. `GET /souborove-sluzby/soubory/{uuid}`
   - Vrací přímo PDF soubor (Content-Type: application/pdf)

### Proč PDF nepoužíváme

- PDF ze státní sbírky jsou **skeny papíru** (obrázky, ne text)
- Nelze z nich extrahovat text pro embedding
- 61 ze 104 stažených PDF bylo detekováno jako skeny
- **Řešení:** Používáme DOCX (informativní znění)

---

## Formát souborů — Přehled

| Formát | Právě závazné | Informativní | Textové |
|--------|---------------|--------------|---------|
| DOCX | ❌ Nepodporován | ✅ Podporován | ✅ Ano |
| PDF | ❌ Skeny | ❌ Chyba | ❌ Ne (obrázky) |
| ZIP | ✅ Async | ✅ Async | ❌ Archív |
| JSON (GLMF) | ✅ | ❌ | ✅ Ano |

---

## Omezení

### 1. Informativní znění - dostupnost
- Některé zákony nemají dostupné informativní znění
- Starší zákony (před 1990) mohou mít omezenou dostupnost
- Nové zákony (2024+) mohou mít ještě nezpracované informativní znění

### 2. Rate Limiting
- e-Sbírka nemá explicitní rate limiting dokumentován
- Doporučuje se pauza mezi požadavky (0.3-2 sekundy)

### 3. Async vs Sync
- ZIP formát vrací async `pozadavekId` pro pozdější stažení
- DOCX formát vrací synchronně `id` pro okamžité stažení

---

## Workflow pro stažení DOCX

```python
# 1. Získat dokumentBaseId
response = GET /sbr-cache/dokumenty-sbirky/{staleUrl}
dokumentBaseId = response['dokumentBaseId']

# 2. Získat DOCX download ID
response = GET /sbr-externi/stahni/informativni-zneni/{dokumentBaseId}/DOCX
docxId = response['id']
stav = response['stavPozadavku']
nazevSouboru = response['nazevDokumentu']

# 3. Stáhnout DOCX
if stav == 'OK':
    response = GET /souborove-sluzby/soubory/{docxId}
    save(response.content, nazevSouboru)
```

---

## Příklady dokumentBaseId

| Zákon | staleUrl | dokumentBaseId | DOCX dostupné |
|-------|----------|----------------|---------------|
| 1/1993 Sb. (Ústava) | /sb/1993/1/2026-01-01 | 30123 | ✅ |
| 8/1918 Sb. | /sb/1918/8/2026-01-01 | 1 | ✅ |
| 235/2012 Sb. | /sb/2012/235/2026-01-01 | 49239 | ✅ |
| 633/2004 Sb. | /sb/2004/633/2026-01-01 | 41641 | ✅ |

---

## Implementace

### Skript: `stahni-docx.py`

```bash
# Stáhnout konkrétní zákon
python3 stahni-docx.py --url "/sb/1993/1/2026-01-01" --output ./zakony

# Hledat a stáhnout zákony
python3 stahni-docx.py --search "ústava" --limit 10 --output ./zakony
```

**Parametry:**
- `--url` — URL konkrétního zákona (např. `/sb/1993/1/2026-01-01`)
- `--search` — Text pro vyhledávání
- `--limit` — Maximální počet výsledků (výchozí: 10)
- `--output` — Cíl pro stažené soubory (výchozí: `./zakony`)

---

## Závěr

| Zdroj | Proč | Proti |
|-------|------|-------|
| **DOCX** | ✅ Textové, lze extrahovat<br>✅ Obsahuje kompletní znění se všemi novelami<br>✅ Oficiální konsolidovaná verze | ❌ Není to pravě závazná verze |
| **JSON GLMF** | ✅ Pravě závazná data<br>✅ Strukturované | ❌ Text je rozdělený do fragmentů<br>❌ Složité mapování |
| **PDF** | — | ❌ Skeny (obrázky, ne text) |

**Doporučení:** Pro embeddování do Elasticsearchu používáme DOCX.
