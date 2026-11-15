# Server MCP dell'archivio documentale

Il backend DocsGPT espone un server MCP (streamable HTTP) che permette a client
come Claude o GPT di cercare nelle fonti indicizzate. Qui c'è tutto quello che
serve a chi lo collega o lo mantiene.

## Collegamento

- **URL**: `https://docsgpt.ilz.it/mcp/` (in locale `http://127.0.0.1:7091/mcp/`).
- **Chiave**: è la chiave API di un agente DocsGPT. Il client cerca nelle
  sorgenti collegate a quell'agente e solo in quelle. L'agente principale è
  **"Archivio documentale"**: la promozione di una fonte (`prova_fonte.py
  promuovi`) la collega a lui.
- **Autenticazione**, in uno dei due modi:
  - header `Authorization: Bearer <chiave>`;
  - per i client che non possono mandare header (i connettori personalizzati
    di claude.ai), la chiave nell'URL: `/mcp/?key=<chiave>`.

La chiave non va mai nel repository. Per leggerla:
`docker exec docsgpt-postgres-1 psql -U docsgpt -d docsgpt -tAc "select key from agents where name='Archivio documentale'"`.

## Strumenti

### `list_sources()`

Elenca le fonti consultabili con la chiave. Ogni voce ha:
- `nome`: da usare in `sources`;
- `gruppo`: per esempio `Fisco`, `Locazioni`, `Codici`;
- `descrizione`: che cosa contiene la fonte;
- `aggiornata`: data dell'ultimo caricamento (AAAA-MM-GG).

Serve al modello per rispondere a domande come "quali fonti sono attive?" e
per scegliere dove cercare.

### `search_docs(query, keywords=None, max_documents=10, sources=None)`

Ricerca ibrida, semantica e per parole chiave in italiano (`websearch_to_tsquery`,
configurazione `italian`), con fusione dei risultati per rango (RRF). Restituisce
documenti distinti e non frammenti duplicati.

- `query`: la domanda o i termini tecnici.
- `keywords`: termini da trovare testualmente. Accetta `"frase esatta"`, `OR`
  e `-escludi`. Se non si trova nessun documento con tutti i termini, si
  accettano anche quelli che ne contengono solo alcuni.
- `max_documents`: da 1 a 20.
- `sources`: nomi di fonti o di gruppi, presi da `list_sources`. Maiuscole e
  minuscole non contano. Senza `sources` si cerca in tutte le fonti. Un nome
  sconosciuto dà un errore con l'elenco delle fonti disponibili, invece di un
  risultato vuoto che sembrerebbe "non c'è nulla".

La descrizione dello strumento chiede al modello di cercare prima nelle fonti
nominate dalla domanda (per esempio "cosa prevede il codice civile…" →
`sources=["Codice civile"]`) e poi di allargare la ricerca se serve.

Campi di ogni risultato: `title`, `date`, `fonte`, `ente`, `url` (link
pubblico al documento), `source`, `similarity`, `passages` (ciascun passaggio
con `text`, `found_by`, `similarity`).

## Catalogo delle fonti

`nome`, `gruppo` e `descrizione` vengono dai file `loader/fonti/*.yaml`,
montati in sola lettura in `/app/fonti` (variabile `CATALOGO_FONTI` per
cambiare cartella). Si legge ogni voce di `sorgenti` che ha un `nome`.
`gruppo` è il `gruppo_catalogo` della voce, altrimenti il suo `gruppo`,
altrimenti quello in testa al file. In `pipeline.yaml` il `gruppo` è anche la
cartella dell'archivio e la chiave dei caricamenti (minuscolo, es. `codici`):
il nome mostrato va in `gruppo_catalogo` (es. `Codici`).

| File | Fonti | Gruppo |
|---|---|---|
| `fisco.yaml` | prassi, normativa, giurisprudenza, novità (`downloader.py`) | `Fisco`, in testa al file |
| `locazioni.yaml` | elenco curato delle locazioni | `Locazioni`, in testa al file |
| `pipeline.yaml` | fonti della pipeline `/nuova-fonte` | `gruppo_catalogo` della voce (se manca, `gruppo`) |

I file si leggono a ogni chiamata, quindi le modifiche valgono senza
riavviare. Una sorgente che manca dal catalogo compare comunque: il gruppo si
ricava dal nome (`"<Gruppo> - <nome>"`) e la descrizione resta vuota. La
promozione di una fonte nuova si rifiuta se in `pipeline.yaml` manca la
`descrizione`.

## Implementazione e manutenzione

Il server è codice upstream di DocsGPT, corretto con patch locali montate
sopra i file dell'immagine (vedi `docker-compose-standalone.yaml`):

- `patches/mcp_server.py`: strumenti, descrizioni, chiave in `?key=`;
- `patches/search_service.py`: `search_documents`, `list_sources`, selezione
  delle fonti, controllo dei permessi sulle sorgenti dell'agente.

Le copie upstream sono in `*.upstream`. Dopo un aggiornamento dell'immagine,
confrontale con i file della nuova immagine e riporta le differenze.
Per rendere effettive le modifiche al compose (montaggi nuovi) serve
`docker compose -f docker-compose-standalone.yaml up -d backend`. Per le
modifiche alle patch basta riavviare il backend.
