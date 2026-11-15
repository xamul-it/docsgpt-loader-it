#!/usr/bin/env python3
"""
Ripara i metadati dei manifest CeRDEF di giurisprudenza e normativa (issue #2)
senza riscaricare i documenti:

  - authority: il giudice o l'organo emanante, dagli estremi;
  - title: gli estremi (tipo, data, numero, giudice) al posto dei segnaposto
    "Nessuna intitolazione presente" e "Non disponibile";
  - il file si rinomina come lo chiamerebbe oggi downloader.py, col giudice o
    l'organo nel nome (DocsGPT mostra il nome del file, non il manifest).

Gli estremi si leggono dai file HTML già su disco; per i PDF si rilegge solo
la pagina di dettaglio. Si può interrompere e rilanciare: il manifest si
riscrive a blocchi e un file già rinominato viene ritrovato.

Uso:
  python3 ripara_metadati.py --target /mnt/Multimedia/circolari --dry-run
  python3 ripara_metadati.py --target /mnt/Multimedia/circolari --source giurisprudenza/sentenza
  python3 ripara_metadati.py --target /mnt/Multimedia/circolari --workers 4 --rate 3

Dopo la riparazione le sorgenti giurisprudenza e normativa vanno ricaricate
in DocsGPT (carica_docsgpt.py --replace).
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import os
import re
from collections import Counter
from dataclasses import asdict, fields
from pathlib import Path
from typing import Optional

import downloader as d

CHANNELS = ("giurisprudenza", "normativa")
# Righe tra un salvataggio del manifest e l'altro.
CHECKPOINT = 1000

_ESTREMI_IN_HTML = re.compile(r"</h1>\s*<p>(.*?)</p>", re.S)
_DOC_FIELDS = {f.name for f in fields(d.Doc)}


def estremi_from_file(path: Path) -> Optional[str]:
    """Estremi dal documento HTML sintetico di downloader.py ("<h1>titolo</h1><p>estremi</p>")."""
    if path.suffix != ".html" or not path.is_file():
        return None
    with path.open("r", encoding="utf-8", errors="replace") as f:
        head = f.read(20000)
    m = _ESTREMI_IN_HTML.search(head)
    return d.norm_text(html_lib.unescape(m.group(1))) if m else None


def locate(store: d.Store, row: dict, key: str) -> Optional[Path]:
    """File del documento; se manca, quello già rinominato da un'esecuzione interrotta.

    choose_path chiude ogni nome con "__<sha1 dell'URL>": lo si cerca nella
    stessa cartella.
    """
    if not row.get("local_file"):
        return None
    path = store.root / row["local_file"]
    if path.exists():
        return path
    digest = hashlib.sha1((row.get("download_url") or row.get("detail_url") or key).encode("utf-8")).hexdigest()[:10]
    found = list(path.parent.glob(f"*__{digest}{path.suffix}"))
    return found[0] if len(found) == 1 else path


def from_detail(http, channel: str, url: str) -> tuple[str, str]:
    """(estremi, enti) dalla pagina di dettaglio CeRDEF."""
    html = http.get(url).text
    if channel == "giurisprudenza":
        root = d.parse_embedded_xml(d.extract_js_xml_var(html, "xmlDettaglio"))
        prov = root.find(".//Provvedimento") if root is not None else None
        if prov is None:
            raise RuntimeError("dettaglio senza xmlDettaglio")
        return d.norm_text(prov.findtext("estremi") or ""), ""
    root = d.parse_embedded_xml(d.extract_js_xml_var(html, "xmlSommario"))
    if root is None:
        raise RuntimeError("dettaglio senza xmlSommario")
    enti = "; ".join(d.norm_text(e.text) for e in root.iterfind("intestazione/enti/ente") if d.norm_text(e.text))
    return d.norm_text(root.findtext("intestazione/estremiAtto") or ""), enti


def repaired(row: dict, estremi: str, enti: str = "") -> dict:
    new = dict(row)
    new["authority"] = d.organ_from_estremi(estremi) or enti or row.get("authority", "")
    if d.norm_text(row.get("title", "")).lower() in d.PLACEHOLDER_TITLES and estremi:
        new["title"] = estremi
    return new


def ripara_store(store: d.Store, http, channel: str, dry_run: bool = False) -> Counter:
    stats = Counter()
    rows = list(store.existing.items())

    # 1. Estremi: dal file se c'è, altrimenti dalla pagina di dettaglio.
    estremi: dict[str, tuple[str, str]] = {}
    remote = []
    for key, row in rows:
        path = locate(store, row, key)
        local = estremi_from_file(path) if path is not None else None
        if local is None:
            remote.append((key, row))
        else:
            estremi[key] = (local, "")

    def fetch(job):
        key, row = job
        try:
            return key, from_detail(http, channel, row.get("detail_url") or key), None
        except Exception as e:
            return key, None, e

    for key, value, err in http.map(fetch, remote):
        if err is not None:
            stats["errori"] += 1
            store.error(f"ripara_metadati {key}: {err}")
            continue
        stats["letti_dal_sito"] += 1
        estremi[key] = value

    # 2. Metadati e nome del file.
    pending = 0
    for key, row in rows:
        if key not in estremi:
            continue
        if not estremi[key][0]:
            stats["senza_estremi"] += 1
            continue
        new = repaired(row, *estremi[key])
        doc = d.Doc(**{k: v for k, v in new.items() if k in _DOC_FIELDS})
        old_path = locate(store, row, key)
        if old_path is not None:
            out = store.choose_path(doc.title, doc.document_date, row.get("download_url") or row.get("detail_url") or key,
                                    old_path.suffix, d.file_prefix(doc))
            new["local_file"] = str(out.relative_to(store.root))
        if new == row:
            continue
        stats["aggiornati"] += 1
        if new.get("local_file") != row.get("local_file"):
            stats["rinominati"] += 1
        if dry_run:
            continue
        if old_path is not None and old_path != out:
            if old_path.exists():
                os.replace(old_path, out)
            elif not out.exists():
                stats["file_mancanti"] += 1
                store.error(f"ripara_metadati {key}: file mancante {row['local_file']}")
                continue
        store.existing[key] = new
        pending += 1
        if pending >= CHECKPOINT:
            store.compact()
            pending = 0
    if pending and not dry_run:
        store.compact()
    return stats


def stores_under(target: Path, source: str) -> list[tuple[str, str]]:
    """(source, canale) di ogni manifest sotto la raccolta indicata."""
    out = []
    for manifest in sorted((target / source).glob("**/manifest.jsonl")):
        rel = manifest.parent.relative_to(target).as_posix()
        channel = rel.split("/")[0]
        if channel in CHANNELS:
            out.append((rel, channel))
    return out


def main():
    p = argparse.ArgumentParser(description="Ripara authority e titoli dei manifest di giurisprudenza e normativa.")
    p.add_argument("--target", required=True, help="Radice dell'archivio")
    p.add_argument("--source", nargs="*", default=list(CHANNELS),
                   help="Raccolte o tipologie (default: giurisprudenza normativa)")
    p.add_argument("--dry-run", action="store_true", help="Conta le modifiche senza toccare file e manifest")
    p.add_argument("--workers", type=int, default=d.WORKERS)
    p.add_argument("--rate", type=float, default=d.RATE)
    args = p.parse_args()
    d.WORKERS, d.RATE = args.workers, args.rate

    target = Path(args.target)
    http = d.Http()
    total = Counter()
    for source in args.source:
        if source.split("/")[0] not in CHANNELS:
            p.error(f"{source}: solo giurisprudenza e normativa")
        for name, channel in stores_under(target, source):
            store = d.Store(target, name, "refresh")
            stats = ripara_store(store, http, channel, dry_run=args.dry_run)
            total.update(stats)
            print(f"{name}: {len(store.existing)} documenti, " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items())), flush=True)
    print(("PROVA - " if args.dry_run else "") + "totale: " + ", ".join(f"{k} {v}" for k, v in sorted(total.items())))


if __name__ == "__main__":
    main()
