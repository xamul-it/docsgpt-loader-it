---
name: nuova-fonte
description: Porta una fonte documentale nuova (sito, banca dati, export XML/JSON, elenco di pagine) dall'URL alla sorgente DocsGPT collegata all'archivio consultato dai client MCP. Scrive il loader come adattatore standard, lo testa, misura formati e link, blocca il caricamento se l'estrazione è scadente. Usare per "importa/aggiungi/carica la fonte X da <url>".
argument-hint: "<richiesta in linguaggio naturale con URL della fonte>"
---

# /nuova-fonte

Procedura standard per aggiungere una fonte. Il lavoro sui documenti lo fanno
gli script in `loader/` (deterministici, uscite brevi); i modelli leggono
riepiloghi, scrivono l'adattatore e decidono con le regole qui sotto.

Spec: `docs/superpowers/specs/2026-11-11-pipeline-ingestion-design.md`.
Tutti i comandi si lanciano da `loader/`. `ARCH=/home/htpc/containers/docsgpt/circolari`.

## Regole fisse

- **Domande all'utente: solo due.** (a) All'avvio, se manca l'URL della fonte o
  non si capisce cosa importare: UN messaggio con tutte le domande. (b) In fase 7,
  per l'approvazione, con il rapporto. Tutto il resto lo decidi tu con le
  regole; ogni decisione presa da solo va nel rapporto ("Decisioni prese in
  autonomia"), così l'utente la può ribaltare.
- **Un nuovo tentativo per fase**, con la correzione indicata dal test. Se
  fallisce ancora: stop, rapporto con l'esito "fermata alla fase N", e nient'altro.
- **Mai caricare** (fase 6) se `qualita_estrazione.py` non è tutto OK.
- **Uscite compatte.** Leggi solo le righe stampate dagli script; apri i JSON
  (`ricognizione.json`, `misure.json`, `link.json`, `qualita.json`) solo per i
  casi falliti. Non leggere mai pagine o file interi della fonte: usa estratti
  (`head -c`, `grep -o '.\{200\}parola.\{200\}'`).
- **Niente regole di una fonte nel nucleo** (`fonte.py`, `estrai.py`,
  `misura_formati.py`, `valuta_link.py`, `qualita_estrazione.py`,
  `prova_fonte.py`). Se serve modificarlo, è un difetto del nucleo: fermati e
  scrivilo nel rapporto, non aggirarlo nell'adattatore.
- **Modelli.** Fasi 1–4 e domande di prova: sottoagente `fonte-analista`
  (Sonnet). Esecuzione di script e riepiloghi (fasi 5–6, 7 dopo il sì):
  `fonte-esecutore` (Haiku). Opus solo in fase 3 se due criteri di primo
  livello indicano formati diversi (vedi sotto). Passa ai sottoagenti solo:
  id, URL, percorso del rapporto, fase, decisioni già prese.

## Avvio

Ricava dalla richiesta: URL, argomento, un `id` breve (minuscole e trattini,
es. `codice-civile`), un `gruppo` (cartella di primo livello dell'archivio e
`--source` di carica_docsgpt: es. `codici`; non usare gruppi esistenti di
`downloader.py`: agenzia-entrate, ministero-finanze, agenzia-dogane-monopoli,
normativa, giurisprudenza, locazioni), il `nome` della sorgente su DocsGPT.
Copia `docs/ingestion/_modello.md` in `docs/ingestion/<id>.md`.

## Fase 1 — Ricognizione e adattatore

```
python3 esplora_fonte.py --url <url> --out $ARCH/<gruppo>/<id>
```
Se servono altre pagine (un esempio di unità, un export elencato nei formati),
rilancia con il loro URL e lo stesso `--out`. **Esce** con la scheda della fonte
nel rapporto: ente e se è ufficiale, formati, come si rileva l'aggiornamento,
accesso libero, avvisi. **Si ferma** se: la fonte non è ufficiale e non c'è
un'alternativa ufficiale; serve login; il contenuto c'è solo via JavaScript e
non esiste un export o un'API.

Riga `adattatore:` della ricognizione: un nome → **riuso** (salta la scrittura
in fase 2); `da scrivere` → fase 2 completa.

## Fase 2 — Adattatore

Riuso: aggiungi solo la voce in `fonti/pipeline.yaml` (sotto).

Scrittura (nome adattatore = sito o ente, es. `normattiva`, `agenzia-entrate-faq`):
1. **Unità**: decidi qual è il pezzo che la ricerca deve restituire (articolo,
   risposta, scheda, FAQ). Regola: la più piccola parte che ha senso da sola e
   che un utente citerebbe. Scrivi la scelta nel rapporto.
2. **Fixture** in `loader/fixtures/<nome>/`: le risposte reali (dalla cache
   `$ARCH/<gruppo>/<id>/raw/`) che servono a produrre 5–10 unità, comprese
   quelle difficili che hai visto (omonimie, numerazioni bis/ter, unità
   abrogate o vuote, la più lunga, pagina di errore). File grandi: ritagliali
   tenendo la struttura (totale ≤ 500 KB). Nella cache ogni risposta è
   `<sha256>.bin` con l'URL nel `<sha256>.json` accanto
   (`grep -l '<pezzo di url>' raw/*.json`). `contratto.json` come descritto in
   `test_contratto.py`.
3. **Codice**: copia `adattatori/_modello.py` in `adattatori/<nome>.py`, segui
   le regole del docstring, registra in `adattatori/__init__.py`. Ogni formato
   che la fonte offre (export intero, pagina per unità, …) è un valore di
   `formati`; una combinazione (testo da un formato, gerarchia da un altro) è
   un formato a sé.
4. **Test** `loader/test_<nome>.py`: almeno un test per ogni caso difficile
   visto, e uno che una risposta inattesa (HTML di errore) dia `FonteError`.
5. **Esce** con `python3 -m unittest test_contratto test_<nome>` → OK.

Voce in `loader/fonti/pipeline.yaml` (valori di partenza, li cambiano le fasi 3–4):
```yaml
  - id: <id>
    gruppo: <gruppo>
    nome: "<nome sorgente>"
    descrizione: "<una frase sul contenuto>"   # la legge chi sceglie le fonti (list_sources del server MCP)
    adattatore: <nome>
    url: "<url>"
    formato: <primo formato dell'adattatore>
    link: locale
    soglia_caratteri: 2400
    ente: "<ente>"
    tipo_documento: "<tipo>"
    provvisorio: true
```

## Fase 3 — Formati

```
python3 misura_formati.py --target $ARCH --id <id>
```
**Regola di scelta**, criteri in ordine (vince il primo che distingue):
1. completezza: `trov/att` pieno e `vuote` = 0;
2. residui = 0;
3. titolo presente;
4. percorso presente;
5. costo totale (richieste, MB).
Un formato con ERRORE è escluso. Se i criteri 1 e 2 indicano formati diversi
(uno completo ma sporco, l'altro pulito ma incompleto): chiedi a
`fonte-analista` con `model: opus` di proporre un formato combinato o una
correzione dell'adattatore, poi rimisura. Scrivi la tabella e la scelta nel
rapporto; aggiorna `formato` in `pipeline.yaml`.

## Fase 4 — Divisione e link

- **Soglia di divisione**: 2400 caratteri, fissa. I chunk di DocsGPT sono di 700
  token (~2500-2800 caratteri): un file più lungo verrebbe ri-spezzato e i pezzi
  dopo il primo perderebbero percorso e titolo. Scrivi p95 e max nel rapporto.
- **Link**:
  ```
  python3 valuta_link.py --target $ARCH --id <id>
  ```
  Regola: precisione `unità%` più alta > corrispondenza di versione (il sito
  ufficiale mostra sempre l'ultima, la copia locale quella caricata) >
  disponibilità (`200%`) > costo. Un candidato ufficiale con `unità%` = 100 e
  `200%` = 100 → `entrambi:<candidato>`; altrimenti `locale`. Aggiorna `link`.

## Fase 5 — Estrazione e qualità (fonte-esecutore)

```
python3 estrai.py --target $ARCH --id <id> --forza
python3 qualita_estrazione.py --target $ARCH --id <id>
```
Tutto OK → fase 6. Un FAIL (anche un `ERRORE` di `estrai.py`, che rifiuta
unità vuote, di navigazione o con residui) → `fonte-analista` corregge
l'adattatore (con un test che riproduce il caso, dai `esempi` della riga) e si
rilancia una volta. `riscontro_link` controlla sulla pagina ufficiale che il
testo stia dove l'adattatore dice, anche con `link: locale`. `prova_fonte.py`
si rifiuta di caricare senza `qualita.json` tutto OK sull'estrazione attuale.

## Fase 6 — Prova e benchmark

1. `fonte-analista` scrive 6–10 domande in `eval/domande.yaml`, id `<id>-01`…,
   tipi diversi: riferimento esatto, parafrasi, unità con identificativi vicini,
   titolo senza numero, i casi difficili della fonte. Formato:
   ```yaml
   - id: <id>-01
     categoria: riferimento-esatto
     domanda: "…come la scriverebbe un utente…"
     attesi: [<gruppo>/<id>/files/<unit_id>.html]
     keywords: '"parola esatta" altra'
     fatti: ["fatto verificato sul testo"]
     trappola: "…"
   ```
   (unità divisa: `<unit_id>-p1.html`).
2. `fonte-esecutore`:
   ```
   python3 prova_fonte.py prova --id <id>
   python3 prova_fonte.py bench --id <id>
   ```
   PASS = unità attesa entro i primi 3 in ≥ 80% delle domande con keywords.
   FAIL → un tentativo (domanda mal scritta? titolo o percorso poco
   descrittivi nell'adattatore?), poi stop.

## Fase 7 — Approvazione

Un solo messaggio all'utente: link al rapporto e 5 righe (unità e quante,
formato e perché, link e perché, qualità, benchmark). Domanda: "Collego la
fonte all'agente "Archivio documentale"?" (è quello dei client MCP).
- **Sì** (`fonte-esecutore`): `python3 prova_fonte.py promuovi --id <id>`
  (rifiuta se manca un benchmark PASS sull'estrazione attuale o la
  `descrizione` in `pipeline.yaml`); in
  `pipeline.yaml` togli `provvisorio: true`; commit sul branch corrente di
  adattatore, fixture, test, `pipeline.yaml`, domande e rapporto. Da allora il
  notifier la aggiorna da solo (le voci provvisorie le ignora).
- **No**: `python3 prova_fonte.py pulisci --id <id>`; togli la voce da
  `pipeline.yaml`; rapporto con l'esito.
