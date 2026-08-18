# e-Sbírka - Získávání textových PDF/DOCX dokumentů

## Přehled

Projekt se snaží získat textové PDF/DOCX dokumenty z e-Sbírky pro embedování do Elasticsearchu.
Pravě závazná znění jsou pouze scan PDF, která nelze textově extrahovat.
Informativní znění (konsolidovaná verze) je dostupné jako textový DOCX.

---

## API Endpointy

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
  "nazev": "Ústava České republiky",
  ...
}
```

**Poznámky:**
- `staleUrl` musí být URL-encoded (lomitka → `%2F`)
- Vrací `dokumentBaseId` který se používá v dalších API

---

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

---

### 3. Stažení DOCX souboru

**Endpoint:** `GET /souborove-sluzby/soubory/{id}`

**Příklad:**
```
GET /souborove-sluzby/soubory/fbb1a045-9278-496b-bf2c-b00af999eb64
```

**Response:**
- Binární data DOCX souboru
- Content-Type: application/octet-stream

---

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

**Poznámky:**
- Vrací až 46 025 záznamů
- `pocetZaznamuNaStrance` omezuje počet výsledků na stránce
- `stavDokumentuSbirky` může být "AKTUALNE_PLATNY" nebo jiná hodnota

---

## FormatSouboru Enum

Pro endpoint `/stahni/{typ}/{dokumentBaseId}/{formatSouboru}` jsou podporovány tyto formáty:

| Formát | Popis | Právě závazné | Informativní |
|--------|-------|---------------|--------------|
| DOCX   | Word  | ❌ Nepodporován | ✅ Podporován |
| PDF    | PDF   | ❌ Skeny      | ❌ Chyba |
| ZIP    | ZIP   | ✅ Async      | ✅ Async |
| JSON   | JSON  | ❌            | ❌ |
| XML    | XML   | ❌            | ❌ |

---

## Omezení

### 1. Pravě závazná znění
- Endpoint `/sbr-externi/stahni/pravne-zavazne-zneni/{dokumentBaseId}/PDF` vrací "Replika nenalezena"
- Textové PDF není dostupné pro pravě závazná znění

### 2. Informativní znění - dostupnost
- Některé zákony nemají dostupné informativní znění (vrací chybu nebo jiný stav)
- Starší zákony (před 1990) mohou mít omezenou dostupnost
- Nové zákony (2024+) mohou mít ještě nezpracované informativní znění

### 3. Async vs Sync
- ZIP formát vrací async `pozadavekId` pro pozdější stažení
- DOCX formát vrací synchronně `id` pro okamžité stažení
- Async download může selhat s "CHYBA" stavem

### 4. Rate Limiting
- e-Sbírka nemá explicitní rate limiting dokumentován
- Doporučuje se pauza mezi požadavky (1-2 sekundy)

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

Skript `stahni-docx.py` implementuje celý workflow:

```bash
# Stáhnout konkrétní zákon
python3 stahni-docx.py --url "/sb/1993/1/2026-01-01" --output ./zakony

# Hledat a stáhnout zákony
python3 stahni-docx.py --search "ústava" --limit 10 --output ./zakony
```

### Parametry

- `--url` - URL konkrétního zákona (např. `/sb/1993/1/2026-01-01`)
- `--search` - Text pro vyhledávání
- `--limit` - Maximální počet výsledků (výchozí: 10)
- `--output` - Cíl pro stažené soubory (výchozí: `./zakony`)

---

## Poznámky k zpracování DOCX

- DOCX soubory obsahují kompletní text zákona včetně novel
- Lze číst pomocí `python-docx` knihovny
- Text je dostupný jako `paragraphs` (odstavce)
- Formátování (nadpisy, odstavce) je zachováno

---

## Alternativní endpointy (nevyužité)

Následující endpointy existují, ale nejsou pro nás relevantní:

- `/stahni/overena-zneni/{dokumentId}` - Vrací scan PDF (ne text)
- `/stahni/pravne-zavazne-zneni/{dokumentId}/{formatSouboru}` - Vrací chybu "Replika nenalezena"
- `/stahni/informativni-zneni-porovnani/{staleUrl}/{srovnavaneStaleUrl}/{formatSouboru}` - Porovnání verzí

---

## Závěr

Pro embedování do Elasticsearchu doporučujeme informativní znění ve formátu DOCX:
- ✅ Textové, lze extrahovat
- ✅ Obsahuje kompletní znění se všemi novelami
- ✅ Oficiální konsolidovaná verze e-Sbírky
- ❌ Není to pravě závazná verze (ale pro vyhledávání a RAG stačí)
