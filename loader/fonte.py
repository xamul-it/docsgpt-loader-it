#!/usr/bin/env python3
"""Nucleo della pipeline delle nuove fonti: contratto dell'unità, rete con cache,
interfaccia degli adattatori, configurazione (fonti/pipeline.yaml).

Qui non c'è nessuna regola specifica di una fonte: quelle stanno negli
adattatori (adattatori/<nome>.py), che riducono la fonte a una lista di Unita.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import requests

import downloader

ID_RE = r"^[a-z0-9][a-z0-9-]*$"

# Testo di pagine di navigazione: non deve mai finire in un'unità.
RESIDUI_COMUNI = [
    r"Vai al contenuto",
    r"Seleziona tutti",
    r"Il tuo browser non supporta",
    r"Accetta (tutti i )?cookie",
]

PIPELINE_YAML = Path(__file__).resolve().parent / "fonti" / "pipeline.yaml"
CAMPI_CONFIG = ("id", "gruppo", "nome", "adattatore", "url", "formato", "link", "soglia_caratteri", "ente", "tipo_documento")


class FonteError(Exception):
    """Errore della fonte o dei dati: la pipeline si ferma con questo messaggio."""


class ReteMancante(FonteError):
    """URL non previsto dalle fixture."""


@dataclass
class Unita:
    """Il pezzo che diventa un file e che la ricerca deve restituire.

    testo: blocchi naturali (commi, paragrafi) separati da una riga vuota.
    """
    id: str
    titolo: str
    percorso: list[str]
    testo: str
    metadati: dict[str, str] = field(default_factory=dict)
    link_ufficiali: dict[str, str] = field(default_factory=dict)
    breve_ammesso: bool = False


@dataclass
class Risposta:
    url: str
    status: int
    content_type: str
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class Rete:
    """GET con cache su disco: ogni risposta si scarica una volta e serve a
    misura, estrazione e test. fresca=True ignora la cache e la aggiorna."""

    def __init__(self, cache_dir: Path | None, ua: str = downloader.CURATED_UA, pausa: float = 0.5):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.pausa = pausa
        self.richieste_rete = 0
        self.byte_rete = 0
        self._ultima = 0.0
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": ua, "Accept-Language": "it-IT,it;q=0.9"})
        self._s.verify = downloader.ca_bundle()

    def _percorsi(self, url: str) -> tuple[Path, Path]:
        h = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{h}.bin", self.cache_dir / f"{h}.json"

    def get(self, url: str, fresca: bool = False) -> Risposta:
        if self.cache_dir and not fresca:
            corpo, meta = self._percorsi(url)
            if corpo.exists() and meta.exists():
                m = json.loads(meta.read_text())
                return Risposta(url, m["status"], m["content_type"], corpo.read_bytes())
        attesa = self.pausa - (time.monotonic() - self._ultima)
        if attesa > 0:
            time.sleep(attesa)
        r = self._s.get(url, timeout=downloader.TIMEOUT)
        self._ultima = time.monotonic()
        self.richieste_rete += 1
        self.byte_rete += len(r.content)
        out = Risposta(url, r.status_code, r.headers.get("Content-Type", ""), r.content)
        if self.cache_dir and r.status_code == 200:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            corpo, meta = self._percorsi(url)
            downloader.write_atomic(corpo, r.content)
            downloader.write_atomic(meta, json.dumps({"url": url, "status": out.status, "content_type": out.content_type}).encode())
        return out


class ReteFixture(Rete):
    """Rete finta per i test: fixture_dir/contratto.json mappa URL -> file."""

    def __init__(self, fixture_dir: Path):
        self.fixture_dir = Path(fixture_dir)
        self.mappa = json.loads((self.fixture_dir / "contratto.json").read_text())["rete"]
        self.richieste_rete = 0
        self.byte_rete = 0

    def get(self, url: str, fresca: bool = False) -> Risposta:
        if url not in self.mappa:
            raise ReteMancante(url)
        data = (self.fixture_dir / self.mappa[url]).read_bytes()
        ctype = "text/xml" if self.mappa[url].endswith(".xml") else "text/html"
        return Risposta(url, 200, ctype, data)


class Adattatore(Protocol):
    """Interfaccia di un adattatore di fonte. Vedi adattatori/_modello.py."""

    nome: str
    formati: tuple[str, ...]              # il primo è il predefinito
    metadati_obbligatori: tuple[str, ...]
    residui: tuple[str, ...]              # regex in aggiunta a RESIDUI_COMUNI
    candidati_link: tuple[str, ...]       # chiavi di Unita.link_ufficiali

    def riconosce(self, url: str) -> bool: ...
    def versione(self, rete: Rete, cfg: dict) -> str: ...
    def attesi(self, rete: Rete, cfg: dict) -> list[str]: ...
    def unita(self, rete: Rete, cfg: dict, formato: str, solo: set[str] | None = None) -> list[Unita]: ...
    def casi_limite(self, rete: Rete, cfg: dict) -> list[str]: ...


def configs(path: Path = PIPELINE_YAML) -> list[dict]:
    import yaml  # qui: chi non usa la configurazione non ne ha bisogno
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return list(data.get("sorgenti") or [])


def config(id: str, path: Path = PIPELINE_YAML) -> dict:
    for c in configs(path):
        if c.get("id") == id:
            mancanti = [k for k in CAMPI_CONFIG if c.get(k) in (None, "")]
            if mancanti:
                raise FonteError(f"{id}: mancano in {Path(path).name}: {', '.join(mancanti)}")
            return c
    raise FonteError(f"{id}: fonte non presente in {Path(path).name}")


def cartella(archivio: Path, cfg: dict) -> Path:
    return Path(archivio) / cfg["gruppo"] / cfg["id"]
