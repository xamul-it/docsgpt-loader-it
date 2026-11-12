# Codice civile — rapporto di ingestion

- id: `codice-civile` · gruppo: `codici` · sorgente DocsGPT: "Codice civile"
- URL: https://www.normattiva.it/uri-res/N2Ls?urn:nir:stato:regio.decreto:1942-03-16;262 (l'utente ha dato la homepage https://www.normattiva.it/)
- Data: 2026-11-11 · esito: approvata

## 1. Scheda della fonte

| voce | valore |
|---|---|
| ente / ufficiale | Presidenza del Consiglio dei Ministri, gestito dall'Istituto Poligrafico e Zecca dello Stato (IPZS): fonte ufficiale della legge vigente |
| formati offerti | (1) pagina dell'atto con l'indice di tutti gli articoli, 2,6 MB; (2) un articolo per richiesta (`/atto/caricaArticolo`, HTML, 3283 richieste); (3) export Akoma Ntoso dell'intero atto (`/do/atto/caricaAKN`, 10,6 MB, una richiesta, accenti scritti con l'apostrofo, senza libri/titoli/capi) |
| rilevamento aggiornamenti | nell'indice ogni articolo ha `art.versione=N`, che sale a ogni modifica: la versione dell'adattatore è un hash di (parte, articolo, N) |
| accesso (login, JavaScript) | libero, senza login; le richieste dinamiche (articolo, AKN) rispondono 500 / pagina di errore senza il cookie di sessione preso aprendo la pagina dell'atto: l'adattatore lo riapre e riprova una volta |
| condizioni d'uso | non verificate: la pagina ha un rimando a "Note legali" che non è stato letto |
| avvisi della ricognizione | nessuno. La homepage non porta all'atto: l'URN `urn:nir:stato:regio.decreto:1942-03-16;262` e `urn:nir:stato:codice.civile:1942-03-16;262` danno la stessa pagina (5119 voci d'indice = 3283 articoli attuali + versioni precedenti) |

Adattatore: scritto `normattiva`.

## 2. Adattatore

- Unità scelta e perché: l'articolo (3283: 3250 del Codice civile, 31 delle Disposizioni sulla legge in generale, 2 del R.D. di approvazione). È la più piccola parte che si cita da sola ("art. 2043"). Gli articoli sono 3281 negli allegati (AKN) + 2 dell'atto; l'indice della pagina li elenca tutti.
- Id: `codice-civile-art-42-bis`, `disposizioni-sulla-legge-in-generale-art-17`, `atto-art-1` (parte + numero: il numero si ripete tra parti).
- Percorso: allegato, Libro, Titolo, Capo, Sezione (dall'albero della pagina dell'atto, comune ai due formati).
- Formati: `articolo` (una richiesta per articolo, accenti corretti, data di entrata in vigore) e `akn-indice` (testo dall'export AKN in una richiesta + gerarchia dall'indice: è la combinazione, quindi un formato a sé).
- Casi difficili visti: numerazione bis (`codice-civile-art-42-bis`, nell'indice "art. 42 bis"); numerazione con barra (`codice-civile-art-314-2` = "art. 314/2", abrogato); articoli abrogati con testo corto (`codice-civile-art-3`: "ARTICOLO ABROGATO DALLA L. 8 MARZO 1975, N. 39", 254 su 3281 negli allegati) => `breve_ammesso`; testo tutto tra `((…))` (`-42-bis`); numero ripetuto in due allegati (`disposizioni-sulla-legge-in-generale-art-1` e `codice-civile-art-1`); il più lungo (`codice-civile-art-2426`, ~9,8 mila caratteri, 10+ blocchi); articolo con 20 note "AGGIORNAMENTO" e rimandi "(3a) (15a)…" (`-art-5`); articolo dell'atto con preambolo (`atto-art-1`); intestazioni di percorso con `((…))` (Titolo VIII, Capo V); versioni precedenti nell'indice (`agg.N`, `orig.`: 1836 voci da escludere).
- Fixture (`loader/fixtures/normattiva/`, KB): 138 KB di file (pagina dell'atto ritagliata a 11 articoli con le loro intestazioni, 11 articoli HTML reali, AKN ritagliato con gli stessi articoli)
- Test: `python3 -m unittest test_contratto test_normattiva` → OK (21 test)

## 3. Formati

```
formato           trov/att vuote resid titolo  perc  meta    p95    max rich.tot  MB tot
articolo           30/30       0     0     30    30    30   1299   9752     3392   302.1
akn-indice         30/30       0    16     30    30    30   1304   9781        2    13.2
```

Scelta: `articolo` — criterio 1 pari (30/30, 0 vuote), criterio 2 decide: 0 residui contro 16 dell'indice (accenti con apostrofo). Criteri 1 e 2 concordano, nessuna escalation. Il costo (3392 richieste, 302 MB contro 2 e 13 MB) è l'ultimo criterio e non pesa.

## 4. Divisione e link

- Lunghezze (campione 30, formato articolo): p95 = 1299, max = 9752 → soglia_caratteri = 2400 (fissa); il p95 sta sotto, gli articoli lunghi (max 9752) si dividono sui blocchi.
```
candidato              200%  unità%  atto% assente%  byte medi
locale                100.0   100.0    0.0      0.0       1289
normattiva            100.0     0.0    0.0    100.0    2595219
```
- Link scelto: `locale` — `normattiva` ha unità% = 0 (assente 100%: es. `atto-art-1` → `…;262~art1`) e pagine da 2,6 MB; il locale ha unità% = 100.

## 5. Qualità dell'estrazione

Primo `estrai.py --forza`: ERRORE, 88 unità non valide, non tocco nulla (nessun file scritto). Cause (diagnosi sulla cache, 3323 unità):

| Causa | Unità | Correzione |
|---|---|---|
| residuo `articolo (precedente\|successivo)` troppo largo: è un rimando nel testo di legge (art. 8 "dall'articolo precedente"); la navigazione sta fuori da `.bodyTesto` | 78 | il residuo vale solo come riga a sé (`^\s*articolo (precedente\|successivo)\s*$`) |
| note "AGGIORNAMENTO (n)" inline dopo una riga di trattini (art. 529, 2142), con rimando attaccato al testo "amministrazione.(111)" | 9 | testo tagliato alla prima riga di soli trattini; tolti i rimandi `(n)` attaccati, solo per gli n elencati nelle note |
| art. 968, "La subenfiteusi non è ammessa." (30 caratteri) scartato come vuoto | 1 | `breve_ammesso` anche per testi < 300 caratteri che finiscono con un punto |

Difetto trovato di passaggio: `<div class="ins-akn">((figli))</div>` (1754 occorrenze) spezzava la frase in blocchi ("di\n\nfigli\n\n."); ora il div resta nella frase. Test in `test_normattiva.py` (`ResiduiDelCodiceTest`, fixture `art-codice-civile-art-8/529/2142/968.html`); `test_contratto test_normattiva`: OK (26 test).

Secondo `estrai.py --forza` (dalla cache): `codice-civile: scritte 3323, invariate 0, rimosse 0, divise 30`, uscita 0. Nessun errore DNS in questa esecuzione: il codice d'uscita dopo un errore DNS non è stato verificato.

`qualita_estrazione.py`:
```
OK   file_e_manifest            0        soglia 0
OK   conteggio_unita            1.0      soglia 1.0
OK   testo_vuoto_o_navigazione  0        soglia 0
OK   residui                    0        soglia 0
OK   titolo                     1.0      soglia 0.99
OK   metadati_obbligatori       1.0      soglia 1.0
OK   confronto_testo            1.0      soglia 1.0
OK   accordo_formati            0.9766   soglia 0.9
FAIL riscontro_link             1.0      soglia 1.0   es. atto-art-1: https://www.normattiva.it/uri-res/N2Ls?urn:nir:stato:regio.decreto:1942-03-16;262~art1, disposizioni-sulla-legge-in-generale-art-1 (...;262:1~art1), codice-civile-art-1 (...;262:2~art1)
```
`riscontro_link`: le pagine rispondono 200 (2,6 MB) ma titolo e inizio del testo non vi compaiono (100% assente): il testo dell'articolo è caricato via AJAX e non sta nell'HTML statico del link. Non è un difetto dell'estrazione; da decidere (vedi Decisioni).

### Ripresa (2026-11-12)

Diagnosi corretta del FAIL `riscontro_link`: non era (salvo un caso) il JavaScript. In 4 link su 5 il testo dell'articolo è nell'HTML statico; il controllo falliva perché cercava il titolo composto dall'adattatore ("Codice civile, art. 1418 - Cause di nullità del contratto") come frase identica, mentre il sito scrive "Art. 1418. (Cause di nullità del contratto)". Solo `atto-art-1` (art. 1 del R.D.) ha davvero il testo via JavaScript (verificato in Chrome headless).

Correzioni:
- nucleo `valuta_link.py`: il titolo è presente se ci sono tutte le sue parole (`titolo_presente`); nuova categoria `dinamica` (pagina con script, senza il testo dell'unità né delle vicine) → in `qualita_estrazione.py` `riscontro_link` dà **WARN** (ok, non blocca) "serve un browser" invece di FAIL. Test in `test_qualita.py`.
- adattatore: gli articoli con barra (314/2 … 314/28, 27 abrogati) non hanno URN su normattiva (`~art314/2` apre l'art. 314: errore vero trovato dal controllo corretto); il loro link è la pagina dell'atto con ancora (`…;262#art314-2`).

```
codice-civile: scritte 0, invariate 3323, rimosse 0, divise 30
OK  file_e_manifest, conteggio_unita, testo_vuoto_o_navigazione, residui, titolo,
    metadati_obbligatori, confronto_testo, accordo_formati 0.9766
WARN riscontro_link  non verificabile su 20.0% del campione: il testo arriva via JavaScript (atto-art-1)
```
`valuta_link.py` rifatto: normattiva 200% 100, unità% 80, dinamica 20, assente 0 → link resta `locale` (regola: unità% < 100).

## 6. Benchmark

- Domande: `codice-civile-01` … `codice-civile-08` (tipi: 01 riferimento-esatto, 02 parafrasi, 03 numeri-vicini, 04 titolo-senza-numero, 05 articolo-bis, 06 disposizioni-preliminari, 07 articolo-lungo-diviso, 08 articolo-abrogato)
- `codice-civile-01         rank [2]  top3: codice-civile-art-454.html, codice-civile-art-2043.html, codice-civile-art-342-t`
- `codice-civile-02         rank [1]  top3: codice-civile-art-1418.html, codice-civile-art-1354.html, codice-civile-art-1423`
- `codice-civile-03         rank [1]  top3: codice-civile-art-2050.html, codice-civile-art-2059.html, codice-civile-art-2044`
- `codice-civile-04         rank [1]  top3: codice-civile-art-2697.html, codice-civile-art-1521.html, codice-civile-art-2698`
- `codice-civile-05         rank [1]  top3: codice-civile-art-42-bis.html, codice-civile-art-28.html, codice-civile-art-2500`
- `codice-civile-06         rank [1]  top3: disposizioni-sulla-legge-in-generale-art-12.html, disposizioni-sulla-legge-in-ge`
- `codice-civile-07         rank [1]  top3: codice-civile-art-1129-p1.html, codice-civile-art-2396-decies.html, codice-civil`
- `codice-civile-08         rank [1]  top3: codice-civile-art-314-2.html, disposizioni-sulla-legge-in-generale-art-31.html, `
- `benchmark codice-civile: 100% entro i primi 3 (soglia 80%) → PASS  [risultati-codice-civile-2026-11-12.jsonl]`

## Decisioni prese in autonomia

- URL della fonte: `.../uri-res/N2Ls?urn:nir:stato:regio.decreto:1942-03-16;262` (la homepage non porta all'atto e `adattatore: da scrivere` nella ricognizione).
- Perimetro: gli articoli del Codice civile (3250) + Disposizioni sulla legge in generale (31) + i 2 articoli del R.D. di approvazione, perché sono nello stesso atto (R.D. 262/1942). Le Disposizioni di attuazione sono un atto diverso (R.D. 318/1942): escluse. Se si vuole solo il codice, si tolgono i prefissi `atto-` e `disposizioni-…` dall'adattatore.
- Testo: tolte le note "AGGIORNAMENTO (n)" (nell'art. 5 la risposta è 17,9 KB per 209 caratteri di testo: il testo dell'articolo sarebbe sepolto), i rimandi alle note ("(3a) (15a)", "((289a))") e i segni `((` `))` del testo modificato; resta il testo vigente. Si perde il testo delle note (modifiche storiche, sentenze della Corte costituzionale); si recupera dal link ufficiale.
- Rimandi alle note scritti nel corpo, attaccati al testo (es. "danno (12)"): non tolti (non distinguibili da un elenco "(1)").
- Vigenza: l'AKN si scarica con la `dataVigenza` del link che la pagina stessa offre (oggi); la versione dell'adattatore non dipende dalla data ma dalle versioni degli articoli.
- Link ufficiale `normattiva`: `…;262:<n. allegato>~art<numero>` (`:2~art5` per il codice, `:1~` per le disposizioni, senza parte per l'atto). Le URN senza numero di allegato (`…;262~art5`, `codice.civile:…~art5`) mostrano sempre l'articolo 1 del decreto. Da verificare con `valuta_link.py`.
- `akn-indice` è fuori dal campione di `test_contratto`: ha per costruzione residui (accenti scritti con l'apostrofo, "e'", "integrita'") che il contratto rifiuta; resta provato in `test_normattiva` e la fase 3 lo misura.
- `casi_limite` come parametro della voce in `pipeline.yaml` (articolo più lungo e uno abrogato): sull'indice non si vede la lunghezza né l'abrogazione.
- Ritmo: 0,5 s tra richieste (valore del nucleo) => il formato `articolo` richiede ~3283 richieste, circa 30-40 minuti.
- Difetti del nucleo: nessuno trovato.
- Formato: `articolo` (0 residui, 30/30 completi; akn-indice 16 residui).
- Link: `locale` (regola della skill: unità% normattiva = 0). Da notare: nel campione `valuta_link` il candidato normattiva è 0% anche per gli articoli del codice, mentre la ricognizione prevedeva `…;262:2~art5`; la scelta resta `locale` per regola, ma il generatore del candidato potrebbe non usare la forma con allegato.
- Prima esecuzione di `valuta_link.py` fallita per errore DNS transitorio (rete), rilanciata senza modifiche e passata.
- Fase 5, correzione: residuo `articolo precedente/successivo` limitato alla riga a sé (78 unità: rimando legittimo nel testo di legge, p. es. art. 8); note di aggiornamento inline tagliate alla riga di trattini + rimandi attaccati tolti (9 unità: art. 529, 2142); `breve_ammesso` per i testi brevi con punto finale (art. 968, 30 caratteri); `ins-akn` non spezza più la frase.
- 2026-11-12, su indicazione dell'utente ("se non riesci a sistemare il check li togli", "lo metti come warning"): `riscontro_link` su pagine che caricano il testo via JavaScript è un avviso, non un errore; corretto anche il confronto del titolo (parole, non frase identica). Link degli articoli con barra → pagina dell'atto.
- Fase 5, `riscontro_link` FAIL non risolto dall'adattatore: il link ufficiale non contiene il testo nell'HTML statico (AJAX), 10/10 campioni assenti. Resta aperto per il chiamante.

## Esito

Approvata dall'utente il 2026-11-12. `prova_fonte.py promuovi`: sorgente "Codice civile" caricata e collegata a "Claude - archivio fiscale" (16 → 17 sorgenti); sorgente e agente di prova cancellati. `provvisorio` tolto da `pipeline.yaml`: da ora la aggiorna il notifier.
