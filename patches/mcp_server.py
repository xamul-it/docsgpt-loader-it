"""FastMCP server exposing DocsGPT retrieval over streamable HTTP.

LOCAL PATCH (mounted over the image's file, see the compose file):
``search_docs`` returns distinct documents (up to 20) with their relevant
passages and public links (hybrid vector + Italian keyword search), via ``search_documents`` from the patched
search_service.py, instead of raw, often duplicated chunks. The tool
description is in Italian for the fiscal archive. Upstream copy in
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
from fastmcp.server.dependencies import get_http_headers, get_http_request

from docsgpt.services.search_service import (
    InvalidAPIKey,
    SearchFailed,
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


mcp = FastMCP("docsgpt")


@mcp.tool
async def search_docs(query: str, keywords: str | None = None, max_documents: int = 10) -> list[dict]:
    """Cerca nell'archivio fiscale italiano (prassi di Agenzia Entrate, Ministero
    delle Finanze, Agenzia Dogane e Monopoli; normativa; giurisprudenza
    tributaria; novità del mese in corso) e restituisce documenti DISTINTI,
    dal più pertinente.

    Ricerca ibrida: semantica (per significato) + per parole chiave (testo
    esatto, con normalizzazione italiana di plurali e desinenze).

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

    Per domande complesse fai più ricerche con formulazioni diverse.

    Ogni risultato contiene: title, date (AAAA-MM-GG, se nota), ente, url
    (link pubblico al documento originale: citalo sempre come link),
    similarity (similarità semantica 0-1 del passaggio migliore, se trovato
    per significato) e passages: i frammenti pertinenti, ciascuno con
    found_by ("semantica", "parole chiave" o "entrambe") e similarity.
    I risultati sono già ordinati per pertinenza complessiva.
    """
    api_key = _extract_bearer_token()
    if not api_key:
        raise PermissionError("Missing Bearer token")
    try:
        return await asyncio.to_thread(search_documents, api_key, query, max_documents, keywords)
    except InvalidAPIKey as exc:
        raise PermissionError("Invalid API key") from exc
    except SearchFailed:
        logger.exception("search_docs failed")
        raise
