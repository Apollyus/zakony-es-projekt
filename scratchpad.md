# Scratchpad - notes a plány

## 2026-08-18
- Vytvořena branch: batch-download-docx
- ✅ Vylepšen stahni-docx.py: retry logic, pause, resume, dry-run, year filter
- ✅ Vytvořen extrahuj-docx.py pro extrakci textu z DOCX
- ✅ Batch staženo 37 zákonů (20 starších + 17 nových)
- ✅ Extrahováno 5080 odstavců, 622K znaků
- 2024+ zákony nemají informativni zneni (API vrací 400)

## TODO
- [ ] Zkusit batch stáhnout více zákonů (jiný search query)
- [ ] Přidat ingest do Elasticsearchu
- [ ] Zkontrolovat coverage pro různé roky
- [ ] Vytvořit chunking strategii pro dlouhé zákony
