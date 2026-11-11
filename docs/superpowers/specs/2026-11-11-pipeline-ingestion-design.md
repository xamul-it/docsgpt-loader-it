# Pipeline agentica per nuove ingestion — design

Data: 2026-11-11. Caso pilota: Codice civile (R.D. 16 marzo 1942, n. 262) da Normattiva.

## Perché

Le fonti nuove si aggiungono già con un agente, ma in sessioni libere: ogni volta
si riscopre il metodo, si chiariscono le stesse cose con l'utente e nessun
controllo dice se l'estrazione è utile. La fonte curata dell'Emilia-Romagna
(issue #3) è entrata nell'indice come pagina di presentazione senza il documento,
e nessuno se n'è accorto.

La pipeline rende il processo **standard, verificato ed economico**:
- stesse fasi e stessi controlli per ogni fonte;
- qualità della fonte e dell'estrazione misurate, non stimate a occhio;
- pochi token: istruzioni precise, poche domande all'utente, modello adatto a
  ogni fase, nessun modello sui singoli documenti.

"Utile" vuol dire: da Claude Desktop, via `search_docs`, si trova il pezzo
giusto (es. l'articolo) e si arriva con un link al testo da consultare.

## Obiettivi

1. Una procedura ripetibile per aggiungere una fonte, dalla richiesta
   ("importa il Codice civile") alla sorgente collegata all'agente di Claude.
2. Valutazione esplicita e documentata di: qualità della fonte, formato da cui
   estrarre, unità di divisione, destinazione dei link.
3. Test automatici sulla qualità dell'estrazione, con soglie.
4. Verifica finale sulla ricerca reale (domande dedicate in `eval/`).
5. Consumo di token contenuto e prevedibile.

## Non obiettivi

- La pipeline che diagnostica e risolve i problemi di ricerca (seconda
  pipeline): progetto separato, dopo questo. Questa pipeline le fornisce
  rapporti e domande di prova.
- Cambiare `search_service` o lo schema dei metadati (issue #5): qui si
  producono documenti e metadati pronti, il recupero non si tocca.
- Aggiornare DocsGPT (resta 0.21.0).
- Lo storico delle versioni (multivigenza) come funzione di ricerca: si valuta
  il formato, ma l'obiettivo del pilota è il testo vigente.

## Anti-obiettivi (falliti anche se "funziona")

- Una fonte caricata senza che i test confermino che il contenuto c'è (caso #3).
- Decisioni prese per partito preso: formato, divisione e link si scelgono
  confrontando le opzioni su dati misurati, e il rapporto dice perché.
- Una pipeline che chiede chiarimenti continui all'utente.
- Un modello di linguaggio che elabora i documenti uno a uno.
- Rompere le fonti esistenti, il notifier o i link delle citazioni.

## Principi di economia dei token

1. **Istruzioni precise.** Ogni fase ha input, output, regole di decisione,
   valori predefiniti e criteri di uscita scritti. L'agente non deve
   interpretare.
2. **Poche domande all'utente, in punti fissi:**
   - **all'avvio, solo se manca qualcosa di essenziale nella richiesta**
     (argomento o fonte), in un unico messaggio;
   - **all'approvazione finale**, prima di collegare la sorgente all'agente di
     Claude, con il rapporto in mano.
   Il resto lo decide la pipeline con le regole sotto; ogni decisione presa da
   sola è scritta nel rapporto, così l'utente la può ribaltare a posteriori.
3. **Il modello giusto per ogni fase** (vedi tabella): il più piccolo che fa
   bene quel lavoro. I lavori meccanici li fanno gli script, senza modello.
4. **Codice deterministico per i documenti.** L'agente lavora su campioni e
   scrive l'estrattore; l'estrattore gira su tutto senza modello.
5. **Uscite compatte.** Script e test stampano riepiloghi brevi (contatori,
   pochi esempi), mai documenti interi. I file grandi stanno su disco; l'agente
   ne legge estratti.

## Fasi

Ogni fase scrive il suo risultato in un rapporto unico della fonte
(`docs/ingestion/<fonte>.md`) e passa alla successiva solo se il suo criterio
di uscita è soddisfatto. Se non lo è, la pipeline riprova al massimo una volta
con la correzione indicata dal test, poi si ferma e riporta.

| # | Fase | Chi la fa | Modello |
|---|---|---|---|
| 0 | Richiesta | utente | — |
| 1 | Verifica della fonte | agente | Sonnet |
| 2 | Confronto dei formati | script di misura + agente che decide | Sonnet |
| 3 | Scelta di divisione, metadati e link | agente | Sonnet; Opus solo se i criteri sono in conflitto |
| 4 | Estrattore | agente scrive codice | Sonnet |
| 5 | Test di qualità dell'estrazione | script | nessuno (Haiku per il riepilogo, se serve) |
| 6 | Caricamento in prova e domande dedicate | script + agente scrive le domande | Haiku per eseguire, Sonnet per le domande |
| 7 | Approvazione e messa in servizio | utente, poi script | — |

I nomi dei modelli sono le famiglie disponibili (Haiku, Sonnet, Opus 5.5); la
pipeline li imposta per fase (es. nel frontmatter dei sottoagenti).

### 1. Verifica della fonte
Controlla: ente ufficiale o no; data di aggiornamento e come rilevarla (es.
ELI `CONSOLIDATED/<data>`, data ultimo aggiornamento della pagina); formati
offerti; condizioni d'uso; stabilità degli URL; accesso senza login o
JavaScript. **Esce** con una scheda della fonte. **Si ferma** se la fonte non
è ufficiale e non c'è un'alternativa ufficiale, o se il contenuto richiede
login.

### 2. Confronto dei formati
Per ogni formato disponibile, uno script misura su un campione fisso (per il
Codice civile: 30 articoli scelti in modo deterministico, inclusi abrogati,
modificati e con commi numerati):
- **completezza**: articoli presenti contro attesi, testo non vuoto;
- **struttura**: articolo, rubrica, gerarchia (libro/titolo/capo/sezione);
- **pulizia**: marcatori di nota, menu, residui di impaginazione;
- **metadati**: identificativo stabile, data di vigenza;
- **costo**: richieste e byte per l'intero atto;
- **rilevamento aggiornamenti**: c'è una data o un hash affidabile?

**Esce** con una tabella e la scelta motivata (anche una combinazione:
es. testo da un formato e gerarchia da un altro).

Già osservato sul Codice civile, da confermare con le misure:
- AKN vigente (`/do/atto/caricaAKN?dataGU=…&codiceRedaz=…&dataVigenza=…`):
  una richiesta, 10,6 MB, 3.281 allegati = articoli, testo pulito salvo i
  marcatori `((n))`; **gerarchia assente** (solo testo); data FRBR
  dell'espressione inaffidabile (1944-11-10), data vera nell'alias ELI.
- Pagina HTML dell'atto: l'indice contiene la gerarchia (es. "CAPO VII Del
  contratto per persona da nominare", art. 1401–1405).
- Esistono anche l'export multivigente e l'"atto completo" (`esporta/attoCompleto`):
  da misurare.

### 3. Divisione, metadati e link
- **Unità di divisione**: si sceglie con una regola sulla lunghezza. Per la
  normativa il valore predefinito è l'articolo; un articolo oltre la soglia
  (da fissare con le misure della fase 2) si divide per comma, ripetendo in
  testa il percorso (atto › libro › titolo › capo › art. N – rubrica).
- **Metadati minimi per ogni pezzo**: atto, tipo, numero e data, articolo,
  rubrica, percorso, data di vigenza, identificativo stabile (URN/ELI/AKN), link.
- **Link: va valutato, non assunto.** Le opzioni:
  1. copia locale servita dall'app (come oggi per le circolari);
  2. link al sito ufficiale;
  3. entrambe (link ufficiale e copia locale come riserva).

  Criteri, misurati e scritti nel rapporto:
  - **precisione**: porta all'articolo o all'atto intero?
  - **corrispondenza**: mostra la stessa versione indicizzata? Il sito ufficiale
    mostra sempre l'ultima, la copia locale quella caricata;
  - **stabilità** nel tempo;
  - **disponibilità** se il sito ufficiale è irraggiungibile;
  - **costo di servizio** (spazio, generazione dei file, voce in `doc_links`).

  Dato già verificato: `https://www.normattiva.it/uri-res/N2Ls?urn:nir:stato:regio.decreto:1942-03-16;262~art1399`
  risponde 200 ma apre l'atto intero con l'indice, non l'articolo. Va verificato
  se esiste una forma che apre il singolo articolo.

### 4. Estrattore
Codice nel repository (`loader/`), con la sua cartella di appoggio, nello
stile esistente: manifest JSONL (`title`, `document_date`, `authority`,
`document_type`, `number`, `local_file`, `sha256`, …) più i metadati della
fase 3. Aggiornamento incrementale basato sul rilevamento della fase 1.
Integrazione con `downloader.py` / `carica_docsgpt.py` / `fonti/*.yaml` e con
il notifier come per le fonti esistenti.

### 5. Test di qualità dell'estrazione
Script riutilizzabile, parametrico per fonte. Controlli e soglie predefinite:
- conteggio unità estratte contro atteso (per il Codice civile, dagli
  allegati AKN o dall'indice): 100%;
- nessuna unità con testo sotto soglia o fatto di sola navigazione: 0 casi;
- marcatori e residui rimossi: 0 casi;
- rubrica presente dove la fonte la prevede: ≥ 99%;
- confronto testuale con la fonte su un campione: identico dopo la
  normalizzazione;
- metadati obbligatori presenti: 100%;
- link: un campione risponde 200 e porta al contenuto atteso.

Il test stampa solo un riepilogo. Se un controllo fallisce, la pipeline torna
alla fase 4 una volta.

### 6. Caricamento in prova
Carica come sorgente separata, non collegata all'agente di Claude. Aggiunge a
`eval/domande.yaml` 5–10 domande sulla fonte, di tipi diversi: riferimento
esatto, parafrasi, articoli con numeri vicini, rubrica senza numero. Lancia
`eval/bench.py` limitato a quella sorgente. **Soglia predefinita**: la fonte
attesa entro i primi 3 risultati in almeno l'80% delle domande con keywords.

### 7. Approvazione e messa in servizio
Unico messaggio all'utente con il rapporto: fonte, formato scelto e perché,
link scelto e perché, risultati dei test, benchmark. Con il sì: collegamento
all'agente, voce in `doc_links`, notifier. Con il no: la sorgente di prova si
cancella.

## Forma

- Una skill di Claude Code nel repository (`.claude/skills/nuova-fonte/`) con
  le istruzioni delle fasi, le regole di decisione e i valori predefiniti.
- Sottoagenti per fase con il modello indicato.
- Script riutilizzabili: misura dei formati, test di qualità, caricamento in
  prova.
- Un rapporto per fonte in `docs/ingestion/`.

## Decisioni prese in fase di design (da confermare)

- **Avvio:** l'utente lancia la skill con la richiesta in linguaggio naturale
  (es. `/nuova-fonte importa il Codice civile da <url>`).
- **Un file per unità.** Ogni unità (articolo o comma) è un file con in testa
  il percorso e la rubrica, così DocsGPT non spezza l'articolo a metà e il
  titolo del risultato dice già dove siamo.
- **Benchmark della sola fonte:** un agente DocsGPT temporaneo
  (`prova-<fonte>`) collegato solo alla sorgente di prova; `bench.py` usa la
  sua chiave. Si cancella con la sorgente se la fonte non passa.
- **Soglia del benchmark** (prima 3 nell'80% delle domande con keywords):
  valore iniziale, da rivedere dopo il pilota.
- **Fonti già presenti:** l'estratto curato del Codice civile in
  `locazioni-nazionale-lungo-termine` (Libro IV, locazione e condominio) resta
  com'è. Unificarlo è una decisione successiva.

## Lasciato a chi costruisce

- Le soglie numeriche di dettaglio (lunghezza oltre cui si divide per comma,
  soglia di testo vuoto), da fissare con le misure del pilota e scrivere nella
  skill.
- La struttura interna degli script e dei sottoagenti.
- Se i metadati aggiuntivi entrano nei pezzi via nome file, intestazione nel
  testo o altro meccanismo compatibile con DocsGPT 0.21 senza toccare
  `search_service`.

## Rischi noti

- I metadati aggiuntivi oggi non sono usati dalla ricerca (issue #5): il
  beneficio pieno arriva con la seconda pipeline.
- Normattiva non documenta ufficialmente l'API AKN usata; se cambia,
  l'estrattore se ne accorge al test della fase 5.
