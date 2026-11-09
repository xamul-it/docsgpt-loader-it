#!/usr/bin/env python3
"""
carica_docsgpt.py

Carica su DocsGPT le raccolte scaricate da downloader.py, una sorgente per ente.
Le fonti sulle locazioni (cartelle locazioni/<id>) sono sorgenti a sé, una per
amministrazione, con il nome dato in fonti/locazioni.yaml.

Uso:
  python carica_docsgpt.py --target /data/rag                       # tutti gli enti presenti
  python carica_docsgpt.py --target /data/rag --source agenzia-entrate
  python carica_docsgpt.py --target /data/rag --source normativa --replace
  python carica_docsgpt.py --target /data/rag --source locazioni --agent "Claude - archivio fiscale"

Per ogni ente (cartella di primo livello dell'archivio, oppure locazioni/<id>):
  - impacchetta in uno zip non compresso i soli documenti citati nei manifest
    (<tipologia>/files/...); manifest, log, cartelle di debug e file orfani
    restano fuori. Nello zip il livello "files" viene tolto:
    <tipologia>/<anno>/<documento>
    (per locazioni/<id>, che non ha tipologie: direttamente <documento>);
  - lo invia a DocsGPT e attende la fine dell'indicizzazione;
  - se una sorgente con lo stesso nome esiste già l'ente viene saltato, a meno
    di --replace (che carica la nuova e, a indicizzazione riuscita, ricollega
    gli agenti ed elimina la vecchia: le ricerche non restano mai scoperte);
  - con --agent collega la sorgente appena caricata all'agente indicato (le
    sorgenti nuove non hanno agenti da ricollegare), senza togliergli le sue;
  - a caricamento riuscito toglie i documenti dell'ente dalla sorgente delle
    novità alimentata dal notifier: ormai stanno nella sorgente dell'ente;
  - registra dove sono pubblicati i documenti dell'ente (--docs-url), così le
    fonti citate nelle risposte di DocsGPT si aprono con un clic.

Gli enti vengono caricati uno alla volta, dal più piccolo al più grande. Il
fallimento di uno non ferma gli altri: rilanciando lo script si riprende da
quelli mancanti.

Exit code: 0 se tutti gli enti richiesti risultano caricati, 1 altrimenti.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import requests

API = "http://127.0.0.1:7091"
TMP_DIR = "/home/htpc/containers/docsgpt/upload-tmp"
POLL_SECONDS = 30
# Sorgente in cui il notifier mette i documenti nuovi tra un caricamento e l'altro.
NOVITA_SOURCE = "Novità fiscali"
# Indirizzo a cui il web server pubblica la radice dell'archivio, e file in cui
# DocsGPT (patch labels.py) legge la corrispondenza sorgente -> indirizzo.
DOCS_URL = "https://docsgpt.ilz.it/docs"
LINKS_FILE = "/home/htpc/containers/docsgpt/data/doc_links/doc_links.json"

# Cartella dell'ente -> nome della sorgente su DocsGPT.
SOURCE_NAMES = {
    "agenzia-entrate": "Agenzia Entrate",
    "ministero-finanze": "Ministero Finanze",
    "agenzia-dogane-monopoli": "Agenzia Dogane e Monopoli",
    "normativa": "Normativa",
    "giurisprudenza": "Giurisprudenza",
}


def locazioni_names(path: Path) -> dict[str, str]:
    """Cartella locazioni/<id> -> nome della sorgente, dall'elenco curato."""
    try:
        import yaml  # solo qui: nel container del notifier non c'è e non serve
    except ImportError:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {f"locazioni/{s['id']}": s.get("nome") or f"Locazioni - {s['id']}" for s in data.get("sorgenti") or []}


SOURCE_NAMES.update(locazioni_names(Path(__file__).resolve().parent / "fonti" / "locazioni.yaml"))
# --source locazioni = tutte le sorgenti locazioni/<id>.
GROUPS = {"locazioni": [e for e in SOURCE_NAMES if e.startswith("locazioni/")]}

# Stessa configurazione usata per i primi caricamenti manuali.
SOURCE_CONFIG = {
    "chunking": {"strategy": "classic_chunk", "max_tokens": 700, "min_tokens": 150},
    "retrieval": {"chunks": 10},
}


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def ente_documents(ente_dir: Path) -> list[tuple[Path, str]]:
    """Documenti dell'ente come (percorso su disco, percorso nello zip)."""
    docs = []
    for files_dir in sorted(ente_dir.glob("**/files")):
        source_dir = files_dir.parent
        manifest = source_dir / "manifest.jsonl"
        if manifest.exists():
            # Solo i file del manifest: su disco possono restare orfani di vecchi scarichi.
            rows = (json.loads(line) for line in manifest.read_text().splitlines() if line.strip())
            paths = sorted({source_dir / r["local_file"] for r in rows if r.get("local_file")})
        else:
            paths = sorted(p for p in files_dir.rglob("*") if p.is_file())
        tipologia = source_dir.relative_to(ente_dir).as_posix()
        for path in paths:
            if not path.is_file() or path.stat().st_size == 0:
                continue
            inside = path.relative_to(files_dir).as_posix()
            # Ente senza tipologie (locazioni/<id>): niente "./" davanti.
            docs.append((path, inside if tipologia == "." else f"{tipologia}/{inside}"))
    return docs


def build_zip(docs: list[tuple[Path, str]], zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    part = zip_path.with_suffix(".zip.part")
    # ZIP_STORED: i PDF non si comprimono e l'impacchettamento è molto più rapido.
    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, arcname in docs:
            zf.write(path, arcname)
    part.replace(zip_path)


def list_sources(api: str) -> dict[str, list[str]]:
    """Nome sorgente -> id delle sorgenti con quel nome."""
    resp = requests.get(f"{api}/api/sources", timeout=60)
    resp.raise_for_status()
    found: dict[str, list[str]] = {}
    for src in resp.json():
        found.setdefault(src.get("name", ""), []).append(src["id"])
    return found


def delete_source(api: str, source_id: str) -> None:
    resp = requests.get(f"{api}/api/delete_old", params={"source_id": source_id}, timeout=600)
    resp.raise_for_status()


def clear_novita(api: str, ente: str) -> None:
    """Toglie la cartella dell'ente dalla sorgente delle novità, se c'è."""
    source_ids = list_sources(api).get(NOVITA_SOURCE, [])
    if not source_ids:
        return
    resp = requests.post(
        f"{api}/api/manage_source_files",
        data={"source_id": source_ids[0], "operation": "remove_directory", "directory_path": ente},
        # Chiave unica: senza, DocsGPT riusa per 24 ore l'esito della prima reindicizzazione.
        headers={"Idempotency-Key": str(uuid.uuid4())},
        timeout=600,
    )
    if resp.ok and resp.json().get("success"):
        log(f"    novità dell'ente tolte da \"{NOVITA_SOURCE}\"")
    elif resp.status_code != 404:
        log(f"    novità non tolte da \"{NOVITA_SOURCE}\": {resp.text[:200]}")


def register_links(links_file: Path, docs_url: str, ente: str, new_id: str, old_ids: list[str], depth: int = 1) -> None:
    """Associa la nuova sorgente all'indirizzo pubblico dei documenti dell'ente."""
    links = json.loads(links_file.read_text()) if links_file.exists() else {}
    for old in old_ids:
        links.pop(old, None)
    # depth 1: nella sorgente <tipologia>/<anno>/..., in archivio <tipologia>/files/<anno>/...
    # depth 0 (locazioni/<id>): nella sorgente <documento>, in archivio files/<documento>
    links[new_id] = {"base": f"{docs_url.rstrip('/')}/{ente}", "depth": depth}
    links_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = links_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(links, indent=1))
    tmp.replace(links_file)


def agents_using(api: str, source_ids: list[str]) -> list[dict]:
    """Agenti che hanno tra le sorgenti una di quelle indicate."""
    if not source_ids:
        return []
    resp = requests.get(f"{api}/api/get_agents", timeout=60)
    resp.raise_for_status()
    return [a for a in resp.json() if set(a.get("sources") or []) & set(source_ids)]


def repoint_agents(api: str, agents: list[dict], old_ids: list[str], new_id: str) -> None:
    """Sostituisce negli agenti le sorgenti eliminate con quella appena caricata."""
    for agent in agents:
        sources = [s for s in agent.get("sources") or [] if s not in old_ids] + [new_id]
        resp = requests.put(f"{api}/api/update_agent/{agent['id']}", json={"sources": sources}, timeout=60)
        if resp.ok:
            log(f"    agente \"{agent.get('name')}\" ricollegato alla nuova sorgente")
        else:
            log(f"    agente \"{agent.get('name')}\" NON ricollegato: {resp.text[:200]}")


def attach_to_agent(api: str, agent_name: str, source_id: str) -> None:
    """Aggiunge la sorgente a quelle dell'agente, senza togliergli le altre."""
    resp = requests.get(f"{api}/api/get_agents", timeout=60)
    resp.raise_for_status()
    agents = [a for a in resp.json() if a.get("name") == agent_name]
    if len(agents) != 1:
        raise RuntimeError(f"agente \"{agent_name}\": trovati {len(agents)}, ne serve esattamente uno")
    agent = agents[0]
    sources = list(agent.get("sources") or [])
    if source_id in sources:
        return
    resp = requests.put(f"{api}/api/update_agent/{agent['id']}", json={"sources": sources + [source_id]}, timeout=60)
    resp.raise_for_status()
    log(f"    sorgente aggiunta all'agente \"{agent_name}\" ({len(sources)} -> {len(sources) + 1} sorgenti)")


def upload_zip(api: str, name: str, zip_path: Path) -> tuple[str, str]:
    """Invia lo zip e restituisce (task_id dell'indicizzazione, id della nuova sorgente)."""
    # curl e non requests: requests costruisce l'intero corpo multipart in
    # memoria, e gli zip più grandi pesano diversi GB.
    proc = subprocess.run(
        [
            "curl", "-sS", "-X", "POST", f"{api}/api/upload",
            "-F", "user=local",
            "-F", f"name={name}",
            "-F", f"config={json.dumps(SOURCE_CONFIG)}",
            "-F", f"file=@{zip_path}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl exit {proc.returncode}: {proc.stderr.strip()}")
    try:
        body = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"risposta inattesa: {proc.stdout[:300]}") from None
    if not body.get("success") or not body.get("task_id"):
        raise RuntimeError(f"upload rifiutato: {body}")
    return body["task_id"], body.get("source_id", "")


def wait_task(api: str, task_id: str) -> None:
    """Attende la fine dell'indicizzazione; solleva se fallisce."""
    last = None
    failures = 0
    while True:
        try:
            resp = requests.get(f"{api}/api/task_status", params={"task_id": task_id}, timeout=60)
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as e:
            log(f"    stato non leggibile ({e}), riprovo")
            time.sleep(POLL_SECONDS)
            continue
        status = body.get("status")
        if status == "SUCCESS":
            return
        if status in ("FAILURE", "REVOKED"):
            # Un doppione del task (riconsegna del broker) può segnare FAILURE
            # mentre l'originale lavora ancora: vale solo se resta tale.
            failures += 1
            if failures >= 4:
                raise RuntimeError(f"indicizzazione {status}: {body.get('result')}")
            time.sleep(POLL_SECONDS)
            continue
        failures = 0
        result = body.get("result")
        current = result.get("current") if isinstance(result, dict) else None
        if current != last:
            log(f"    avanzamento: {current}%" if current is not None else f"    stato: {status}")
            last = current
        time.sleep(POLL_SECONDS)


def load_ente(api: str, target: Path, ente: str, tmp_dir: Path, replace: bool, docs_url: str, links_file: Path, agent_name: str = "") -> bool:
    name = SOURCE_NAMES[ente]
    log(f"== {ente} -> sorgente \"{name}\"")
    existing = list_sources(api).get(name, [])
    if existing and not replace:
        log("    già presente, salto (usa --replace per ricaricarla)")
        if agent_name:
            attach_to_agent(api, agent_name, existing[0])
        return True

    docs = ente_documents(target / ente)
    if not docs:
        log("    nessun documento, salto")
        return False
    size = sum(p.stat().st_size for p, _ in docs)
    log(f"    documenti: {len(docs)}, {size / 1e9:.2f} GB")

    zip_path = tmp_dir / f"{ente.replace('/', '_')}.zip"
    started = time.time()
    try:
        build_zip(docs, zip_path)
        log(f"    zip pronto in {time.time() - started:.0f}s")
        task_id, new_id = upload_zip(api, name, zip_path)
        log(f"    upload accettato, task {task_id}")
    finally:
        # Dopo l'upload DocsGPT ha già la sua copia dei file.
        zip_path.unlink(missing_ok=True)
        zip_path.with_suffix(".zip.part").unlink(missing_ok=True)
    # La sorgente vecchia resta in uso finché la nuova non è pronta: niente
    # buchi nelle ricerche durante le ore di indicizzazione.
    try:
        wait_task(api, task_id)
    except Exception:
        if new_id:
            log(f"    indicizzazione fallita: elimino la sorgente incompleta {new_id}, resta quella vecchia")
            delete_source(api, new_id)
        raise
    if new_id:
        register_links(links_file, docs_url, ente, new_id, existing, depth=0 if "/" in ente else 1)
    # Letti solo ora: gli agenti potrebbero essere cambiati durante l'indicizzazione.
    agents = agents_using(api, existing)
    if agents and new_id:
        repoint_agents(api, agents, existing, new_id)
    if agent_name and new_id:
        attach_to_agent(api, agent_name, new_id)
    for source_id in existing:
        log(f"    elimino la sorgente precedente {source_id}")
        delete_source(api, source_id)
    clear_novita(api, ente)
    log(f"    completato in {(time.time() - started) / 60:.1f} minuti")
    return True


def get_args():
    p = argparse.ArgumentParser(description="Carica su DocsGPT le raccolte dell'archivio fiscale.")
    p.add_argument("--target", required=True, help="Directory radice dell'archivio")
    p.add_argument(
        "--source",
        action="append",
        choices=list(SOURCE_NAMES) + list(GROUPS),
        help="Ente da caricare, o il gruppo locazioni (ripetibile; default: tutti quelli presenti)",
    )
    p.add_argument("--replace", action="store_true", help="Ricarica le sorgenti già presenti (la vecchia viene eliminata a nuova pronta)")
    p.add_argument("--agent", default="", help="Agente a cui collegare le sorgenti caricate (di solito per quelle nuove)")
    p.add_argument("--api", default=API, help=f"URL di DocsGPT (default {API})")
    p.add_argument("--tmp", default=TMP_DIR, help=f"Cartella per lo zip temporaneo (default {TMP_DIR})")
    p.add_argument("--docs-url", default=DOCS_URL, help=f"Indirizzo pubblico della radice dell'archivio (default {DOCS_URL})")
    p.add_argument("--links-file", default=LINKS_FILE, help="File dei link ai documenti letto da DocsGPT")
    return p.parse_args()


def main() -> int:
    args = get_args()
    target = Path(args.target).resolve()
    if args.source:
        enti = [e for s in args.source for e in GROUPS.get(s, [s])]
    else:
        enti = [e for e in SOURCE_NAMES if (target / e).is_dir()]

    # Dal più piccolo al più grande: i problemi emergono prima sui carichi brevi.
    def ente_size(ente: str) -> int:
        return sum(p.stat().st_size for p, _ in ente_documents(target / ente))

    enti = sorted(enti, key=ente_size)

    failed = []
    for ente in enti:
        try:
            if not load_ente(args.api, target, ente, Path(args.tmp), args.replace, args.docs_url, Path(args.links_file), args.agent):
                failed.append(ente)
        except Exception as e:
            log(f"    ERRORE: {e}")
            failed.append(ente)

    log(f"Riepilogo: {len(enti) - len(failed)}/{len(enti)} enti caricati" + (f"; falliti: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
