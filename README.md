# Loader normativa italiana

Estensione per [DocsGPT](https://github.com/arc53/DocsGPT) che costruisce e
mantiene aggiornato un archivio di **normativa, prassi e giurisprudenza
italiana**, consultabile da Claude (e da altri client MCP) con citazione delle
fonti. Non è un fork di DocsGPT: DocsGPT gira dall'immagine ufficiale e questo
repository aggiunge solo ciò che serve per le fonti normative italiane.

## Componenti

| Cartella | Cosa fa |
|---|---|
| `loader/downloader.py` | Scarica le raccolte ufficiali (CeDEF / def.finanze.it per normativa e giurisprudenza tributaria, siti di Agenzia delle Entrate, MEF, ADM) in cartelle per ente e tipologia, con `manifest.jsonl` (titolo, data, ente, URL, sha256). Modalità `refresh` incrementale. |
| `loader/carica_docsgpt.py` | Carica ogni ente su DocsGPT come sorgente, sostituisce la versione precedente e ricollega gli agenti. |
| `loader/quadratura.py` | Controlli di coerenza tra archivio su disco e sorgenti caricate. |
| `notifier/` | Servizio che segue le novità (feed degli enti e refresh periodico dell'archivio), le indicizza e ne invia la sintesi. |
| `patches/` | Modifiche al backend DocsGPT montate nel container: server MCP con `search_docs(query, keywords, max_documents)` (ricerca ibrida), servizio di ricerca, etichette. I file `*.upstream` sono gli originali di riferimento. |
| `claude/istruzioni-progetto.md` | Istruzioni per Claude su come cercare, pesare le fonti e citarle. |

## Dati

L'archivio scaricato (decine di GB) e i dati di DocsGPT non sono nel
repository. La cartella dell'archivio si passa agli script con `--target`.

## Note

- `docker-compose-standalone.yaml` non è versionato perché contiene
  credenziali; va pubblicato come esempio senza segreti.
- Gli script in `loader/` sono anche copiati nella cartella dell'archivio,
  dove li esegue il container `notifier`: finché il notifier non legge da
  `loader/`, le modifiche vanno riportate lì.
