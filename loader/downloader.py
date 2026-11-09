#!/usr/bin/env python3
"""
fiscal_sources_downloader.py

Scarica documenti da raccolte fiscali/giuridiche in cartelle separate.

Uso:
  python fiscal_sources_downloader.py
  python fiscal_sources_downloader.py --target /data/rag --source agenzia-entrate --mode refresh
  python fiscal_sources_downloader.py --target /data/rag --source agenzia-entrate/interpelli --mode refresh
  python fiscal_sources_downloader.py --target /data/rag --source all --mode refresh
  python fiscal_sources_downloader.py --target /data/rag --source ministero-finanze/circolari --mode rewrite
  python fiscal_sources_downloader.py --target /data/rag --source all --mode refresh --workers 2 --rate 3
  python fiscal_sources_downloader.py --target /data/rag --source normativa --mode refresh --skip-existing

Dipendenze:
  pip install requests beautifulsoup4

Modalità:
  refresh : conserva i file già presenti; scarica nuovi documenti e riscarica quelli cambiati
  rewrite : cancella la cartella della source selezionata e la ricrea da zero

Output per ogni source (per la prassi una source per ente e tipologia:
agenzia-entrate/circolari, agenzia-entrate/interpelli, ministero-finanze/risoluzioni, ...;
normativa e giurisprudenza si suddividono da sole per tipo: normativa/legge, giurisprudenza/sentenza, ...):
  <target>/<source>/
      files/...
      manifest.jsonl
      errors.log

Il manifest contiene almeno:
  source, title, document_date, authority, document_type,
  detail_url, download_url, local_file, sha256, downloaded_at
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urldefrag, parse_qs, urlencode, urlunparse, quote
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "FiscalArchiveCollector/1.1"
)

TIMEOUT = 40
# Documenti elaborati in parallelo e richieste al secondo verso uno stesso host.
# È RATE a proteggere i siti: vale per tutti i thread insieme.
WORKERS = 4
RATE = 5.0
MIN_RATE = 0.5
# In refresh, i documenti CeRDEF già in archivio non vengono riletti dalla
# fonte: molto più veloce, ma non si accorge dei testi modificati.
SKIP_EXISTING = False
# Se impostato, le fonti CeRDEF vengono scandite solo da quest'anno in poi
# (controlli periodici: le novità stanno negli ultimi anni).
FROM_YEAR: Optional[int] = None
MAX_PAGES_PER_SOURCE = 20000
MAX_RETRIES = 4
DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".zip", ".p7m")
VERBOSE = 1

# ---------------------------------------------------------------------------
# SOURCE
# ---------------------------------------------------------------------------

CEDEF_PRASSI = {
    "kind": "cedef-prassi",
    "base": "https://def.finanze.it/DocTribFrontend/",
    "home": "https://def.finanze.it/DocTribFrontend/RS2_HomePage.jsp",
    "search_url": "https://def.finanze.it/DocTribFrontend/callRicAvanzataPrassi.do?js_enabled=1&reset=y",
}

# Tipo nella select "Tipo" della ricerca prassi CeRDEF -> cartella della tipologia.
# Le risposte a interpello sono il tipo "Interpello".
TIPOLOGIE_PRASSI = {
    "Circolare": "circolari",
    "Risoluzione": "risoluzioni",
    "Interpello": "interpelli",
    "Comunicato Stampa": "comunicati-stampa",
    "Lettera circolare": "lettere-circolari",
    "Nota": "note",
    "Direttiva": "direttive",
    "Delibera": "delibere",
    "Telegramma": "telegrammi",
}


def prassi_sources(folder: str, label: str, authorities: list[str], tipi: dict[str, int]) -> dict:
    """Una source CeRDEF per tipologia: <folder>/<tipologia>.

    tipi: tipo CeRDEF -> primo anno da interrogare. Vanno elencati solo i tipi
    per cui l'ente ha davvero documenti in CeRDEF.
    """
    return {
        f"{folder}/{TIPOLOGIE_PRASSI[tipo]}": {
            **CEDEF_PRASSI,
            "label": f"{label} - {TIPOLOGIE_PRASSI[tipo].replace('-', ' ')}",
            "document_type": tipo,
            "authority_contains": authorities,
            "start_year": start_year,
        }
        for tipo, start_year in tipi.items()
    }


# Il nome della source è anche il percorso della sua cartella sotto il target:
# "agenzia-entrate/circolari" -> <target>/agenzia-entrate/circolari/.
SOURCES = {
    **prassi_sources(
        "agenzia-entrate",
        "Agenzia delle Entrate",
        ["Agenzia delle Entrate"],
        {
            "Circolare": 2001,
            "Risoluzione": 2001,
            # Prima del 2016 CeRDEF non ha interpelli; la pubblicazione sistematica parte dal 2018.
            "Interpello": 2016,
            "Comunicato Stampa": 2001,
            "Lettera circolare": 2001,
        },
    ),
    # CeRDEF dichiara copertura della prassi post riforma tributaria del 1972.
    **prassi_sources(
        "ministero-finanze",
        "Ministero Finanze / MEF / Dipartimento Finanze",
        [
            "Min. Finanze",
            "Ministero delle Finanze",
            "Min. Economia e Finanze",
            "Ministero Economia e Finanze",
            "Dipartimento delle Finanze",
            "Dipartimento Politiche Fiscali",
        ],
        {
            "Circolare": 1972,
            "Risoluzione": 1972,
            "Comunicato Stampa": 1972,
            "Lettera circolare": 1972,
            "Nota": 1972,
            "Direttiva": 1972,
            "Delibera": 1972,
            "Telegramma": 1972,
        },
    ),
    # Le circolari ADM si prendono dal sito dell'Agenzia (più complete di
    # CeRDEF); le altre tipologie esistono solo in CeRDEF.
    "agenzia-dogane-monopoli/circolari": {
        "label": "Agenzia Dogane e Monopoli - circolari",
        "kind": "crawl",
        "seeds": [
            "https://www.adm.gov.it/portale/atti-amministrativi-generali",
            "https://www.adm.gov.it/portale/circolari-dogane",
            "https://www.adm.gov.it/portale/circolari-funzionamento-adm",
            "https://www.adm.gov.it/portale/circolari-e-note2",
        ],
        "allowed_domains": ["adm.gov.it", "www.adm.gov.it"],
        # Si seguono solo i link del corpo pagina (non i menu) che portano a
        # elenchi di circolari o ai loro archivi; dagli indici di archivio si
        # seguono tutti i link ("Anni 2022 – 2023" non ha parole chiave).
        "follow_keywords": ["circolar", "archiv"],
        # Nelle pagine miste (accise: circolari, determinazioni e note) si
        # tengono solo le voci che si dichiarano circolari.
        "document_keywords": ["circolar"],
        "start_year": 2001,
    },
    **prassi_sources(
        "agenzia-dogane-monopoli",
        "Agenzia Dogane e Monopoli",
        ["Agenzia delle Dogane", "Agenzia delle Dogane e dei Monopoli"],
        {"Risoluzione": 2001, "Lettera circolare": 2001, "Nota": 2001},
    ),
    "normativa": {
        "label": "Normativa fiscale - CeRDEF",
        "kind": "cedef-normativa",
        "base": "https://def.finanze.it/DocTribFrontend/",
        "home": "https://def.finanze.it/DocTribFrontend/RS2_HomePage.jsp",
        # Form di ricerca avanzata dedicata (come per la prassi): evita il form
        # di ricerca "semplice" della home page, che non permette di fissare
        # correttamente il campo ambitoRicerca sul canale giusto.
        "search_url": "https://def.finanze.it/DocTribFrontend/callRicAvanzataNormativa.do?js_enabled=1&reset=y",
        # Una cartella per tipo di atto (normativa/legge, normativa/decreto-legislativo, ...):
        # i tipi sono decine, quindi si interroga tutto insieme e si smista per tipo.
        "split_by_type": True,
        "start_year": 1972,
    },
    "giurisprudenza": {
        "label": "Giurisprudenza tributaria - CeRDEF",
        "kind": "cedef-giurisprudenza",
        "base": "https://def.finanze.it/DocTribFrontend/",
        "home": "https://def.finanze.it/DocTribFrontend/RS2_HomePage.jsp",
        "search_url": "https://def.finanze.it/DocTribFrontend/callRicAvanzataGiurisprudenza.do?js_enabled=1&reset=y",
        # Una cartella per tipo di provvedimento (giurisprudenza/sentenza, giurisprudenza/ordinanza, ...).
        "split_by_type": True,
        "start_year": 1972,
    },
}

# Gruppi: il primo livello di cartella seleziona tutte le sue tipologie
# (--source agenzia-entrate).
SOURCE_GROUPS: dict[str, list[str]] = {}
for _name in SOURCES:
    if "/" in _name:
        SOURCE_GROUPS.setdefault(_name.split("/", 1)[0], []).append(_name)


def source_choices() -> list[str]:
    return list(SOURCE_GROUPS) + list(SOURCES) + ["all"]


def expand_source(source: str) -> list[str]:
    if source == "all":
        return list(SOURCES)
    return SOURCE_GROUPS.get(source) or [source]


# ---------------------------------------------------------------------------
# MODELLI / UTILITY
# ---------------------------------------------------------------------------

@dataclass
class Doc:
    source: str
    title: str
    document_date: str = ""
    authority: str = ""
    document_type: str = ""
    number: str = ""
    detail_url: str = ""
    download_url: str = ""
    local_file: str = ""
    sha256: str = ""
    downloaded_at: str = ""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def clean_title(s: str) -> str:
    """Titolo in testo semplice: CeRDEF a volte lo fornisce come frammento HTML."""
    s = re.sub(r"<!--.*?-->", "", s or "", flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return norm_text(html_lib.unescape(s))


def number_from_estremi(text: str) -> str:
    """Estrae il numero da estremi tipo "Circolare del 11/05/2001 n. 46 - ..."."""
    m = re.search(r"\bn\.\s*(\d+(?:\s*/\s*\w+)?)", text or "", re.I)
    return re.sub(r"\s+", "", m.group(1)) if m else ""


def ascii_slug(s: str, max_len: int = 150) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("/", "-").replace("\\", "-")
    s = re.sub(r"[^A-Za-z0-9._ -]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("._-")
    return (s[:max_len] or "documento").strip("._-")


def circolare_number(text: str) -> str:
    """Numero da testi ADM tipo "Circolare n. 19/2026", "Circolare 11/2026", "Circolare n° 18/D", "Circolare n. 9D"."""
    m = re.search(
        r"(?i:\bcirc(?:olare|\.)\s*(?:n[.°º]?\s*)?)(\d+(?:\s*/\s*(?:\d{4}|[A-Z]{1,3})\b|[A-Z]{1,2}\b)?)",
        text or "",
    )
    return re.sub(r"\s+", "", m.group(1)) if m else ""


def file_prefix(doc: "Doc") -> str:
    """Prefisso "data_tipo_numero_": rende il file riconoscibile senza manifest."""
    parts = [doc.document_date, doc.document_type if doc.number else "", doc.number]
    return "".join(ascii_slug(x, 40) + "_" for x in parts if x)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    """Scrive `data` su disco in modo durevole, mai un file vuoto o parziale.

    Scrittura su file temporaneo + fsync + rename: se la macchina si spegne a
    metà, resta il vecchio file (o nessuno), non un file a 0 byte.
    """
    if not data:
        raise ValueError(f"contenuto vuoto, file non salvato: {path.name}")
    tmp = path.with_name(path.name + ".part")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def same_domain(url: str, allowed: Iterable[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {x.lower() for x in allowed}


def strip_query(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def is_document_url(url: str) -> bool:
    """Link a un file. Liferay (ADM) li espone come /documents/.../nome.pdf/<uuid>?t=..."""
    path = urlparse(url).path.lower()
    return "/documents/" in path or path.endswith(DOC_EXTENSIONS)


def url_suffix(url: str) -> str:
    """Estensione dal path; Liferay mette il nome del file nel penultimo segmento."""
    path = urlparse(url).path
    for segment in reversed(path.split("/")[-2:]):
        suffix = Path(segment).suffix.lower()
        if suffix in DOC_EXTENSIONS:
            return suffix
    return Path(path).suffix


def manifest_key(d: dict) -> str:
    """Chiave che identifica un documento nel manifest.

    CeRDEF: la pagina di dettaglio. Crawl (ADM): l'URL del file senza query,
    perché una stessa pagina elenca molti file e ?t= cambia a ogni revisione.
    """
    detail = d.get("detail_url") or ""
    download = d.get("download_url") or ""
    if SOURCES.get(d.get("source"), {}).get("kind") == "crawl":
        return strip_query(detail if is_document_url(detail) else (download or detail))
    return detail or download


def is_pdf_response(resp: requests.Response, url: str) -> bool:
    ct = (resp.headers.get("content-type") or "").lower()
    return "application/pdf" in ct or urlparse(url).path.lower().endswith(".pdf")


def add_or_replace_query(url: str, **params) -> str:
    p = urlparse(url)
    q = parse_qs(p.query, keep_blank_values=True)
    for k, v in params.items():
        q[k] = [str(v)]
    query = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, query, p.fragment))


def guess_date(text: str) -> str:
    # dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy
    m = re.search(r"\b([0-3]?\d)[/.\-]([01]?\d)[/.\-]((?:19|20)\d{2})\b", text)
    if not m:
        return ""
    d, mo, y = map(int, m.groups())
    try:
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return ""


MESI = (
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
)


def guess_date_words(text: str) -> str:
    """Come guess_date, ma riconosce anche "28 dicembre 2007": vale la prima data del testo."""
    pattern = (
        r"\b([0-3]?\d)(?:[/.\-]([01]?\d)[/.\-]|°?\s+(" + "|".join(MESI) + r")\s+)((?:19|20)\d{2})\b"
    )
    for m in re.finditer(pattern, text or "", re.I):
        day, month, month_name, year = m.groups()
        try:
            return datetime(int(year), int(month) if month else MESI.index(month_name.lower()) + 1, int(day)).date().isoformat()
        except ValueError:
            continue
    return ""


def year_from_text(text: str) -> str:
    m = re.search(r"\b((?:19|20)\d{2})\b", text)
    return m.group(1) if m else "unknown"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class HostLimiter:
    """Ritmo massimo di richieste verso un host, condiviso fra tutti i thread."""

    def __init__(self, rate: float):
        self.max_rate = rate
        self.rate = rate
        self.next_at = 0.0
        self.last_slow_down = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        """Blocca finché non tocca a questa richiesta partire."""
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + 1.0 / self.rate
        if at > now:
            time.sleep(at - now)

    def slow_down(self, retry_after: float = 0.0) -> None:
        """L'host è in affanno (429, 503, timeout): dimezza il ritmo per tutti."""
        with self.lock:
            now = time.monotonic()
            # Più thread vedono lo stesso affanno: si dimezza una volta sola.
            if now - self.last_slow_down >= 2.0:
                self.last_slow_down = now
                self.rate = max(self.rate / 2, min(MIN_RATE, self.max_rate))
                log(f"  ~ host in affanno: ritmo ridotto a {self.rate:.1f} richieste/s")
            self.next_at = max(self.next_at, now + retry_after)

    def speed_up(self) -> None:
        """Richiesta riuscita: il ritmo risale poco alla volta verso il massimo."""
        with self.lock:
            self.rate = min(self.max_rate, self.rate + self.max_rate / 100)


def retry_after_seconds(resp: requests.Response) -> float:
    value = (resp.headers.get("Retry-After") or "").strip()
    return min(float(value), 120.0) if value.isdigit() else 0.0


class Http:
    def __init__(self):
        self.local = threading.local()
        self.limiters: dict[str, HostLimiter] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(WORKERS, thread_name_prefix="doc") if WORKERS > 1 else None

    @property
    def s(self) -> requests.Session:
        """Una sessione per thread: quella del thread principale tiene lo stato
        della ricerca CeRDEF (paginatorXml.do), i worker non la toccano."""
        s = getattr(self.local, "s", None)
        if s is None:
            s = self.local.s = requests.Session()
            s.headers.update({
                "User-Agent": UA,
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.6",
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            })
        return s

    def limiter(self, url: str) -> HostLimiter:
        host = urlparse(url).netloc.lower()
        with self.lock:
            if host not in self.limiters:
                self.limiters[host] = HostLimiter(RATE)
            return self.limiters[host]

    def map(self, fn, items) -> list:
        """fn su ogni elemento, in parallelo sui worker; risultati in ordine."""
        if self.pool is None:
            return [fn(x) for x in items]
        futures = [self.pool.submit(fn, x) for x in items]
        try:
            return [f.result() for f in futures]
        except BaseException:
            # Ctrl-C: i documenti non ancora iniziati non devono partire.
            for f in futures:
                f.cancel()
            raise

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        limiter = self.limiter(url)
        last = None
        for n in range(MAX_RETRIES):
            limiter.wait()
            try:
                r = self.s.request(
                    method, url, timeout=TIMEOUT, allow_redirects=True, **kwargs
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    if r.status_code in (429, 503):
                        limiter.slow_down(retry_after_seconds(r))
                    time.sleep(1.5 * (n + 1))
                    last = RuntimeError(f"HTTP {r.status_code}: {url}")
                    continue
                r.raise_for_status()
                limiter.speed_up()
                return r
            except Exception as e:
                if isinstance(e, requests.Timeout):
                    limiter.slow_down()
                last = e
                time.sleep(1.5 * (n + 1))
        raise RuntimeError(f"Errore HTTP dopo {MAX_RETRIES} tentativi: {url}: {last}")

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)


# ---------------------------------------------------------------------------
# STORAGE
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, root: Path, source: str, mode: str):
        self.source = source
        self.root = root / source
        self.files = self.root / "files"
        self.manifest_path = self.root / "manifest.jsonl"
        self.error_path = self.root / "errors.log"
        # manifest ed errors.log sono scritti dai worker: uno alla volta.
        self.lock = threading.Lock()

        if mode == "rewrite" and self.root.exists():
            shutil.rmtree(self.root)

        self.files.mkdir(parents=True, exist_ok=True)
        self.existing = self._load_manifest()

    def _load_manifest(self) -> dict[str, dict]:
        out = {}
        if not self.manifest_path.exists():
            return out
        with self.manifest_path.open("r", encoding="utf-8") as f:
            for line in f:
                # i NUL sono il residuo di uno spegnimento brusco durante l'append
                line = line.replace("\x00", "").strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    key = manifest_key(d)
                    if key:
                        out[key] = d
                except Exception:
                    pass
        return out

    def append(self, doc: Doc):
        with self.lock:
            with self.manifest_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(doc), ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.existing[manifest_key(asdict(doc))] = asdict(doc)

    def has_file(self, old: Optional[dict]) -> bool:
        """True se il documento in manifest ha davvero un file non vuoto su disco."""
        if not old or not old.get("local_file"):
            return False
        path = self.root / old["local_file"]
        return path.is_file() and path.stat().st_size > 0

    def error(self, msg: str):
        with self.lock, self.error_path.open("a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")

    def compact(self):
        """Riscrive il manifest con una sola riga per documento (l'ultima)."""
        if not self.existing:
            return
        lines = (json.dumps(d, ensure_ascii=False) + "\n" for d in self.existing.values())
        write_atomic(self.manifest_path, "".join(lines).encode("utf-8"))

    def for_type(self, tipo: str) -> "Store":
        """Store in cui va un documento del tipo indicato (vedi SplitStore)."""
        return self

    def choose_path(self, title: str, date: str, url: str, ext: str, prefix: str = "") -> Path:
        year = (date[:4] if date else year_from_text(title))
        folder = self.files / year
        folder.mkdir(parents=True, exist_ok=True)

        base = ascii_slug(title, 120)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        return folder / f"{prefix}{base}__{digest}{ext}"


class SplitStore:
    """Raccolta suddivisa per tipologia: <target>/<source>/<tipologia>/.

    Ogni tipologia è uno Store completo (files/, manifest.jsonl), creato alla
    prima occorrenza del tipo: la raccolta si interroga una volta sola e i
    documenti vengono smistati in base al tipo dichiarato negli estremi.
    errors.log e debug/ restano nella cartella della raccolta.
    """

    def __init__(self, root: Path, source: str, mode: str):
        self.source = source
        self.target = root
        self.root = root / source
        self.error_path = self.root / "errors.log"
        self.lock = threading.Lock()
        self.subs: dict[str, Store] = {}

        if mode == "rewrite" and self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def for_type(self, tipo: str) -> Store:
        folder = tipologia_folder(tipo)
        with self.lock:
            if folder not in self.subs:
                self.subs[folder] = Store(self.target, f"{self.source}/{folder}", "refresh")
            return self.subs[folder]

    def error(self, msg: str):
        with self.lock, self.error_path.open("a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")

    def compact(self):
        for sub in self.subs.values():
            sub.compact()


# ---------------------------------------------------------------------------
# DOWNLOAD
# ---------------------------------------------------------------------------

def relocate_existing(store: Store, doc: Doc, old: dict, out: Path) -> None:
    """Documento già scaricato: allinea nome file e metadati senza riscaricarlo."""
    old_path = store.root / old["local_file"]
    if out == old_path and old.get("title") == doc.title and old.get("number", "") == doc.number:
        return None
    if out != old_path:
        os.replace(old_path, out)
    doc.download_url = old.get("download_url") or doc.download_url
    doc.local_file = str(out.relative_to(store.root))
    doc.sha256 = old.get("sha256", "")
    doc.downloaded_at = old.get("downloaded_at", "")
    store.append(doc)
    print(f"  ~ rinominato: {doc.local_file}")
    return None


def download_document(
    http: Http,
    store: Store,
    doc: Doc,
    mode: str,
    content: Optional[bytes] = None,
    ext: Optional[str] = None,
) -> Optional[Doc]:
    old = store.existing.get(manifest_key(asdict(doc)))
    have_old = mode == "refresh" and store.has_file(old)
    prefix = file_prefix(doc)

    if content is not None:
        # Contenuto già assemblato (es. testo normativa ricostruito da più
        # richieste): nessun fetch, scriviamo direttamente quanto ricevuto.
        out = store.choose_path(doc.title, doc.document_date, doc.download_url or doc.detail_url, ext or ".html", prefix)
        if have_old and old.get("sha256") == hashlib.sha256(content).hexdigest():
            return relocate_existing(store, doc, old, out)
        write_atomic(out, content)
        if have_old and store.root / old["local_file"] != out:
            (store.root / old["local_file"]).unlink()
        doc.local_file = str(out.relative_to(store.root))
        doc.sha256 = sha256_file(out)
        doc.downloaded_at = now_iso()
        store.append(doc)
        print(f"  + {doc.title} -> {doc.local_file}")
        return doc

    url = doc.download_url or doc.detail_url
    if have_old:
        # Stesso URL finale e stessa estensione del primo download: il nome
        # atteso si ricalcola senza rifare la richiesta.
        old_path = store.root / old["local_file"]
        out = store.choose_path(doc.title, doc.document_date, old.get("download_url") or url, old_path.suffix, prefix)
        return relocate_existing(store, doc, old, out)

    r = http.get(url)

    ct = (r.headers.get("content-type") or "").lower()
    if is_pdf_response(r, r.url):
        ext = ".pdf"
    elif "text/html" in ct or r.text.lstrip().lower().startswith("<!doctype"):
        ext = ".html"
    elif "text/plain" in ct:
        ext = ".txt"
    else:
        # Per un RAG è meglio conservare comunque il documento con estensione binaria.
        ext = url_suffix(r.url) or ".bin"

    out = store.choose_path(doc.title, doc.document_date, r.url, ext, prefix)
    write_atomic(out, r.content)

    doc.download_url = r.url
    doc.local_file = str(out.relative_to(store.root))
    doc.sha256 = sha256_file(out)
    doc.downloaded_at = now_iso()

    store.append(doc)
    print(f"  + {doc.title} -> {doc.local_file}")
    return doc


# ---------------------------------------------------------------------------
# HTML HELPERS
# ---------------------------------------------------------------------------

def soup_of(resp: requests.Response) -> BeautifulSoup:
    return BeautifulSoup(resp.text, "html.parser")


def links_from(soup: BeautifulSoup, base_url: str):
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a.get("href"))
        text = norm_text(a.get_text(" ", strip=True))
        yield a, href, text


def content_links(soup: BeautifulSoup, base_url: str):
    """Link del corpo pagina: esclude menu laterale, breadcrumb, header e footer."""
    root = soup.find(id="main-content") or soup
    for a, href, text in links_from(root, base_url):
        if a.find_parent(["nav", "header", "footer"]) is None:
            yield a, urldefrag(href)[0], text


def page_title_of(soup: BeautifulSoup) -> str:
    """<title> senza il nome del sito in coda ("... - Agenzia delle dogane e dei Monopoli")."""
    title = norm_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    return title.rsplit(" - ", 1)[0] if " - " in title else title


# ---------------------------------------------------------------------------
# CRAWLER GENERICO (ADM)
# ---------------------------------------------------------------------------

def crawl_doc(source_name: str, a, href: str, text: str, page_title: str) -> Doc:
    """Metadati di un file a partire dalla voce d'elenco che lo contiene."""
    # La voce (<li>) dice più del solo link: "[343856 del 19/06/2026] –
    # Circolare n. 16/2026 - <a>Sostituita dalla circolare n. 19/2026</a>".
    item = a.find_parent("li")
    if item is not None:
        item_text = norm_text(item.get_text(" ", strip=True))
        files = sum(1 for x in item.find_all("a", href=True) if is_document_url(x["href"]))
        if files == 1 and item_text and len(item_text) <= 500:
            text = item_text

    title = context = text or page_title or Path(urlparse(href).path).name
    # Allegato nella pagina dedicata a una singola circolare ("Linee guida",
    # "Allegato 1"): da solo sarebbe irriconoscibile, lo leghiamo alla pagina.
    if text and not guess_date_words(text) and "circolar" in page_title.lower() and re.search(r"\d", page_title):
        context = page_title
        if text.lower() not in page_title.lower():
            title = f"{page_title} – {text}"

    return Doc(
        source=source_name,
        title=title,
        document_date=guess_date_words(context),
        authority="Agenzia delle Dogane e dei Monopoli",
        document_type="Circolare",
        number=circolare_number(context),
        detail_url=strip_query(href),
        download_url=href,
    )


def crawl_source(http: Http, store: Store, source_name: str, cfg: dict, mode: str):
    # (url, testo del link, la pagina di provenienza è una pagina di sole circolari)
    queue = [(url, "", False) for url in cfg["seeds"]]
    seen = set()
    docs_seen = set()
    allowed = cfg["allowed_domains"]
    stats = {"pages": 0, "found": 0, "downloaded": 0, "skipped_existing": 0, "rejected_type": 0}

    def is_new(doc: Doc) -> bool:
        # Lo stesso file è linkato dall'elenco e dalla pagina di dettaglio:
        # vale la prima occorrenza, così il titolo non cambia a ogni giro.
        key = manifest_key(asdict(doc))
        if key in docs_seen:
            return False
        docs_seen.add(key)
        stats["found"] += 1
        return True

    def save(job) -> Optional[str]:
        """Eseguita dai worker: ritorna il contatore da incrementare."""
        doc, content = job
        try:
            result = download_document(http, store, doc, mode, content=content, ext=".pdf" if content else None)
        except Exception as e:
            store.error(f"{doc.download_url} -> {e}")
            print(f"  ! {e}")
            return None
        return "downloaded" if result else "skipped_existing"

    def save_all(jobs: list):
        for counter in http.map(save, jobs):
            if counter:
                stats[counter] += 1

    while queue:
        if len(seen) >= MAX_PAGES_PER_SOURCE:
            print("Limite MAX_PAGES_PER_SOURCE raggiunto.")
            break

        url, link_text, from_circolari = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        try:
            r = http.get(url)
        except Exception as e:
            store.error(str(e))
            print(f"  ! {e}")
            continue
        seen.add(r.url)

        if is_pdf_response(r, r.url):
            # Un link seguito come pagina era in realtà un file: è già scaricato dal GET.
            doc = Doc(
                source=source_name,
                title=link_text or Path(urlparse(r.url).path).stem,
                document_date=guess_date_words(link_text),
                authority="Agenzia delle Dogane e dei Monopoli",
                document_type="Circolare",
                number=circolare_number(link_text),
                detail_url=strip_query(r.url),
                download_url=r.url,
            )
            if is_new(doc):
                save_all([(doc, r.content)])
            continue

        stats["pages"] += 1
        soup = soup_of(r)
        page_title = page_title_of(soup)
        is_archive_index = page_title.lower().startswith("archiv")
        # "Circolari e Direttive", la pagina di una circolare, e ciò che ne
        # discende (archivio, anni): lì tutto è circolare, anche le voci che
        # non lo dicono ("[509 del 11/01/2007] - Distributori automatici...").
        only_circolari = from_circolari or "circolar" in page_title.lower()
        log(f"  pagina: {page_title or r.url}")

        # I file della pagina si scaricano in parallelo, le pagine una alla volta.
        jobs = []
        for a, href, text in content_links(soup, r.url):
            if not same_domain(href, allowed):
                continue

            if is_document_url(href):
                doc = crawl_doc(source_name, a, href, text, page_title)
                low = (doc.title + " " + href).lower()
                if only_circolari or any(k in low for k in cfg.get("document_keywords", [])):
                    if is_new(doc):
                        jobs.append((doc, None))
                else:
                    stats["rejected_type"] += 1
                    log(f"    scartato tipo: {doc.title}", 2)
                continue

            low = (text + " " + href).lower()
            if is_archive_index or any(k in low for k in cfg.get("follow_keywords", [])):
                if href not in seen:
                    queue.append((href, text, only_circolari))

        save_all(jobs)

    print("\nRIEPILOGO CRAWL")
    print(f"  pagine visitate        : {stats['pages']}")
    print(f"  documenti trovati      : {stats['found']}")
    print(f"  scaricati              : {stats['downloaded']}")
    print(f"  già presenti           : {stats['skipped_existing']}")
    print(f"  scartati (non circolari): {stats['rejected_type']}")


# ---------------------------------------------------------------------------
# LOGGING / DEBUG
# ---------------------------------------------------------------------------

def log(msg: str, level: int = 1):
    if VERBOSE >= level:
        print(msg, flush=True)


def dump_debug(store: Store, name: str, content: str):
    d = store.root / "debug"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(content, encoding="utf-8", errors="replace")
    return path

# ---------------------------------------------------------------------------
# CEDEF: FORM DISCOVERY + RESULT WALKER
# ---------------------------------------------------------------------------

def form_context(form) -> str:
    return norm_text(form.get_text(" ", strip=True)).lower()


def field_label(form, element) -> str:
    # label[for=id]
    eid = element.get("id")
    if eid:
        lab = form.find("label", attrs={"for": eid})
        if lab:
            return norm_text(lab.get_text(" ", strip=True))
    # parent text fallback
    p = element.parent
    return norm_text(p.get_text(" ", strip=True))[:250] if p else ""


def choose_option(select, wanted_fragments: list[str]) -> Optional[str]:
    wanted = [w.lower() for w in wanted_fragments]
    scored = []
    for opt in select.find_all("option"):
        txt = norm_text(opt.get_text(" ", strip=True))
        low = txt.lower()
        score = sum(1 for w in wanted if w in low)
        if score:
            scored.append((score, len(txt), opt.get("value", "")))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][2]


def find_cedef_form(soup: BeautifulSoup, channel: str):
    forms = soup.find_all("form")
    if not forms:
        return None

    channel_words = {
        "prassi": ["prassi", "autorità", "autorita", "estremi"],
        "normativa": ["normativa", "gazzetta", "tipo atto", "estremi"],
        "giurisprudenza": ["giurisprudenza", "autorità", "autorita", "estremi"],
    }[channel]

    scored = []
    for f in forms:
        text = form_context(f)
        score = sum(1 for w in channel_words if w in text)
        # bonus per molti campi
        score += min(5, len(f.find_all(["input", "select"]))) / 10
        scored.append((score, f))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else None


def _is_authority_select(form, sel, cfg: dict) -> bool:
    label = field_label(form, sel).lower()
    options_text = " ".join(
        norm_text(o.get_text(" ", strip=True)).lower()
        for o in sel.find_all("option")
    )
    return "autor" in label or any(
        x.lower() in options_text for x in cfg.get("authority_contains", [])
    )


def cedef_authority_values(form, cfg: dict, channel: str) -> list[Optional[str]]:
    """VALUE della select autorità da interrogare, uno per ricerca.

    La select accetta una sola autorità alla volta: per coprire tutte quelle
    di authority_contains serve una ricerca distinta per ciascuna. [None]
    significa "nessun filtro per autorità" (una sola ricerca).
    """
    wanted = cfg.get("authority_contains") or []
    if channel != "prassi" or not wanted:
        return [None]
    values: list[Optional[str]] = []
    for sel in form.find_all("select"):
        if not sel.get("name") or not _is_authority_select(form, sel, cfg):
            continue
        for w in wanted:
            v = choose_option(sel, [w])
            if v is not None and v not in values:
                values.append(v)
    return values or [None]


def build_form_payload(form, cfg: dict, channel: str, year: int, authority: Optional[str] = None) -> dict:
    """Serializza la form in modo simile a un browser reale, poi applica i filtri.

    CeRDEF usa JavaScript per modificare alcuni hidden prima del submit; requests
    non esegue JS, quindi forziamo esplicitamente js_enabled=1.
    """
    payload = {}

    # 1) INPUT: includi tutti i controlli "successful" che un browser invierebbe.
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()

        if typ in ("button", "reset", "image", "file"):
            continue
        if typ in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue

        payload[name] = inp.get("value", "")

    # 2) TEXTAREA
    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if name:
            payload[name] = ta.get_text() or ""

    # 3) SELECT: usa l'opzione selected, oppure la prima opzione come il browser.
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        selected = sel.find_all("option", selected=True)
        if not selected:
            first = sel.find("option")
            selected = [first] if first else []
        if selected:
            payload[name] = selected[0].get("value", "")

    # CeRDEF imposta questo campo via JavaScript. requests non lo fa.
    if "js_enabled" in payload:
        payload["js_enabled"] = "1"

    # 4) Applica i filtri sui SELECT usando i VALUE reali delle option.
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        label = field_label(form, sel).lower()
        options_text = " ".join(
            norm_text(o.get_text(" ", strip=True)).lower()
            for o in sel.find_all("option")
        )

        value = None

        if channel == "prassi" and (
            "tipo" in label or "circolare" in options_text
        ):
            value = choose_option(sel, [cfg.get("document_type", "Circolare")])

        if channel == "prassi" and _is_authority_select(form, sel, cfg):
            value = authority if authority is not None else choose_option(sel, cfg.get("authority_contains", []))

        if "canale" in label or "macro" in label or "tipologia" in label:
            value = value or choose_option(sel, [channel])

        if value is not None:
            payload[name] = value

    # 5) Anno: CeRDEF accetta anche il solo anno. Per una scansione annuale
    # impostiamo sia "da" sia "a" quando entrambi sono presenti.
    year_fields = []
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()
        if typ not in ("text", "number", "search"):
            continue
        label = (field_label(form, inp) + " " + name).lower()
        if "anno" in label:
            payload[name] = str(year)
            year_fields.append(name)

    return payload

def submit_form(http: Http, page_url: str, form, payload: dict) -> requests.Response:
    action = urljoin(page_url, form.get("action") or page_url)
    method = (form.get("method") or "get").lower()
    if method == "post":
        return http.post(action, data=payload)
    return http.get(action, params=payload)


def _extract_urls_from_js(js: str, base_url: str):
    """Estrae URL HTTP/relativi incorporati in onclick/javascript."""
    if not js:
        return []
    out = []
    # URL assoluti o path .do/.jsp, con o senza query string (es. CeRDEF usa
    # window.location.href='paginatorXml.do' senza alcun parametro).
    for m in re.finditer(r"(?:https?://[^'\"\s)]+|[A-Za-z0-9_./-]+(?:\.do|\.jsp)(?:\?[^'\"\s)]+)?)", js):
        u = m.group(0).replace("&amp;", "&")
        out.append(urljoin(base_url, u))
    return out


# ---------------------------------------------------------------------------
# CEDEF: estrazione dati dall'XML incorporato nello <script>
# ---------------------------------------------------------------------------
#
# CeRDEF NON rende i risultati lato server: li incorpora come stringa XML
# dentro una variabile JavaScript (es. `var xmlResult = '<?xml ...>...';`)
# in un <script>, e li inietta nel DOM solo tramite jQuery lato client.
# requests/BeautifulSoup non eseguono JS, quindi il markup statico non
# contiene mai i link ai documenti: vanno letti direttamente da quell'XML.

_JS_STR_RE_CACHE: dict[str, re.Pattern] = {}


_JS_ESCAPE_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_JS_ESCAPE_CHARS = {"n": "\n", "r": "\r", "t": "\t"}


def _js_unescape(m: re.Match) -> str:
    esc = m.group(1)
    if len(esc) > 1:
        return chr(int(esc[1:], 16))
    # In JS un backslash davanti a un carattere qualsiasi vale il carattere stesso.
    return _JS_ESCAPE_CHARS.get(esc, esc)


def extract_js_xml_var(html: str, varname: str) -> str:
    """Estrae e deescapa il contenuto di `var <varname> = '...';`."""
    pattern = _JS_STR_RE_CACHE.get(varname)
    if pattern is None:
        pattern = re.compile(r"var\s+" + re.escape(varname) + r"\s*=\s*'((?:[^'\\]|\\.)*)'\s*;")
        _JS_STR_RE_CACHE[varname] = pattern
    m = pattern.search(html)
    if not m:
        return ""
    return _JS_ESCAPE_RE.sub(_js_unescape, m.group(1))


def parse_embedded_xml(xml_str: str) -> Optional[ET.Element]:
    xml_str = (xml_str or "").strip()
    if not xml_str:
        return None
    # Mitigazione XXE/billion-laughs: l'XML incorporato da CeRDEF non ha mai
    # una DTD; un DOCTYPE/ENTITY indicherebbe una risposta manomessa (MITM) e
    # va rifiutato prima di passarlo al parser expat di ElementTree.
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_str, re.I):
        return None
    # ElementTree non accetta una dichiarazione <?xml ... encoding=...?> su
    # una stringa python (solo su bytes): la stringa è già testo decodificato.
    xml_str = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_str).strip()
    if not xml_str:
        return None
    try:
        return ET.fromstring(xml_str)
    except ET.ParseError:
        return None


CEDEF_DETAIL_ENDPOINT = {
    "prassi": "getPrassiDetail.do",
    "normativa": "getAttoNormativoDetail.do",
    "giurisprudenza": "getGiurisprudenzaDetail.do",
}


def cedef_listing_from_html(html: str):
    """Legge l'elenco documenti e la paginazione dall'XML incorporato (xmlResult).

    Ritorna (items, pagination) dove items è una lista di tuple
    (guid, estremi, titolo) e pagination contiene i contatori del batch
    corrente (CeRDEF restituisce i risultati in blocchi da 50 alla volta).
    """
    root = parse_embedded_xml(extract_js_xml_var(html, "xmlResult"))
    items: list[tuple[str, str, str]] = []
    pagination = {"pagina": "", "ultimaPagina": "", "totale": "", "ulteriori": False}
    if root is None:
        return items, pagination

    risultati = root.find("risultati")
    if risultati is not None:
        pagination["pagina"] = norm_text(risultati.findtext("pagina") or "")
        pagination["ultimaPagina"] = norm_text(risultati.findtext("ultimaPagina") or "")
        pagination["totale"] = norm_text(risultati.findtext("totaleProvvedimenti") or "")
        pagination["ulteriori"] = (
            norm_text(risultati.findtext("ulterioriRisultati") or "").lower() == "true"
        )

    for prov in root.iter("Provvedimento"):
        guid = prov.get("idProvvedimento")
        if not guid:
            continue
        estremi = norm_text(prov.findtext("estremi") or "")
        titoli = [norm_text(t.text or "") for t in prov.iter("titoloProvvedimento")]
        titolo = norm_text(" ".join(t for t in titoli if t))
        items.append((guid, estremi, titolo))

    return items, pagination


def cedef_next_links(soup: BeautifulSoup, base_url: str):
    """Restituisce solo URL realmente navigabili di paginazione.

    Gli href javascript:void(0) NON sono URL e non devono mai arrivare a
    requests. Se la paginazione è gestita esclusivamente da JS, viene loggata
    separatamente in walk_cedef_results per poter implementare il POST corretto.
    """
    seen = set()
    for a in soup.find_all("a"):
        text = norm_text(a.get_text(" ", strip=True))
        low = text.lower()
        if not (
            low in ("avanti", "successiva", "successivo", ">", ">>")
            or "pagina successiva" in low
            or "next" in low
        ):
            continue

        href_raw = (a.get("href") or "").strip()
        onclick = (a.get("onclick") or "").strip()
        candidates = []
        # "#" (o altro frammento puro) non è un URL di paginazione: urljoin lo
        # risolverebbe nella pagina corrente stessa, facendola sembrare una
        # pagina successiva valida mentre non lo è.
        if href_raw and not href_raw.lower().startswith("javascript:") and not href_raw.startswith("#"):
            href = urljoin(base_url, href_raw)
            if href != base_url:
                candidates.append(href)
        # Una navigazione esplicita via JS vale anche verso la pagina corrente:
        # paginatorXml.do è stateful e dal secondo batch in poi rimanda a sé stesso.
        candidates.extend(_extract_urls_from_js(onclick, base_url))

        for href in candidates:
            if href.startswith("http") and href not in seen:
                seen.add(href)
                yield href


def _authority_from_estremi(text: str) -> str:
    m = re.search(r"-(\s*(?:Agenzia|Min\.|Ministero|Dipartimento)[^-]{2,120})", text, flags=re.I)
    return norm_text(m.group(1)) if m else ""


def _doctype_from_estremi(text: str, default: str) -> str:
    # Il tipo è tutto ciò che precede la data: "Decreto del Presidente della
    # Repubblica del 26/10/1972 n. 633", "Decreto-legge del ...".
    m = re.match(r"\s*(.+?)\s+del\s+\d{1,2}/\d{1,2}/\d{4}", text) or re.match(r"\s*([A-Za-zÀ-ÿ'\. -]+?)\s+del\b", text)
    return norm_text(m.group(1)) if m else default


def tipologia_folder(tipo: str) -> str:
    """Cartella di una tipologia ricavata dal tipo CeRDEF: "Decreto legislativo" -> "decreto-legislativo"."""
    if not norm_text(tipo):
        return "non-classificati"
    return ascii_slug(tipo, 80).lower().replace("_", "-").replace(".", "")


def _synthetic_html(title: str, *parts: str) -> bytes:
    body = "".join(p for p in parts if p)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>"
    ).encode("utf-8")


def _parse_prassi_multiparte(http: Http, source_name: str, detail_url: str, html: str):
    """Provvedimento in più parti: l'indice è in xmlSommario e ogni parte va
    richiesta a getParteDetail.do (testo in xmlParte). Le parti vengono
    accodate in un unico documento HTML. Ritorna None se non c'è un sommario.
    """
    root = parse_embedded_xml(extract_js_xml_var(html, "xmlSommario"))
    if root is None or root.find("sommario/itemSommario") is None:
        return None

    estremi = norm_text(root.findtext("intestazione/estremiPrassi") or "")
    oggetto = clean_title(root.findtext("intestazione/oggetto") or "")
    title = oggetto or estremi or detail_url
    id_prassi = norm_text(root.findtext("id") or "")

    doc = Doc(
        source=source_name,
        title=title,
        document_date=guess_date(estremi),
        authority=_authority_from_estremi(estremi),
        document_type=_doctype_from_estremi(estremi, "Prassi"),
        number=number_from_estremi(estremi),
        detail_url=detail_url,
        download_url=detail_url,
    )

    chunks = [f"<p>{estremi}</p>"]
    for item in root.iterfind("sommario/itemSommario"):
        parte_id = item.get("id") or ""
        label = norm_text(item.get("parte") or "")
        parte_url = urljoin(
            detail_url,
            "getParteDetail.do?idPrassi=" + quote(id_prassi, safe="") + "&id=" + quote(parte_id, safe=""),
        )
        proot = parse_embedded_xml(extract_js_xml_var(http.get(parte_url).text, "xmlParte"))
        parte = proot.find("parte") if proot is not None else None
        if parte is None:
            # Meglio un errore in errors.log che un documento salvato a metà.
            raise RuntimeError(f"{label or parte_id}: parte non leggibile ({parte_url})")

        sintesi = (parte.findtext("sintesi") or item.findtext("sintesi") or "").strip()
        testo = (parte.findtext("testo") or "").strip()
        allegato = ""
        if parte.get("allegato") == "true":
            pdf_url = urljoin(detail_url, "getContent.do?id=" + quote(parte_id, safe=""))
            allegato = f'<p><a href="{pdf_url}">Allegato PDF</a></p>'
            log(f"         parte con allegato PDF non incorporato: {pdf_url}")
        chunks.append(f"<h2>{label}</h2>" + (f"<p>{sintesi}</p>" if sintesi else "") + testo + allegato)

    return doc, _synthetic_html(title, *chunks)


def _parse_prassi_detail(http: Http, source_name: str, detail_url: str, html: str):
    """Legge il dettaglio prassi dall'XML incorporato (xmlParteUnica).

    I provvedimenti in più parti non hanno xmlParteUnica ma un sommario:
    vedi _parse_prassi_multiparte.
    """
    root = parse_embedded_xml(extract_js_xml_var(html, "xmlParteUnica"))
    if root is None:
        multi = _parse_prassi_multiparte(http, source_name, detail_url, html)
        if multi is not None:
            return multi
        doc = Doc(source=source_name, title=detail_url, detail_url=detail_url, download_url=detail_url)
        return doc, None

    estremi = norm_text(root.findtext("intestazione/estremiPrassi") or "")
    oggetto = clean_title(root.findtext("intestazione/oggetto") or "")
    title = oggetto or estremi or detail_url

    doc = Doc(
        source=source_name,
        title=title,
        document_date=guess_date(estremi),
        authority=_authority_from_estremi(estremi),
        document_type=_doctype_from_estremi(estremi, "Prassi"),
        number=number_from_estremi(estremi),
        detail_url=detail_url,
        download_url=detail_url,
    )

    parte = root.find("parte")
    if parte is not None and parte.get("allegato") == "true":
        parte_id = parte.get("id") or ""
        if parte_id:
            doc.download_url = urljoin(detail_url, "getContent.do?id=" + quote(parte_id, safe=""))
            return doc, None  # download_document scarica il PDF reale

    # Nessun PDF allegato: il testo integrale è comunque nell'XML incorporato,
    # quindi lo salviamo direttamente invece della pagina HTML non renderizzata.
    sintesi = ((parte.findtext("sintesi") if parte is not None else "") or "").strip()
    testo = ((parte.findtext("testo") if parte is not None else "") or "").strip()
    content = _synthetic_html(title, f"<p>{estremi}</p>", testo or sintesi or "<p><em>Nessun testo disponibile.</em></p>")
    return doc, content


def _parse_giurisprudenza_detail(source_name: str, detail_url: str, html: str):
    """Legge il dettaglio giurisprudenza dall'XML incorporato (xmlDettaglio)."""
    root = parse_embedded_xml(extract_js_xml_var(html, "xmlDettaglio"))
    prov = root.find(".//Provvedimento") if root is not None else None
    if prov is None:
        doc = Doc(source=source_name, title=detail_url, detail_url=detail_url, download_url=detail_url)
        return doc, None

    estremi = norm_text(prov.findtext("estremi") or "")
    intitolazione = clean_title(prov.findtext(".//intitolazione") or "")
    massima = ((prov.findtext(".//massima") or "")).strip()
    title = intitolazione or clean_title(massima) or estremi or detail_url

    doc = Doc(
        source=source_name,
        title=title,
        document_date=guess_date(estremi),
        authority=_authority_from_estremi(estremi),
        document_type=_doctype_from_estremi(estremi, "Giurisprudenza"),
        number=number_from_estremi(estremi),
        detail_url=detail_url,
        download_url=detail_url,
    )

    if prov.get("allegato") == "true":
        guid = prov.get("idProvvedimento") or ""
        if guid:
            doc.download_url = urljoin(detail_url, "getContent.do?id=" + quote(guid, safe=""))
            return doc, None  # download_document scarica il PDF reale

    # Nessun allegato: salviamo direttamente il testo/massima già estratti.
    content = _synthetic_html(title, f"<p>{estremi}</p>", massima or "<p><em>Nessun testo disponibile.</em></p>")
    return doc, content


MAX_ARTICOLI_PER_ATTO = 400


def _parse_normativa_detail(http: Http, source_name: str, detail_url: str, html: str):
    """Ricostruisce il testo di un atto normativo dall'XML incorporato.

    Normativa non ha mai un singolo PDF scaricabile: il portale espone il
    testo articolo per articolo (xmlSommario elenca gli articoli, ciascuno
    va recuperato con una richiesta dedicata che restituisce xmlArticoli).
    Qui concateniamo tutti gli articoli in un unico documento HTML.
    Ritorna (Doc, contenuto_bytes) perché non esiste un singolo download_url
    da passare a download_document: il contenuto va assemblato qui.
    """
    root = parse_embedded_xml(extract_js_xml_var(html, "xmlSommario"))
    if root is None:
        doc = Doc(source=source_name, title=detail_url, detail_url=detail_url, download_url=detail_url)
        return doc, None

    estremi = norm_text(root.findtext("intestazione/estremiAtto") or "")
    titolo_atto = clean_title(root.findtext("intestazione/titoloAtto") or "")
    gazzetta = norm_text(root.findtext("intestazione/gazzettaUfficiale") or "")
    title = titolo_atto or estremi or detail_url
    atto_id = norm_text(root.findtext("id") or "")

    parts_html = []
    n = 0
    for item in root.iter("itemSommario"):
        if n >= MAX_ARTICOLI_PER_ATTO:
            parts_html.append("<p><em>[troncato: limite articoli raggiunto]</em></p>")
            break
        label = item.get("articolo") or ""
        cod = item.get("codOrdinamento") or ""
        if not (atto_id and cod):
            continue
        n += 1
        art_url = urljoin(
            detail_url,
            "getAttoNormativoDetail.do?ACTION=getArticolo&id=" + quote(atto_id, safe="")
            + "&codiceOrdinamento=" + quote(cod, safe="")
            + "&articolo=" + quote(label, safe=""),
        )
        try:
            ar = http.get(art_url)
        except Exception:
            continue
        art_root = parse_embedded_xml(extract_js_xml_var(ar.text, "xmlArticoli"))
        if art_root is None:
            continue
        for art in art_root.iter("articolo"):
            art_title = norm_text(art.findtext("titolo") or label)
            art_testo = (art.findtext("testo") or "").strip()
            if art_testo:
                parts_html.append(f"<h2>{art_title}</h2>\n{art_testo}")

    body = "\n".join(parts_html) or "<p><em>Nessun testo articolo disponibile.</em></p>"
    html_doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><h1>{title}</h1>"
        f"<p>{estremi}</p><p>{gazzetta}</p>{body}</body></html>"
    )

    doc = Doc(
        source=source_name,
        title=title,
        document_date=guess_date(estremi),
        authority="",
        document_type=_doctype_from_estremi(estremi, ""),
        number=number_from_estremi(estremi),
        detail_url=detail_url,
        download_url=detail_url,
    )
    return doc, html_doc.encode("utf-8")


def parse_cedef_detail(http: Http, source_name: str, channel: str, detail_url: str, html: str):
    """Ritorna (Doc, contenuto_bytes_o_None) per il canale CeRDEF indicato.

    contenuto_bytes è None quando esiste un vero allegato PDF scaricabile
    (download_document fa il fetch di doc.download_url), altrimenti è il
    testo/XML già estratto, assemblato qui in un piccolo documento HTML.
    """
    if channel == "prassi":
        return _parse_prassi_detail(http, source_name, detail_url, html)
    if channel == "giurisprudenza":
        return _parse_giurisprudenza_detail(source_name, detail_url, html)
    if channel == "normativa":
        return _parse_normativa_detail(http, source_name, detail_url, html)
    raise ValueError(f"canale CeDEF sconosciuto: {channel}")


def matches_authority(doc: Doc, cfg: dict) -> bool:
    wanted = cfg.get("authority_contains")
    if not wanted:
        return True
    hay = (doc.title + " " + doc.authority).lower()
    return any(w.lower() in hay for w in wanted)


def matches_document_type(doc: Doc, cfg: dict) -> bool:
    wanted = cfg.get("document_type")
    if not wanted:
        return True
    return wanted.lower() in (doc.title + " " + doc.document_type).lower()


def walk_cedef_results(
    http: Http,
    store: Store,
    source_name: str,
    cfg: dict,
    channel: str,
    first_response: requests.Response,
    mode: str,
    year: int,
):
    queue = [first_response.url]
    html_cache = {first_response.url: first_response.text}
    seen_pages = set()
    seen_docs = set()
    stats = {"pages": 0, "detail_links": 0, "accepted": 0, "downloaded": 0, "skipped_existing": 0, "rejected_type": 0, "rejected_authority": 0}

    while queue:
        url = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        stats["pages"] += 1

        try:
            if url in html_cache:
                html = html_cache.pop(url)
                final_url = url
            else:
                r = http.get(url)
                html = r.text
                final_url = r.url
        except Exception as e:
            store.error(f"Risultati {url}: {e}")
            log(f"    ! errore pagina risultati: {e}")
            continue

        items, pagination = cedef_listing_from_html(html)
        # I batch successivi sono cumulativi (ripetono i documenti precedenti).
        new_items = sum(
            1 for guid, _, _ in items
            if urljoin(final_url, CEDEF_DETAIL_ENDPOINT[channel] + "?id=" + quote(guid, safe="")) not in seen_docs
        )
        stats["detail_links"] += new_items
        log(f"    pagina risultati {stats['pages']}: {final_url}", 2)
        log(
            f"      documenti nel batch: {len(items)} "
            f"(batch pagina {pagination['pagina']}/{pagination['ultimaPagina']}, "
            f"totale per l'anno {pagination['totale']}, ulteriori={pagination['ulteriori']})",
            1,
        )

        if not items and stats["pages"] == 1:
            dbg = dump_debug(store, f"cedef_{channel}_{year}_results.html", html)
            soup = BeautifulSoup(html, "html.parser")
            title = norm_text(soup.title.get_text(" ", strip=True)) if soup.title else "(senza title)"
            log(f"      NESSUN RISULTATO. HTML salvato in: {dbg}")
            log(f"      title risposta: {title}")

        # I documenti del batch si elaborano in parallelo; la paginazione
        # resta su questo thread, perché dipende dalla sessione.
        jobs = []
        for guid, estremi, titolo in items:
            detail_url = urljoin(final_url, CEDEF_DETAIL_ENDPOINT[channel] + "?id=" + quote(guid, safe=""))
            if detail_url in seen_docs:
                continue
            seen_docs.add(detail_url)
            sub = store.for_type(_doctype_from_estremi(estremi, ""))
            if SKIP_EXISTING and mode == "refresh" and sub.has_file(sub.existing.get(detail_url)):
                stats["accepted"] += 1
                stats["skipped_existing"] += 1
                log(f"      = già presente, non riletto: {titolo or estremi or detail_url}", 2)
                continue
            jobs.append((detail_url, titolo or estremi or detail_url, sub))

        def fetch(job) -> list[str]:
            """Eseguita dai worker: ritorna i contatori da incrementare."""
            detail_url, label, sub = job
            counters = []
            try:
                log(f"      -> dettaglio: {label}", 2)
                r = http.get(detail_url)
                doc, content = parse_cedef_detail(http, sub.source, channel, r.url, r.text)

                if not matches_document_type(doc, cfg):
                    log(f"         scartato tipo: {doc.title}", 2)
                    return ["rejected_type"]
                if not matches_authority(doc, cfg):
                    log(f"         scartato autorità: {doc.title}", 2)
                    return ["rejected_authority"]

                counters.append("accepted")
                result = download_document(http, sub, doc, mode, content=content)
                if result is None:
                    counters.append("skipped_existing")
                    log(f"         = già presente: {doc.title}", 2)
                else:
                    counters.append("downloaded")
            except Exception as e:
                store.error(f"Dettaglio {detail_url}: {e}")
                log(f"      ! errore dettaglio {detail_url}: {e}")
            return counters

        for counters in http.map(fetch, jobs):
            for counter in counters:
                stats[counter] += 1

        # Oltre ai link di paginazione "classici", CeRDEF restituisce i
        # risultati in batch da 50: il link "ulteriori risultati" (onclick
        # window.location.href='paginatorXml.do') porta al batch successivo
        # e viene già individuato da cedef_next_links tramite l'onclick.
        soup = BeautifulSoup(html, "html.parser")
        nexts = list(cedef_next_links(soup, final_url))
        log(f"      pagine/batch successivi trovati: {len(nexts)}", 2)
        if pagination["ulteriori"] and not nexts:
            log("      ATTENZIONE: CeRDEF segnala ulteriori risultati ma nessun link di batch è stato trovato", 1)

        # paginatorXml.do è stateful: stesso URL, ma ogni GET restituisce il
        # batch successivo. Va quindi richiamato finché CeRDEF segnala
        # ulteriori risultati e il batch appena letto ha portato documenti nuovi.
        more = pagination["ulteriori"] and new_items > 0
        for nxt in nexts:
            if more:
                seen_pages.discard(nxt)
            if nxt not in seen_pages and nxt not in queue:
                queue.append(nxt)

    return stats

def cedef_source(http: Http, store: Store, source_name: str, cfg: dict, mode: str, channel: str):
    current_year = datetime.now().year
    start_year = int(cfg.get("start_year", current_year))
    if FROM_YEAR:
        start_year = max(start_year, FROM_YEAR)

    search_url = cfg.get("search_url") or cfg["home"]
    log(f"Apro pagina di ricerca CeRDEF: {search_url}")
    search = http.get(search_url)
    log(f"HTTP {search.status_code} -> {search.url}", 2)
    log(f"HTML ricevuto: {len(search.text):,} byte", 2)

    soup = soup_of(search)
    form = find_cedef_form(soup, channel)

    if form is None:
        dbg = dump_debug(store, f"cedef_{channel}_search_form.html", search.text)
        raise RuntimeError(
            "Non riesco a individuare la form CeRDEF. "
            f"Pagina salvata per diagnosi in {dbg}"
        )

    action = urljoin(search.url, form.get("action") or search.url)
    method = (form.get("method") or "get").upper()
    log(f"Form individuata: {method} {action}")
    log(f"Campi form: {len(form.find_all(['input','select']))}", 2)

    # In debug mostriamo nomi e select disponibili: è essenziale se CeRDEF cambia HTML.
    if VERBOSE >= 2:
        for el in form.find_all(["input", "select"]):
            name = el.get("name") or "(senza name)"
            label = field_label(form, el)
            if el.name == "select":
                opts = [norm_text(o.get_text(" ", strip=True)) for o in el.find_all("option")]
                log(f"  SELECT {name!r} label={label!r} opzioni={opts[:12]}", 2)
            else:
                log(f"  INPUT  {name!r} type={el.get('type','text')!r} label={label!r}", 2)

    print(f"Scansione {start_year}-{current_year}...")
    total = {"detail_links": 0, "accepted": 0, "downloaded": 0, "skipped_existing": 0}

    authorities = cedef_authority_values(form, cfg, channel)
    if authorities != [None]:
        print(f"Autorità interrogate: {', '.join(authorities)}")

    for year, authority in ((y, a) for y in range(start_year, current_year + 1) for a in authorities):
        print(f"\n[{source_name}] anno {year}" + (f" - {authority}" if authority else ""))
        payload = build_form_payload(form, cfg, channel, year, authority)

        year_keys = [k for k, v in payload.items() if str(v) == str(year)]
        log(f"  campo/i anno: {year_keys or 'NESSUNO'}")
        if VERBOSE >= 2:
            safe_payload = {k: v for k, v in payload.items() if not re.search(r"token|csrf|session", k, re.I)}
            log(f"  payload: {safe_payload}", 2)

        if not year_keys:
            dbg = dump_debug(store, f"cedef_{channel}_{year}_form.html", str(form))
            store.error(f"Anno {year}: campo anno non identificato. Form: {dbg}")
            raise RuntimeError(
                "La form CeRDEF è stata trovata, ma non riesco a identificare il campo ANNO. "
                f"Form salvata in {dbg}. Usa -vv per vedere tutti i campi."
            )

        try:
            result = submit_form(http, search.url, form, payload)
            log(f"  risposta ricerca: HTTP {result.status_code} -> {result.url}")
            log(f"  dimensione risposta: {len(result.text):,} byte", 2)

            # CeRDEF spesso restituisce HTTP 200 anche per errori applicativi.
            rsoup = soup_of(result)
            rtitle = norm_text(rsoup.title.get_text(" ", strip=True)) if rsoup.title else ""
            rtext = norm_text(rsoup.get_text(" ", strip=True))
            if "errore" in rtitle.lower() or "si è verificato un errore" in rtext.lower() or "ambito di ricerca non valido" in rtext.lower():
                dbg = dump_debug(store, f"cedef_{channel}_{year}_ERROR.html", result.text)
                print(f"  !! CeRDEF ha restituito un ERRORE applicativo, non zero risultati")
                print(f"     title: {rtitle}")
                m = re.search(r"Errore[:\s]+([^<]{0,250})", result.text, re.I)
                if m:
                    print(f"     dettaglio: {norm_text(BeautifulSoup(m.group(1), 'html.parser').get_text(' ', strip=True))}")
                print(f"     HTML diagnostico: {dbg}")
                print(f"     js_enabled inviato: {payload.get('js_enabled')!r}")
                store.error(f"Anno {year}: errore applicativo CeRDEF; HTML={dbg}; title={rtitle}")
                continue

            stats = walk_cedef_results(
                http, store, source_name, cfg, channel, result, mode, year
            )
            for k in total:
                total[k] += stats.get(k, 0)

            print(
                "  risultato anno: "
                f"documenti_trovati={stats['detail_links']} "
                f"accettati={stats['accepted']} "
                f"scaricati={stats['downloaded']} "
                f"già_presenti={stats['skipped_existing']} "
                f"scartati_tipo={stats['rejected_type']} "
                f"scartati_autorità={stats['rejected_authority']}"
            )
        except Exception as e:
            store.error(f"Anno {year}: {e}")
            print(f"  ! anno {year}: {e}")

    print("\nRIEPILOGO CEDEF")
    print(f"  link documento trovati : {total['detail_links']}")
    print(f"  documenti accettati    : {total['accepted']}")
    print(f"  scaricati              : {total['downloaded']}")
    print(f"  già presenti           : {total['skipped_existing']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def prompt_choice(prompt: str, choices: list[str]) -> str:
    print(prompt)
    for i, c in enumerate(choices, 1):
        label = SOURCES.get(c, {}).get("label", c)
        print(f"  {i}) {c} - {label}")
    while True:
        v = input("> ").strip()
        if v.isdigit() and 1 <= int(v) <= len(choices):
            return choices[int(v) - 1]
        if v in choices:
            return v
        print("Scelta non valida.")


def get_args():
    p = argparse.ArgumentParser(
        description="Scarica fonti fiscali/giuridiche per archivio RAG locale."
    )
    p.add_argument("--target", help="Directory radice di destinazione")
    p.add_argument(
        "--source",
        choices=source_choices(),
        help="Raccolta da scaricare",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="Più dettagli; usa -vv per mostrare campi e payload CeRDEF")
    p.add_argument(
        "--mode",
        choices=["refresh", "rewrite"],
        help="refresh=incrementale; rewrite=cancella e riscarica",
    )
    p.add_argument("--workers", type=int, default=WORKERS, help=f"Documenti scaricati in parallelo (default {WORKERS}; 1=sequenziale)")
    p.add_argument("--rate", type=float, default=RATE, help=f"Richieste al secondo verso uno stesso sito, sommate su tutti i worker (default {RATE:g})")
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="In refresh non rilegge dalla fonte i documenti CeRDEF già in archivio (più veloce, ma non rileva i testi modificati)",
    )
    p.add_argument(
        "--from-year",
        type=int,
        help="Scandisce le fonti CeRDEF solo da quest'anno in poi (controlli periodici delle novità)",
    )
    args = p.parse_args()
    if args.workers < 1 or args.rate <= 0:
        p.error("--workers deve essere >= 1 e --rate > 0")
    return args


def resolve_args(args):
    target = args.target
    source = args.source
    mode = args.mode

    if not target:
        target = input("Percorso target: ").strip()
    if not target:
        raise SystemExit("Percorso target obbligatorio.")

    if not source:
        source = prompt_choice(
            "Scegli il contenuto:",
            source_choices(),
        )

    if not mode:
        mode = prompt_choice(
            "Modalità:",
            ["refresh", "rewrite"],
        )

    return Path(target).expanduser().resolve(), source, mode


def run_one(target: Path, source_name: str, mode: str):
    cfg = SOURCES[source_name]
    print()
    print("=" * 78)
    print(cfg["label"])
    print(f"Target : {target / source_name}")
    print(f"Mode   : {mode}")
    print(f"Ritmo  : {WORKERS} worker, max {RATE:g} richieste/s per sito")
    if SKIP_EXISTING and mode == "refresh":
        print("Già presenti: non riletti dalla fonte (--skip-existing)")
    print("=" * 78)

    store = (SplitStore if cfg.get("split_by_type") else Store)(target, source_name, mode)
    http = Http()

    kind = cfg["kind"]
    try:
        if kind == "crawl":
            crawl_source(http, store, source_name, cfg, mode)
        elif kind == "cedef-prassi":
            cedef_source(http, store, source_name, cfg, mode, "prassi")
        elif kind == "cedef-normativa":
            cedef_source(http, store, source_name, cfg, mode, "normativa")
        elif kind == "cedef-giurisprudenza":
            cedef_source(http, store, source_name, cfg, mode, "giurisprudenza")
        else:
            raise RuntimeError(f"Collector sconosciuto: {kind}")
    finally:
        store.compact()


def main():
    global VERBOSE, WORKERS, RATE, SKIP_EXISTING, FROM_YEAR
    args = get_args()
    VERBOSE = 1 + int(args.verbose or 0)
    WORKERS, RATE = args.workers, args.rate
    SKIP_EXISTING = args.skip_existing
    FROM_YEAR = args.from_year
    target, source, mode = resolve_args(args)
    target.mkdir(parents=True, exist_ok=True)

    names = expand_source(source)

    failures = []
    for name in names:
        try:
            run_one(target, name, mode)
        except KeyboardInterrupt:
            print("\nInterrotto.")
            return 130
        except Exception as e:
            failures.append((name, str(e)))
            print(f"\nERRORE [{name}]: {e}", file=sys.stderr)

    print()
    if failures:
        print("Completato con errori:")
        for name, err in failures:
            print(f"  - {name}: {err}")
        return 2

    print("Completato.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
