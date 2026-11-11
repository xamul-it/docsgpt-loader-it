# Pipeline agentica per nuove ingestion — piano di implementazione

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** costruire la skill di Claude Code (`/nuova-fonte`) e gli script riutilizzabili che portano una fonte nuova dalla richiesta alla sorgente DocsGPT collegata a Claude, con misure e test di qualità. Il collaudo sul Codice civile è fuori da questo piano (sessione nuova).

**Architecture:** gli script deterministici in `loader/` fanno tutto il lavoro sui documenti (scarico, analisi AKN, misura dei formati, valutazione dei link, estrazione un-file-per-articolo, test di qualità, caricamento in prova). La skill descrive le fasi, le regole di decisione e i punti di contatto con l'utente; due sottoagenti con modello fissato (Sonnet per analisi e codice, Haiku per eseguire e riassumere) la eseguono. Ogni fonte ha un rapporto in `docs/ingestion/`.

**Tech Stack:** Python 3 (stile di `loader/`: `requests`, `lxml`, `beautifulsoup4`, `unittest`), DocsGPT 0.21.0 API su `http://127.0.0.1:7091`, Claude Code skill e sottoagenti.

**Spec:** `docs/superpowers/specs/2026-11-11-pipeline-ingestion-design.md`

## Global Constraints

- DocsGPT resta 0.21.0; `patches/search_service.py` non si tocca.
- Nessun modello di linguaggio sui singoli documenti: solo codice deterministico.
- Uscite degli script compatte: riepiloghi e contatori, mai documenti interi; output completi su file.
- Domande all'utente solo in due punti: all'avvio se manca argomento o fonte (un messaggio), e all'approvazione finale.
- Modelli: Sonnet per analisi/codice/domande, Opus solo per criteri in conflitto, Haiku per esecuzione e riepiloghi; test senza modello.
- Il formato e la destinazione dei link si scelgono da misure scritte nel rapporto, mai a priori.
- Soglie predefinite: conteggio unità 100%; testo vuoto/navigazione 0; residui 0; rubrica ≥ 99%; metadati obbligatori 100%; benchmark: fonte attesa entro i primi 3 in ≥ 80% delle domande con keywords.
- Un file per unità, con in testa percorso e rubrica.
- Al massimo un nuovo tentativo per fase, poi stop e rapporto.
- Test nello stile esistente: `loader/test_*.py`, `python3 -m unittest`, nessuna richiesta di rete (fixture in `loader/fixtures/`).
- Non toccare l'estratto del Codice civile in `locazioni-nazionale-lungo-termine`.

## Review Focus

1. **Numeri di articolo ripetuti tra parti dell'atto** (art. 1 delle Disposizioni sulla legge in generale e art. 1 del Codice): devono dare due unità distinte, non sovrascriversi. → test in Task 2 e Task 4.
2. **Articoli "bis/ter/…" (205 nel Codice civile) e abrogati (~193)**: numero normalizzato (`42-bis`), l'abrogato è un'unità valida con testo breve e non un "testo vuoto". → test in Task 2 e Task 5.
3. **Normattiva risponde con una pagina HTML di errore invece dell'XML**: lo scarico deve fallire con un messaggio chiaro, non produrre zero articoli "validi". → test in Task 1.
4. **Riesecuzione con data ELI invariata**: nessuna riscrittura; con data cambiata, riscritte solo le unità cambiate (sha256). → test in Task 4.
5. **Articoli lunghi** (max osservato 10.503 caratteri, p95 1.604): divisi per comma oltre soglia, ogni parte con il percorso in testa e numero di parte nel nome. → test in Task 4.

---

### Task 1: Client Normattiva

**Files:**
- Create: `loader/normattiva.py`
- Create: `loader/fixtures/normattiva/akn_campione.xml` (estratto di `caricaAKN` del Codice civile: `meta` dell'atto + allegati `annesso_1`, `annesso_2`, `annesso_43` (art. 42 bis), l'art. 1399, un articolo abrogato, l'art. 1 del Codice; ritagliato dal file scaricato in sessione o riscaricato)
- Create: `loader/fixtures/normattiva/indice_campione.html` (porzione della pagina atto con l'indice da "CAPO VI" a "CAPO VIII" del Libro IV)
- Test: `loader/test_normattiva.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ActRef: data_gu: str  # "YYYYMMDD"`, `codice_redaz: str`, `urn: str | None = None`
  - `act_ref_from_url(url: str) -> ActRef` — accetta `/eli/id/AAAA/MM/GG/<codice>[/...]`, `caricaDettaglioAtto?atto.dataPubblicazioneGazzetta=AAAA-MM-GG&atto.codiceRedazionale=<codice>`, `uri-res/N2Ls?urn:nir:...` (in questo caso `data_gu`/`codice_redaz` si ricavano scaricando la pagina: funzione separata `resolve_urn(session, url) -> ActRef`).
  - `akn_url(ref: ActRef, data_vigenza: str) -> str` → `https://www.normattiva.it/do/atto/caricaAKN?dataGU={data_gu}&codiceRedaz={codice_redaz}&dataVigenza={data_vigenza}`
  - `fetch_akn(session: requests.Session, ref: ActRef, data_vigenza: str) -> bytes` — solleva `NormattivaError` se la risposta non è XML con radice `akomaNtoso`.
  - `parse_indice(html: str) -> dict[str, list[str]]` — numero articolo normalizzato (`"1401"`, `"42-bis"`) → percorso gerarchico (es. `["LIBRO QUARTO …", "TITOLO II …", "CAPO VII Del contratto per persona da nominare"]`).
  - `normalizza_num(s: str) -> str` — `"42 bis"`, `"42-bis"`, `"Art. 42 bis."` → `"42-bis"`.
  - Costante `UA = downloader.CURATED_UA`.

- [ ] **Step 1: Write the failing tests**

```python
def test_act_ref_da_eli(self):
    r = n.act_ref_from_url("https://www.normattiva.it/eli/id/1942/04/04/042U0262?utm_source=x")
    self.assertEqual((r.data_gu, r.codice_redaz), ("19420404", "042U0262"))

def test_act_ref_da_dettaglio(self):
    r = n.act_ref_from_url("https://www.normattiva.it/atto/caricaDettaglioAtto?atto.dataPubblicazioneGazzetta=1942-04-04&atto.codiceRedazionale=042U0262")
    self.assertEqual(r.codice_redaz, "042U0262")

def test_akn_url(self):
    self.assertEqual(n.akn_url(n.ActRef("19420404", "042U0262"), "20261111"),
        "https://www.normattiva.it/do/atto/caricaAKN?dataGU=19420404&codiceRedaz=042U0262&dataVigenza=20261111")

def test_fetch_akn_rifiuta_html(self):   # Review Focus 3
    sess = mock_session(content=b"<html><body>Errore</body></html>", content_type="text/html")
    with self.assertRaises(n.NormattivaError):
        n.fetch_akn(sess, n.ActRef("19420404", "042U0262"), "20261111")

def test_normalizza_num(self):
    for s in ("42 bis", "42-bis", "Art. 42 bis."):
        self.assertEqual(n.normalizza_num(s), "42-bis")

def test_indice_capo_vii(self):
    idx = n.parse_indice(FIXTURE("indice_campione.html"))
    self.assertTrue(idx["1401"][-1].startswith("CAPO VII Del contratto per persona da nominare"))
    self.assertNotIn("CAPO VII", " ".join(idx["1400"]))
```

- [ ] **Step 2:** `cd loader && python3 -m unittest test_normattiva -v` → FAIL (modulo assente)
- [ ] **Step 3:** implementare `loader/normattiva.py` con le firme sopra (lxml per l'XML, BeautifulSoup per l'indice; studiare il markup reale dell'indice nella fixture).
- [ ] **Step 4:** stesso comando → PASS
- [ ] **Step 5:** `git add loader/normattiva.py loader/test_normattiva.py loader/fixtures/normattiva && git commit -m "normattiva: client AKN e indice dell'atto"`

### Task 2: Analisi AKN

**Files:**
- Create: `loader/akn.py`
- Test: `loader/test_akn.py`

**Interfaces:**
- Consumes: `normattiva.normalizza_num`
- Produces:
  - `@dataclass class Unita: parte: str  # "CODICE CIVILE" | "Disposizioni sulla legge in generale"`, `num: str  # normalizzato`, `rubrica: str`, `testo: str  # senza marcatori ((n))`, `eid: str  # es. "annesso_1501"`, `abrogato: bool`, `marcatori_rimossi: int`
  - `@dataclass class AttoAkn: frbr_uri: str`, `urn_nir: str`, `eli: str`, `data_consolidamento: str  # "YYYY-MM-DD" dall'alias ELI CONSOLIDATED`, `unita: list[Unita]`
  - `parse_akn(xml: bytes) -> AttoAkn`
  - `slug_parte(parte: str) -> str` — `"CODICE CIVILE"` → `"cc"`, `"Disposizioni sulla legge in generale"` → `"preleggi"`, altrimenti slug ascii.

- [ ] **Step 1: Write the failing tests** (sulla fixture del Task 1)

```python
def test_meta(self):
    a = akn.parse_akn(FIX)
    self.assertEqual(a.urn_nir, "urn:nir:stato:regio.decreto:1942-03-16;262")
    self.assertRegex(a.data_consolidamento, r"^\d{4}-\d{2}-\d{2}$")   # dall'ELI, non dal FRBRdate 1944-11-10

def test_art1_doppio(self):           # Review Focus 1
    a = akn.parse_akn(FIX)
    uno = [u for u in a.unita if u.num == "1"]
    self.assertEqual({u.parte for u in uno}, {"CODICE CIVILE", "Disposizioni sulla legge in generale"})

def test_rubrica_e_testo_1399(self):
    u = next(u for u in akn.parse_akn(FIX).unita if u.num == "1399")
    self.assertEqual(u.rubrica, "Ratifica")
    self.assertTrue(u.testo.startswith("Nell'ipotesi prevista dall'articolo precedente"))
    self.assertNotRegex(u.testo, r"\(\(\d+\)\)")

def test_bis_e_abrogato(self):        # Review Focus 2
    unita = akn.parse_akn(FIX).unita
    self.assertTrue(any(u.num == "42-bis" for u in unita))
    ab = next(u for u in unita if u.abrogato)
    self.assertGreater(len(ab.testo), 0)
```

- [ ] **Step 2:** `python3 -m unittest test_akn -v` → FAIL
- [ ] **Step 3:** implementare `parse_akn` (un `attachment/doc` = un'unità; `parte` e `num` dal `@name` `"<PARTE>-art. <num>"`; rubrica = testo tra parentesi dopo "Art. N." in testa; `abrogato` se il testo dopo la rubrica è dominato da "abrogat"/"((ARTICOLO ABROGATO" entro 300 caratteri).
- [ ] **Step 4:** → PASS
- [ ] **Step 5:** commit `"akn: analisi dell'export Normattiva in unità per articolo"`

### Task 3: Misura dei formati (fase 2)

**Files:**
- Create: `loader/misura_formati.py`
- Test: `loader/test_misura_formati.py`

**Interfaces:**
- Consumes: `normattiva.*`, `akn.parse_akn`
- Produces:
  - `campione(nums: list[str], n: int = 30, seed: str = "nuova-fonte", obbligatori: list[str] = ()) -> list[str]` — deterministico (ordinamento per sha256(seed+num)); include sempre gli `obbligatori` (il chiamante passa almeno un abrogato, un `-bis` e il più lungo, se presenti) e ha lunghezza `n`.
  - `@dataclass class Misura: formato: str, articoli_attesi: int, articoli_trovati: int, vuoti: int, con_rubrica: int, con_percorso: int, residui: int, byte: int, richieste: int, data_aggiornamento: str | None`
  - `misura_testi(formato: str, testi: dict[str, tuple[str, str, list[str]]], attesi: list[str]) -> Misura` — `testi`: num → (rubrica, testo, percorso); il conteggio dei residui usa `RESIDUI = [r"\(\(\d+\)\)", r"Vai al contenuto", r"Seleziona tutti", r"Il tuo browser non supporta"]`.
  - CLI: `python3 misura_formati.py --url <url atto> --vigenza AAAAMMGG --out <dir>` scarica i formati disponibili (AKN vigente; pagina atto con indice; pagina N2Ls `~artN` per gli articoli del campione; `esporta/attoCompleto`), salva le risposte in `<dir>/raw/`, scrive `<dir>/misure.json` e stampa una tabella di ≤ 15 righe.

- [ ] **Step 1: Write the failing tests**

```python
def test_campione_deterministico(self):
    nums = [str(i) for i in range(1, 500)] + ["42-bis"]
    self.assertEqual(m.campione(nums, obbligatori=["42-bis"]), m.campione(nums, obbligatori=["42-bis"]))
    self.assertIn("42-bis", m.campione(nums, obbligatori=["42-bis"]))
    self.assertEqual(len(m.campione(nums, obbligatori=["42-bis"])), 30)

def test_misura_conta_residui_e_vuoti(self):
    testi = {"1": ("Fonti", "Sono fonti del diritto ((3))", []), "2": ("", "Vai al contenuto", [])}
    r = m.misura_testi("x", testi, attesi=["1", "2", "3"])
    self.assertEqual((r.articoli_trovati, r.residui, r.con_rubrica), (2, 2, 1))
```

- [ ] **Step 2:** → FAIL · **Step 3:** implementare · **Step 4:** → PASS
- [ ] **Step 5:** commit `"misura_formati: confronto misurato dei formati di una fonte normativa"`

### Task 4: Estrattore normativa, un file per unità

**Files:**
- Create: `loader/estrai_normativa.py`
- Create: `loader/fonti/codici.yaml`
- Modify: `loader/carica_docsgpt.py` (gruppo `codici` accanto a `locazioni`: cartelle `codici/<id>`, nomi da `fonti/codici.yaml`)
- Test: `loader/test_estrai_normativa.py`

**Interfaces:**
- Consumes: `normattiva.*`, `akn.*`
- Produces:
  - `fonti/codici.yaml`: `sorgenti: [{id: codice-civile, nome: "Codice civile", url: <url ELI>, formato: <scelto in fase 2>, link: <locale|ufficiale|entrambi, scelto in fase 3>, soglia_caratteri: 4000}]`
  - `scrivi_unita(atto: AttoAkn, indice: dict[str, list[str]], out_dir: Path, soglia_caratteri: int, link_mode: str, base_link: str) -> Rapporto` dove `@dataclass class Rapporto: scritte: int, invariate: int, rimosse: int, divise: int`
  - File: `<out_dir>/files/<slug_parte>-art-<num>[-p<k>].html`; HTML minimale con `<title>` = `"<Atto> › <percorso…> › Art. <num> – <rubrica>"`, in testa `<p class="percorso">` e `<h1>`, poi il testo.
  - `<out_dir>/manifest.jsonl`, righe con i campi esistenti (`source, title, document_date, authority="Normattiva", document_type="codice", number=<num>, detail_url, download_url, local_file, sha256, downloaded_at`) più `urn, eli, parte, articolo, rubrica, percorso, data_vigenza, link`.
  - `<out_dir>/stato.json` con `data_consolidamento`; CLI `python3 estrai_normativa.py --target <archivio> [--id codice-civile]`: se `data_consolidamento` non è cambiata stampa "invariato" ed esce 0.
  - `link`: `link_mode="locale"` → vuoto (il link lo dà `doc_links`); `"ufficiale"`/`"entrambi"` → URL ufficiale per articolo nella forma scelta in fase 3.

- [ ] **Step 1: Write the failing tests**

```python
def test_art1_due_file(self):                 # Review Focus 1
    r = e.scrivi_unita(ATTO, INDICE, tmp, 4000, "locale", "")
    self.assertTrue((tmp/"files/cc-art-1.html").exists() and (tmp/"files/preleggi-art-1.html").exists())

def test_titolo_con_percorso(self):
    e.scrivi_unita(ATTO, INDICE, tmp, 4000, "locale", "")
    row = manifest_row(tmp, "1401")
    self.assertIn("CAPO VII", row["title"]); self.assertEqual(row["rubrica"], RUBRICA_1401)

def test_divisione_articolo_lungo(self):      # Review Focus 5
    r = e.scrivi_unita(ATTO_CON_ART_LUNGO_9000, {}, tmp, 4000, "locale", "")
    parti = sorted((tmp/"files").glob("cc-art-9999-p*.html"))
    self.assertGreaterEqual(len(parti), 3)
    self.assertTrue(all("Art. 9999" in p.read_text() for p in parti))

def test_rieseguo_invariato(self):            # Review Focus 4
    e.scrivi_unita(ATTO, INDICE, tmp, 4000, "locale", "")
    r = e.scrivi_unita(ATTO, INDICE, tmp, 4000, "locale", "")
    self.assertEqual((r.scritte, r.invariate), (0, len(ATTO.unita)))

def test_rieseguo_un_articolo_cambiato(self):
    e.scrivi_unita(ATTO, INDICE, tmp, 4000, "locale", "")
    r = e.scrivi_unita(atto_con_testo_cambiato(ATTO, "1399"), INDICE, tmp, 4000, "locale", "")
    self.assertEqual(r.scritte, 1)
```

- [ ] **Step 2:** → FAIL · **Step 3:** implementare (divisione per comma: separatori di comma del testo; se un comma supera ancora la soglia, taglio al punto più vicino) · **Step 4:** → PASS
- [ ] **Step 5:** test di `carica_docsgpt`: `ente_documents(<archivio>/codici/codice-civile)` restituisce i file del manifest con percorso nello zip senza `files/` → aggiungere a `test_estrai_normativa.py`, PASS.
- [ ] **Step 6:** commit `"estrai_normativa: un file per articolo con percorso, rubrica e aggiornamento incrementale"`

### Task 5: Valutazione dei link (fase 3) e test di qualità (fase 5)

**Files:**
- Create: `loader/valuta_link.py`
- Create: `loader/qualita_estrazione.py`
- Test: `loader/test_qualita.py`

**Interfaces:**
- Consumes: manifest del Task 4, `akn.parse_akn`
- Produces:
  - `valuta_link(candidati: dict[str, Callable[[dict], str]], righe: list[dict], session) -> dict[str, dict]` — per ogni candidato (`"locale"`, `"ufficiale_urn"`, …) e riga del campione: `status`, `precisione` (`"articolo"` se rubrica e prime 8 parole del testo compaiono nella pagina senza il testo di altri articoli adiacenti, `"atto"` se compaiono insieme a molti altri articoli, `"assente"`), `byte`. CLI stampa una tabella candidato × {200%, articolo%, atto%, byte medi}.
  - `@dataclass class Esito: nome: str, valore: float, soglia: float, ok: bool, esempi: list[str]  # max 3`
  - `controlla(out_dir: Path, atteso: list[str], sorgente_testi: dict[str, str], min_caratteri: int = 40) -> list[Esito]` con controlli e soglie dei Global Constraints; un'unità con `abrogato=true` nel manifest è esente dal minimo di caratteri. CLI `python3 qualita_estrazione.py --dir <out_dir> --url <url atto> --vigenza AAAAMMGG`: stampa una riga per controllo, exit 1 se uno fallisce.

- [ ] **Step 1: Write the failing tests**

```python
def test_tutto_ok(self):
    esiti = q.controlla(DIR_BUONA, ATTESI, TESTI_FONTE)
    self.assertTrue(all(x.ok for x in esiti))

def test_pagina_di_navigazione(self):          # caso issue #3
    scrivi(DIR, "cc-art-5.html", "Vai al contenuto Vai alla navigazione Home / Turismo")
    self.assertFalse(esito(q.controlla(DIR, ATTESI, TESTI_FONTE), "testo_vuoto_o_navigazione").ok)

def test_abrogato_non_e_vuoto(self):           # Review Focus 2
    self.assertTrue(esito(q.controlla(DIR_CON_ABROGATO, ATTESI, TESTI_FONTE), "testo_vuoto_o_navigazione").ok)

def test_mancante(self):
    self.assertFalse(esito(q.controlla(DIR_BUONA, ATTESI + ["9999"], TESTI_FONTE), "conteggio_unita").ok)

def test_precisione_link(self):
    pagina_atto = "Art. 1398 ... Art. 1399. (Ratifica). Nell'ipotesi prevista ... Art. 1400 ..." * 50
    r = v.valuta_link({"u": lambda row: "http://x"}, [RIGA_1399], mock_session(text=pagina_atto))
    self.assertEqual(r["u"]["precisione"]["atto"], 1)
```

- [ ] **Step 2:** → FAIL · **Step 3:** implementare · **Step 4:** → PASS
- [ ] **Step 5:** commit `"qualita: valutazione dei link e test di qualità dell'estrazione"`

### Task 6: Caricamento in prova, benchmark della fonte, promozione

**Files:**
- Create: `loader/prova_fonte.py`
- Modify: `eval/bench.py` (filtro per fonte: env `SOLO_PREFISSO`, es. `cc-`, esegue solo le domande il cui `id` inizia così)
- Modify: `notifier/notifier.py` (ente `codici` aggiornato con `estrai_normativa.py` invece di `downloader.py`; poi `carica_docsgpt.py --source codici --replace` come gli altri)
- Test: `loader/test_prova_fonte.py`

**Interfaces:**
- Consumes: `carica_docsgpt.upload_zip, wait_task, build_zip, ente_documents, attach_to_agent, register_links, delete_source`
- Produces:
  - `prova(api: str, archivio: Path, gruppo_id: str) -> tuple[str, str]` → `(source_id, agent_id)`: carica `codici/<id>` come sorgente `"prova - <nome>"`, crea l'agente `prova-<id>` con `POST /api/create_agent` (`name, description, sources=[source_id], chunks=10, retriever="classic", prompt_id="", status="published", agent_type="classic"`; verificare i campi richiesti in `docsgpt/api/user/agents/routes.py` dell'immagine 0.21.0).
  - `esito_benchmark(righe: list[dict], soglia_top: int = 3, soglia_quota: float = 0.8) -> tuple[bool, float]` — solo modalità `B`, prima fonte attesa.
  - `promuovi(api, gruppo_id)`: ricarica come sorgente definitiva `<nome>` (stesso flusso di `load_ente`), collega a `"Claude - archivio fiscale"`, registra in `doc_links.json`, cancella sorgente e agente di prova.
  - `pulisci(api, gruppo_id)`: cancella sorgente e agente di prova (`/api/delete_agent?id=`, `delete_source`).
  - CLI: `python3 prova_fonte.py {prova|bench|promuovi|pulisci} --id codice-civile`; `bench` lancia `eval/bench.py` nel container con la chiave dell'agente di prova e `SOLO_PREFISSO`, salva `eval/risultati-<id>-<data>.jsonl`, stampa quota e PASS/FAIL.

- [ ] **Step 1: Write the failing tests**

```python
def test_esito_soglia(self):
    righe = [{"mode": "B", "ranks": [r]} for r in (1, 2, 3, 3, None)]
    self.assertEqual(p.esito_benchmark(righe), (True, 0.8))
    righe[0]["ranks"] = [4]
    self.assertEqual(p.esito_benchmark(righe)[0], False)

def test_esito_ignora_modalita_a(self):
    self.assertEqual(p.esito_benchmark([{"mode": "A", "ranks": [None]}, {"mode": "B", "ranks": [1]}]), (True, 1.0))

def test_create_agent_payload(self):
    with mock.patch("prova_fonte.requests.post") as post:
        post.return_value.json.return_value = {"id": "a1", "key": "k"}
        p.crea_agente_prova("http://api", "codice-civile", "s1")
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["name"], body["sources"], body["retriever"]), ("prova-codice-civile", ["s1"], "classic"))
```

- [ ] **Step 2:** → FAIL · **Step 3:** implementare · **Step 4:** → PASS
- [ ] **Step 5:** verifica manuale non distruttiva: `python3 prova_fonte.py prova --id codice-civile` su una cartella di 20 articoli, poi `pulisci`; `curl -s :7091/api/get_agents` non mostra `prova-*`.
- [ ] **Step 6:** commit `"prova_fonte: caricamento in prova, benchmark della fonte e promozione"`

### Task 7: Skill `/nuova-fonte` e sottoagenti

**Files:**
- Create: `.claude/skills/nuova-fonte/SKILL.md`
- Create: `.claude/agents/fonte-analista.md` (`model: sonnet`; fasi 1–4, scrittura domande di prova)
- Create: `.claude/agents/fonte-esecutore.md` (`model: haiku`; lancia script, riassume uscite, fasi 5–6)
- Create: `docs/ingestion/_modello.md` (rapporto: scheda fonte; tabella formati + scelta; divisione/metadati; tabella link + scelta; esiti qualità; benchmark; decisioni prese in autonomia; esito)

**Interfaces:**
- Consumes: CLI dei Task 3–6.
- Produces: la skill con, per ogni fase: input, comando, criterio di uscita, regola di decisione, valore predefinito, cosa scrivere nel rapporto; i due soli punti di domanda all'utente (avvio se manca argomento/fonte; approvazione finale con il rapporto); regola "un tentativo poi stop"; regola di scelta del formato (ordine: completezza 100% → residui 0 → rubrica → percorso → costo; combinazioni ammesse); regola di scelta del link (precisione "articolo" > corrispondenza versione > disponibilità > costo; a parità, `entrambi`); escalation a Opus solo se due criteri di primo livello indicano formati diversi.

- [ ] **Step 1:** scrivere i tre file e il modello di rapporto.
- [ ] **Step 2:** verifica: `python3 -c "import yaml,sys; [yaml.safe_load(open(f).read().split('---')[1]) for f in sys.argv[1:]]" .claude/skills/nuova-fonte/SKILL.md .claude/agents/fonte-*.md` → nessun errore; ogni comando citato nella skill esiste (`grep -oE 'python3 [a-z_]+\.py' SKILL.md` → tutti presenti in `loader/`).
- [ ] **Step 3:** commit `"skill nuova-fonte: fasi, regole di decisione e sottoagenti"`

## Fuori da questo piano: collaudo in una sessione nuova

Il collaudo della pipeline si fa in una sessione nuova di Claude Code, senza il
contesto di questa: è la prova che la skill funziona da sola.

Avvio: `/nuova-fonte importa il Codice civile da https://www.normattiva.it/eli/id/1942/04/04/042U0262`

Criteri di riuscita del collaudo:
- la pipeline arriva alla fase 7 chiedendo all'utente al massimo all'avvio e all'approvazione;
- `qualita_estrazione.py` tutto OK (3.281 unità attese = 3.250 c.c. + 31 preleggi, salvo variazioni della data di vigenza riportate nel rapporto);
- `prova_fonte.py bench` PASS (≥ 80% entro i primi 3) su 8 domande `cc-*` (2 riferimenti esatti incluso art. 1 preleggi contro art. 1 c.c., 2 parafrasi, 1 numeri vicini 1398/1399, 1 rubrica senza numero, 1 articolo `-bis`, 1 abrogato);
- `docs/ingestion/codice-civile.md` compilato, con le scelte di formato e link motivate dalle misure;
- dopo la promozione, `eval/bench.py` completo: nessuna domanda esistente peggiora oltre 2 posizioni rispetto a `eval/baseline-2026-11-11.jsonl`.

Le scelte di formato, link e soglia in `loader/fonti/codici.yaml` le fissa il collaudo: questo piano lascia nel file i valori di partenza (`formato: akn-vigente`, `link: locale`, `soglia_caratteri: 4000`), marcati come provvisori.
