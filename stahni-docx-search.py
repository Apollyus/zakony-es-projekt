#!/usr/bin/env python3
"""
Batch download DOCX z e-Sbírky - informativni zneni (roky <= 2023).
S podporou paralelniho stahovani.

Vzorek:
    python3 stahni-docx.py --search "" --output ./zakony
    python3 stahni-docx.py --search "" --output ./zakony --workers 10
    python3 stahni-docx.py --search "" --output ./zakony --pause 0.5 --workers 15

Funkcnost:
    - Automaticka paginace pres vsechny stranky search API
    - Filtr podle roku (predvolene: rok <= 2023, protoze 2024+ nemaji informativni zneni)
    - Paralelni stahovani DOCX souboru (Thread Pool)
    - Checkpointy ukladane po kazde zpracovane strance
    - Pokracovani po pretrzeni (Ctrl+C) tam, kde to skoncilo
    - Preskakovani uz stazenych souboru
"""

import argparse
import json
import sys
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote


BASE_URL = "https://e-sbirka.gov.cz"
MAX_RETRIES = 3
RETRY_DELAY = 2
BATCH_PAUSE = 1.5
SEARCH_PER_PAGE = 50
DEFAULT_MAX_YEAR = 2023
DEFAULT_WORKERS = 8


# Globalni lock pro tisk z vice vlaken
print_lock = threading.Lock()


def safe_print(msg=""):
    """Bezpecny tisk z vice vlaken."""
    with print_lock:
        print(msg)
        import sys
        sys.stdout.flush()


def load_state(state_path):
    """Nacte stav z disku."""
    if state_path.exists():
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_state(state_path, state):
    """Ulozi stav na disk."""
    try:
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"  VAROVANI: Nelze ulozit stav: {e}")


def api_get(url, retries=MAX_RETRIES):
    """Volani GET na e-Sbirka API s retry logikou."""
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
    """Volani POST na e-Sbirka API s retry logikou."""
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
    """Stahne DOCX soubor podle ID s retry logikou."""
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
                print(f"  Retry stahovani {attempt+1}/{MAX_RETRIES}: {e}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"  Chyba stahovani (po {MAX_RETRIES} pokusech): {e}")
                return 0


def get_dokument_base_id(stale_url):
    """Ziskava dokumentBaseId ze URL zakona."""
    encoded = quote(stale_url, safe='')
    url = f"{BASE_URL}/sbr-cache/dokumenty-sbirky/{encoded}"
    data = api_get(url)
    if data and 'chyby' not in data:
        return data.get('dokumentBaseId')
    return None


def get_docx_download_id(dokument_base_id):
    """Ziskava ID pro stazeni DOCX z informativni-zneni endpointu."""
    url = f"{BASE_URL}/sbr-externi/stahni/informativni-zneni/{dokument_base_id}/DOCX"
    data = api_get(url)
    if data:
        return data.get('id'), data.get('stavPozadavku'), data.get('nazevDokumentu')
    return None, None, None


def parse_zakon_nazev(kod_dokumentu):
    """Vytvori jmeno souboru z kodu zakona."""
    nazev = kod_dokumentu.replace(' Sb.', '').replace('/', '_')
    return nazev


def extract_year_from_kod(kod):
    """Vytahne rok z kodu zakona (napr. '1/1993 Sb.' -> 1993)."""
    parts = kod.split('/')
    if len(parts) >= 2:
        try:
            return int(parts[1].strip().split()[0])
        except (ValueError, IndexError):
            return None
    return None


def process_single_zakon(zakon, output_dir, dry_run=False):
    """Zpracuje jeden zakon (pro paralelni volani)."""
    kod = zakon['kod']
    nazev_zakona = zakon['nazev']
    rok = extract_year_from_kod(kod)
    rok_str = f" ({rok})" if rok else ""
    
    # Filtr roku
    if rok and rok > DEFAULT_MAX_YEAR:
        return {'kod': kod, 'nazev': nazev_zakona, 'result': 'skipped_year'}
    
    docx_id, stav, nazev = get_docx_download_id(zakon['dokumentBaseId'])
    
    if not docx_id:
        return {'kod': kod, 'nazev': nazev_zakona, 'result': 'no_docx_id'}
    
    if stav != 'OK':
        return {'kod': kod, 'nazev': nazev_zakona, 'result': 'stav_not_ok'}
    
    soubor_nazev = parse_zakon_nazev(kod)
    output_path = output_dir / f"{soubor_nazev}.docx"
    
    if output_path.exists() and not dry_run:
        return {'kod': kod, 'nazev': nazev_zakona, 'result': 'exists'}
    
    size = download_docx(docx_id, str(output_path), dry_run=dry_run)
    
    if size > 0:
        return {'kod': kod, 'nazev': nazev_zakona, 'result': 'success', 'size': size, 'soubor': soubor_nazev}
    else:
        return {'kod': kod, 'nazev': nazev_zakona, 'result': 'download_failed'}


def process_page(page_num, search_text, output_dir, dry_run=False, pause=BATCH_PAUSE, workers=DEFAULT_WORKERS):
    """Zpracuje jednu stranku search vysledku s paralelnim stahovanim."""
    result = api_post(
        f"{BASE_URL}/sbr-cache/jednoducha-vyhledavani",
        {"text": search_text, "stranka": page_num, "pocetZaznamuNaStrance": SEARCH_PER_PAGE}
    )
    
    if not result:
        return {'success': 0, 'skipped_year': 0, 'skipped_other': 0, 'total_on_page': 0}, 'api_error'
    
    seznam = result.get('seznam', [])
    if not seznam:
        return {'success': 0, 'skipped_year': 0, 'skipped_other': 0, 'total_on_page': 0}, 'no_results'
    
    # Ziskat dokumentBaseId pro vsechny
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
    
    if not zakony:
        return {'success': 0, 'skipped_year': 0, 'skipped_other': 0, 'total_on_page': 0}, 'ok'
    
    safe_print(f"\n[Stranka {page_num}] {len(zakony)} zakonů | workers={workers} | dry_run={dry_run}")
    
    # Paralelni zpracovani
    success = 0
    skipped_year = 0
    skipped_other = 0
    
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for i, zakon in enumerate(zakony, 1):
            rok = extract_year_from_kod(zakon['kod'])
            rok_str = f" ({rok})" if rok else ""
            safe_print(f"  [{i}/{len(zakony)}] {zakon['kod']}{rok_str} - {zakon['nazev'][:50]}...")
            
            future = executor.submit(process_single_zakon, zakon, output_dir, dry_run)
            futures[future] = zakon
        
        for future in as_completed(futures):
            res = future.result()
            result_type = res.get('result', 'unknown')
            
            if result_type == 'success':
                success += 1
                safe_print(f"    [OK] {res.get('soubor', '')}.docx ({res.get('size', 0)} bajtu)")
            elif result_type == 'skipped_year':
                skipped_year += 1
            else:
                skipped_other += 1
    
    return {
        'success': success,
        'skipped_year': skipped_year,
        'skipped_other': skipped_other,
        'total_on_page': len(zakony)
    }, 'ok'


def main():
    parser = argparse.ArgumentParser(description='Batch download DOCX z e-Sbirky s pagination a parallelism')
    parser.add_argument('--search', type=str, default='', help='Text pro vyhledavani (prazne = vsechny)')
    parser.add_argument('--url', type=str, help='URL konkretniho zakona')
    parser.add_argument('--year-to', type=int, default=DEFAULT_MAX_YEAR, help=f'Maksimalni rok (vychozi: {DEFAULT_MAX_YEAR})')
    parser.add_argument('--output', type=str, default='./zakony', help='Cil pro stazene soubory')
    parser.add_argument('--dry-run', action='store_true', help='Pouze vypsat co by se stahlo')
    parser.add_argument('--pause', type=float, default=BATCH_PAUSE, help='Pauza mezi stranami (sekundy, vychozi: 1.5)')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS, help=f'Počet paralelnich stahovani (vychozi: {DEFAULT_WORKERS})')
    
    args = parser.parse_args()
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    state_path = output_dir / ".download_state.json"
    state = load_state(state_path)
    
    if args.url:
        print(f"Stahuji konkretni zakon: {args.url}")
        if state and state.get('last_downloaded'):
            print(f"  Pozor: state file existuje")
        
        success = 0
        for i in range(1, 100):
            result, status = process_page(i, args.url, output_dir, args.dry_run, args.pause, args.workers)
            if status == 'no_results' or not result:
                break
            success += result.get('success', 0)
        
        print(f"\nHotovo: {success} souboru stazeno")
        sys.exit(0)
    
    # Batch mode s pagination
    print(f"\n{'='*70}")
    print(f"BATCH DOWNLOAD - e-Sbirka")
    print(f"{'='*70}")
    print(f"Search: '{args.search}'")
    print(f"Maximalni rok: {args.year_to}")
    print(f"Output: {output_dir.absolute()}")
    print(f"Workers: {args.workers} (paralelni stahovani)")
    print(f"Pauza mezi stranami: {args.pause}s")
    print(f"{'='*70}")
    
    if state is None:
        state = {
            'current_page': 1,
            'started': time.strftime('%Y-%m-%d %H:%M:%S'),
            'last_updated': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_success': 0,
            'total_skipped_year': 0,
            'total_skipped_other': 0,
            'last_downloaded': None,
            'pages_completed': 0
        }
        print(f"\n*** NOVY START ***")
    else:
        print(f"\n*** NALEZEN STATE - pokracuji od stranky {state['current_page']} ***")
        print(f"    Stazeno dosud: {state['total_success']} souboru")
        print(f"    Posledni aktualizace: {state['last_updated']}")
        if state.get('last_downloaded'):
            print(f"    Posledni soubor: {state['last_downloaded']}")
    
    # Hlavni cyklus paginace
    page = state['current_page']
    max_pages = 2000
    
    while page <= max_pages:
        result, status = process_page(page, args.search, output_dir, args.dry_run, args.pause, args.workers)
        
        if status == 'no_results' or not result:
            print(f"\n*** KONEC VYSLEDKU ***")
            break
        
        state['current_page'] = page + 1
        state['last_updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
        state['total_success'] += result.get('success', 0)
        state['total_skipped_year'] += result.get('skipped_year', 0)
        state['total_skipped_other'] += result.get('skipped_other', 0)
        state['pages_completed'] += 1
        
        if result.get('success') > 0:
            state['last_downloaded'] = f"page_{page}_success_{result['success']}"
        
        save_state(state_path, state)
        
        print(f"\n{'='*70}")
        print(f"STATISTIKA po strance {page}:")
        print(f"  Stazeno na strance: {result.get('success', 0)}")
        print(f"  Preskoceno (rok > {args.year_to}): {result.get('skipped_year', 0)}")
        print(f"  Celkem stazeno: {state['total_success']}")
        print(f"  Další strana: {page + 1}")
        print(f"{'='*70}")
        
        page += 1
        
        if page % 20 == 0:
            print(f"\n  Pauza 10s po 20 strankach...")
            time.sleep(10)
    
    if state_path.exists():
        state_path.unlink()
    
    print(f"\n{'='*70}")
    print(f"FINALNI VYSLEDEK:")
    print(f"  Celkem stazeno: {state['total_success']}")
    print(f"  Preskoceno (rok): {state['total_skipped_year']}")
    print(f"  Preskoceno (jine): {state['total_skipped_other']}")
    print(f"  Stran zpracovano: {state['pages_completed']}")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
