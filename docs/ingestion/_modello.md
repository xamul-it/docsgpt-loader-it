# <Nome fonte> — rapporto di ingestion

- id: `<id>` · gruppo: `<gruppo>` · sorgente DocsGPT: "<nome>"
- URL: <url>
- Data: AAAA-MM-GG · esito: in corso | approvata | respinta | fermata alla fase N

## 1. Scheda della fonte

| voce | valore |
|---|---|
| ente / ufficiale | |
| formati offerti | |
| rilevamento aggiornamenti | |
| accesso (login, JavaScript) | |
| condizioni d'uso | |
| avvisi della ricognizione | |

Adattatore: riuso `<nome>` | scritto `<nome>`.

## 2. Adattatore

- Unità scelta e perché:
- Casi difficili visti (con l'id dell'unità d'esempio):
- Fixture (`loader/fixtures/<nome>/`, KB):
- Test: `python3 -m unittest test_contratto test_<nome>` →

## 3. Formati

(tabella di `misura_formati.py`)

Scelta: `<formato>` — perché (criterio che ha deciso):

## 4. Divisione e link

- Lunghezze: p95 = , max = → soglia_caratteri =
- (tabella di `valuta_link.py`)
- Link scelto: `<locale | entrambi:candidato>` — perché:

## 5. Qualità dell'estrazione

(righe di `estrai.py` e `qualita_estrazione.py`)

## 6. Benchmark

- Domande: `<id>-01` … `<id>-NN` (tipi)
- (righe di `prova_fonte.py bench`)

## Decisioni prese in autonomia

- 

## Esito

(approvazione dell'utente, promozione, o motivo della fermata)
