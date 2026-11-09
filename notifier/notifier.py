#!/usr/bin/env python3
"""
notifier.py

Tiene d'occhio le novità fiscali e, per ogni documento nuovo:
  1. lo aggiunge alla sorgente DocsGPT delle novità (NOVITA_SOURCE_NAME);
  2. ne fa scrivere la sintesi a DocsGPT, con il prompt dedicato;
  3. manda una mail con la sintesi.

Due canali, controllati a ogni giro:
  - feed RSS "normativa e prassi" dell'Agenzia delle Entrate: avvisa il giorno
    stesso. Il documento viene preso dal sito dell'Agenzia e archiviato in
    agenzia-entrate/sito-agenzia/ come copia provvisoria;
  - archivio (downloader.py, fonte CeRDEF e siti degli enti): copre tutti gli
    enti ma arriva con qualche giorno di ritardo. Quando qui compare un atto già
    arrivato dal feed, la copia provvisoria viene tolta e non parte una seconda
    mail.

Perché una sorgente a parte: per aggiungere anche un solo file DocsGPT rilegge
tutti i documenti della sorgente (un quarto d'ora per Agenzia Entrate, ore per
la giurisprudenza). La sorgente delle novità resta piccola e si aggiorna in
pochi secondi; quando un ente viene ricaricato per intero con
carica_docsgpt.py --replace, le sue novità sono già nell'archivio e quello
script le toglie da qui.

Tutta la configurazione arriva da variabili d'ambiente (vedi Config).

Uso:
  python notifier.py                 # ciclo continuo
  python notifier.py --once          # un solo giro
  python notifier.py --once --replay 2   # prova: tratta come nuovi gli ultimi 2 elementi del feed
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import smtplib
import subprocess
import sys
import time
import unicodedata
import uuid
import zipfile
import defusedxml.ElementTree as ET
from datetime import datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) FiscalArchiveNotifier/1.0"
RSS_SOURCE = "agenzia-entrate/sito-agenzia"

# Enti dell'archivio (cartelle di primo livello) che downloader.py sa aggiornare.
ENTI = ["agenzia-entrate", "ministero-finanze", "agenzia-dogane-monopoli", "normativa", "giurisprudenza"]

# Stessa configurazione delle sorgenti caricate con carica_docsgpt.py.
SOURCE_CONFIG = {
    "chunking": {"strategy": "classic_chunk", "max_tokens": 700, "min_tokens": 150},
    "retrieval": {"chunks": 10},
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Config:
    archive_dir = Path(env("ARCHIVE_DIR", "/archive"))
    state_file = Path(env("STATE_DIR", "/state")) / "state.json"
    links_file = Path(env("DOC_LINKS_FILE", "/doc_links/doc_links.json"))

    poll_minutes = float(env("POLL_MINUTES", "30"))
    rss_url = env(
        "RSS_URL",
        "https://www.agenziaentrate.gov.it/portale/c/portal/rss/entrate?idrss=0753fcb1-1a42-4f8c-f40d-02793c6aefb4",
    )
    # Enti da rileggere con downloader.py, e ogni quante ore. Vuoto = solo feed RSS.
    archive_sources = [s for s in env("ARCHIVE_SOURCES", ",".join(ENTI)).split(",") if s.strip()]
    archive_every_hours = float(env("ARCHIVE_EVERY_HOURS", "6"))
    # Ricaricamento mensile: il primo del mese, da quest'ora, gli enti vengono
    # ricaricati per intero e le loro novità tolte dalla sorgente delle novità.
    # Vuoto = disattivato.
    monthly_reload_hour = env("MONTHLY_RELOAD_HOUR", "2")
    monthly_reload_attempts = int(env("MONTHLY_RELOAD_ATTEMPTS", "3"))
    upload_tmp = Path(env("UPLOAD_TMP_DIR", "/state/upload-tmp"))

    docsgpt_api = env("DOCSGPT_API", "http://backend:7091").rstrip("/")
    docsgpt_public_url = env("DOCSGPT_PUBLIC_URL").rstrip("/")
    novita_source = env("NOVITA_SOURCE_NAME", "Novità fiscali")
    summary_prompt = env("SUMMARY_PROMPT_NAME", "Sintesi nuovo documento")
    # Agenti a cui collegare la sorgente delle novità quando viene creata
    # (separati da virgola). Il primo è anche quello linkato nelle mail.
    agent_names = [a.strip() for a in env("AGENT_NAME", "Novità fiscali").split(",") if a.strip()]
    agent_name = agent_names[0] if agent_names else ""
    summary_max_chars = int(env("SUMMARY_MAX_CHARS", "120000"))

    smtp_host = env("SMTP_HOST", "mailpit")
    smtp_port = int(env("SMTP_PORT", "1025"))
    smtp_user = env("SMTP_USER")
    smtp_password = env("SMTP_PASSWORD")
    smtp_security = env("SMTP_SECURITY", "none").lower()  # none | starttls | ssl
    mail_from = env("MAIL_FROM", "novita-fiscali@localhost")
    mail_to = [a.strip() for a in env("MAIL_TO", "studio@localhost").split(",") if a.strip()]


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Stato
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if Config.state_file.exists():
        return json.loads(Config.state_file.read_text())
    return {}


def save_state(state: dict) -> None:
    Config.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = Config.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(Config.state_file)


# ---------------------------------------------------------------------------
# DocsGPT
# ---------------------------------------------------------------------------

def api(method: str, path: str, **kwargs):
    kwargs.setdefault("timeout", 300)
    if method == "POST":
        # Senza chiave DocsGPT ne deriva una dalla sorgente e per 24 ore
        # risponde con l'esito della prima reindicizzazione, senza rifarla.
        kwargs.setdefault("headers", {})["Idempotency-Key"] = str(uuid.uuid4())
    resp = requests.request(method, f"{Config.docsgpt_api}{path}", **kwargs)
    resp.raise_for_status()
    return resp.json()


def novita_source_id() -> str | None:
    for src in api("GET", "/api/sources"):
        if src.get("name") == Config.novita_source:
            return src["id"]
    return None


def wait_task(task_id: str, timeout: int = 3600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = api("GET", "/api/task_status", params={"task_id": task_id})
        if body.get("status") == "SUCCESS":
            return
        if body.get("status") in ("FAILURE", "REVOKED"):
            raise RuntimeError(f"indicizzazione {body.get('status')}: {body.get('result')}")
        time.sleep(5)
    raise RuntimeError(f"indicizzazione non conclusa in {timeout}s (task {task_id})")


def index_add(path: Path, parent_dir: str) -> str:
    """Aggiunge il file alla sorgente delle novità; restituisce il percorso nella sorgente."""
    source_id = novita_source_id()
    with path.open("rb") as fh:
        if source_id:
            body = api(
                "POST",
                "/api/manage_source_files",
                data={"source_id": source_id, "operation": "add", "parent_dir": parent_dir},
                files={"file": (path.name, fh)},
            )
            task_id = body.get("reingest_task_id")
        else:
            # Prima novità in assoluto: la sorgente nasce con questo documento.
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr(f"{parent_dir}/{path.name}", fh.read())
            body = api(
                "POST",
                "/api/upload",
                data={"user": "local", "name": Config.novita_source, "config": json.dumps(SOURCE_CONFIG)},
                files={"file": ("novita.zip", buf.getvalue())},
            )
            task_id = body.get("task_id")
    if not body.get("success") or not task_id:
        raise RuntimeError(f"aggiunta rifiutata: {body}")
    wait_task(task_id)
    if not source_id and body.get("source_id"):
        attach_to_agent(body["source_id"])
        register_links(body["source_id"])
    return f"{parent_dir}/{path.name}"


def register_links(source_id: str) -> None:
    """Dice a DocsGPT dove sono pubblicati i documenti della sorgente delle novità."""
    if not Config.docsgpt_public_url:
        return
    try:
        links = json.loads(Config.links_file.read_text()) if Config.links_file.exists() else {}
        # depth 2: nella sorgente <ente>/<tipologia>/<anno>/..., in archivio <ente>/<tipologia>/files/<anno>/...
        links[source_id] = {"base": f"{Config.docsgpt_public_url}/docs", "depth": 2}
        tmp = Config.links_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(links, indent=1))
        tmp.replace(Config.links_file)
    except Exception as e:
        log(f"  link ai documenti non registrati: {e}")


def attach_to_agent(source_id: str) -> None:
    """Aggiunge la sorgente delle novità appena creata agli agenti di AGENT_NAME."""
    try:
        for agent in api("GET", "/api/get_agents"):
            if agent.get("name") in Config.agent_names:
                # In testa: DocsGPT accoda le sorgenti in ordine e la prima può
                # riempire da sola lo spazio per i risultati.
                sources = [source_id] + list(agent.get("sources") or [])
                api("PUT", f"/api/update_agent/{agent['id']}", json={"sources": sources})
                log(f"  sorgente \"{Config.novita_source}\" collegata all'agente \"{agent.get('name')}\"")
    except Exception as e:
        log(f"  sorgente non collegata all'agente: {e}")


def index_remove(rel_path: str) -> None:
    source_id = novita_source_id()
    if not source_id:
        return
    body = api(
        "POST",
        "/api/manage_source_files",
        data={"source_id": source_id, "operation": "remove", "file_paths": json.dumps([rel_path])},
    )
    if not body.get("success"):
        raise RuntimeError(f"rimozione rifiutata: {body}")
    if body.get("reingest_task_id"):
        wait_task(body["reingest_task_id"])


def summarize(text: str) -> str:
    """Sintesi scritta da DocsGPT con il prompt dedicato (modificabile dalla sua interfaccia)."""
    prompt_id = next(
        (p["id"] for p in api("GET", "/api/get_prompts") if p.get("name") == Config.summary_prompt), None
    )
    if not prompt_id:
        raise RuntimeError(f"prompt DocsGPT \"{Config.summary_prompt}\" non trovato")
    if len(text) > Config.summary_max_chars:
        text = text[: Config.summary_max_chars] + "\n\n[testo troncato]"
    body = api(
        "POST",
        "/api/answer",
        json={
            "question": f"Documento da sintetizzare:\n\n{text}",
            "prompt_id": prompt_id,
            "isNoneDoc": True,
            "save_conversation": False,
        },
        timeout=900,
    )
    answer = (body.get("answer") or "").strip()
    if not answer:
        raise RuntimeError(f"risposta senza sintesi: {str(body)[:300]}")
    return answer


def agent_link() -> str:
    """Link pubblico all'agente DocsGPT per approfondire, se configurato e condiviso."""
    if not Config.docsgpt_public_url:
        return ""
    try:
        for agent in api("GET", "/api/get_agents"):
            if agent.get("name") == Config.agent_name and agent.get("shared_token"):
                return f"{Config.docsgpt_public_url}/shared/agent/{agent['shared_token']}"
    except Exception as e:
        log(f"  link all'agente non disponibile: {e}")
    return Config.docsgpt_public_url


# ---------------------------------------------------------------------------
# Testo e mail
# ---------------------------------------------------------------------------

def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        import pymupdf

        with pymupdf.open(path) as doc:
            return "\n".join(page.get_text() for page in doc).strip()
    if path.suffix.lower() in (".html", ".htm"):
        from bs4 import BeautifulSoup

        return BeautifulSoup(path.read_text(errors="replace"), "html.parser").get_text("\n").strip()
    return path.read_text(errors="replace").strip()


def markdown_to_html(md: str) -> str:
    """Quel poco di markdown che il prompt di sintesi produce: grassetti ed elenchi."""
    out, in_list = [], False
    for line in md.splitlines():
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(line.strip()))
        item = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)", esc)
        if item and not re.match(r"^\d+[.)]\s+<strong>", esc):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{item.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if esc:
            out.append(f"<p>{esc}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def send_mail(doc: dict, summary: str, note: str = "") -> None:
    link = agent_link()
    estremi = " · ".join(x for x in (doc.get("ente"), doc.get("tipo"), doc.get("date")) if x)

    msg = EmailMessage()
    msg["Subject"] = f"[Novità fiscali] {doc['title']}"[:240]
    msg["From"] = Config.mail_from
    msg["To"] = ", ".join(Config.mail_to)

    text = [doc["title"], estremi, "", summary, ""]
    if note:
        text += [note, ""]
    if doc.get("url"):
        text.append(f"Documento originale: {doc['url']}")
    if link:
        text.append(f"Approfondisci su DocsGPT: {link}")
    msg.set_content("\n".join(text))

    parts = [f"<h2>{html.escape(doc['title'])}</h2>", f"<p><em>{html.escape(estremi)}</em></p>", markdown_to_html(summary)]
    if note:
        parts.append(f"<p><em>{html.escape(note)}</em></p>")
    links = []
    if doc.get("url"):
        links.append(f'<a href="{html.escape(doc["url"])}">Documento originale</a>')
    if link:
        links.append(f'<a href="{html.escape(link)}">Approfondisci su DocsGPT</a>')
    if links:
        parts.append("<p>" + " · ".join(links) + "</p>")
    msg.add_alternative("<html><body>" + "\n".join(parts) + "</body></html>", subtype="html")

    smtp_cls = smtplib.SMTP_SSL if Config.smtp_security == "ssl" else smtplib.SMTP
    with smtp_cls(Config.smtp_host, Config.smtp_port, timeout=60) as smtp:
        if Config.smtp_security == "starttls":
            smtp.starttls()
        if Config.smtp_user:
            smtp.login(Config.smtp_user, Config.smtp_password)
        smtp.send_message(msg)


def process(doc: dict, path: Path, parent_dir: str) -> str:
    """Indicizza, sintetizza e notifica un documento; restituisce il percorso nella sorgente.

    L'indicizzazione viene prima: se fallisce il documento resta da fare e
    verrà ripreso al giro successivo, senza mail doppie.
    """
    rel_path = index_add(path, parent_dir)
    log(f"  indicizzato in \"{Config.novita_source}\": {rel_path}")
    note = ""
    try:
        text = extract_text(path)
        if len(text) < 200:
            summary, note = "", "Il documento non contiene testo estraibile (probabile scansione): sintesi non disponibile."
        else:
            summary = summarize(text)
    except Exception as e:
        log(f"  sintesi non riuscita: {e}")
        summary, note = "", f"Sintesi non riuscita ({e})."
    send_mail(doc, summary, note)
    log(f"  mail inviata a {', '.join(Config.mail_to)}")
    return rel_path


# ---------------------------------------------------------------------------
# Canale 1: feed RSS dell'Agenzia delle Entrate
# ---------------------------------------------------------------------------

def slug(s: str, max_len: int = 120) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:max_len]


def act_key(title: str, link: str, year: str) -> str:
    """Estremi dell'atto ("Risoluzione|34|2026") per riconoscerlo quando arriva da CeRDEF."""
    m = re.search(r"\((circolare|risoluzione|risposta)\s+n\.\s*(\d+)[^)]*\)\s*$", title.strip(), re.I)
    if not m:
        return ""
    kind = m.group(1).lower()
    if kind == "risposta":
        # "risposta n." è usato anche per i principi di diritto, che hanno numerazione propria.
        if "principi" in link.lower():
            return ""
        kind = "interpello"
    return f"{kind.capitalize()}|{int(m.group(2))}|{year}"


def rss_items() -> list[dict]:
    resp = requests.get(Config.rss_url, headers={"User-Agent": UA}, timeout=60)
    resp.raise_for_status()
    items = []
    for it in ET.fromstring(resp.content).iter("item"):
        link = (it.findtext("link") or "").strip()
        title = " ".join((it.findtext("title") or "").split())
        if not link or not title:
            continue
        try:
            date = parsedate_to_datetime(it.findtext("pubDate") or "").strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            date = datetime.now().strftime("%Y-%m-%d")
        items.append({"guid": (it.findtext("guid") or link).strip(), "title": title, "url": link, "date": date})
    return items


def fetch_rss_document(item: dict) -> Path:
    resp = requests.get(item["url"], headers={"User-Agent": UA}, timeout=120)
    resp.raise_for_status()
    ext = ".pdf" if resp.content[:5] == b"%PDF-" else ".html"
    out = Config.archive_dir / RSS_SOURCE / "files" / item["date"][:4] / f"{item['date']}_{slug(item['title'])}{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    return out


def check_rss(state: dict, replay: int = 0) -> None:
    items = rss_items()
    seen = set(state.get("rss_seen", []))
    if "rss_seen" not in state:
        # Primo avvio: quello che c'è già nel feed non è una novità.
        new = []
        log(f"feed RSS: primo avvio, {len(items)} elementi presi come già noti")
    else:
        new = [it for it in items if it["guid"] not in seen]
    if replay:
        new = items[:replay]
    state["rss_seen"] = sorted(seen | {it["guid"] for it in items if it not in new})
    log(f"feed RSS: {len(items)} elementi, {len(new)} nuovi")

    for item in reversed(new):  # dal più vecchio
        log(f"nuovo dal feed: {item['title'][:110]}")
        try:
            path = fetch_rss_document(item)
            doc = {**item, "ente": "Agenzia delle Entrate", "tipo": "dal sito dell'Agenzia"}
            rel_path = process(doc, path, f"{RSS_SOURCE}/{item['date'][:4]}")
            key = act_key(item["title"], item["url"], item["date"][:4])
            if key:
                state.setdefault("provisional", {})[key] = {"rel_path": rel_path, "archive_file": str(path)}
            state["rss_seen"] = sorted(set(state["rss_seen"]) | {item["guid"]})
        except Exception as e:
            log(f"  ERRORE, riprovo al prossimo giro: {e}")
        save_state(state)


# ---------------------------------------------------------------------------
# Canale 2: archivio (downloader.py)
# ---------------------------------------------------------------------------

def manifest_rows(ente: str):
    """Righe dei manifest dell'ente come (chiave, cartella della raccolta, riga)."""
    for manifest in sorted((Config.archive_dir / ente).glob("**/manifest.jsonl")):
        if manifest.parent == Config.archive_dir / RSS_SOURCE:
            continue
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            ident = row.get("detail_url") or row.get("download_url") or row.get("local_file")
            if ident and row.get("local_file"):
                yield f"{manifest.parent.relative_to(Config.archive_dir)}|{ident}", manifest.parent, row


def run_downloader(ente: str) -> None:
    cmd = [
        sys.executable, str(Config.archive_dir / "downloader.py"),
        "--target", str(Config.archive_dir), "--source", ente,
        "--mode", "refresh", "--skip-existing",
        # Anno corrente e precedente: copre anche gli atti pubblicati a cavallo d'anno.
        "--from-year", str(datetime.now().year - 1),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
    if proc.returncode != 0:
        raise RuntimeError(f"downloader exit {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}")


def check_archive(state: dict) -> None:
    known = state.setdefault("archive_seen", {})
    for ente in Config.archive_sources:
        if ente not in ENTI:
            log(f"archivio: ente sconosciuto \"{ente}\", salto")
            continue
        first_run = ente not in known
        try:
            if not first_run:
                run_downloader(ente)
        except Exception as e:
            log(f"archivio {ente}: ERRORE downloader: {e}")
            continue
        seen = set(known.get(ente, []))
        rows = list(manifest_rows(ente))
        if first_run:
            # Primo avvio: l'archivio esistente è il punto di partenza.
            known[ente] = sorted(k for k, _, _ in rows)
            log(f"archivio {ente}: primo avvio, {len(rows)} documenti presi come già noti")
            save_state(state)
            continue
        new = [(k, d, r) for k, d, r in rows if k not in seen]
        log(f"archivio {ente}: {len(rows)} documenti, {len(new)} nuovi")

        for key, source_dir, row in new:
            title = row.get("title") or row["local_file"]
            log(f"nuovo in archivio: {title[:110]}")
            try:
                path = source_dir / row["local_file"]
                tipologia = source_dir.relative_to(Config.archive_dir / ente).as_posix()
                inner_dir = Path(row["local_file"]).relative_to("files").parent.as_posix()
                parent_dir = "/".join(p for p in (ente, tipologia, inner_dir) if p and p != ".")
                year = (row.get("document_date") or "")[:4]
                act = f"{row.get('document_type', '')}|{row.get('number', '')}|{year}"
                provisional = state.get("provisional", {}).get(act) if ente == "agenzia-entrate" else None
                if provisional:
                    # Già notificato dal feed: la copia CeRDEF sostituisce quella provvisoria.
                    rel_path = index_add(path, parent_dir)
                    try:
                        index_remove(provisional["rel_path"])
                    except Exception as e:
                        # Tipicamente già tolta da un ricaricamento completo dell'ente.
                        log(f"  copia provvisoria non rimossa dall'indice: {e}")
                    Path(provisional["archive_file"]).unlink(missing_ok=True)
                    del state["provisional"][act]
                    log(f"  già notificato dal feed: copia provvisoria sostituita da {rel_path}")
                else:
                    doc = {
                        "title": title,
                        "url": row.get("detail_url") or row.get("download_url"),
                        "date": row.get("document_date", ""),
                        "ente": row.get("authority") or ente,
                        "tipo": " ".join(x for x in (row.get("document_type"), row.get("number") and f"n. {row['number']}") if x),
                    }
                    process(doc, path, parent_dir)
                seen.add(key)
                known[ente] = sorted(seen)
            except Exception as e:
                log(f"  ERRORE, riprovo al prossimo giro: {e}")
            save_state(state)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ricaricamento mensile (carica_docsgpt.py)
# ---------------------------------------------------------------------------
# La sorgente delle novità tiene i documenti arrivati nel mese. Il primo del
# mese gli enti vengono ricaricati per intero: i documenti del mese chiuso
# entrano nelle sorgenti degli enti e carica_docsgpt.py li toglie dalle novità.
# Gira dentro il ciclo del notifier, così nel frattempo non arrivano documenti
# che potrebbero restare fuori da entrambe le sorgenti.

def reload_due(state: dict) -> str | None:
    """Mese (AAAA-MM) da chiudere con il ricaricamento, o None se non serve ora."""
    if not Config.monthly_reload_hour or not Config.archive_sources:
        return None
    now = datetime.now()
    month = now.strftime("%Y-%m")
    reload = state.setdefault("monthly_reload", {})
    if "month" not in reload:
        # Primo avvio con questa funzione: si parte dal mese in corso.
        reload["month"] = month
        return None
    if reload["month"] == month or now.hour < int(Config.monthly_reload_hour):
        return None
    return month


def run_reload(ente: str) -> None:
    cmd = [
        sys.executable, str(Config.archive_dir / "carica_docsgpt.py"),
        "--target", str(Config.archive_dir), "--source", ente, "--replace",
        "--api", Config.docsgpt_api,
        "--tmp", str(Config.upload_tmp),
        "--links-file", str(Config.links_file),
    ]
    # Senza limite di tempo: la giurisprudenza richiede ore.
    proc = subprocess.run(cmd, capture_output=True, text=True)
    for line in (proc.stdout or "").splitlines():
        log(f"  | {line}")
    if proc.returncode != 0:
        raise RuntimeError(f"carica_docsgpt exit {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}")


def monthly_reload(state: dict, month: str) -> None:
    reload = state["monthly_reload"]
    if reload.get("target") != month:
        reload.update(target=month, done=[], attempts={})
    pending = [e for e in Config.archive_sources if e in ENTI and e not in reload["done"]]
    log(f"ricaricamento mensile: chiudo {reload['month']}, enti da ricaricare: {', '.join(pending) or 'nessuno'}")
    for ente in pending:
        attempts = reload["attempts"].get(ente, 0)
        if attempts >= Config.monthly_reload_attempts:
            continue
        reload["attempts"][ente] = attempts + 1
        save_state(state)
        started = time.time()
        try:
            run_reload(ente)
        except Exception as e:
            log(f"ricaricamento {ente}: ERRORE (tentativo {attempts + 1}/{Config.monthly_reload_attempts}): {e}")
            continue
        reload["done"].append(ente)
        log(f"ricaricamento {ente}: fatto in {(time.time() - started) / 60:.0f} min")
        save_state(state)
    left = [e for e in pending if e not in reload["done"]]
    exhausted = [e for e in left if reload["attempts"].get(e, 0) >= Config.monthly_reload_attempts]
    if not left or left == exhausted:
        if exhausted:
            log(f"ricaricamento mensile: rinuncio per {', '.join(exhausted)}, le loro novità restano fino al mese prossimo")
        reload["month"] = month
        log(f"ricaricamento mensile: completato, novità di {month} da qui in avanti")
    save_state(state)


def tick(replay: int = 0) -> None:
    state = load_state()
    try:
        check_rss(state, replay)
    except Exception as e:
        log(f"feed RSS: ERRORE: {e}")
    reload_month = reload_due(state)
    archive_due = time.time() - state.get("archive_checked_at", 0) >= Config.archive_every_hours * 3600
    # Prima del ricaricamento, un ultimo giro dell'archivio: entra anche quanto
    # pubblicato negli ultimi giorni del mese.
    if Config.archive_sources and (archive_due or (reload_month and not state["monthly_reload"].get("target") == reload_month)):
        check_archive(state)
        state["archive_checked_at"] = time.time()
    save_state(state)
    if reload_month:
        monthly_reload(state, reload_month)


def main() -> int:
    p = argparse.ArgumentParser(description="Notifica e indicizza le novità fiscali.")
    p.add_argument("--once", action="store_true", help="Un solo giro, poi esce")
    p.add_argument("--replay", type=int, default=0, help="Prova: tratta come nuovi gli ultimi N elementi del feed RSS")
    args = p.parse_args()

    log(
        f"avvio: feed ogni {Config.poll_minutes:g} min, archivio ogni {Config.archive_every_hours:g} h "
        f"({', '.join(Config.archive_sources) or 'disattivato'}); "
        f"ricaricamento mensile {'il 1° dalle ' + Config.monthly_reload_hour + ':00' if Config.monthly_reload_hour else 'disattivato'}; mail a {', '.join(Config.mail_to)} "
        f"via {Config.smtp_host}:{Config.smtp_port}"
    )
    while True:
        tick(args.replay)
        if args.once:
            return 0
        time.sleep(Config.poll_minutes * 60)


if __name__ == "__main__":
    raise SystemExit(main())
