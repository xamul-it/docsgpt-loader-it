#!/usr/bin/env python3
"""Estrazione di una fonte della pipeline: un file HTML per unità (o per parte).

Comune a tutte le fonti: l'adattatore dà le unità, qui si scrivono file,
manifest.jsonl e stato.json nella struttura dell'archivio
(<archivio>/<gruppo>/<id>/), con aggiornamento incrementale (sha256).

  python3 estrai.py --target <archivio> (--id <id> | --gruppo <gruppo>) [--forza]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import adattatori
import downloader
import fonte
from fonte import FonteError, Rete, Unita

CAMPI_MANIFEST = ("source", "title", "document_date", "authority", "document_type", "number", "detail_url",
                  "download_url", "local_file", "sha256", "downloaded_at", "unit_id", "parte", "percorso", "titolo",
                  "breve_ammesso", "link", "versione")


@dataclass
class Rapporto:
    scritte: int
    invariate: int
    rimosse: int
    divise: int


def _taglia(blocco: str, soglia: int) -> list[str]:
    """Blocco oltre soglia: tagli all'ultimo '. ' prima della soglia (in mancanza, alla soglia)."""
    out = []
    while len(blocco) > soglia:
        cut = blocco.rfind(". ", 0, soglia)
        cut = cut + 1 if cut > 0 else soglia
        out.append(blocco[:cut].strip())
        blocco = blocco[cut:].strip()
    if blocco:
        out.append(blocco)
    return out


def dividi(testo: str, soglia: int) -> list[str]:
    """Parti di al massimo `soglia` caratteri, sui blocchi separati da riga vuota."""
    if len(testo) <= soglia:
        return [testo]
    blocchi = [b.strip() for b in re.split(r"\n\s*\n", testo) if b.strip()]
    parti, corrente = [], ""
    for b in blocchi:
        for pezzo in _taglia(b, soglia):
            if corrente and len(corrente) + 2 + len(pezzo) > soglia:
                parti.append(corrente)
                corrente = pezzo
            else:
                corrente = f"{corrente}\n\n{pezzo}" if corrente else pezzo
    if corrente:
        parti.append(corrente)
    return parti


def link_scelto(u: Unita, cfg: dict) -> str:
    modo = cfg["link"]
    if modo == "locale":
        return ""
    candidato = modo.split(":", 1)[1] if modo.startswith("entrambi:") else modo
    if candidato not in u.link_ufficiali:
        raise FonteError(f"{u.id}: manca il link '{candidato}' richiesto da link: {modo}")
    return u.link_ufficiali[candidato]


def _html(cfg: dict, u: Unita, testo: str, k: int, n: int, link: str) -> str:
    e = html.escape
    titolo = u.titolo + (f" (parte {k} di {n})" if n > 1 else "")
    righe = ["<!doctype html>", '<html lang="it"><head><meta charset="utf-8">',
             f"<title>{e(' › '.join([cfg['nome'], *u.percorso, u.titolo]))}</title></head><body>",
             f'<p class="percorso">{e(" › ".join([cfg["nome"], *u.percorso]))}</p>',
             f"<h1>{e(titolo)}</h1>"]
    if link:
        righe.append(f'<p class="fonte">Testo ufficiale: <a href="{e(link)}">{e(link)}</a></p>')
    righe += [f"<p>{e(b.strip())}</p>" for b in re.split(r"\n\s*\n", testo) if b.strip()]
    righe.append("</body></html>")
    return "\n".join(righe) + "\n"


def _detail_url(u: Unita, cfg: dict, link: str, k: int, n: int) -> str:
    base = link or next(iter(u.link_ufficiali.values()), "") or f"{cfg['url']}#{u.id}"
    if n == 1:
        return base
    return f"{base}-p{k}" if "#" in base else f"{base}#p{k}"


def _scrivi(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    downloader.write_atomic(path, data)


def _leggi_manifest(out_dir: Path) -> list[dict]:
    m = out_dir / "manifest.jsonl"
    if not m.exists():
        return []
    return [json.loads(x) for x in m.read_text(encoding="utf-8").splitlines() if x.strip()]


def scrivi_unita(unita: list[Unita], cfg: dict, out_dir: Path, versione: str, minimo_precedente: float = 0.5) -> Rapporto:
    out_dir = Path(out_dir)
    ids = [u.id for u in unita]
    doppi = sorted({i for i in ids if ids.count(i) > 1})
    if doppi:
        raise FonteError(f"id duplicati: {', '.join(doppi[:10])}")
    non_validi = [i for i in ids if not re.match(fonte.ID_RE, i)]
    if non_validi:
        raise FonteError(f"id non validi (servono minuscole, cifre e trattini): {', '.join(non_validi[:10])}")
    precedenti = _leggi_manifest(out_dir)
    n_prec = len({r.get("unit_id") for r in precedenti})
    if not unita or (n_prec and len(unita) < minimo_precedente * n_prec):
        raise FonteError(f"{len(unita)} unità contro {n_prec} già in archivio: la fonte ha risposto in modo anomalo, "
                         "non tocco nulla")
    vecchie = {r["local_file"]: r for r in precedenti}

    adesso = datetime.now().astimezone().isoformat(timespec="seconds")
    righe: list[dict] = []
    contenuti: dict[str, bytes] = {}
    divise = 0
    for u in unita:
        conflitti = set(u.metadati) & set(CAMPI_MANIFEST)
        if conflitti:
            raise FonteError(f"{u.id}: metadati con nomi riservati del manifest: {', '.join(sorted(conflitti))}")
        link = link_scelto(u, cfg)
        parti = dividi(u.testo, int(cfg["soglia_caratteri"]))
        divise += len(parti) > 1
        for k, testo in enumerate(parti, 1):
            nome = f"files/{u.id}" + (f"-p{k}" if len(parti) > 1 else "") + ".html"
            data = _html(cfg, u, testo, k, len(parti), link).encode("utf-8")
            sha = hashlib.sha256(data).hexdigest()
            contenuti[nome] = data
            prima = vecchie.get(nome)
            righe.append({
                "source": f"{cfg['gruppo']}/{cfg['id']}",
                "title": " › ".join([cfg["nome"], *u.percorso, u.titolo]),
                "document_date": u.metadati.get("data", ""),
                "authority": cfg["ente"],
                "document_type": cfg["tipo_documento"],
                "number": u.metadati.get("numero", ""),
                "detail_url": _detail_url(u, cfg, link, k, len(parti)),
                "download_url": cfg["url"],
                "local_file": nome,
                "sha256": sha,
                "downloaded_at": prima["downloaded_at"] if prima and prima.get("sha256") == sha else adesso,
                "unit_id": u.id,
                "parte": f"{k}/{len(parti)}" if len(parti) > 1 else "",
                "percorso": u.percorso,
                "titolo": u.titolo,
                "breve_ammesso": u.breve_ammesso,
                "link": link,
                "versione": versione,
                **u.metadati,
            })
    detail = [r["detail_url"] for r in righe]
    if len(detail) != len(set(detail)):
        raise FonteError("detail_url ripetuti: i link ufficiali dell'adattatore non sono unici per unità")

    scritte = invariate = 0
    for r in righe:
        prima = vecchie.get(r["local_file"])
        path = out_dir / r["local_file"]
        if prima and prima.get("sha256") == r["sha256"] and path.exists():
            invariate += 1
            continue
        _scrivi(path, contenuti[r["local_file"]])
        scritte += 1
    nuovi = {r["local_file"] for r in righe}
    rimosse = 0
    for nome in set(vecchie) - nuovi:
        (out_dir / nome).unlink(missing_ok=True)
        rimosse += 1
    _scrivi(out_dir / "manifest.jsonl",
                            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in righe).encode("utf-8"))
    _scrivi(out_dir / "stato.json", json.dumps(
        {"versione": versione, "unita": len(unita), "file": len(righe), "aggiornato": adesso}, indent=1).encode())
    return Rapporto(scritte, invariate, rimosse, divise)


def estrai_fonte(archivio: Path, cfg: dict, forza: bool = False) -> Rapporto | None:
    """None = versione invariata, niente da fare."""
    ad = adattatori.adattatore(cfg["adattatore"])
    out = fonte.cartella(archivio, cfg)
    rete = Rete(out / "raw")
    versione = ad.versione(rete, cfg)
    stato = out / "stato.json"
    if not forza and stato.exists() and json.loads(stato.read_text()).get("versione") == versione:
        return None
    # Versione nuova: la cache non vale più, si rilegge la fonte.
    rete = Rete(out / "raw" / versione.replace("/", "_")) if versione else Rete(out / "raw")
    return scrivi_unita(ad.unita(rete, cfg, cfg["formato"]), cfg, out, versione)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--target", type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--id")
    g.add_argument("--gruppo")
    p.add_argument("--forza", action="store_true", help="riestrae anche se la versione non è cambiata")
    a = p.parse_args(argv)
    try:
        cfgs = [fonte.config(a.id)] if a.id else [c for c in fonte.configs() if c.get("gruppo") == a.gruppo]
    except FonteError as e:
        print(f"ERRORE: {e}")
        return 1
    if not cfgs:
        print(f"ERRORE: nessuna fonte nel gruppo {a.gruppo}")
        return 1
    errori = 0
    for cfg in cfgs:
        try:
            r = estrai_fonte(a.target, cfg if a.id else fonte.config(cfg["id"]), a.forza)
        except FonteError as e:
            print(f"{cfg['id']}: ERRORE: {e}")
            errori += 1
            continue
        if r is None:
            print(f"{cfg['id']}: invariato")
        else:
            print(f"{cfg['id']}: scritte {r.scritte}, invariate {r.invariate}, rimosse {r.rimosse}, divise {r.divise}")
    return 1 if errori else 0


if __name__ == "__main__":
    sys.exit(main())
