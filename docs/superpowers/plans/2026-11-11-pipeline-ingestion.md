# Pipeline agentica per nuove fonti — piano di implementazione

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** costruire il nucleo comune (contratto dell'unità, rete con cache, misura, estrazione, qualità, prova) e la skill `/nuova-fonte`, che crea il loader di una fonte generica come adattatore; il primo adattatore è Normattiva. Il collaudo sul Codice civile è fuori da questo piano (sessione nuova).

**Architecture:** ogni fonte è un adattatore in `loader/adattatori/` che produce `fonte.Unita`; tutto il resto è codice comune in `loader/` (misura dei formati, scrittura un-file-per-unità con aggiornamento incrementale, valutazione dei link, test di qualità, caricamento in prova). I test di contratto valgono per ogni adattatore e girano su fixture, senza rete. La skill descrive fasi, regole di decisione e punti di contatto con l'utente; due sottoagenti con modello fissato (Sonnet per analisi e codice, Haiku per eseguire e riassumere) la eseguono. Ogni fonte ha un rapporto in `docs/ingestion/`.

**Tech Stack:** Python 3 (`requests`, `beautifulsoup4`, `defusedxml`, `PyYAML`, `unittest`: le stesse librerie del container del notifier), DocsGPT 0.21.0 API su `http://127.0.0.1:7091`, Claude Code skill e sottoagenti.

**Spec:** `docs/superpowers/specs/2026-11-11-pipeline-ingestion-design.md`

## Global Constraints

- DocsGPT resta 0.21.0; `patches/search_service.py` non si tocca.
- Nessun modello di linguaggio sui singoli documenti: solo codice deterministico.
- Nel nucleo nessuna regola specifica di una fonte: "articolo", "rubrica", "abrogato", "Normattiva" compaiono solo in `loader/adattatori/` e nelle sue fixture.
- Dipendenze solo tra quelle del notifier (`notifier/requirements.txt`): XML con `defusedxml.ElementTree`, niente `lxml`.
- Uscite degli script al massimo ~15 righe (contatori, ≤ 3 esempi); i risultati completi in JSON su file.
- Domande all'utente solo in due punti: all'avvio se manca argomento o fonte (un messaggio), e all'approvazione finale.
- Modelli: Sonnet per analisi, adattatori e domande; Opus solo per criteri di primo livello in conflitto; Haiku per esecuzione e riepiloghi; test senza modello.
- Formato e destinazione dei link si scelgono da misure scritte nel rapporto, mai a priori.
- Soglie predefinite: conteggio unità 100%; testo vuoto/navigazione 0; residui 0; titolo ≥ 99%; metadati obbligatori 100%; benchmark: unità attesa entro i primi 3 in ≥ 80% delle domande con keywords.
- Un file per unità (o parte), con in testa percorso e titolo; soglia di divisione predefinita 4000 caratteri.
- Al massimo un nuovo tentativo per fase, poi stop e rapporto.
- Test nello stile esistente: `loader/test_*.py`, `cd loader && python3 -m unittest`, nessuna richiesta di rete (fixture in `loader/fixtures/<adattatore>/`).
- Fonti esistenti (`downloader.py`, `fonti/locazioni.yaml`, l'estratto del Codice civile in `locazioni-nazionale-lungo-termine`) non si toccano.

## Review Focus

1. **Id ripetuti** (art. 1 delle preleggi e art. 1 del codice): l'adattatore Normattiva dà due id distinti; se un adattatore qualsiasi produce id duplicati, `estrai` si ferma con errore che li nomina invece di sovrascrivere. → test in Task 3 e Task 5.
2. **Unità corte legittime e numeri "bis"** (205 bis/ter/…, ~193 abrogati nel Codice civile): id normalizzato (`cc-art-42-bis`); l'abrogato ha `breve_ammesso=True` e non conta come testo vuoto. → test in Task 3 e Task 6.
3. **La fonte risponde con una pagina di errore invece del formato atteso** (HTML al posto dell'XML, zero unità): l'adattatore solleva un errore chiaro; `estrai` con zero unità, o meno della metà di quelle già nel manifest, esce con codice 1 senza toccare i file. → test in Task 2 e Task 5.
4. **Riesecuzione con versione invariata**: nessuna riscrittura; con versione cambiata, riscritte solo le unità cambiate (sha256), rimossi i file delle unità sparite. → test in Task 5.
5. **Unità lunghe** (max osservato 10.503 caratteri, p95 1.604): divise sui blocchi separati da riga vuota oltre soglia, ogni parte con percorso e titolo in testa e `-p<k>` nel nome. → test in Task 5.

---

### Task 1: Nucleo — contratto, rete, registro, configurazione

**Files:**
- Create: `loader/fonte.py`
- Create: `loader/adattatori/__init__.py` (registro)
- Create: `loader/adattatori/_modello.py` (adattatore da copiare: ogni metodo con docstring che dice cosa restituire e `raise NotImplementedError`)
- Create: `loader/fonti/pipeline.yaml` (`sorgenti: []` e commento con lo schema della voce)
- Create: `loader/test_fonte.py`, `loader/test_contratto.py`

**Interfaces:**
- Produces (in `fonte.py`):
  - `@dataclass class Unita: id: str; titolo: str; percorso: list[str]; testo: str; metadati: dict[str, str] = {}; link_ufficiali: dict[str, str] = {}; breve_ammesso: bool = False` — `testo` ha i blocchi naturali separati da `"\n\n"`.
  - `ID_RE = r"^[a-z0-9][a-z0-9-]*$"`
  - `RESIDUI_COMUNI = [r"Vai al contenuto", r"Seleziona tutti", r"Il tuo browser non supporta", r"Accetta (tutti i )?cookie"]`
  - `@dataclass class Risposta: url: str; status: int; content_type: str; content: bytes` con proprietà `text`.
  - `class Rete: __init__(self, cache_dir: Path | None, ua: str = downloader.CURATED_UA, pausa: float = 0.5)`; `get(self, url: str, fresca: bool = False) -> Risposta` (cache su disco in `cache_dir/<sha256(url)>.bin` + `.json` con status e content type; `fresca=True` ignora la cache e la aggiorna); contatori `richieste_rete: int`, `byte_rete: int`.
  - `class ReteFixture(Rete): __init__(self, fixture_dir: Path)` legge `fixture_dir/contratto.json` → `{"config": {...}, "rete": {url: file}, "campione": {formato: [id]}}`; `get` di un URL assente solleva `ReteMancante(url)`.
  - `class FonteError(Exception)`.
  - `class Adattatore(Protocol)`: attributi `nome: str`, `formati: tuple[str, ...]` (il primo è il predefinito), `metadati_obbligatori: tuple[str, ...]`, `residui: tuple[str, ...]` (regex in aggiunta a `RESIDUI_COMUNI`), `candidati_link: tuple[str, ...]` (chiavi di `Unita.link_ufficiali`); metodi `riconosce(url: str) -> bool`, `versione(rete, cfg) -> str`, `attesi(rete, cfg) -> list[str]`, `unita(rete, cfg, formato: str, solo: set[str] | None = None) -> list[Unita]`, `casi_limite(rete, cfg) -> list[str]`.
  - `PIPELINE_YAML = Path(__file__).parent / "fonti" / "pipeline.yaml"`; `CAMPI_CONFIG = ("id", "gruppo", "nome", "adattatore", "url", "formato", "link", "soglia_caratteri", "ente", "tipo_documento")`; `config(id: str, path: Path = PIPELINE_YAML) -> dict` (solleva `FonteError` se manca un campo di `CAMPI_CONFIG`); `configs(path=PIPELINE_YAML) -> list[dict]`; `cartella(archivio: Path, cfg: dict) -> Path` → `archivio/<gruppo>/<id>`.
  - `cfg["link"]`: `"locale"`, `"<candidato>"` oppure `"entrambi:<candidato>"`.
- Produces (in `adattatori/__init__.py`): `REGISTRO: dict[str, Adattatore]`, `adattatore(nome: str) -> Adattatore`, `trova(url: str) -> Adattatore | None`.

- [ ] **Step 1: Write the failing tests** (`test_fonte.py`)

```python
def test_config_campo_mancante(self):
    scrivi_yaml(tmp, [{"id": "x", "gruppo": "g"}])
    with self.assertRaises(f.FonteError): f.config("x", tmp / "p.yaml")

def test_cartella(self):
    self.assertEqual(f.cartella(Path("/a"), {"gruppo": "codici", "id": "codice-civile"}), Path("/a/codici/codice-civile"))

def test_rete_usa_la_cache(self):
    with mock.patch("fonte.requests.Session.get", return_value=resp(b"x")) as get:
        r = f.Rete(tmp); r.get("http://u"); r.get("http://u")
        self.assertEqual((get.call_count, r.richieste_rete), (1, 1))
        r.get("http://u", fresca=True); self.assertEqual(get.call_count, 2)

def test_rete_fixture_url_assente(self):
    with self.assertRaises(f.ReteMancante): f.ReteFixture(FIX_FINTA).get("http://non-previsto")

def test_trova(self):
    self.assertIsNone(adattatori.trova("https://www.example.org/x"))
```

- [ ] **Step 2: Write `test_contratto.py`**: per ogni `nome` in `REGISTRO` con `fixtures/<nome>/contratto.json` (nessuna cartella = `self.fail`, ogni adattatore deve averla), un `subTest` per controllo: `riconosce(config["url"])`; `versione` non vuota; `attesi` non vuota e senza duplicati; `casi_limite ⊆ attesi`; per ogni formato in `campione`: `unita(..., solo=set(ids))` restituisce esattamente quegli id, ognuno con `ID_RE`, `titolo` e `testo` non vuoti, tutti i `metadati_obbligatori`, `set(link_ufficiali) == set(candidati_link)`, nessun residuo (`RESIDUI_COMUNI` + `residui`) nel testo. Con `REGISTRO` vuoto passa.
- [ ] **Step 3:** `cd loader && python3 -m unittest test_fonte test_contratto -v` → FAIL (moduli assenti)
- [ ] **Step 4:** implementare `fonte.py`, `adattatori/__init__.py`, `_modello.py`, `fonti/pipeline.yaml`
- [ ] **Step 5:** stesso comando → PASS; `python3 -m unittest test_downloader` → PASS (niente di rotto)
- [ ] **Step 6:** `git add loader/fonte.py loader/adattatori loader/fonti/pipeline.yaml loader/test_fonte.py loader/test_contratto.py && git commit -m "pipeline: contratto dell'unità, rete con cache e registro degli adattatori"`

### Task 2: Adattatore Normattiva — client e indice

**Files:**
- Create: `loader/adattatori/normattiva.py`
- Modify: `loader/test_normattiva.py` (già scritto, non in git: solo l'import diventa `from adattatori import normattiva as n`)
- Fixture già presenti: `loader/fixtures/normattiva/akn_campione.xml`, `indice_campione.html`
- Create: `loader/fixtures/normattiva/articolo_1399.html` (pagina `caricaArticolo` dell'art. 1399, scaricata una volta con `UA`)

**Interfaces:**
- Consumes: `fonte.Rete`, `fonte.FonteError`
- Produces (tutto già fissato da `test_normattiva.py`, che è la specifica):
  - `class NormattivaError(FonteError)`
  - `@dataclass(frozen=True) class ActRef: data_gu: str; codice_redaz: str; urn: str | None = None`
  - `act_ref_from_url(url) -> ActRef` (ELI con o senza `/CONSOLIDATED/<data>`, `caricaDettaglioAtto`; altro → `NormattivaError`)
  - `akn_url(ref, data_vigenza) -> str`, `pagina_atto_url(ref) -> str`
  - `fetch_akn(rete_o_session, ref, data_vigenza) -> bytes` — `NormattivaError` se non è XML con radice `akomaNtoso`
  - `normalizza_num(s) -> str` (`"Art. 42 bis."` → `"42-bis"`)
  - `@dataclass class Voce: num: str; percorso: list[str]; url_articolo: str`; `parse_indice(html) -> list[Voce]` in ordine di pagina (le voci senza "art." in testa sono gli articoli del R.D. ed escluse); `allinea(nums: list[str], voci: list[Voce]) -> list[Voce]` (stessa lunghezza e ordine di `nums`, gestisce i numeri ripetuti scorrendo in avanti)
  - `UA = downloader.CURATED_UA`
- `fetch_akn` accetta sia `fonte.Rete` sia un oggetto con `.get(url)` che restituisce `status_code`, `headers`, `content` (il mock del test): normalizzare in un helper.

- [ ] **Step 1:** aggiornare l'import in `test_normattiva.py`; `cd loader && python3 -m unittest test_normattiva -v` → FAIL (modulo assente)
- [ ] **Step 2:** implementare le funzioni sopra (BeautifulSoup per l'indice: studiare il markup nella fixture; `defusedxml.ElementTree.fromstring` per il controllo della radice)
- [ ] **Step 3:** → PASS
- [ ] **Step 4:** `git add loader/adattatori/normattiva.py loader/test_normattiva.py loader/fixtures/normattiva && git commit -m "adattatore normattiva: client AKN e indice dell'atto"`

### Task 3: Adattatore Normattiva — AKN, formati, contratto

**Files:**
- Create: `loader/adattatori/akn.py`
- Modify: `loader/adattatori/normattiva.py` (classe `Normattiva`), `loader/adattatori/__init__.py` (registrarla)
- Create: `loader/fixtures/normattiva/contratto.json`
- Test: `loader/test_akn.py`

**Interfaces:**
- Consumes: Task 1 (`Unita`, `Adattatore`, `Rete`), Task 2
- Produces:
  - `akn.py`: `@dataclass class ArticoloAkn: parte: str; num: str; rubrica: str; testo: str; eid: str; abrogato: bool; marcatori_rimossi: int`; `@dataclass class AttoAkn: frbr_uri: str; urn_nir: str; eli: str; data_consolidamento: str; articoli: list[ArticoloAkn]`; `parse_akn(xml: bytes) -> AttoAkn`; `slug_parte(parte) -> str` (`"CODICE CIVILE"` → `"cc"`, `"Disposizioni sulla legge in generale"` → `"preleggi"`, altrimenti slug ASCII).
  - `Normattiva` (adattatore): `nome="normattiva"`; `formati=("akn-indice", "akn", "html-articolo")` (testo AKN + percorso dall'indice; solo AKN; pagina `caricaArticolo` per articolo); `metadati_obbligatori=("urn", "parte", "articolo", "data_vigenza")`; `residui=(r"\(\(\d+\)\)",)`; `candidati_link=("urn", "articolo")` (`uri-res/N2Ls?<urn>~art<num senza trattino>` e `Voce.url_articolo`).
  - Unità: `id = f"{slug_parte(parte)}-art-{num}"`, `titolo = f"Art. {num} – {rubrica}"` (senza rubrica: `f"Art. {num}"`), `percorso = [parte] + voce.percorso` se la voce esiste, `metadati` con `urn, eli, parte, articolo, rubrica, abrogato ("si"/"no"), data_vigenza`, `breve_ammesso = abrogato`, commi separati da `"\n\n"`.
  - `cfg` propri: `vigenza` (`AAAAMMGG`, predefinito oggi). `versione` = `data_consolidamento` (dall'alias ELI `CONSOLIDATED/<data>`, letto con `rete.get(..., fresca=True)`). `attesi` = id dagli allegati AKN. `casi_limite` = un abrogato, un `-bis`, il più lungo, i due art. 1.
  - `contratto.json`: config con `url` ELI del Codice civile e `vigenza: "20261111"`, `rete` con gli URL AKN, pagina atto e `caricaArticolo` dell'art. 1399 → file delle fixture, `campione` con `akn-indice` e `akn`: `["cc-art-1", "preleggi-art-1", "cc-art-42-bis", "cc-art-1399", "cc-art-1401", <id dell'abrogato in fixture>]`, `html-articolo`: `["cc-art-1399"]`.

- [ ] **Step 1: Write the failing tests** (`test_akn.py`, sulla fixture AKN)

```python
def test_meta(self):
    a = akn.parse_akn(FIX)
    self.assertEqual(a.urn_nir, "urn:nir:stato:regio.decreto:1942-03-16;262")
    self.assertRegex(a.data_consolidamento, r"^\d{4}-\d{2}-\d{2}$")   # dall'ELI, non dal FRBRdate 1944-11-10

def test_art1_doppio_id_distinti(self):                 # Review Focus 1
    ids = [u.id for u in NORMATTIVA.unita(RETE, CFG, "akn")]
    self.assertIn("cc-art-1", ids); self.assertIn("preleggi-art-1", ids)
    self.assertEqual(len(ids), len(set(ids)))

def test_rubrica_e_testo_1399(self):
    a = next(x for x in akn.parse_akn(FIX).articoli if x.num == "1399")
    self.assertEqual(a.rubrica, "Ratifica")
    self.assertTrue(a.testo.startswith("Nell'ipotesi prevista dall'articolo precedente"))
    self.assertNotRegex(a.testo, r"\(\(\d+\)\)")

def test_bis_e_abrogato(self):                          # Review Focus 2
    u = {x.id: x for x in NORMATTIVA.unita(RETE, CFG, "akn")}
    self.assertIn("cc-art-42-bis", u)
    ab = next(x for x in u.values() if x.metadati["abrogato"] == "si")
    self.assertTrue(ab.breve_ammesso); self.assertGreater(len(ab.testo), 0)

def test_percorso_dall_indice(self):
    u = {x.id: x for x in NORMATTIVA.unita(RETE, CFG, "akn-indice", solo={"cc-art-1401"})}
    self.assertTrue(u["cc-art-1401"].percorso[-1].startswith("CAPO VII"))
```

- [ ] **Step 2:** `python3 -m unittest test_akn test_contratto -v` → FAIL
- [ ] **Step 3:** implementare `parse_akn` (un `attachment/doc` = un articolo; `parte` e `num` dal `@name` `"<PARTE>-art. <num>"`; rubrica = testo tra parentesi dopo "Art. N." in testa; `abrogato` se entro 300 caratteri dopo la rubrica il testo è dominato da "abrogat"/"((ARTICOLO ABROGATO"), poi `Normattiva` e la registrazione
- [ ] **Step 4:** → PASS (anche `test_normattiva`)
- [ ] **Step 5:** `git commit -m "adattatore normattiva: articoli dall'AKN, formati e test di contratto"`

### Task 4: Misura dei formati (fase 3)

**Files:**
- Create: `loader/misura_formati.py`
- Test: `loader/test_misura_formati.py`

**Interfaces:**
- Consumes: Task 1, `adattatori.adattatore`
- Produces:
  - `campione(ids: list[str], n: int = 30, seed: str = "nuova-fonte", obbligatori: list[str] = ()) -> list[str]` — deterministico (ordinamento per `sha256(seed + id)`), include sempre gli `obbligatori`, lunghezza `min(n, len(ids))`.
  - `@dataclass class Misura: formato: str; attese: int; trovate: int; vuote: int; con_titolo: int; con_percorso: int; con_metadati: int; residui: int; lunghezza_p95: int; lunghezza_max: int; richieste: int; byte: int; stima_richieste_totali: int; stima_byte_totali: int; versione: str | None`
  - `misura(formato: str, unita: list[Unita], attese: list[str], obbligatori: tuple[str, ...], residui: list[str], min_caratteri: int = 40) -> Misura` (una unità con `breve_ammesso` non è "vuota" se ha testo; le stime totali le calcola la CLI scalando i contatori della rete per `len(attesi) / len(campione)`)
  - CLI: `python3 misura_formati.py --target <archivio> --id <id>`: rete con cache in `<cartella>/raw/`, per ogni formato dell'adattatore misura il campione, scrive `<cartella>/misure.json`, stampa una tabella formato × misure (≤ 15 righe). Un formato che solleva `FonteError` compare con l'errore in una riga, non interrompe gli altri.

- [ ] **Step 1: Write the failing tests**

```python
def test_campione_deterministico(self):
    ids = [f"u-{i}" for i in range(500)] + ["u-42-bis"]
    a = m.campione(ids, obbligatori=["u-42-bis"])
    self.assertEqual(a, m.campione(ids, obbligatori=["u-42-bis"]))
    self.assertIn("u-42-bis", a); self.assertEqual(len(a), 30)

def test_misura_conta_residui_vuoti_titoli(self):
    us = [Unita("a", "T", [], "Testo lungo abbastanza ((3)) " * 3), Unita("b", "", [], "Vai al contenuto")]
    r = m.misura("x", us, attese=["a", "b", "c"], obbligatori=(), residui=fonte.RESIDUI_COMUNI + [r"\(\(\d+\)\)"])
    self.assertEqual((r.trovate, r.residui, r.con_titolo, r.vuote), (2, 2, 1, 1))

def test_breve_ammesso_non_vuoto(self):
    r = m.misura("x", [Unita("a", "T", [], "Abrogato", breve_ammesso=True)], ["a"], (), [])
    self.assertEqual(r.vuote, 0)
```

- [ ] **Step 2:** → FAIL · **Step 3:** implementare · **Step 4:** → PASS
- [ ] **Step 5:** `git commit -m "misura_formati: confronto misurato dei formati di una fonte"`

### Task 5: Estrazione generica, un file per unità

**Files:**
- Create: `loader/estrai.py`
- Modify: `loader/carica_docsgpt.py` (nomi e gruppi anche da `fonti/pipeline.yaml`: `SOURCE_NAMES[f"{gruppo}/{id}"] = nome`, `GROUPS[gruppo]` = le sue cartelle; stesso schema di `locazioni_names`)
- Test: `loader/test_estrai.py`

**Interfaces:**
- Consumes: Task 1; `carica_docsgpt.ente_documents`
- Produces:
  - `@dataclass class Rapporto: scritte: int; invariate: int; rimosse: int; divise: int`
  - `dividi(testo: str, soglia: int) -> list[str]` — accumula i blocchi separati da `"\n\n"` fino alla soglia; un blocco oltre soglia si taglia all'ultimo `". "` prima della soglia (in mancanza, alla soglia).
  - `scrivi_unita(unita: list[Unita], cfg: dict, out_dir: Path, versione: str, minimo_precedente: float = 0.5) -> Rapporto` — `FonteError` con gli id se ci sono id duplicati; `FonteError` senza toccare nulla se `unita` è vuota o ha meno di `minimo_precedente` × le unità del manifest esistente.
  - File `<out_dir>/files/<id>[-p<k>].html`: HTML minimale, `<title>` = `" › ".join([cfg["nome"], *percorso, titolo])`, in testa `<p class="percorso">` e `<h1>` (titolo, con " (parte k di n)" se divisa), poi, se `link` non è vuoto, `<p class="fonte">Testo ufficiale: <a href="{link}">{link}</a></p>` (è l'unico modo in cui il link arriva a Claude: la ricerca restituisce il testo, non i campi del manifest), poi un `<p>` per blocco. Si riscrive solo se lo sha256 del contenuto cambia; i file di unità non più presenti si cancellano.
  - `<out_dir>/manifest.jsonl`, una riga per file: campi esistenti `source=f"{gruppo}/{id}", title, document_date=metadati.get("data", ""), authority=cfg["ente"], document_type=cfg["tipo_documento"], number=metadati.get("numero", ""), detail_url, download_url=cfg["url"], local_file, sha256, downloaded_at` più `unit_id, parte ("k/n" o ""), percorso, titolo, breve_ammesso, link, versione` e i `metadati` **piatti**, come i campi extra delle locazioni (`livello`, `ambito`); un metadato con il nome di un campo già presente → `FonteError`. `link`: `"locale"` → `""`; `"<c>"` o `"entrambi:<c>"` → `link_ufficiali[c]`. `detail_url` deve essere **unico per riga** (è la chiave del notifier, `notifier.manifest_rows`): il link scelto o, se locale, il primo di `link_ufficiali` o, se non ce ne sono, `f"{cfg['url']}#{unit_id}"`; per le parti si aggiunge `#p<k>` (o `-p<k>` dopo un `#` già presente).
  - `<out_dir>/stato.json`: `{"versione": ..., "unita": n, "aggiornato": iso}`.
  - CLI: `python3 estrai.py --target <archivio> (--id <id> | --gruppo <g>) [--forza]`: `versione` con `rete.get(..., fresca=True)`; se uguale a `stato.json` e niente `--forza` stampa `"<id>: invariato"` ed esce 0; altrimenti `unita(rete, cfg, cfg["formato"])` + `scrivi_unita`; stampa il `Rapporto` in una riga; `FonteError` → messaggio + exit 1.

- [ ] **Step 1: Write the failing tests** (unità costruite a mano, nessun adattatore)

```python
def test_id_duplicati_fermano(self):                   # Review Focus 1
    with self.assertRaises(FonteError) as c:
        e.scrivi_unita([U("a"), U("a")], CFG, tmp, "v1")
    self.assertIn("a", str(c.exception)); self.assertFalse((tmp / "manifest.jsonl").exists())

def test_titolo_con_percorso(self):
    e.scrivi_unita([U("cc-art-1401", titolo="Art. 1401 – Nomina", percorso=["LIBRO QUARTO", "CAPO VII X"])], CFG, tmp, "v1")
    row = manifest(tmp)[0]
    self.assertIn("CAPO VII", row["title"]); self.assertEqual(row["unit_id"], "cc-art-1401")

def test_divisione_unita_lunga(self):                   # Review Focus 5
    testo = "\n\n".join(["Comma " + "x" * 1500 + "."] * 6)
    r = e.scrivi_unita([U("cc-art-9999", titolo="Art. 9999", testo=testo)], CFG, tmp, "v1")
    parti = sorted((tmp / "files").glob("cc-art-9999-p*.html"))
    self.assertGreaterEqual(len(parti), 3); self.assertEqual(r.divise, 1)
    self.assertTrue(all("Art. 9999" in p.read_text() for p in parti))

def test_rieseguo_invariato(self):                      # Review Focus 4
    e.scrivi_unita(TRE, CFG, tmp, "v1")
    self.assertEqual(e.scrivi_unita(TRE, CFG, tmp, "v1"), e.Rapporto(0, 3, 0, 0))

def test_rieseguo_una_cambiata_una_sparita(self):
    e.scrivi_unita(TRE, CFG, tmp, "v1")
    r = e.scrivi_unita([cambia(TRE[0]), TRE[1]], CFG, tmp, "v2")
    self.assertEqual((r.scritte, r.invariate, r.rimosse), (1, 1, 1))

def test_risposta_vuota_non_cancella(self):            # Review Focus 3
    e.scrivi_unita(TRE, CFG, tmp, "v1")
    with self.assertRaises(FonteError): e.scrivi_unita([], CFG, tmp, "v2")
    self.assertEqual(len(manifest(tmp)), 3)

def test_link_scelto_nel_manifest_e_nel_testo(self):
    e.scrivi_unita([U("a", link_ufficiali={"urn": "http://u"})], {**CFG, "link": "entrambi:urn"}, tmp, "v1")
    self.assertEqual(manifest(tmp)[0]["link"], "http://u")
    self.assertIn('href="http://u"', (tmp / "files" / "a.html").read_text())

def test_detail_url_unico_anche_locale(self):          # chiave del notifier
    e.scrivi_unita(TRE + [U("lunga", testo=LUNGO)], CFG, tmp, "v1")
    urls = [r["detail_url"] for r in manifest(tmp)]
    self.assertEqual(len(urls), len(set(urls)))

def test_metadati_piatti(self):
    e.scrivi_unita([U("a", metadati={"rubrica": "R"})], CFG, tmp, "v1")
    self.assertEqual(manifest(tmp)[0]["rubrica"], "R")
    with self.assertRaises(FonteError): e.scrivi_unita([U("b", metadati={"title": "x"})], CFG, tmp, "v2")

def test_carica_docsgpt_vede_i_file(self):
    e.scrivi_unita(TRE, CFG, tmp / "codici" / "x", "v1")
    docs = carica_docsgpt.ente_documents(tmp / "codici" / "x")
    self.assertEqual(sorted(a for _, a in docs), sorted(f"{u.id}.html" for u in TRE))
```

- [ ] **Step 2:** → FAIL · **Step 3:** implementare `estrai.py` e la modifica a `carica_docsgpt.py` · **Step 4:** → PASS, e `python3 -m unittest test_downloader` → PASS
- [ ] **Step 5:** `git commit -m "estrai: un file per unità con percorso, divisione e aggiornamento incrementale"`

### Task 6: Valutazione dei link (fase 4) e test di qualità (fase 5)

**Files:**
- Create: `loader/valuta_link.py`, `loader/qualita_estrazione.py`
- Test: `loader/test_qualita.py`

**Interfaces:**
- Consumes: manifest e file del Task 5, Task 1, `misura_formati.campione`
- Produces:
  - `precisione(pagina: str, unita: Unita, vicine: list[Unita]) -> str` — `"unita"` se titolo e prime 8 parole del testo sono nella pagina e nessuna delle `vicine` (prime 8 parole) lo è; `"atto"` se ci sono anche le vicine; `"assente"` altrimenti. Confronto sul testo della pagina senza tag e con spazi normalizzati.
  - `valuta_link(ad: Adattatore, unita: list[Unita], campione: list[str], rete: Rete) -> dict[str, dict]` — per ogni candidato in `ad.candidati_link`: `{"status_200": %, "unita": %, "atto": %, "assente": %, "byte_medi": int}`; `"locale"` ha sempre precisione `"unita"`. Vicine = unità precedente e successiva nell'ordine di `unita`. CLI `python3 valuta_link.py --target --id`: scrive `<cartella>/link.json`, stampa la tabella candidato × misure.
  - `@dataclass class Esito: nome: str; valore: float; soglia: float; ok: bool; esempi: list[str]` (al massimo 3)
  - `controlla(out_dir: Path, attesi: list[str], riferimento: dict[str, str], obbligatori: tuple[str, ...], residui: list[str], min_caratteri: int = 40) -> list[Esito]` con i nomi `file_e_manifest` (riusa `quadratura.check_local`: zero righe senza file e zero file fuori manifest), `conteggio_unita`, `testo_vuoto_o_navigazione`, `residui`, `titolo`, `metadati_obbligatori`, `confronto_testo` e le soglie dei Global Constraints. Legge manifest e file; ricompone le parti di un'unità divisa; le righe con `breve_ammesso` vero sono esenti dal minimo di caratteri; "navigazione" = testo fatto per oltre metà di `RESIDUI_COMUNI` o sotto il minimo.
  - CLI `python3 qualita_estrazione.py --target --id`: `attesi` dall'adattatore (rete in cache); `riferimento` = testi del campione con un formato diverso da `cfg["formato"]` se l'adattatore ne ha, altrimenti con lo stesso; se `cfg["link"] != "locale"` aggiunge l'esito `link` (status 200 e precisione `"unita"` o `"atto"` sul campione di 5). Una riga per esito, exit 1 se uno fallisce.

- [ ] **Step 1: Write the failing tests**

```python
def test_tutto_ok(self):
    self.assertTrue(all(x.ok for x in q.controlla(DIR_BUONA, ATTESI, RIF, OBBL, RES)))

def test_pagina_di_navigazione(self):          # caso issue #3
    sovrascrivi(DIR_BUONA, "u-5", "Vai al contenuto Vai alla navigazione Home / Turismo")
    self.assertFalse(esito(q.controlla(DIR_BUONA, ATTESI, RIF, OBBL, RES), "testo_vuoto_o_navigazione").ok)

def test_breve_ammesso_non_e_vuoto(self):      # Review Focus 2
    self.assertTrue(esito(q.controlla(DIR_CON_BREVE, ATTESI, RIF, OBBL, RES), "testo_vuoto_o_navigazione").ok)

def test_mancante(self):
    self.assertFalse(esito(q.controlla(DIR_BUONA, ATTESI + ["u-9999"], RIF, OBBL, RES), "conteggio_unita").ok)

def test_unita_divisa_ricomposta(self):
    self.assertTrue(esito(q.controlla(DIR_CON_DIVISA, ATTESI_D, RIF_D, OBBL, RES), "confronto_testo").ok)

def test_precisione_atto(self):
    pagina = " ".join(u.titolo + " " + u.testo for u in TRE_VICINE) * 5
    self.assertEqual(v.precisione(pagina, TRE_VICINE[1], [TRE_VICINE[0], TRE_VICINE[2]]), "atto")

def test_precisione_unita(self):
    self.assertEqual(v.precisione(TRE_VICINE[1].titolo + " " + TRE_VICINE[1].testo, TRE_VICINE[1], [TRE_VICINE[0]]), "unita")
```

(`DIR_*` costruite nel `setUp` con `estrai.scrivi_unita`.)

- [ ] **Step 2:** → FAIL · **Step 3:** implementare · **Step 4:** → PASS
- [ ] **Step 5:** `git commit -m "qualita: valutazione dei link e test di qualità dell'estrazione"`

### Task 7: Caricamento in prova, benchmark della fonte, promozione, notifier

**Files:**
- Create: `loader/prova_fonte.py`
- Modify: `eval/bench.py` (keywords da `q.get("keywords")`, in mancanza da `KW`; env `SOLO_PREFISSO`: solo le domande il cui `id` inizia così)
- Modify: `notifier/notifier.py` (i gruppi di `fonti/pipeline.yaml`, letto da `Config.loader_dir / "fonti" / "pipeline.yaml"`, si aggiungono a `ENTI` e si aggiornano con `estrai.py --target <archivio> --gruppo <g>` invece di `downloader.py`)
- Modify: `docker-compose-standalone.yaml` (il default di `NOTIFIER_ARCHIVE_SOURCES` è una lista fissa: aggiungere i gruppi quando la prima fonte viene promossa; nella skill, fase 7)
- Test: `loader/test_prova_fonte.py`

**Interfaces:**
- Consumes: `carica_docsgpt.upload_zip, wait_task, build_zip, ente_documents, attach_to_agent, register_links, delete_source, load_ente`; `fonte.config, cartella`
- Produces:
  - `crea_agente_prova(api: str, id: str, source_id: str) -> tuple[str, str]` → `(agent_id, key)` con `POST /api/create_agent` e corpo `name=f"prova-{id}", description, sources=[source_id], chunks=10, retriever="classic", prompt_id="", status="published", agent_type="classic"` (verificare i campi richiesti in `docsgpt/api/user/agents/routes.py` dell'immagine 0.21.0, nel container `docsgpt-backend-1`).
  - `prova(api, archivio, id) -> tuple[str, str]` → `(source_id, agent_id)`: carica `cartella(archivio, cfg)` come sorgente `f"prova - {cfg['nome']}"` e crea l'agente.
  - `esito_benchmark(righe: list[dict], soglia_top: int = 3, soglia_quota: float = 0.8) -> tuple[bool, float]` — solo modalità `B`, primo rank.
  - `promuovi(api, archivio, id)`: `load_ente` della sorgente definitiva collegata a `"Claude - archivio fiscale"`, con registrazione in `doc_links.json`; poi `pulisci`.
  - `pulisci(api, id)`: cancella sorgente e agente di prova (`/api/delete_agent?id=`, `delete_source`); idempotente.
  - CLI `python3 prova_fonte.py {prova|bench|promuovi|pulisci} --id <id> [--target <archivio>]`; `bench` lancia `eval/bench.py` nel container con la chiave dell'agente di prova e `SOLO_PREFISSO=f"{id}-"`, salva `eval/risultati-<id>-<AAAA-MM-GG>.jsonl`, stampa quota e PASS/FAIL.

- [ ] **Step 1: Write the failing tests**

```python
def test_esito_soglia(self):
    righe = [{"mode": "B", "ranks": [r]} for r in (1, 2, 3, 3, None)]
    self.assertEqual(p.esito_benchmark(righe), (True, 0.8))
    righe[0]["ranks"] = [4]
    self.assertFalse(p.esito_benchmark(righe)[0])

def test_esito_ignora_modalita_a(self):
    self.assertEqual(p.esito_benchmark([{"mode": "A", "ranks": [None]}, {"mode": "B", "ranks": [1]}]), (True, 1.0))

def test_create_agent_payload(self):
    with mock.patch("prova_fonte.requests.post") as post:
        post.return_value.json.return_value = {"id": "a1", "key": "k"}
        p.crea_agente_prova("http://api", "codice-civile", "s1")
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["name"], body["sources"], body["retriever"]), ("prova-codice-civile", ["s1"], "classic"))
```

- [ ] **Step 2:** → FAIL · **Step 3:** implementare `prova_fonte.py`, le modifiche a `bench.py` e al notifier · **Step 4:** → PASS
- [ ] **Step 5:** verifica non distruttiva: una voce temporanea in `fonti/pipeline.yaml` con 20 unità già estratte, `python3 prova_fonte.py prova --id <id>`, poi `pulisci`; `curl -s :7091/api/get_agents` non mostra `prova-*`; la voce temporanea si toglie.
- [ ] **Step 6:** `python3 -c "import ast; ast.parse(open('notifier/notifier.py').read())"` e `docker compose config -q` → nessun errore
- [ ] **Step 7:** `git commit -m "prova_fonte: caricamento in prova, benchmark della fonte, promozione e notifier"`

### Task 8: Skill `/nuova-fonte`, sottoagenti, modelli

**Files:**
- Create: `.claude/skills/nuova-fonte/SKILL.md`
- Create: `.claude/agents/fonte-analista.md` (`model: sonnet`; fasi 1–4 e 6: verifica, adattatore, decisioni su formato e link, domande di prova)
- Create: `.claude/agents/fonte-esecutore.md` (`model: haiku`; lancia gli script, riassume le uscite; fasi 3–5 e 6–7 per la parte di esecuzione)
- Create: `docs/ingestion/_modello.md` (rapporto: scheda fonte; adattatore riusato o scritto e test; tabella formati + scelta; lunghezze e soglia di divisione; tabella link + scelta; esiti qualità; benchmark; decisioni prese in autonomia; esito)
- Modify: `loader/adattatori/_modello.py` se la skill ne cita parti mancanti

**Interfaces:**
- Consumes: le CLI dei Task 4–7, `test_contratto.py`, `adattatori/_modello.py`
- Produces: la skill con, per ogni fase della spec: input, comando, criterio di uscita, regola di decisione, valore predefinito, cosa scrivere nel rapporto. In particolare:
  - fase 1: `python3 -c "import adattatori; print(adattatori.trova('<url>'))"` decide riuso o scrittura;
  - fase 2, scrittura: salvare in `loader/fixtures/<nome>/` le risposte per 5–10 unità inclusi i casi difficili visti, copiare `_modello.py` in `adattatori/<nome>.py`, scrivere `contratto.json` e `test_<nome>.py`, registrare; esce con `python3 -m unittest test_contratto test_<nome>` PASS;
  - regola di scelta del formato (completezza 100% → residui 0 → titolo → percorso → costo; combinazioni solo se dichiarate dall'adattatore come formato);
  - regola di scelta del link (precisione "unita" > corrispondenza di versione > disponibilità > costo; a parità `entrambi:<candidato>`);
  - escalation a Opus solo se due criteri di primo livello indicano formati diversi;
  - i due soli punti di domanda all'utente; "un tentativo poi stop";
  - le regole di uscita compatta (leggere i JSON solo per i casi falliti).

- [ ] **Step 1:** scrivere i quattro file
- [ ] **Step 2:** verifica: `python3 -c "import yaml,sys; [yaml.safe_load(open(f).read().split('---')[1]) for f in sys.argv[1:]]" .claude/skills/nuova-fonte/SKILL.md .claude/agents/fonte-*.md` → nessun errore; `grep -oE 'python3 [a-z_]+\.py' .claude/skills/nuova-fonte/SKILL.md | sort -u` → ogni script esiste in `loader/` (o `eval/`); `grep -nE 'articol|rubric|abrogat|Normattiva' .claude/skills/nuova-fonte/SKILL.md` compare solo negli esempi dichiarati come tali
- [ ] **Step 3:** `git commit -m "skill nuova-fonte: fasi, regole di decisione, sottoagenti e modelli"`

## Fuori da questo piano: collaudi in sessioni nuove

I collaudi si fanno in sessioni nuove di Claude Code, senza il contesto di
questa: sono la prova che la skill funziona da sola.

**Collaudo 1, Codice civile:**
`/nuova-fonte importa il Codice civile da https://www.normattiva.it/eli/id/1942/04/04/042U0262`
- fase 1 trova l'adattatore `normattiva` (riuso, nessun codice nuovo);
- domande all'utente al massimo all'avvio e all'approvazione;
- `qualita_estrazione.py` tutto OK (3.281 unità attese = 3.250 c.c. + 31 preleggi, salvo variazioni della data di vigenza riportate nel rapporto);
- `prova_fonte.py bench` PASS su 8 domande `codice-civile-*` (2 riferimenti esatti incluso art. 1 preleggi contro art. 1 c.c., 2 parafrasi, 1 numeri vicini 1398/1399, 1 rubrica senza numero, 1 articolo `-bis`, 1 abrogato);
- `docs/ingestion/codice-civile.md` compilato, con formato, soglia e link motivati dalle misure;
- dopo la promozione, `eval/bench.py` completo: nessuna domanda esistente peggiora oltre 2 posizioni rispetto a `eval/baseline-2026-11-11.jsonl`.

**Collaudo 2, genericità:** una fonte non normativa scelta dall'utente (es.
risposte a interpello o FAQ di un ente). Riesce se la pipeline scrive un
adattatore che passa `test_contratto.py` e arriva all'approvazione **senza
modifiche al nucleo**; ogni modifica al nucleo che si rende necessaria è un
difetto del nucleo, da correggere e registrare nel rapporto.

La voce `codice-civile` in `loader/fonti/pipeline.yaml` la scrive il collaudo 1
con le scelte misurate; i valori di partenza della skill sono `formato` = primo
formato dell'adattatore, `link: locale`, `soglia_caratteri: 4000`.
