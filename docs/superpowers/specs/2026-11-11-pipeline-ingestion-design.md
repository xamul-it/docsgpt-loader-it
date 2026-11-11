# Pipeline agentica per nuove fonti — design

Data: 2026-11-11 (rivista lo stesso giorno: da pipeline per la normativa a
pipeline generica con la normativa come primo caso d'uso).
Primo caso d'uso: Codice civile (R.D. 16 marzo 1942, n. 262) da Normattiva.

## Perché

Le fonti nuove si aggiungono già con un agente, ma in sessioni libere: ogni volta
si riscopre il metodo, si scrive un loader diverso, si chiariscono le stesse cose
con l'utente e nessun controllo dice se l'estrazione è utile. La fonte curata
dell'Emilia-Romagna (issue #3) è entrata nell'indice come pagina di
presentazione senza il documento, e nessuno se n'è accorto.

La pipeline è il modo **standard** di creare il loader di una fonte qualsiasi
(sito, banca dati, export XML, API) e portarla fino alla sorgente DocsGPT
collegata a Claude. Quattro scopi, in quest'ordine di verifica:
- **standardizzazione**: stesse fasi, stesso contratto dei dati, stessi
  controlli e stesso rapporto per ogni fonte;
- **qualità**: fonte ed estrazione misurate, non stimate a occhio;
- **ottimizzazione**: un loader nuovo è solo un *adattatore* piccolo; tutto il
  resto (scrittura, aggiornamento incrementale, misure, test, caricamento) è
  codice comune già collaudato;
- **efficienza dei token**: istruzioni precise, poche domande, modello adatto a
  ogni fase, nessun modello sui singoli documenti, uscite compatte.

"Utile" vuol dire: da Claude Desktop, via `search_docs`, si trova il pezzo
giusto (es. l'articolo) e si arriva con un link al testo da consultare.

## Obiettivi

1. Una procedura ripetibile, indipendente dal tipo di fonte, dalla richiesta
   ("importa il Codice civile da <url>") alla sorgente collegata all'agente di
   Claude.
2. Un **nucleo comune** con un contratto dei dati unico e un'**interfaccia di
   adattatore**: aggiungere una fonte = scrivere (o riusare) un adattatore che
   supera i test di contratto.
3. Se per la fonte non esiste un adattatore, la pipeline lo **scrive da sola**
   partendo da un modello, su campioni salvati, e lo accetta solo quando passa i
   test di contratto e i suoi test specifici.
4. Valutazione esplicita e documentata di: qualità della fonte, formato da cui
   estrarre, unità di divisione, destinazione dei link.
5. Test automatici sulla qualità dell'estrazione, con soglie uguali per tutti.
6. Verifica finale sulla ricerca reale (domande dedicate in `eval/`).
7. Consumo di token contenuto e prevedibile.

## Non obiettivi

- La pipeline che diagnostica e risolve i problemi di ricerca (seconda
  pipeline): progetto separato. Questa le fornisce rapporti e domande di prova.
- Cambiare `search_service` o lo schema dei metadati (issue #5).
- Aggiornare DocsGPT (resta 0.21.0).
- Migrare le fonti esistenti (`downloader.py`, elenco curato `locazioni`) nel
  nuovo nucleo: restano come sono. Si potrà fare dopo, fonte per fonte.
- Fonti da caricare come file originali (PDF, DOCX) senza estrarne il testo: le
  gestisce `downloader.py`. Il nucleo lavora su unità di testo.
- Lo storico delle versioni (multivigenza) come funzione di ricerca.

## Anti-obiettivi (falliti anche se "funziona")

- Una fonte caricata senza che i test confermino che il contenuto c'è (caso #3).
- Un loader scritto da zero per ogni fonte, con scrittura, manifest, controlli o
  aggiornamento propri invece di quelli del nucleo.
- Regole specifiche di una fonte (es. "articolo", "rubrica", "abrogato") nel
  nucleo: stanno nell'adattatore e arrivano al nucleo come campi generici.
- Decisioni prese per partito preso: formato, divisione e link si scelgono
  confrontando le opzioni su dati misurati, e il rapporto dice perché.
- Una pipeline che chiede chiarimenti continui all'utente.
- Un modello di linguaggio che elabora i documenti uno a uno.
- Rompere le fonti esistenti, il notifier o i link delle citazioni.

## Architettura

```
            ┌──────────────── skill /nuova-fonte (fasi, regole, domande) ───────────────┐
            │      fonte-analista (Sonnet)              fonte-esecutore (Haiku)         │
            └───────────────┬──────────────────────────────────┬────────────────────────┘
                            │ scrive/riusa                     │ lancia, riassume
                 ┌──────────▼──────────┐        ┌──────────────▼──────────────────────┐
                 │ adattatore di fonte │        │ nucleo comune (loader/)             │
                 │ adattatori/<nome>.py│◄──────►│ fonte.py: contratto, rete con cache │
                 │ + fixture + test    │        │ misura_formati · estrai · valuta_   │
                 └─────────────────────┘        │ link · qualita_estrazione · prova_  │
                                                │ fonte · carica_docsgpt              │
                                                └─────────────────────────────────────┘
```

### Contratto dei dati: l'unità

Ogni fonte, qualunque sia, si riduce a una lista di **unità**: il pezzo che
diventa un file e che la ricerca deve restituire (un articolo di legge, una
risposta a interpello, una scheda, una FAQ). Un'unità ha:

- `id`: stabile e unico nella fonte, ASCII (diventa il nome del file);
- `titolo`: breve, leggibile (es. "Art. 1399 – Ratifica");
- `percorso`: la gerarchia da cui l'unità dipende, anche vuota (es. libro ›
  titolo › capo);
- `testo`: pulito, con i blocchi naturali (commi, paragrafi) separati da una
  riga vuota: il nucleo divide lì le unità troppo lunghe;
- `metadati`: campi propri della fonte (es. `urn`, `articolo`, `rubrica`);
- `link_ufficiali`: un URL per ciascun candidato di link della fonte;
- `breve_ammesso`: vero se un testo corto è legittimo (es. articolo abrogato).

Dall'unità il nucleo produce sempre lo stesso risultato, nella struttura
dell'archivio esistente (`<archivio>/<gruppo>/<id>/manifest.jsonl` e `files/`):
un file HTML per unità (o per parte, se divisa) con in testa il percorso, il
titolo e, se scelto, il link ufficiale (la ricerca restituisce il testo, non i
campi del manifest: il link arriva a Claude solo così); una riga di manifest
con i campi esistenti più i metadati piatti, come le locazioni, e un
`detail_url` unico per riga (è la chiave con cui il notifier riconosce i
documenti nuovi); lo stato della versione per l'aggiornamento incrementale.

### Interfaccia dell'adattatore

Un adattatore conosce solo la sua fonte. Dichiara:
- quali URL riconosce;
- i **formati** che offre (es. export XML, pagina HTML per unità) e, per
  ognuno, come produrre le unità, anche solo per un campione;
- le **unità attese** (l'elenco completo secondo la fonte, per il conteggio);
- la **versione** corrente della fonte (data o hash) per l'aggiornamento;
- i **candidati di link** ufficiali;
- i metadati obbligatori e i residui tipici da cercare (es. marcatori di nota).

Tutte le richieste passano da una **rete con cache su disco**: ogni risposta si
scarica una volta e serve a misura, estrazione e test. La stessa interfaccia
accetta una rete finta che legge da fixture: i test non usano mai Internet.

### Configurazione

Una voce per fonte in `loader/fonti/pipeline.yaml` (id, gruppo, nome della
sorgente, adattatore, URL, formato scelto, link scelto, soglia di divisione,
ente e tipo documento, parametri propri dell'adattatore). Da lì
`carica_docsgpt.py` e il notifier sanno nomi, cartelle e come aggiornare.

## Principi di economia dei token

1. **Istruzioni precise.** Ogni fase ha input, comando, criterio di uscita,
   regola di decisione, valore predefinito e cosa scrivere nel rapporto.
2. **Poche domande all'utente, in punti fissi:**
   - all'avvio, solo se manca l'argomento o la fonte, in un unico messaggio;
   - all'approvazione finale, con il rapporto in mano.
   Ogni decisione presa da sola è scritta nel rapporto.
3. **Il modello giusto per ogni fase**: il più piccolo che fa bene quel lavoro.
4. **Codice deterministico per i documenti.** L'agente scrive l'adattatore su
   campioni; l'estrazione gira su tutto senza modello.
5. **Riuso prima della scrittura.** Se un adattatore riconosce l'URL, la fase
   di scrittura si salta.
6. **Uscite compatte.** Script e test stampano al massimo ~15 righe (contatori,
   pochi esempi); i risultati completi stanno in file JSON che l'agente legge
   solo se serve.

## Fasi

Ogni fase scrive il suo risultato nel rapporto della fonte
(`docs/ingestion/<id>.md`) e passa alla successiva solo se il criterio di uscita
è soddisfatto; altrimenti riprova al massimo una volta con la correzione
indicata dal test, poi si ferma e riporta.

| # | Fase | Chi la fa | Modello |
|---|---|---|---|
| 0 | Richiesta | utente | — |
| 1 | Verifica della fonte e scelta dell'adattatore | agente | Sonnet |
| 2 | Adattatore (riuso, oppure scrittura + test di contratto) | agente scrive codice | Sonnet |
| 3 | Confronto dei formati | script + agente decide | Sonnet; Opus solo se i criteri sono in conflitto |
| 4 | Divisione e link | script + agente decide | Sonnet |
| 5 | Estrazione e test di qualità | script | nessuno (Haiku per il riepilogo) |
| 6 | Caricamento in prova e domande dedicate | script + agente scrive le domande | Haiku esegue, Sonnet scrive le domande |
| 7 | Approvazione e messa in servizio | utente, poi script | — |

### 1. Verifica della fonte
Controlla: ente ufficiale o no; come si rileva l'aggiornamento; formati
offerti; condizioni d'uso; stabilità degli URL; accesso senza login o
JavaScript. Cerca nel registro un adattatore che riconosce l'URL. **Esce** con
la scheda della fonte e "adattatore: riuso <nome>" oppure "da scrivere".
**Si ferma** se la fonte non è ufficiale e non c'è un'alternativa ufficiale, o
se il contenuto richiede login.

### 2. Adattatore
- **Riuso**: se esiste, si aggiunge solo la voce in `fonti/pipeline.yaml`.
- **Scrittura**: l'agente salva in `loader/fixtures/<nome>/` pochi campioni
  reali (le risposte che servono a produrre 5–10 unità, compresi i casi
  difficili che ha visto), copia il modello di adattatore e lo completa.
  **Esce** quando passano i test di contratto (comuni a tutti gli adattatori)
  e i test specifici scritti dall'agente sui casi difficili.

### 3. Confronto dei formati
Per ogni formato dichiarato dall'adattatore, lo script di misura lavora su un
campione deterministico (30 unità, sempre inclusi i casi limite indicati
dall'adattatore: la più lunga, una a testo breve ammesso, …) e misura:
- **completezza**: unità trovate contro attese, testi vuoti;
- **struttura**: titolo e percorso presenti;
- **pulizia**: residui (marcatori, menu, impaginazione);
- **metadati obbligatori** presenti;
- **costo**: richieste e byte stimati per l'intera fonte;
- **rilevamento aggiornamenti**: versione disponibile o no.

**Esce** con una tabella e la scelta motivata. Regola: completezza 100% →
residui 0 → titolo → percorso → costo; ammesse combinazioni se l'adattatore le
dichiara (es. testo da un formato, gerarchia da un altro).

### 4. Divisione e link
- **Divisione**: un file per unità. Un'unità oltre la soglia (predefinita 4000
  caratteri, da confermare con le lunghezze misurate: p95 e massimo nel
  rapporto) si divide sui blocchi naturali; ogni parte ripete in testa percorso
  e titolo.
- **Link: valutato, non assunto.** Candidati: copia locale servita dall'app
  (sempre disponibile) e i link ufficiali dichiarati dall'adattatore. Criteri,
  misurati su un campione e scritti nel rapporto: precisione (porta all'unità o
  all'intero atto?), corrispondenza di versione, disponibilità, costo. Regola:
  precisione "unità" > corrispondenza > disponibilità > costo; a parità,
  `entrambi` (ufficiale con copia locale di riserva).

### 5. Estrazione e test di qualità
Lo script comune estrae tutto, poi il test di qualità controlla con soglie
uguali per tutte le fonti:
- conteggio unità estratte contro attese: 100%;
- testo vuoto, sotto soglia o fatto di sola navigazione: 0 casi (esenti le
  unità con `breve_ammesso`);
- residui: 0 casi;
- titolo presente: ≥ 99%;
- metadati obbligatori presenti: 100%;
- confronto testuale con la fonte su un campione: identico dopo la
  normalizzazione;
- link scelto: un campione risponde 200 e porta al contenuto atteso.

Se un controllo fallisce si torna alla fase 2 (correzione dell'adattatore) una
volta.

### 6. Caricamento in prova
Carica la fonte come sorgente separata con un agente DocsGPT temporaneo
(`prova-<id>`) collegato solo a lei. L'agente scrive 5–10 domande `<id>-*` in
`eval/domande.yaml`, di tipi diversi (riferimento esatto, parafrasi, unità con
identificativi vicini, titolo senza numero, casi limite della fonte), con le
loro keywords. Lancia `eval/bench.py` solo su quelle. **Soglia**: unità attesa
entro i primi 3 risultati in almeno l'80% delle domande con keywords.

### 7. Approvazione e messa in servizio
Unico messaggio all'utente con il rapporto. Con il sì: sorgente definitiva,
collegamento all'agente "Claude - archivio fiscale", voce in `doc_links`,
aggiornamento periodico nel notifier; cancellazione della prova. Con il no: la
prova si cancella.

## Primo caso d'uso: Codice civile da Normattiva

L'adattatore `normattiva` è il primo scritto con l'interfaccia; serve da
esempio per i successivi. Già osservato, da confermare con le misure:
- AKN vigente (`/do/atto/caricaAKN?dataGU=…&codiceRedaz=…&dataVigenza=…`): una
  richiesta, 10,6 MB, 3.281 allegati = articoli (3.250 c.c. + 31 preleggi),
  testo pulito salvo i marcatori `((n))`; **gerarchia assente**; data vera
  nell'alias ELI `CONSOLIDATED/<data>`, non nel FRBRdate (1944-11-10).
- La pagina dell'atto ha l'indice con la gerarchia e un link `caricaArticolo`
  per ogni articolo: candidato sia come formato (HTML per articolo) sia come
  link ufficiale.
- `uri-res/N2Ls?urn:…~art1399` risponde 200 ma apre l'atto intero: candidato
  link da misurare.
- Casi limite: numeri ripetuti tra parti dell'atto (art. 1 preleggi e art. 1
  c.c.), 205 articoli bis/ter/…, ~193 abrogati, articoli lunghi (max 10.503
  caratteri, p95 1.604).
- L'estratto curato del Codice civile in `locazioni-nazionale-lungo-termine`
  resta com'è.

**Prova di genericità.** Il nucleo si considera generico solo quando una
seconda fonte di tipo diverso (non normativa, es. risposte a interpello o FAQ
di un ente) passa la pipeline senza modifiche al nucleo. È il secondo collaudo,
dopo quello sul Codice civile.

## Forma

- Skill di Claude Code nel repository (`.claude/skills/nuova-fonte/`) con fasi,
  regole di decisione e valori predefiniti.
- Due sottoagenti con modello fissato: `fonte-analista` (Sonnet: verifica,
  adattatore, decisioni, domande) e `fonte-esecutore` (Haiku: lancia gli script
  e riassume).
- Nucleo in `loader/` e adattatori in `loader/adattatori/`, con un modello di
  adattatore da copiare.
- Un rapporto per fonte in `docs/ingestion/`, da un modello unico.

## Decisioni

- **Avvio:** `/nuova-fonte <richiesta in linguaggio naturale>`.
- **Un file per unità** (o per parte), con in testa percorso e titolo.
- **Benchmark della sola fonte** con un agente DocsGPT temporaneo; si cancella
  con la sorgente di prova.
- **Soglia del benchmark** (primi 3 nell'80%): valore iniziale.
- **Dipendenze:** il nucleo e gli adattatori usano solo ciò che c'è anche nel
  container del notifier (`requests`, `beautifulsoup4`, `defusedxml`, `PyYAML`):
  l'XML si legge con `defusedxml.ElementTree`, non con `lxml`.
- **Fonti esistenti:** non migrate (vedi Non obiettivi).

## Lasciato a chi costruisce

- La struttura interna degli script e dei sottoagenti.
- Le soglie di dettaglio dopo il pilota (divisione, testo minimo), scritte in
  `fonti/pipeline.yaml` e nella skill.
- Il controllo file ↔ manifest riusa `quadratura.check_local`; il resto di
  `quadratura.py` resta per le fonti di `downloader.py`.

## Rischi noti

- I metadati aggiuntivi oggi non sono usati dalla ricerca (issue #5): il
  beneficio pieno arriva con la seconda pipeline.
- Un adattatore scritto dall'agente può passare i test sui campioni e sbagliare
  su casi non visti: lo coprono il conteggio al 100% e il campione della fase 3,
  che include i casi limite.
- Normattiva non documenta l'API AKN; se cambia, se ne accorge il test di
  qualità.
