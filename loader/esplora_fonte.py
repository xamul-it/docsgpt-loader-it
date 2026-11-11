#!/usr/bin/env python3
"""Ricognizione di una fonte (fase 1 della pipeline /nuova-fonte).

Scarica la pagina (con cache) e ne stampa un riepilogo compatto: tipo, titolo,
formati scaricabili, elenchi di link ripetuti, struttura, data, avvisi. Serve a
decidere come scrivere l'adattatore senza leggere le pagine intere.

  python3 esplora_fonte.py --url <url> [--out <dir>]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlparse

import adattatori
import downloader
from fonte import Rete

ESTENSIONI = {"xml": ("xml", "akn"), "pdf": ("pdf",), "json": ("json",), "csv": ("csv",),
              "docx": ("doc", "docx", "odt", "rtf"), "zip": ("zip",), "rss": ("rss", "atom")}
MAX_FORMATO = 5
MAX_ELENCHI = 10


@dataclass
class Ricognizione:
    url: str
    status: int
    content_type: str
    byte: int
    titolo: str = ""
    adattatore: str | None = None
    formati: dict[str, list[str]] = field(default_factory=dict)
    elenchi: list[dict] = field(default_factory=list)
    struttura: dict[str, int] = field(default_factory=dict)
    data_trovata: str | None = None
    testo_campione: str = ""
    avvisi: list[str] = field(default_factory=list)


def schema_url(url: str) -> str:
    """Forma generica di un URL: numeri e id lunghi -> {n}, query ordinate senza valori."""
    u = urlparse(url)
    parti = [re.sub(r"^(\d+|[0-9a-f]{8,}|[0-9A-Z]{6,})$", "{n}", p) for p in u.path.split("/")]
    percorso = "/".join(parti)
    query = "&".join(sorted({k for k, _ in parse_qsl(u.query, keep_blank_values=True)}))
    return f"{u.netloc}{percorso}" + (f"?{query}" if query else "")


def _formato_di(url: str, testo: str) -> str | None:
    p = urlparse(url)
    ext = p.path.rsplit(".", 1)[-1].lower() if "." in p.path.rsplit("/", 1)[-1] else ""
    for nome, exts in ESTENSIONI.items():
        if ext in exts:
            return nome
    t = (testo + " " + p.query).lower()
    for nome in ("xml", "json", "csv", "rss"):
        if re.search(rf"\b{nome}\b", t):
            return nome
    return None


def _data(testo: str) -> str | None:
    m = re.search(r"(aggiornat\w*|aggiornamento|vigente al|data)[^\n]{0,60}", testo, re.I)
    for pezzo in ([m.group(0)] if m else []) + [testo[:5000]]:
        d = downloader.guess_date(pezzo) or downloader.guess_date_words(pezzo)
        if d:
            return d
    return None


def _ricognisci_html(r: Ricognizione, url: str, html: str) -> None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    r.titolo = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    r.struttura = {t: len(soup.find_all(t)) for t in ("h1", "h2", "h3", "table", "article", "form", "script", "iframe")}
    gruppi: dict[str, list[str]] = defaultdict(list)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        assoluto = urljoin(url, href)
        fmt = _formato_di(assoluto, a.get_text(" ", strip=True))
        if fmt:
            lst = r.formati.setdefault(fmt, [])
            if assoluto not in lst and len(lst) < MAX_FORMATO:
                lst.append(assoluto)
        gruppi[schema_url(assoluto)].append(assoluto)
    r.elenchi = [{"schema": s, "quanti": len(us), "esempi": list(dict.fromkeys(us))[:3]}
                 for s, us in sorted(gruppi.items(), key=lambda kv: -len(kv[1])) if len(us) >= 3][:MAX_ELENCHI]
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    corpo = soup.find("main") or soup.find("article") or soup.body or soup
    testo = re.sub(r"\s+", " ", corpo.get_text(" ", strip=True))
    r.testo_campione = testo[:600]
    r.data_trovata = _data(testo)
    if len(testo) < 500 and r.struttura["script"] >= 5:
        r.avvisi.append("pagina quasi vuota con molti script: contenuto probabilmente caricato da JavaScript")
    if soup.find("input", attrs={"type": "password"}) or re.search(r"\b(accedi|login|autenticazione)\b", testo[:2000], re.I):
        r.avvisi.append("possibile login richiesto")
    robots = soup.find("meta", attrs={"name": "robots"})
    if robots and "noindex" in (robots.get("content") or "").lower():
        r.avvisi.append("meta robots noindex")


def _ricognisci_xml(r: Ricognizione, data: bytes) -> None:
    try:  # fonte non ancora verificata: defusedxml blocca entità esterne e "billion laughs"
        from defusedxml.ElementTree import fromstring
    except ImportError:  # host senza defusedxml (nel notifier c'è)
        from xml.etree.ElementTree import fromstring
    try:
        root = fromstring(data)
    except Exception as e:
        r.avvisi.append(f"XML non valido: {e}")
        return
    nome = lambda el: el.tag.rsplit("}", 1)[-1]
    conta: Counter = Counter()

    def visita(el, percorso):
        figli = Counter(nome(c) for c in el)
        for c in el:
            p = f"{percorso}/{nome(c)}"
            if figli[nome(c)] > 1:
                conta[p] += 1
            visita(c, p)

    visita(root, nome(root))
    r.titolo = nome(root)
    r.elenchi = [{"schema": s, "quanti": n, "esempi": []} for s, n in conta.most_common(MAX_ELENCHI)]
    r.testo_campione = re.sub(r"\s+", " ", "".join(root.itertext()))[:600]


def ricognisci(rete: Rete, url: str) -> Ricognizione:
    resp = rete.get(url)
    r = Ricognizione(url, resp.status, resp.content_type, len(resp.content))
    ad = adattatori.trova(url)
    r.adattatore = ad.nome if ad else None
    if resp.status != 200:
        r.avvisi.append(f"HTTP {resp.status}")
    ct = resp.content_type.lower()
    inizio = resp.content[:200].lstrip().lower()
    if "xml" in ct or inizio.startswith(b"<?xml") and b"<html" not in resp.content[:1000].lower():
        _ricognisci_xml(r, resp.content)
    elif "html" in ct or inizio.startswith((b"<!doctype html", b"<html")):
        _ricognisci_html(r, url, resp.text)
    if r.content_type == "" and inizio.startswith(b"%pdf"):
        r.content_type = "application/pdf"
    return r


def riepilogo(r: Ricognizione) -> list[str]:
    righe = [f"{r.url}", f"  HTTP {r.status} · {r.content_type or '?'} · {r.byte} byte · titolo: {r.titolo[:80]}",
             f"  adattatore: {r.adattatore or 'da scrivere'} · data: {r.data_trovata or '-'}"]
    if r.formati:
        righe.append("  formati: " + "; ".join(f"{k} {len(v)} (es. {v[0]})" for k, v in r.formati.items())[:300])
    for el in r.elenchi[:5]:
        righe.append(f"  elenco {el['quanti']:>5} × {el['schema'][:110]}")
    if r.struttura:
        righe.append("  struttura: " + " ".join(f"{k}={v}" for k, v in r.struttura.items() if v))
    righe += [f"  AVVISO: {a}" for a in r.avvisi]
    return righe[:15]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", required=True)
    p.add_argument("--out", type=Path, help="cartella per raw/ e ricognizione.json")
    a = p.parse_args()
    rete = Rete(a.out / "raw" if a.out else None)
    r = ricognisci(rete, a.url)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "ricognizione.json").write_text(json.dumps(asdict(r), ensure_ascii=False, indent=1))
    print("\n".join(riepilogo(r)))
    return 0 if r.status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
