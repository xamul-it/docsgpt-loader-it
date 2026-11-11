#!/usr/bin/env python3
"""Test di qualità dell'estrazione (fase 5 della pipeline /nuova-fonte).

Stessi controlli e soglie per ogni fonte: se uno fallisce, la fonte non si
carica. Stampa una riga per controllo; exit 1 se uno fallisce.

  python3 qualita_estrazione.py --target <archivio> --id <id fonte>
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import adattatori
import fonte
import quadratura
from fonte import FonteError, Rete
from misura_formati import MIN_CARATTERI, campione
from valuta_link import normalizza, valuta_link

SOGLIA_TITOLO = 0.99
SOGLIA_ACCORDO_FORMATI = 0.9


@dataclass
class Esito:
    nome: str
    valore: float
    soglia: float
    ok: bool
    esempi: list[str] = field(default_factory=list)


def _testo_file(path: Path) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    return "\n\n".join(p.get_text() for p in soup.find_all("p") if not p.get("class"))


def leggi_unita(out_dir: Path) -> tuple[list[dict], dict[str, dict]]:
    """(righe del manifest, unit_id -> {testo ricomposto, riga della prima parte})."""
    righe = [json.loads(x) for x in (out_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    parti = defaultdict(list)
    for r in righe:
        parti[r["unit_id"]].append(r)
    unita = {}
    for uid, rs in parti.items():
        testi = []
        for r in rs:
            p = out_dir / r["local_file"]
            testi.append(_testo_file(p) if p.is_file() else "")
        unita[uid] = {"testo": "\n\n".join(testi), "riga": rs[0]}
    return righe, unita


def _esito(nome, valore, soglia, ok, esempi):
    return Esito(nome, round(valore, 4), soglia, ok, list(esempi)[:3])


def controlla(out_dir: Path, attesi: list[str], riferimento: dict[str, str], obbligatori: tuple[str, ...],
              residui: list[str], min_caratteri: int = MIN_CARATTERI) -> list[Esito]:
    out_dir = Path(out_dir)
    righe, unita = leggi_unita(out_dir)
    esiti = []

    rotti, orfani = quadratura.check_local(out_dir, righe)
    esiti.append(_esito("file_e_manifest", len(rotti) + len(orfani), 0, not rotti and not orfani, rotti + orfani))

    mancanti = [a for a in attesi if a not in unita]
    quota = (len(attesi) - len(mancanti)) / max(1, len(attesi))
    esiti.append(_esito("conteggio_unita", quota, 1.0, not mancanti and bool(attesi), mancanti))

    vuote = [uid for uid, u in unita.items()
             if fonte.difetto_testo(u["testo"], bool(u["riga"].get("breve_ammesso")), min_caratteri)]
    esiti.append(_esito("testo_vuoto_o_navigazione", len(vuote), 0, not vuote, vuote))

    rx = [re.compile(r) for r in residui]
    sporche = [uid for uid, u in unita.items() if any(r.search(u["testo"]) for r in rx)]
    esiti.append(_esito("residui", len(sporche), 0, not sporche, sporche))

    senza_titolo = [r["unit_id"] for r in righe if not str(r.get("titolo", "")).strip()]
    q = 1 - len(senza_titolo) / max(1, len(righe))
    esiti.append(_esito("titolo", q, SOGLIA_TITOLO, q >= SOGLIA_TITOLO, senza_titolo))

    senza_meta = [uid for uid, u in unita.items() if not all(str(u["riga"].get(k, "")).strip() for k in obbligatori)]
    q = 1 - len(senza_meta) / max(1, len(unita))
    esiti.append(_esito("metadati_obbligatori", q, 1.0, not senza_meta, senza_meta))

    diversi = [uid for uid, testo in riferimento.items()
               if uid not in unita or normalizza(unita[uid]["testo"]) != normalizza(testo)]
    q = 1 - len(diversi) / max(1, len(riferimento))
    esiti.append(_esito("confronto_testo", q, 1.0, not diversi, diversi))
    return esiti


def accordo_formati(unita_scritte: dict[str, dict], altro: dict[str, str]) -> Esito:
    """Somiglianza tra il testo estratto e lo stesso campione letto con un altro
    formato della fonte (controllo indipendente dal formato scelto). Decide la
    peggiore: basta un'unità diversa (es. testo attribuito all'unità sbagliata)."""
    rapporti, bassi = [], []
    for uid, testo in altro.items():
        a = normalizza(unita_scritte.get(uid, {}).get("testo", ""))
        r = difflib.SequenceMatcher(None, a, normalizza(testo), autojunk=False).ratio()
        rapporti.append(r)
        if r < SOGLIA_ACCORDO_FORMATI:
            bassi.append(f"{uid} ({r:.2f})")
    peggiore = min(rapporti) if rapporti else 0.0
    return _esito("accordo_formati", peggiore, SOGLIA_ACCORDO_FORMATI, bool(rapporti) and not bassi, bassi)


def controlla_fonte(archivio: Path, cfg: dict, n: int = 10) -> list[Esito]:
    ad = adattatori.adattatore(cfg["adattatore"])
    base = fonte.cartella(archivio, cfg)
    stato = json.loads((base / "stato.json").read_text()) if (base / "stato.json").exists() else {}
    cache = base / "raw" / str(stato.get("versione", "")).replace("/", "_") if stato.get("versione") else base / "raw"
    rete = Rete(cache)
    attesi = ad.attesi(rete, cfg)
    scelti = campione(attesi, n=n, obbligatori=ad.casi_limite(rete, cfg))
    tutte = ad.unita(rete, cfg, cfg["formato"])
    rif = {u.id: u.testo for u in tutte if u.id in set(scelti)}
    esiti = controlla(base, attesi, rif, ad.metadati_obbligatori, [*fonte.RESIDUI_COMUNI, *ad.residui])
    altri = [f for f in ad.formati if f != cfg["formato"]]
    if altri:
        try:
            altro = {u.id: u.testo for u in ad.unita(Rete(base / "raw" / f"misura-{altri[0]}"), cfg, altri[0], solo=set(scelti))}
            esiti.append(accordo_formati(leggi_unita(base)[1], altro))
        except FonteError as e:
            esiti.append(Esito("accordo_formati", 0, SOGLIA_ACCORDO_FORMATI, False, [f"non misurabile: {e}"[:120]]))
    # Riscontro sulla pagina ufficiale (anche con link: locale): titolo e testo
    # dell'unità devono comparire dove la fonte dice che stanno.
    if ad.candidati_link:
        cand = cfg["link"].split(":", 1)[-1] if cfg["link"] != "locale" else ad.candidati_link[0]
        r = valuta_link(ad, tutte, scelti[:5], rete)[cand]
        ok = r["status_200"] == 100.0 and r["assente"] == 0.0
        esiti.append(_esito("riscontro_link", r["status_200"] / 100, 1.0, ok, r.get("esempi_assenti", [])))
    return esiti


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--id", required=True)
    a = p.parse_args(argv)
    try:
        cfg = fonte.config(a.id)
        esiti = controlla_fonte(a.target, cfg)
    except (FonteError, FileNotFoundError) as e:
        print(f"ERRORE: {e}")
        return 1
    base = fonte.cartella(a.target, cfg)
    # prova_fonte.py carica solo se questo file è tutto ok e il manifest non è cambiato dopo.
    (base / "qualita.json").write_text(json.dumps(
        {"manifest": fonte.impronta(base), "esiti": [e.__dict__ for e in esiti]}, ensure_ascii=False, indent=1))
    for e in esiti:
        print(f"{'OK  ' if e.ok else 'FAIL'} {e.nome:<26} {e.valore:<8} soglia {e.soglia:<5} "
              + (f"es. {', '.join(e.esempi)}"[:110] if e.esempi and not e.ok else ""))
    return 0 if all(e.ok for e in esiti) else 1


if __name__ == "__main__":
    sys.exit(main())
