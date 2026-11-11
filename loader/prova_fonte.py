#!/usr/bin/env python3
"""Caricamento in prova, benchmark, promozione e pulizia di una fonte della
pipeline /nuova-fonte (fasi 6 e 7).

  python3 prova_fonte.py prova    --id <id> [--target <archivio>]
  python3 prova_fonte.py bench    --id <id>
  python3 prova_fonte.py promuovi --id <id> [--target <archivio>]
  python3 prova_fonte.py pulisci  --id <id> [--target <archivio>]

La prova è una sorgente "prova - <nome>" con un agente "prova-<id>" collegato
solo a lei; il benchmark usa la chiave di quell'agente e le domande
<id>-* di eval/domande.yaml. Lo stato della prova sta in <cartella>/prova.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import requests

import carica_docsgpt
import fonte
from fonte import FonteError

ARCHIVIO = Path("/home/htpc/containers/docsgpt/circolari")
AGENTE_CLAUDE = "Claude - archivio fiscale"
EVAL = Path(__file__).resolve().parents[1] / "eval"
BACKEND = "docsgpt-backend-1"


def esito_benchmark(righe: list[dict], soglia_top: int = 3, soglia_quota: float = 0.8) -> tuple[bool, float]:
    """Quota di domande (modalità B, con keywords) con la prima fonte attesa entro i primi soglia_top."""
    b = [r for r in righe if r.get("mode") == "B"]
    if not b:
        return False, 0.0
    buone = sum(1 for r in b if (r.get("ranks") or [None])[0] is not None and r["ranks"][0] <= soglia_top)
    quota = round(buone / len(b), 4)
    return quota >= soglia_quota, quota


def domande_fonte(domande: dict, id: str) -> list[dict]:
    return [d for d in domande.get("domande") or [] if str(d.get("id", "")).startswith(f"{id}-")]


def crea_agente_prova(api: str, id: str, source_id: str) -> tuple[str, str]:
    body = {"name": f"prova-{id}", "description": f"Agente temporaneo per il benchmark della fonte {id}",
            "sources": [source_id], "chunks": 10, "retriever": "classic", "prompt_id": "default",
            "status": "published", "agent_type": "classic"}
    r = requests.post(f"{api}/api/create_agent", json=body, timeout=60)
    if r.status_code not in (200, 201):
        raise FonteError(f"create_agent HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    return data["id"], data["key"]


def _stato_prova(archivio: Path, cfg: dict) -> Path:
    return fonte.cartella(archivio, cfg) / "prova.json"


def _togli_link(links_file: Path, ids: list[str]) -> None:
    if not links_file.exists():
        return
    links = json.loads(links_file.read_text())
    if not any(i in links for i in ids):
        return
    for i in ids:
        links.pop(i, None)
    tmp = links_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(links, indent=1))
    tmp.replace(links_file)


def pulisci(api: str, archivio: Path, cfg: dict, links_file: Path) -> None:
    """Cancella sorgente e agente di prova (anche rimasti da una prova interrotta). Idempotente."""
    stato_path = _stato_prova(archivio, cfg)
    stato = json.loads(stato_path.read_text()) if stato_path.exists() else {}
    agenti = {stato["agent_id"]} if stato.get("agent_id") else set()
    r = requests.get(f"{api}/api/get_agents", timeout=60)
    agenti |= {a["id"] for a in (r.json() or []) if a.get("name") == f"prova-{cfg['id']}"}
    sorgenti = {stato["source_id"]} if stato.get("source_id") else set()
    sorgenti |= set(carica_docsgpt.list_sources(api).get(f"prova - {cfg['nome']}", []))
    for a in sorted(agenti):
        requests.delete(f"{api}/api/delete_agent?id={a}", timeout=60)
    for s in sorted(sorgenti):
        carica_docsgpt.delete_source(api, s)
    _togli_link(links_file, sorted(sorgenti))
    stato_path.unlink(missing_ok=True)
    print(f"pulito: {len(agenti)} agenti, {len(sorgenti)} sorgenti di prova")


def prova(api: str, archivio: Path, cfg: dict, tmp_dir: Path, docs_url: str, links_file: Path) -> tuple[str, str]:
    pulisci(api, archivio, cfg, links_file)
    ente = f"{cfg['gruppo']}/{cfg['id']}"
    docs = carica_docsgpt.ente_documents(Path(archivio) / ente)
    if not docs:
        raise FonteError(f"nessun file in {Path(archivio) / ente}: esegui prima estrai.py")
    zip_path = Path(tmp_dir) / f"prova_{cfg['id']}.zip"
    try:
        carica_docsgpt.build_zip(docs, zip_path)
        task_id, source_id = carica_docsgpt.upload_zip(api, f"prova - {cfg['nome']}", zip_path)
    finally:
        zip_path.unlink(missing_ok=True)
    carica_docsgpt.wait_task(api, task_id)
    carica_docsgpt.register_links(links_file, docs_url, ente, source_id, [], depth=0)
    agent_id, key = crea_agente_prova(api, cfg["id"], source_id)
    _stato_prova(archivio, cfg).write_text(json.dumps({"source_id": source_id, "agent_id": agent_id, "key": key}))
    print(f"prova pronta: sorgente {source_id}, agente prova-{cfg['id']}, {len(docs)} file")
    return source_id, agent_id


def bench(archivio: Path, cfg: dict) -> tuple[bool, float, Path]:
    import yaml

    stato_path = _stato_prova(archivio, cfg)
    if not stato_path.exists():
        raise FonteError("nessuna prova attiva: esegui prima 'prova'")
    key = json.loads(stato_path.read_text())["key"]
    domande = domande_fonte(yaml.safe_load((EVAL / "domande.yaml").read_text(encoding="utf-8")), cfg["id"])
    if not domande:
        raise FonteError(f"nessuna domanda {cfg['id']}-* in eval/domande.yaml")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"domande": domande}, f, ensure_ascii=False)
    subprocess.run(["docker", "cp", str(EVAL / "bench.py"), f"{BACKEND}:/tmp/bench.py"], check=True)
    proc = subprocess.run(
        ["docker", "exec", "-i", "-e", f"AGENT_KEY={key}", "-e", f"SOLO_PREFISSO={cfg['id']}-",
         "-e", "PYTHONPATH=/app", "-w", "/app", BACKEND, "python", "/tmp/bench.py"],
        stdin=open(f.name), capture_output=True, text=True, timeout=3600)
    Path(f.name).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise FonteError(f"bench.py exit {proc.returncode}: {proc.stderr[-300:]}")
    out = EVAL / f"risultati-{cfg['id']}-{date.today().isoformat()}.jsonl"
    out.write_text(proc.stdout)
    righe = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    ok, quota = esito_benchmark(righe)
    for r in righe:
        if r["mode"] == "B":
            print(f"  {r['id']:<24} rank {r['ranks']}  top3: {', '.join(r.get('top3') or [])[:80]}")
    return ok, quota, out


def promuovi(api: str, archivio: Path, cfg: dict, tmp_dir: Path, docs_url: str, links_file: Path) -> None:
    ente = f"{cfg['gruppo']}/{cfg['id']}"
    if ente not in carica_docsgpt.SOURCE_NAMES:
        raise FonteError(f"{ente} non è in loader/fonti/pipeline.yaml")
    if not carica_docsgpt.load_ente(api, Path(archivio), ente, Path(tmp_dir), True, docs_url, links_file, AGENTE_CLAUDE):
        raise FonteError(f"caricamento definitivo di {ente} fallito")
    pulisci(api, archivio, cfg, links_file)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("azione", choices=["prova", "bench", "promuovi", "pulisci"])
    p.add_argument("--id", required=True)
    p.add_argument("--target", type=Path, default=ARCHIVIO)
    p.add_argument("--api", default=carica_docsgpt.API)
    p.add_argument("--tmp", type=Path, default=Path(carica_docsgpt.TMP_DIR))
    p.add_argument("--docs-url", default=carica_docsgpt.DOCS_URL)
    p.add_argument("--links-file", type=Path, default=Path(carica_docsgpt.LINKS_FILE))
    a = p.parse_args(argv)
    try:
        cfg = fonte.config(a.id)
        if a.azione == "prova":
            prova(a.api, a.target, cfg, a.tmp, a.docs_url, a.links_file)
        elif a.azione == "bench":
            ok, quota, out = bench(a.target, cfg)
            print(f"benchmark {cfg['id']}: {quota:.0%} entro i primi 3 (soglia 80%) → {'PASS' if ok else 'FAIL'}  [{out.name}]")
            return 0 if ok else 1
        elif a.azione == "promuovi":
            promuovi(a.api, a.target, cfg, a.tmp, a.docs_url, a.links_file)
        else:
            pulisci(a.api, a.target, cfg, a.links_file)
    except (FonteError, RuntimeError, requests.RequestException) as e:
        print(f"ERRORE: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
