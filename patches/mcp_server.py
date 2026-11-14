"""FastMCP server exposing DocsGPT retrieval over streamable HTTP.

LOCAL PATCH (mounted over the image's file, see the compose file):
``search_docs`` returns distinct documents (up to 20) with their relevant
passages and public links (hybrid vector + Italian keyword search), via
``search_documents`` from the patched search_service.py, instead of raw,
often duplicated chunks, and can be restricted to some sources;
``list_sources`` lists the sources the key can search. Tool descriptions are
in Italian. Documented in docs/mcp-server.md. Upstream copy in
mcp_server.py.upstream; after an image upgrade, diff against the new one.

Mounted at ``/mcp`` by ``docsgpt/asgi.py``. Bearer tokens are the
existing DocsGPT agent API keys — no new credential surface. The
``search_docs`` tool searches the caller's knowledge base.

The tool reads the ``Authorization`` header directly via
``get_http_headers(include={"authorization"})``. The ``include`` kwarg
is required: by default ``get_http_headers`` strips ``authorization``
(and a handful of other hop-by-hop headers) so they aren't forwarded
to downstream services — since we deliberately want the caller's
token, we opt it back in.
"""

from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers, get_http_request

from docsgpt.services.search_service import (
    InvalidAPIKey,
    SearchFailed,
    UnknownSource,
    list_sources as _list_sources,
    search_documents,
)

logger = logging.getLogger(__name__)


def _extract_bearer_token() -> str | None:
    auth = get_http_headers(include={"authorization"}).get("authorization", "")
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
        return parts[1]
    # Fallback for clients that cannot send headers (claude.ai custom
    # connectors): the key in the connector URL, /mcp/?key=<api key>.
    try:
        return get_http_request().query_params.get("key") or None
    except RuntimeError:
        return None


def _require_key() -> str:
    api_key = _extract_bearer_token()
    if not api_key:
        raise PermissionError("Missing Bearer token")
    return api_key


mcp = FastMCP("docsgpt")


@mcp.tool
async def list_sources() -> list[dict]:
    """Elenca le fonti dell'archivio documentale consultabili con search_docs.

    Usalo per sapere che fonti ci sono (per esempio se l'utente chiede quali
    fonti sono attive o che cosa copre l'archivio) e per scegliere quelle da
    passare a search_docs in `sources`.

    Ogni voce contiene: nome (da usare in `sources`), gruppo (raggruppa più
    fonti: anche il gruppo si può usare in `sources`), descrizione del
    contenuto (se nota) e aggiornata (data dell'ultimo caricamento,
    AAAA-MM-GG).
    """
    api_key = _require_key()
    try:
        return await asyncio.to_thread(_list_sources, api_key)
    except InvalidAPIKey as exc:
        raise PermissionError("Invalid API key") from exc
    except SearchFailed:
        logger.exception("list_sources failed")
        raise


@mcp.tool
async def search_docs(
    query: str,
    keywords: str | None = None,
    max_documents: int = 10,
    sources: list[str] | None = None,
) -> list[dict]:
    """Cerca nell'archivio documentale (fonti di temi diversi: prassi e
    normativa fiscale, giurisprudenza, codici, locazioni, …; l'elenco
    completo con le descrizioni è in list_sources) e restituisce documenti
    DISTINTI, dal più pertinente.

    Ricerca ibrida: semantica (per significato) + per parole chiave (testo
    esatto, con normalizzazione italiana di plurali e desinenze).

    Scelta delle fonti: se la domanda nomina una fonte o riguarda
    chiaramente solo alcune fonti (es. "cosa prevede il codice civile…"),
    cerca prima solo lì passando `sources`; poi, se i risultati non bastano
    o servono altri punti di vista (prassi, giurisprudenza…), rifai la
    ricerca su altre fonti o senza `sources`. Senza `sources` si cerca in
    tutte le fonti.

    Args:
        query: cosa cercare, come domanda o con i termini tecnici che compaiono
            nei documenti (es. "inversione contabile cessione biometano").
        keywords: facoltativo, termini da trovare testualmente: numeri di atti,
            articoli, sigle, espressioni precise. Sintassi: "frase esatta",
            OR tra alternative, -parola per escludere (es. '"articolo 17-ter"
            split payment', 'circolare 14/E OR "risoluzione 38"'). Se assente
            si usa query. Se nessun documento contiene tutti i termini, vengono
            accettati anche documenti che ne contengono solo alcuni.
        max_documents: numero massimo di documenti distinti (1-20, default 10).
        sources: facoltativo, nomi di fonti o di gruppi presi da list_sources
            (es. ["Codice civile"], ["Locazioni"], ["Agenzia Entrate",
            "Giurisprudenza"]); maiuscole e minuscole indifferenti. Un nome
            sconosciuto dà errore con l'elenco delle fonti disponibili.

    Per domande complesse fai più ricerche con formulazioni diverse.

    Ogni risultato contiene: title, date (AAAA-MM-GG, se nota), fonte (nome
    della fonte), ente, url (link pubblico al documento originale: citalo
    sempre come link), similarity (similarità semantica 0-1 del passaggio
    migliore, se trovato per significato) e passages: i frammenti
    pertinenti, ciascuno con found_by ("semantica", "parole chiave",
    "entrambe" o "riferimento") e similarity. I risultati sono già ordinati
    per pertinenza complessiva. Se la domanda o le keywords citano un atto
    per tipo, numero e anno ("circolare 21/E del 2016", "d.lgs. 472/1997"),
    l'atto viene per primo con found_by "riferimento". Serve la parola del
    tipo e conviene l'anno ("21/E" da solo non basta). Se manca, l'atto non
    è nell'archivio: non usare al suo posto un documento con lo stesso
    numero. Se l'atto è descritto senza numero, ricava numero e anno dai
    risultati (es. un comunicato che annuncia "la circolare n. 9/E") e
    rifai la ricerca con il riferimento completo.
    """
    api_key = _require_key()
    try:
        return await asyncio.to_thread(
            search_documents, api_key, query, max_documents, keywords, sources
        )
    except InvalidAPIKey as exc:
        raise PermissionError("Invalid API key") from exc
    except UnknownSource as exc:
        raise ToolError(str(exc)) from exc
    except SearchFailed:
        logger.exception("search_docs failed")
        raise
