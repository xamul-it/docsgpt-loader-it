#!/usr/bin/env python3
"""Valutazione dei link di una fonte (fase 4 della pipeline /nuova-fonte).

Per ogni candidato (copia locale + link ufficiali dell'adattatore) apre i link
di un campione di unità e misura: risposta 200, precisione (porta all'unità o
all'atto intero?), byte. Scrive <cartella>/link.json e stampa una tabella.

  python3 valuta_link.py --target <archivio> --id <id fonte>
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import re
import sys
from pathlib import Path

import adattatori
import fonte
from fonte import FonteError, Rete, Unita
from misura_formati import campione

PAROLE_CHIAVE = 8


def normalizza(s: str) -> str:
    s = htmlmod.unescape(s).replace("’", "'").replace("‘", "'").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def testo_pagina(pagina: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(pagina, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return normalizza(soup.get_text(" "))


def chiave(u: Unita) -> str:
    return " ".join(normalizza(u.testo).split()[:PAROLE_CHIAVE])


def precisione(pagina: str, unita: Unita, vicine: list[Unita]) -> str:
    """"unita" se la pagina contiene titolo e inizio del testo dell'unità e non
    quelli delle vicine; "atto" se contiene anche le vicine; altrimenti "assente"."""
    t = testo_pagina(pagina)
    if not (normalizza(unita.titolo) in t and chiave(unita) in t):
        return "assente"
    return "atto" if any(chiave(x) in t for x in vicine) else "unita"


def valuta_link(ad, unita: list[Unita], scelti: list[str], rete) -> dict[str, dict]:
    pos = {u.id: i for i, u in enumerate(unita)}
    righe = [u for u in unita if u.id in set(scelti)]
    out = {"locale": {"status_200": 100.0, "unita": 100.0, "atto": 0.0, "assente": 0.0,
                      "byte_medi": round(sum(len(u.testo) for u in righe) / max(1, len(righe)))}}
    for cand in ad.candidati_link:
        conta = {"status_200": 0, "unita": 0, "atto": 0, "assente": 0}
        byte = 0
        esempi = []
        for u in righe:
            url = u.link_ufficiali.get(cand, "")
            i = pos[u.id]
            vicine = [unita[j] for j in (i - 1, i + 1) if 0 <= j < len(unita)]
            r = rete.get(url) if url else None
            if r is not None and r.status == 200:
                conta["status_200"] += 1
                byte += len(r.content)
                p = precisione(r.text, u, vicine)
            else:
                p = "assente"
            conta[p] += 1
            if p == "assente" and len(esempi) < 3:
                esempi.append(f"{u.id}: {url or '(senza link)'}")
        n = max(1, len(righe))
        out[cand] = {k: round(100.0 * c / n, 1) for k, c in conta.items()}
        out[cand]["byte_medi"] = round(byte / max(1, conta["status_200"]))
        out[cand]["esempi_assenti"] = esempi
    return out


def valuta_fonte(archivio: Path, cfg: dict, n: int = 10) -> dict[str, dict]:
    ad = adattatori.adattatore(cfg["adattatore"])
    base = fonte.cartella(archivio, cfg)
    rete = Rete(base / "raw")
    unita = ad.unita(rete, cfg, cfg["formato"])
    scelti = campione([u.id for u in unita], n=n, obbligatori=ad.casi_limite(rete, cfg))
    r = valuta_link(ad, unita, scelti, rete)
    base.mkdir(parents=True, exist_ok=True)
    (base / "link.json").write_text(json.dumps({"campione": scelti, "candidati": r}, ensure_ascii=False, indent=1))
    return r


def tabella(r: dict[str, dict]) -> list[str]:
    righe = [f"{'candidato':<20} {'200%':>6} {'unità%':>7} {'atto%':>6} {'assente%':>8} {'byte medi':>10}"]
    for k, m in r.items():
        righe.append(f"{k:<20} {m['status_200']:>6} {m['unita']:>7} {m['atto']:>6} {m['assente']:>8} {m['byte_medi']:>10}")
        righe += [f"    assente: {e}" for e in m.get("esempi_assenti", [])[:1]]
    return righe[:15]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--id", required=True)
    p.add_argument("-n", type=int, default=10)
    a = p.parse_args(argv)
    try:
        r = valuta_fonte(a.target, fonte.config(a.id), a.n)
    except FonteError as e:
        print(f"ERRORE: {e}")
        return 1
    print("\n".join(tabella(r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
