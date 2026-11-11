#!/usr/bin/env python3
"""Misura dei formati di una fonte (fase 3 della pipeline /nuova-fonte).

Per ogni formato dichiarato dall'adattatore legge lo stesso campione
deterministico di unità e misura completezza, struttura, pulizia, metadati e
costo. Scrive <cartella>/misure.json e stampa una tabella.

  python3 misura_formati.py --target <archivio> --id <id fonte>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import adattatori
import fonte
from fonte import FonteError, Rete, Unita

MIN_CARATTERI = 40


def campione(ids: list[str], n: int = 30, seed: str = "nuova-fonte", obbligatori=()) -> list[str]:
    """n id scelti in modo deterministico, sempre inclusi gli obbligatori presenti in ids."""
    presenti = set(ids)
    out = list(dict.fromkeys(o for o in obbligatori if o in presenti))[:n]
    resto = sorted((i for i in presenti if i not in out), key=lambda i: hashlib.sha256((seed + i).encode()).hexdigest())
    return out + resto[: max(0, min(n, len(ids)) - len(out))]


@dataclass
class Misura:
    formato: str
    attese: int
    trovate: int
    vuote: int
    con_titolo: int
    con_percorso: int
    con_metadati: int
    residui: int
    lunghezza_p95: int
    lunghezza_max: int
    richieste: int = 0
    byte: int = 0
    stima_richieste_totali: int = 0
    stima_byte_totali: int = 0
    versione: str | None = None
    errore: str | None = None


def vuota(u: Unita, min_caratteri: int = MIN_CARATTERI) -> bool:
    testo = u.testo.strip()
    return not testo if u.breve_ammesso else len(testo) < min_caratteri


def misura(formato: str, unita: list[Unita], attese: list[str], obbligatori: tuple[str, ...],
           residui: list[str], min_caratteri: int = MIN_CARATTERI) -> Misura:
    att = set(attese)
    us = [u for u in unita if u.id in att]
    rx = [re.compile(r) for r in residui]
    lunghezze = sorted(len(u.testo) for u in us)
    p95 = lunghezze[max(0, math.ceil(0.95 * len(lunghezze)) - 1)] if lunghezze else 0
    return Misura(
        formato=formato,
        attese=len(att),
        trovate=len({u.id for u in us}),
        vuote=sum(vuota(u, min_caratteri) for u in us),
        con_titolo=sum(bool(u.titolo.strip()) for u in us),
        con_percorso=sum(bool(u.percorso) for u in us),
        con_metadati=sum(all(u.metadati.get(k) for k in obbligatori) for u in us),
        residui=sum(any(r.search(u.testo) for r in rx) for u in us),
        lunghezza_p95=p95,
        lunghezza_max=lunghezze[-1] if lunghezze else 0,
    )


def misura_fonte(archivio: Path, cfg: dict, n: int = 30) -> list[Misura]:
    ad = adattatori.adattatore(cfg["adattatore"])
    base = fonte.cartella(archivio, cfg)
    rete = Rete(base / "raw")
    attesi = ad.attesi(rete, cfg)
    scelti = campione(attesi, n=n, obbligatori=ad.casi_limite(rete, cfg))
    residui = [*fonte.RESIDUI_COMUNI, *ad.residui]
    out = []
    for formato in ad.formati:
        # Cache separata per formato: il costo misurato è quello del formato da solo.
        r = Rete(base / "raw" / f"misura-{formato}")
        try:
            us = ad.unita(r, cfg, formato, solo=set(scelti))
            mis = misura(formato, us, scelti, ad.metadati_obbligatori, residui)
        except FonteError as e:
            mis = Misura(formato, len(scelti), 0, 0, 0, 0, 0, 0, 0, 0, errore=str(e)[:200])
        mis.richieste, mis.byte = r.richieste_rete, r.byte_rete
        # Formato per unità (circa una richiesta per unità): il costo cresce con la fonte.
        fattore = len(attesi) / max(1, len(scelti)) if r.richieste_rete >= len(scelti) / 2 else 1
        mis.stima_richieste_totali = round(r.richieste_rete * fattore)
        mis.stima_byte_totali = round(r.byte_rete * fattore)
        try:
            mis.versione = ad.versione(rete, cfg) or None
        except FonteError:
            mis.versione = None
        out.append(mis)
    base.mkdir(parents=True, exist_ok=True)
    (base / "misure.json").write_text(json.dumps(
        {"attesi": len(attesi), "campione": scelti, "misure": [asdict(m) for m in out]}, ensure_ascii=False, indent=1))
    return out


def tabella(misure: list[Misura]) -> list[str]:
    righe = [f"{'formato':<16} {'trov/att':>9} {'vuote':>5} {'resid':>5} {'titolo':>6} {'perc':>5} {'meta':>5} "
             f"{'p95':>6} {'max':>6} {'rich.tot':>8} {'MB tot':>7}"]
    for m in misure:
        if m.errore:
            righe.append(f"{m.formato:<16} ERRORE: {m.errore[:90]}")
            continue
        righe.append(f"{m.formato:<16} {m.trovate:>4}/{m.attese:<4} {m.vuote:>5} {m.residui:>5} {m.con_titolo:>6} "
                     f"{m.con_percorso:>5} {m.con_metadati:>5} {m.lunghezza_p95:>6} {m.lunghezza_max:>6} "
                     f"{m.stima_richieste_totali:>8} {m.stima_byte_totali / 1e6:>7.1f}")
    return righe[:15]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--id", required=True)
    p.add_argument("-n", type=int, default=30, help="dimensione del campione")
    a = p.parse_args()
    try:
        misure = misura_fonte(a.target, fonte.config(a.id), a.n)
    except FonteError as e:
        print(f"ERRORE: {e}")
        return 1
    print("\n".join(tabella(misure)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
