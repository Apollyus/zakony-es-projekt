#!/usr/bin/env python3
"""
Stahovani DOCX dokumentu z e-Sbírky.

Vzorek:
    python3 stahni-docx.py --search "ústava" --limit 5 --output ./zakony

Nebo konkrétní zákon:
    python3 stahni-docx.py --url "/sb/1993/1/2026-01-01" --output ./zakony
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


def api_get(url):
    """Volání GET na e-Sbírka API."""
    req = Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
    })
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except (URLError, HTTPError) as e:
        print(f"  Chyba API: {e}")
        return None


def api_post(url, data):
    """Volání POST na e-Sbírka API."""
    req = Request(url, data=json.dumps(data).encode(), headers={
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
    })
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except (URLError, HTTPError) as e:
        print(f"  Chyba API: {e}")
        return None


def get_dokument_base_id(stale_url):
    """Získá dokumentBaseId ze URL zákona."""
    encoded = quote(stale_url, safe='')
    url = f"https://e-sbirka.gov.cz/sbr-cache/dokumenty-sbirky/{encoded}"
    data = api_get(url)
    if data and 'chyby' not in data:
        return data.get('dokumentBaseId')
    return None


def get_docx_download_id(dokument_base_id):
    """Získá ID pro stažení DOCX z informativni-zneni endpointu."""
    url = f"https://e-sbirka.gov.cz/sbr-externi/stahni/informativni-zneni/{dokument_base_id}/DOCX"
    data = api_get(url)
    if data:
        return data.get('id'), data.get('stavPozadavku'), data.get('nazevDokumentu')
    return None, None, None


def download_docx(docx_id, output_path):
    """Stáhne DOCX soubor podle ID."""
    url = f"https://e-sbirka.gov.cz/souborove-sluzby/soubory/{docx_id}"
    req = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
    })
    try:
        with urlopen(req, timeout=60) as resp:
            content = resp.read()
            with open(output_path, 'wb') as f:
                f.write(content)
            return len(content)
    except (URLError, HTTPError) as e:
        print(f"  Chyba stahování: {e}")
        return 0


def parse_zakon_nazev(kod_dokumentu, stale_url):
    """Vytvoří jméno souboru z kódu zákona."""
    # Převede "1/1993 Sb." na "1_1993"
    nazev = kod_dokumentu.replace(' Sb.', '').replace('/', '_')
    return nazev


def process_single_zakon(stale_url, output_dir):
    """Zpracuje jeden zákon."""
    print(f"\nZpracovávám: {stale_url}")
    
    # Získat dokumentBaseId
    dokument_base_id = get_dokument_base_id(stale_url)
    if not dokument_base_id:
        print(f"  PŘESKOČENO: Nelze získat dokumentBaseId pro {stale_url}")
        return False
    
    print(f"  dokumentBaseId: {dokument_base_id}")
    
    # Získat DOCX download ID
    docx_id, stav, nazev = get_docx_download_id(dokument_base_id)
    if not docx_id:
        print(f"  PŘESKOČENO: Nelze získat DOCX download ID")
        return False
    
    print(f"  Stav: {stav}, Název: {nazev}")
    
    if stav != 'OK':
        print(f"  PŘESKOČENO: Stav není OK ({stav})")
        return False
    
    # Stáhnout DOCX
    output_path = output_dir / f"{nazev}"
    size = download_docx(docx_id, str(output_path))
    
    if size > 0:
        print(f"  STAŽENO: {output_path} ({size} bajtů)")
        return True
    else:
        print(f"  CHYBA: Nepodařilo se stáhnout")
        return False


def process_search(search_text, limit, output_dir):
    """Zpracuje search výsledky."""
    print(f"\nHledám: '{search_text}' (max {limit} výsledků)")
    
    # Search API - použít pocetZaznamuNaStrance pro omezení počtu výsledků
    result = api_post(
        "https://e-sbirka.gov.cz/sbr-cache/jednoducha-vyhledavani",
        {"text": search_text, "stranka": 1, "pocetZaznamuNaStrance": limit}
    )
    
    if not result:
        print("Chyba search API")
        return 0
    
    seznam = result.get('seznam', [])
    if not seznam:
        print("Žádné výsledky")
        return 0
    
    print(f"Nalezeno: {len(seznam)} výsledků (vybráno prvních {limit})")
    
    # Pro každý výsledek získat dokumentBaseId
    zakony = []
    for item in seznam[:limit]:
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
            print(f"  {kod} -> dokumentBaseId: {dokument_base_id}")
        else:
            print(f"  {kod} -> ŽÁDNÝ dokumentBaseId (přeskočeno)")
    
    print(f"\nK zpracování: {len(zakony)} zákonů")
    
    # Stáhnout DOCX
    success = 0
    for i, zakon in enumerate(zakony, 1):
        print(f"\n[{i}/{len(zakony)}] {zakon['kod']} - {zakon['nazev']}")
        
        docx_id, stav, nazev = get_docx_download_id(zakon['dokumentBaseId'])
        
        if not docx_id:
            print(f"  PŘESKOČENO: Nelze získat DOCX download ID")
            continue
        
        if stav != 'OK':
            print(f"  PŘESKOČENO: Stav není OK ({stav})")
            continue
        
        # Vytvořit jméno souboru
        soubor_nazev = parse_zakon_nazev(zakon['kod'], zakon['staleUrl'])
        output_path = output_dir / f"{soubor_nazev}.docx"
        
        size = download_docx(docx_id, str(output_path))
        
        if size > 0:
            print(f"  STAŽENO: {output_path} ({size} bajtů)")
            success += 1
        else:
            print(f"  CHYBA: Nepodařilo se stáhnout")
    
    return success


def main():
    parser = argparse.ArgumentParser(description='Stahovani DOCX z e-Sbírky')
    parser.add_argument('--search', type=str, help='Text pro vyhledávání')
    parser.add_argument('--url', type=str, help='URL konkrétního zákona (např. /sb/1993/1/2026-01-01)')
    parser.add_argument('--limit', type=int, default=10, help='Maximální počet výsledků (výchozí: 10)')
    parser.add_argument('--output', type=str, default='./zakony', help='Cíl pro stažené soubory')
    
    args = parser.parse_args()
    
    # Vytvořit output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if args.url:
        success = process_single_zakon(args.url, output_dir)
        sys.exit(0 if success else 1)
    elif args.search:
        success_count = process_search(args.search, args.limit, output_dir)
        print(f"\nHotovo: {success_count}/{args.limit} souborů staženo")
        sys.exit(0)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
