#!/usr/bin/env python3
"""
Stahovani DOCX dokumentu z e-Sbírky - informativni zneni.

Vzorek:
    python3 stahni-docx.py --search "" --limit 100 --output ./zakony
    python3 stahni-docx.py --url "/sb/1993/1/2026-01-01" --output ./zakony
    python3 stahni-docx.py --search "" --limit 50 --year-from 2000 --year-to 2010

Vyvojarske:
    python3 stahni-docx.py --search "" --limit 5 --dry-run  # pouze vypis bez stahovani
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote


BASE_URL = "https://e-sbirka.gov.cz"
MAX_RETRIES = 3
RETRY_DELAY = 2  # sekundy
BATCH_PAUSE = 1.5  # pauza mezi batch pozadavky


def api_get(url, retries=MAX_RETRIES):
    """Volání GET na e-Sbírka API s retry logikou."""
    for attempt in range(retries):
        req = Request(url, headers={
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        })
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (URLError, HTTPError) as e:
            if attempt < retries - 1:
                print(f"  Retry {attempt+1}/{retries}: {e}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"  Chyba API (po {retries} pokusech): {e}")
                return None


def api_post(url, data, retries=MAX_RETRIES):
    """Volání POST na e-Sbírka API s retry logikou."""
    for attempt in range(retries):
        req = Request(url, data=json.dumps(data).encode(), headers={
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        })
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (URLError, HTTPError) as e:
            if attempt < retries - 1:
                print(f"  Retry {attempt+1}/{retries}: {e}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"  Chyba API (po {retries} pokusech): {e}")
                return None


def download_docx(docx_id, output_path, dry_run=False):
    """Stáhne DOCX soubor podle ID s retry logikou."""
    url = f"{BASE_URL}/souborove-sluzby/soubory/{docx_id}"
    for attempt in range(MAX_RETRIES):
        req = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        })
        try:
            with urlopen(req, timeout=60) as resp:
                content = resp.read()
                if not dry_run:
                    with open(output_path, 'wb') as f:
                        f.write(content)
                return len(content)
        except (URLError, HTTPError) as e:
            if attempt < MAX_RETRIES - 1:
                print(f"  Retry stahování {attempt+1}/{MAX_RETRIES}: {e}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"  Chyba stahování (po {MAX_RETRIES} pokusech): {e}")
                return 0


def get_dokument_base_id(stale_url):
    """Získá dokumentBaseId ze URL zákona."""
    encoded = quote(stale_url, safe='')
    url = f"{BASE_URL}/sbr-cache/dokumenty-sbirky/{encoded}"
    data = api_get(url)
    if data and 'chyby' not in data:
        return data.get('dokumentBaseId')
    return None


def get_docx_download_id(dokument_base_id):
    """Získá ID pro stažení DOCX z informativni-zneni endpointu."""
    url = f"{BASE_URL}/sbr-externi/stahni/informativni-zneni/{dokument_base_id}/DOCX"
    data = api_get(url)
    if data:
        return data.get('id'), data.get('stavPozadavku'), data.get('nazevDokumentu')
    return None, None, None


def parse_zakon_nazev(kod_dokumentu):
    """Vytvoří jméno souboru z kódu zákona."""
    nazev = kod_dokumentu.replace(' Sb.', '').replace('/', '_')
    return nazev


def extract_year_from_kod(kod):
    """Vytáhne rok z kódu zákona (např. '1/1993 Sb.' -> 1993)."""
    parts = kod.split('/')
    if len(parts) >= 2:
        try:
            return int(parts[1].strip().split()[0])
        except (ValueError, IndexError):
            return None
    return None


def process_single_zakon(stale_url, output_dir, dry_run=False, pause=BATCH_PAUSE):
    """Zpracuje jeden zákon."""
    print(f"\n[{stale_url}]")
    
    dokument_base_id = get_dokument_base_id(stale_url)
    if not dokument_base_id:
        print(f"  PŘESKOČENO: Nelze získat dokumentBaseId")
        time.sleep(pause)
        return False
    
    print(f"  dokumentBaseId: {dokument_base_id}")
    
    docx_id, stav, nazev = get_docx_download_id(dokument_base_id)
    if not docx_id:
        print(f"  PŘESKOČENO: Nelze získat DOCX download ID")
        time.sleep(pause)
        return False
    
    print(f"  Stav: {stav}, Název: {nazev}")
    
    if stav != 'OK':
        print(f"  PŘESKOČENO: Stav není OK ({stav})")
        time.sleep(pause)
        return False
    
    output_path = output_dir / nazev
    
    # Resume - pokud soubor existuje, přeskočit
    if output_path.exists() and not dry_run:
        print(f"  UŽExistuje: {output_path} (přeskočeno)")
        time.sleep(pause)
        return True
    
    size = download_docx(docx_id, str(output_path), dry_run=dry_run)
    
    if size > 0:
        if dry_run:
            print(f"  [DRY-RUN] Byste stáhli: {output_path} ({size} bajtů)")
        else:
            print(f"  STAŽENO: {output_path} ({size} bajtů)")
        time.sleep(pause)
        return True
    else:
        print(f"  CHYBA: Nepodařilo se stáhnout")
        time.sleep(pause)
        return False


def process_search(search_text, limit, output_dir, year_from=None, year_to=None, dry_run=False, pause=BATCH_PAUSE):
    """Zpracuje search výsledky s ročním filtrem."""
    print(f"\nHledám: '{search_text}' (max {limit} výsledků)")
    if year_from or year_to:
        print(f"  Filtr roků: {year_from or 'min'} - {year_to or 'max'}")
    
    result = api_post(
        f"{BASE_URL}/sbr-cache/jednoducha-vyhledavani",
        {"text": search_text, "stranka": 1, "pocetZaznamuNaStrance": limit}
    )
    
    if not result:
        print("Chyba search API")
        return 0
    
    seznam = result.get('seznam', [])
    if not seznam:
        print("Žádné výsledky")
        return 0
    
    print(f"Nalezeno: {len(seznam)} výsledků")
    
    # Filtr podle roku
    if year_from or year_to:
        filtered = []
        for item in seznam:
            rok = extract_year_from_kod(item.get('kodDokumentuSbirky', ''))
            if rok:
                if (year_from is None or rok >= year_from) and (year_to is None or rok <= year_to):
                    filtered.append(item)
        print(f"Po filtru roků: {len(filtered)} výsledků")
        seznam = filtered[:limit]
    else:
        seznam = seznam[:limit]
    
    # Získat dokumentBaseId pro všechny
    zakony = []
    for item in seznam:
        stale_url = item.get('staleUrl')
        kod = item.get('kodDokumentuSbirky', '')
        if not stale_url:
            continue
        
        dokument_base_id = get_dokument_base_id(stale_url)
        if dokument_base_id:
            zakony.append({
                'staleUrl': stale_url,
                'kod': kod,
                'nazev': item.get('nazev', ''),
                'dokumentBaseId': dokument_base_id
            })
    
    print(f"K zpracování: {len(zakony)} zákonů")
    
    # Stáhnout DOCX
    success = 0
    skipped = 0
    failed = 0
    
    for i, zakon in enumerate(zakony, 1):
        kod = zakon['kod']
        nazev_zakona = zakon['nazev']
        rok = extract_year_from_kod(kod)
        rok_str = f" ({rok})" if rok else ""
        
        print(f"\n[{i}/{len(zakony)}] {kod}{rok_str} - {nazev_zakona}")
        
        docx_id, stav, nazev = get_docx_download_id(zakon['dokumentBaseId'])
        
        if not docx_id:
            print(f"  PŘESKOČENO: Nelze získat DOCX download ID")
            skipped += 1
            continue
        
        if stav != 'OK':
            print(f"  PŘESKOČENO: Stav není OK ({stav})")
            skipped += 1
            continue
        
        soubor_nazev = parse_zakon_nazev(kod)
        output_path = output_dir / f"{soubor_nazev}.docx"
        
        if output_path.exists() and not dry_run:
            print(f"  UŽExistuje: {soubor_nazev}.docx (přeskočeno)")
            skipped += 1
            time.sleep(pause)
            continue
        
        size = download_docx(docx_id, str(output_path), dry_run=dry_run)
        
        if size > 0:
            if dry_run:
                print(f"  [DRY-RUN] Byste stáhli: {soubor_nazev}.docx ({size} bajtů)")
            else:
                print(f"  STAŽENO: {soubor_nazev}.docx ({size} bajtů)")
            success += 1
        else:
            print(f"  CHYBA: Nepodařilo se stáhnout")
            failed += 1
        
        time.sleep(pause)
    
    print(f"\n{'='*60}")
    print(f"Výsledky batch downloadu:")
    print(f"  Úspěšně: {success}")
    print(f"  Přeskočeno: {skipped}")
    print(f"  Chyby: {failed}")
    print(f"{'='*60}")
    
    return success


def main():
    parser = argparse.ArgumentParser(description='Stahovani DOCX z e-Sbírky')
    parser.add_argument('--search', type=str, default='', help='Text pro vyhledávání (prázné = všechny)')
    parser.add_argument('--url', type=str, help='URL konkrétního zákona')
    parser.add_argument('--limit', type=int, default=50, help='Maximální počet výsledků (výchozí: 50)')
    parser.add_argument('--output', type=str, default='./zakony', help='Cíl pro stažené soubory')
    parser.add_argument('--year-from', type=int, help='Minimální rok (např. 2000)')
    parser.add_argument('--year-to', type=int, help='Maximální rok (např. 2024)')
    parser.add_argument('--dry-run', action='store_true', help='Pouze vypsat co by se stáhlo')
    parser.add_argument('--pause', type=float, default=BATCH_PAUSE, help='Pauza mezi pozadavky (sekundy, výchozí: 1.5)')
    
    args = parser.parse_args()
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if args.url:
        success = process_single_zakon(args.url, output_dir, dry_run=args.dry_run, pause=args.pause)
        sys.exit(0 if success else 1)
    elif args.search or not args.url:
        success_count = process_search(
            search_text=args.search,
            limit=args.limit,
            output_dir=output_dir,
            year_from=args.year_from,
            year_to=args.year_to,
            dry_run=args.dry_run,
            pause=args.pause
        )
        print(f"\nHotovo: {success_count} souborů staženo")
        sys.exit(0)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
