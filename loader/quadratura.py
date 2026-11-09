#!/usr/bin/env python3
"""
quadratura.py

Verifica che le raccolte scaricate da downloader.py quadrino con la fonte.

Uso:
  python quadratura.py /data/rag
  python quadratura.py /data/rag -v        # elenca anche i singoli documenti mancanti

Per ogni source presente nella cartella target (normativa e giurisprudenza
sono suddivise per tipologia: i manifest delle sottocartelle vengono sommati):
  - controllo locale: ogni riga del manifest ha il suo file su disco (non
    vuoto) e ogni file su disco è citato nel manifest;
  - controllo remoto (solo fonti CeRDEF): per ogni anno, e per ogni autorità
    configurata, rilegge l'elenco completo dei risultati dal sito e lo
    confronta con il manifest. Vengono letti solo gli elenchi, non i singoli
    documenti, quindi è molto più rapido di uno scarico.

Le fonti di tipo "crawl" (es. agenzia-dogane-monopoli/circolari) non hanno un indice remoto con
cui confrontarsi: per quelle viene fatto solo il controllo locale.

Exit code: 0 se tutto quadra, 1 altrimenti.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

import downloader as d

CEDEF_CHANNEL = {
    "cedef-prassi": "prassi",
    "cedef-normativa": "normativa",
    "cedef-giurisprudenza": "giurisprudenza",
}


def guid_of(url: str) -> str:
    """GUID CeRDEF dal parametro id= dell'URL di dettaglio (stringa vuota se assente)."""
    ids = parse_qs(urlparse(url or "").query).get("id")
    return ids[0].upper() if ids else ""


def load_manifest(root: Path) -> list[dict]:
    """Ultima riga valida per documento, come fa Store._load_manifest."""
    out: dict[str, dict] = {}
    path = root / "manifest.jsonl"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.replace("\x00", "").strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            key = d.manifest_key(row)
            if key:
                out[key] = row
    return list(out.values())


def check_local(root: Path, rows: list[dict]) -> tuple[list[str], list[str]]:
    """Ritorna (righe di manifest senza file valido, file su disco non in manifest)."""
    referenced = set()
    broken = []
    for row in rows:
        rel = row.get("local_file") or ""
        path = root / rel
        if rel and path.is_file() and path.stat().st_size > 0:
            referenced.add(path.resolve())
        else:
            broken.append(f"{rel or '(senza local_file)'} <- {row.get('detail_url') or row.get('download_url')}")
    files_dir = root / "files"
    orphans = []
    if files_dir.is_dir():
        orphans = sorted(
            str(p.relative_to(root))
            for p in files_dir.rglob("*")
            if p.is_file() and p.resolve() not in referenced
        )
    return broken, orphans


def accepted(cfg: dict, estremi: str, titolo: str) -> bool:
    """Stessi filtri del downloader, applicati agli estremi dell'elenco."""
    doc = d.Doc(
        source="",
        title=titolo,
        authority=d._authority_from_estremi(estremi),
        document_type=d._doctype_from_estremi(estremi, "Prassi"),
    )
    return d.matches_document_type(doc, cfg) and d.matches_authority(doc, cfg)


def remote_listing(http: d.Http, search_url: str, form, cfg: dict, channel: str, year: int, authority):
    """Tutti i documenti (guid -> estremi/titolo) che il sito elenca per anno e autorità."""
    payload = d.build_form_payload(form, cfg, channel, year, authority)
    r = d.submit_form(http, search_url, form, payload)

    soup = d.soup_of(r)
    title = d.norm_text(soup.title.get_text(" ", strip=True)).lower() if soup.title else ""
    text = d.norm_text(soup.get_text(" ", strip=True)).lower()
    if "errore" in title or "si è verificato un errore" in text or "ambito di ricerca non valido" in text:
        raise RuntimeError("errore applicativo CeRDEF")

    found: dict[str, str] = {}
    while True:
        items, pagination = d.cedef_listing_from_html(r.text)
        new = 0
        for guid, estremi, titolo in items:
            g = guid.upper()
            if g in found:
                continue
            new += 1
            if channel != "prassi" or accepted(cfg, estremi, titolo):
                found[g] = d.norm_text(f"{estremi} {titolo}")
        # paginatorXml.do è stateful: ogni GET restituisce il batch successivo.
        if not pagination["ulteriori"] or not new:
            break
        nexts = list(d.cedef_next_links(BeautifulSoup(r.text, "html.parser"), r.url))
        r = http.get(nexts[0] if nexts else urljoin(r.url, "paginatorXml.do"))
    return found


def check_cedef(root: Path, source: str, cfg: dict, rows: list[dict], verbose: bool) -> bool:
    channel = CEDEF_CHANNEL[cfg["kind"]]
    local = {guid_of(row.get("detail_url", "")) for row in rows}
    local.discard("")

    http = d.Http()
    search = http.get(cfg.get("search_url") or cfg["home"])
    form = d.find_cedef_form(d.soup_of(search), channel)
    if form is None:
        print("  !! form di ricerca CeRDEF non trovata: controllo remoto non eseguito")
        return False

    authorities = d.cedef_authority_values(form, cfg, channel)
    current_year = datetime.now().year
    start_year = int(cfg.get("start_year", current_year))

    ok = True
    remote_all: set[str] = set()
    tot_remote = tot_missing = 0
    print(f"  {'anno':<6}{'sito':>7}{'locale':>8}{'mancanti':>10}")
    for year in range(start_year, current_year + 1):
        remote: dict[str, str] = {}
        try:
            for authority in authorities:
                remote.update(remote_listing(http, search.url, form, cfg, channel, year, authority))
        except Exception as e:
            print(f"  {year:<6} !! non verificabile: {e}")
            ok = False
            continue

        missing = {g: txt for g, txt in remote.items() if g not in local}
        remote_all.update(remote)
        tot_remote += len(remote)
        tot_missing += len(missing)
        flag = "" if not missing else "  <-- NON QUADRA"
        print(f"  {year:<6}{len(remote):>7}{len(remote) - len(missing):>8}{len(missing):>10}{flag}", flush=True)
        if missing:
            ok = False
            if verbose:
                for txt in sorted(missing.values()):
                    print(f"           - {txt[:160]}")

    extra = local - remote_all
    print(f"  {'TOTALE':<6}{tot_remote:>7}{tot_remote - tot_missing:>8}{tot_missing:>10}")
    if extra:
        # Non è un errore di scarico: documenti in archivio che il sito non elenca più
        # (o elenca sotto un'autorità/tipo fuori configurazione).
        print(f"  in archivio ma non elencati dal sito: {len(extra)}")
        if verbose:
            by_guid = {guid_of(row.get("detail_url", "")): row for row in rows}
            for g in sorted(extra):
                print(f"           - {by_guid[g].get('local_file')}")
    return ok


def check_source(target: Path, source: str, cfg: dict, verbose: bool) -> bool:
    root = target / source
    print()
    print("=" * 78)
    print(f"{source} - {cfg['label']}")
    print("=" * 78)

    # Le raccolte suddivise per tipologia hanno un manifest per sottocartella.
    if cfg.get("split_by_type"):
        roots = sorted(p.parent for p in root.glob("*/manifest.jsonl"))
        print(f"  tipologie: {len(roots)}")
    else:
        roots = [root]
    rows, broken, orphans = [], [], []
    for sub in roots:
        sub_rows = load_manifest(sub)
        sub_broken, sub_orphans = check_local(sub, sub_rows)
        prefix = "" if sub == root else sub.name + "/"
        rows += sub_rows
        broken += [prefix + x for x in sub_broken]
        orphans += [prefix + x for x in sub_orphans]
    print(f"  manifest: {len(rows)} documenti | senza file: {len(broken)} | file non in manifest: {len(orphans)}")
    for label, lst in (("senza file", broken), ("non in manifest", orphans)):
        for item in lst if verbose else lst[:5]:
            print(f"    {label}: {item}")
        if not verbose and len(lst) > 5:
            print(f"    ... altri {len(lst) - 5} (usa -v)")
    ok = not broken and not orphans

    if cfg["kind"] in CEDEF_CHANNEL:
        ok = check_cedef(root, source, cfg, rows, verbose) and ok
    else:
        print("  fonte senza indice remoto: eseguito solo il controllo locale")

    print(f"  ESITO {source}: {'QUADRA' if ok else 'NON QUADRA'}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description="Quadratura fra archivio locale e fonti di downloader.py.")
    p.add_argument("target", help="Directory radice usata come --target dal downloader")
    p.add_argument("-v", "--verbose", action="store_true", help="Elenca i singoli documenti che non quadrano")
    args = p.parse_args()

    target = Path(args.target).expanduser().resolve()
    sources = [name for name in d.SOURCES if (target / name).is_dir()]
    if not sources:
        print(f"Nessuna source trovata in {target}", file=sys.stderr)
        return 1

    results = {}
    for name in sources:
        try:
            results[name] = check_source(target, name, d.SOURCES[name], args.verbose)
        except KeyboardInterrupt:
            print("\nInterrotto.")
            return 130
        except Exception as e:
            print(f"  !! {name}: {e}")
            results[name] = False

    print()
    print("RIEPILOGO QUADRATURA")
    for name, ok in results.items():
        print(f"  {name:<34} {'QUADRA' if ok else 'NON QUADRA'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
