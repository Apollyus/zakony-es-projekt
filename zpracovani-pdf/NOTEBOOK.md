# Zakony-dluk - Notebook

## Účel
Stahovat všechny zákony ze státní sbírky ČR (e-Sbírka) ve strojově čitelné PDF podobě.

## API struktura
Základní zdroje:
- Hlavní web: https://e-sbirka.gov.cz
- OpenData API: https://opendata.eselpoint.gov.cz
- REST API (sbr-externi): https://e-sbirka.gov.cz/sbr-externi

### Datové sady OpenData (gzipped JSON)
| Soubor | Popis | Počet záznamů |
|--------|-------|---------------|
| 002PravniAkt.json.gz | Seznam právních aktů (zákonů) | ~46,018 |
| 006PravniAktMetadata.json.gz | Metadata právních aktů | ~46,524 |
| 010PravniAktBinarniSoubor.json.gz | Binární soubory (PDF, TIFF, JPEG, PNG) | ~45,860 |
| 043PravniAktDigitalniReplika.json.gz | Digitální repliky (overená zneni) | ~12,919 |

### Struktura 002PravniAkt
```json
{
  "typ": "právní-akt",
  "akt-citace": "8/1918 Sb.",
  "akt-rok-předpisu": 1918,
  "akt-číslo-předpisu": "8",
  "akt-sbírka-kód": "sb",
  "akt-název-vyhlášený": "...",
  "znění-dokument-id": [1, 60047, 60049]  // více verzí jednoho předpisu
}
```

### Struktura 010PravniAktBinarniSoubor
```json
{
  "typ": "právní-akt-binární-soubor",
  "binární-soubor-id": 778987,
  "binární-soubor-obsah": [
    {
      "obsah-url": "https://e-sbirka.gov.cz/sbr-externi/souborove-dokumenty/778987/ORIGINAL/STAHNI/...",
      "obsah-typ": "application/pdf",  // strojově čitelné PDF
      "cis-esb-typ-soubor-druh-obsahu-položka": "ORIGINAL"
    },
    {
      "obsah-typ": "image/tiff",  // scan papíru
      "cis-esb-typ-soubor-druh-obsahu-položka": "ORIGINAL"
    }
  ]
}
```

### Typy souborů
- `application/pdf` + `ORIGINAL` = strojově čitelné PDF (10,253 záznamů) ✓
- `image/jpeg` + `ORIGINAL` = scan papíru (starší zákony) ✗
- `image/tiff` + `ORIGINAL` = scan papíru (starší zákony) ✗
- `image/png` + `ORIGINAL` = scan papíru ✗
- `*/jpeg` + `NAHLED` = náhled (preview) ✗

### API endpointy pro stahování PDF
1. `GET /sbr-externi/stahni/overene-zneni/{znění-dokument-id}`
   - Vrací JSON: `{"pozadavekId": "...", "id": "...", "stavPozadavku": "OK", "nazevDokumentu": "Sb_1918_1_Castka_OZ.pdf"}`
   - `stavPozadavku` může být "OK" nebo "NEPRIPRAVEN"

2. `GET /souborove-sluzby/verejne-pozadavky-dokumenty/pozadavky/{pozadavekId}`
   - Vrací: `{"stav": "OK", "id": "uuid"}`

3. `GET /souborove-sluzby/soubory/{uuid}`
   - Vrací přímo PDF soubor (Content-Type: application/pdf)

## Strategii
1. Stáhnout 002PravniAkt.json.gz (~5MB)
2. Pro každý právní akt vybrat poslední znění (poslední prvek v `právní-akt-znění`)
3. Získat `znění-dokument-id` posledního znění
4. Zavolat API endpoint pro stažení PDF
5. Pokud API vrací "OK" → stáhnout PDF
6. Pokud API vrací "NEPRIPRAVEN" nebo chybu → přeskočit (scan papíru)

### Poznámky
- Právní akty mají více verzí znění (historické změny)
- Pro download potřebujeme posledního znění (nejaktuálnější verzi)
- Starší zákony (před ~1994) mohou být pouze jako skeny (TIFF/JPEG)
- API endpoint `/sbr-externi/stahni/overene-zneni/{id}` vrací JSON i pro skeny - musíme zkontrolovat content-type response

## Plán skriptu
1. Stáhnout 002PravniAkt.json.gz
2. Parsovat JSON (položky)
3. Seřadit podle roku a čísla předpisu
4. Vzít prvních N zákonů (pro test = 100)
5. Pro každý zákon:
   a. Získat znění-dokument-id posledního znění
   b. Zavolat overene-zneni endpoint
   c. Pokud OK, stáhnout PDF
   d. Uložit do data/{rok}_{cislo}.pdf
   e. Logovat úspěch/chybu

## Testovací výsledky (2026-08-06)
- Skript: `stahovack.py`
- Limit: 100 zákonů
- Výsledek:
  - PDF staženo: 77
  - Bez PDF (skeny): 0
  - Chyby: 0
  - Přeskočeno (už existovalo): 23
  - Celková velikost: 590 MB
  - Čas zpracování: ~166s (2.8 min)
- Pozorování:
  - Prvních 100 zákonů jsou převážně úřední vyhlášky z let 1945-1961
  - Všechny měly PDF k dispozici (starší zákony se zdají být digitalizované)
  - API endpoint vrací validní PDF pro všechny testované znění-dokument-id
  - Některé zákony měly stejný PDF název (duplikáty v datech)
vojtech@vojtech-HP-EliteBook-840-G6:~/zakony-dluk$ 
