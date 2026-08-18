#!/usr/bin/env python3
"""
Extrakce textu z DOCX souborů stažených z e-Sbírky.

Výstup: JSON s metadaty a textem pro embedování do Elasticsearchu.

Použití:
    python3 extrahuj-docx.py --input ./zakony --output ./zakony_json
    python3 extrahuj-docx.py --file ./zakony/1_1993.docx --output ./zakony_json
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from docx import Document
except ImportError:
    print("Chyba: python-docx není nainstalován")
    print("Instalace: pip install python-docx")
    sys.exit(1)


def extract_text_from_docx(file_path):
    """Extrahuje text a strukturu z DOCX souboru."""
    doc = Document(str(file_path))
    
    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append({
                'text': text,
                'style': para.style.name if para.style else '',
                'heading_level': _get_heading_level(para.style.name)
            })
    
    return {
        'paragraphs': paragraphs,
        'full_text': '\n\n'.join(p['text'] for p in paragraphs),
        'paragraph_count': len(paragraphs)
    }


def _get_heading_level(style_name):
    """Získá úroveň nadpisu ze stylu."""
    if not style_name:
        return 0
    match = re.search(r'Nadpis (\d)', style_name)
    if match:
        return int(match.group(1))
    return 0


def extract_metadata(file_path):
    """Extrahuje metadata z názvu souboru."""
    stem = file_path.stem
    
    # Parse "1_1993" -> number=1, year=1993
    parts = stem.split('_')
    if len(parts) >= 2:
        return {
            'number': parts[0],
            'year': parts[1],
            'source': 'e-sbirka',
            'version': 'informativni-zneni'
        }
    return {
        'source': 'e-sbirka',
        'version': 'informativni-zneni'
    }


def process_file(file_path, output_dir):
    """Zpracuje jeden DOCX soubor."""
    print(f"\nZpracovávám: {file_path.name}")
    
    # Extrahovat text
    extracted = extract_text_from_docx(file_path)
    
    # Extrahovat metadata
    metadata = extract_metadata(file_path)
    
    # Vytvořit dokument pro Elasticsearch
    doc = {
        'metadata': metadata,
        'file_name': file_path.name,
        'file_path': str(file_path),
        'extracted': {
            'paragraph_count': extracted['paragraph_count'],
            'full_text_length': len(extracted['full_text']),
            'full_text': extracted['full_text'],
            'paragraphs': extracted['paragraphs'][:50]  # Prvních 50 odstavců pro náhled
        }
    }
    
    # Uložit JSON
    json_path = output_dir / f"{file_path.stem}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    
    print(f"  Odstavců: {extracted['paragraph_count']}")
    print(f"  Délka textu: {len(extracted['full_text'])} znaků")
    print(f"  Uloženo: {json_path}")
    
    return doc


def process_directory(input_dir, output_dir):
    """Zpracuje všechny DOCX soubory v adresáři."""
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    docx_files = sorted(input_path.glob('*.docx'))
    
    if not docx_files:
        print(f"Nebyly nalezeny žádné DOCX soubory v {input_dir}")
        return
    
    print(f"Nalezeno {len(docx_files)} DOCX souborů")
    
    results = []
    for i, file_path in enumerate(docx_files, 1):
        print(f"\n[{i}/{len(docx_files)}] {file_path.name}")
        doc = process_file(file_path, output_path)
        results.append(doc)
    
    # Vytvořit souhrn
    summary = {
        'total_files': len(docx_files),
        'total_paragraphs': sum(d['extracted']['paragraph_count'] for d in results),
        'total_text_length': sum(d['extracted']['full_text_length'] for d in results),
        'files': [r['file_name'] for r in results]
    }
    
    summary_path = output_path / 'summary.json'
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'='*60}")
    print(f"SOUHRN:")
    print(f"  Souborů: {summary['total_files']}")
    print(f"  Odstavců: {summary['total_paragraphs']}")
    print(f"  Celková délka textu: {summary['total_text_length']} znaků")
    print(f"{'='*60}")
    
    return results


def main():
    parser = argparse.ArgumentParser(description='Extrakce textu z DOCX souborů e-Sbírky')
    parser.add_argument('--input', '-i', type=str, default='./zakony', help='Vstupní adresář s DOCX')
    parser.add_argument('--output', '-o', type=str, default='./zakony_json', help='Výstupní adresář pro JSON')
    parser.add_argument('--file', '-f', type=str, help='Jeden DOCX soubor')
    
    args = parser.parse_args()
    
    if args.file:
        file_path = Path(args.file)
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        process_file(file_path, output_dir)
    else:
        process_directory(args.input, args.output)


if __name__ == '__main__':
    main()
