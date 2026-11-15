#!/usr/bin/env python3
"""Testo utile delle pagine HTML di un archivio curato (issue #3).

Per ogni voce html dei manifest sotto <cartella>/*/manifest.jsonl misura il
testo totale e quello "utile": tolti script, menu, intestazioni, piè di pagina,
moduli e barre laterali, e preso <main>/<article> quando c'è. Segnala anche i
campi password e le pagine a frame (login o registrazione). downloader.py usa
misura() e sospetti() per segnalare le voci curate sospette.

Due protezioni contro l'errore opposto, cioè togliere il documento stesso:
- non si toglie mai un antenato di <main>/<article> (es. il layout Liferay della
  Regione Toscana con classe "rt-layout-sidebar" attorno a tutto il testo);
- non si toglie un blocco che contiene almeno metà del testo della pagina (es. le
  pagine ASP.NET dove <form id="form1"> avvolge tutto il body).

  python3 misura_testo_utile.py /mnt/Multimedia/circolari/locazioni
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

# Classi, id o role che indicano navigazione e contorno della pagina.
CONTORNO = re.compile(
    r"(^|[-_ ])(nav|navbar|menu|breadcrumbs?|footer|header|cookie|sidebar|skip|social|share)([-_ ]|$)", re.I
)
TAG_CONTORNO = ("nav", "header", "footer", "aside", "form")
QUOTA_DOCUMENTO = 0.5
# Sotto questa soglia di testo utile una pagina è sospetta: sui 36 HTML delle
# locazioni (2026-11-15) le pagine d'indice o vuote stanno sotto 750, quelle
# buone sopra 1.600.
SOGLIA_UTILE = 1000


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Misura:
    totale: int
    utile: int
    password: bool
    main: bool
    frame: bool


def misura(html: bytes | str) -> Misura:
    soup = BeautifulSoup(html, "html.parser")
    password = bool(soup.select("input[type=password]"))
    frame = soup.find(["frameset", "frame"]) is not None
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    totale = len(_norm(soup.get_text(" ")))

    corpo = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    protetti = {id(corpo), *(id(a) for a in corpo.parents)} if corpo else set()

    candidati = []
    for t in soup.find_all(True):
        attrs = " ".join(t.get("class") or []) + " " + (t.get("id") or "") + " " + (t.get("role") or "")
        if t.name in TAG_CONTORNO or CONTORNO.search(attrs):
            candidati.append(t)
    for t in candidati:
        if t.attrs is None or id(t) in protetti:  # già tolto con un antenato, o contiene il documento
            continue
        if totale and len(_norm(t.get_text(" "))) >= QUOTA_DOCUMENTO * totale:
            continue
        t.decompose()

    utile = len(_norm((corpo or soup).get_text(" ")))
    return Misura(totale=totale, utile=utile, password=password, main=corpo is not None, frame=frame)


def sospetti(m: Misura) -> list[str]:
    """Motivi per cui la pagina sembra non essere il documento (vuoto se nessuno)."""
    motivi = []
    if m.utile < SOGLIA_UTILE:
        motivi.append(f"testo utile {m.utile} caratteri (sotto {SOGLIA_UTILE})")
    if m.password:
        motivi.append("campo password (login o registrazione)")
    if m.frame:
        motivi.append("pagina a frame")
    return motivi


def voci_html(radice: Path):
    for manifest in sorted(radice.glob("*/manifest.jsonl")):
        for riga in manifest.read_text(encoding="utf-8").splitlines():
            if not riga.strip():
                continue
            r = json.loads(riga)
            if r.get("local_file", "").endswith(".html"):
                yield manifest.parent.name, r, manifest.parent / r["local_file"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cartella", type=Path, help="radice dell'archivio (contiene <sorgente>/manifest.jsonl)")
    ap.add_argument("--json", action="store_true", help="una riga JSON per pagina invece della tabella")
    args = ap.parse_args(argv)

    righe = []
    for sorgente, r, path in voci_html(args.cartella):
        m = misura(path.read_bytes())
        righe.append((m, sorgente, r))
        if args.json:
            print(json.dumps({"sorgente": sorgente, "titolo": r["title"], "file": r["local_file"], **m.__dict__},
                             ensure_ascii=False))
    if not args.json:
        print(f"{'totale':>7} {'utile':>7} PW FR M  sorgente / titolo")
        for m, sorgente, r in sorted(righe, key=lambda x: x[0].utile):
            print(f"{m.totale:7d} {m.utile:7d} {'PW' if m.password else '  '} {'FR' if m.frame else '  '} "
                  f"{'M' if m.main else ' '}  "
                  f"{sorgente} / {r['title'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
