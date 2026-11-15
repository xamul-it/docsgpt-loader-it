"""Shared retrieval service used by the HTTP search route and the MCP tool.

LOCAL PATCH (mounted over the image's file, see the compose file): each hit
also carries ``url``, the public link to the original document, built from
DOC_LINKS_FILE like the web UI's source links (retriever/labels.py patch).
Upstream drops the source id before returning, so a client (e.g. Claude via
MCP) could not rebuild the link from ``source`` alone. Everything else is
upstream's file as of image arc53/docsgpt 2026-11-02, plus (below
``search``) ``search_documents`` and the source catalog (``list_sources``,
selection by name or group) used by the MCP tools (copy in
search_service.py.upstream); after an image upgrade, diff against the new one.

Flask-free. Raises domain exceptions (``InvalidAPIKey``, ``SearchFailed``)
that callers translate into their own wire protocol (HTTP status codes,
MCP error responses, etc.).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from sqlalchemy import text as _sql

from docsgpt.core.settings import settings
from docsgpt.retriever.fanout import fetch_per_source
from docsgpt.storage.db.repositories.agents import AgentsRepository
from docsgpt.storage.db.session import db_readonly
from docsgpt.vectorstore.vector_creator import VectorCreator

from docsgpt.retriever.labels import _link_for

logger = logging.getLogger(__name__)


class InvalidAPIKey(Exception):
    """The supplied ``api_key`` does not resolve to an agent."""


class SearchFailed(Exception):
    """Unexpected error during retrieval (e.g. DB outage). Caller maps to 5xx."""


class UnknownSource(ValueError):
    """A requested source/group matches none of the agent's sources."""


def _collect_source_ids(agent: Dict[str, Any]) -> List[str]:
    """Extract the ordered list of source UUIDs to search.

    Prefers ``extra_source_ids`` (PG ARRAY(UUID) of multi-source agents);
    falls back to the legacy single ``source_id`` field.
    """
    source_ids: List[str] = []
    extra = agent.get("extra_source_ids") or []
    for src in extra:
        if src:
            source_ids.append(str(src))
    if not source_ids:
        single = agent.get("source_id")
        if single:
            source_ids.append(str(single))
    return source_ids


def _authorized_source_ids(conn, agent: Dict[str, Any], source_ids: List[str]) -> List[str]:
    """Drop source ids the agent's owner may not read.

    ``_collect_source_ids`` trusts whatever the agent row carries, and this
    service searches those ids directly. That made it the second half of a
    real disclosure: ``/api/share`` resolved a client-supplied source with no
    ownership predicate and baked it into the agent it created, after which
    this path returned another tenant's documents. The share route is fixed,
    but a row written before that — or by any future write path with the same
    gap — is still live here, so re-resolve against the owner rather than
    trusting the stored value.

    Args:
        conn: Open read connection.
        agent: The agent row resolved from the API key.
        source_ids: Ids extracted from that row.

    Returns:
        list: The subset the agent's owner may read.
    """
    owner = agent.get("user_id")
    if not owner:
        logger.warning("Agent %s has no owner; refusing to search its sources.", agent.get("id"))
        return []

    from docsgpt.api.user.team_sharing import can_access

    allowed = []
    for sid in source_ids:
        try:
            permitted = can_access(conn, "source", str(sid), owner)
        except Exception:
            # Fail closed, matching the answer path.
            logger.warning("Access check failed for source %s; dropping it.", sid)
            continue
        if permitted:
            allowed.append(sid)
        else:
            logger.warning(
                "Agent %s references source %s its owner %s cannot read; dropping it.",
                agent.get("id"), sid, owner,
            )
    return allowed


def _search_one(
    source_id: str,
    docsearch: Any,
    query: str,
    k: int,
    query_vector: Optional[List[float]],
) -> Optional[List[Any]]:
    """Search one source, returning its hits or ``None`` when it fails.

    Builds the vector store when not supplied, so each worker thread owns its
    own store instance (and therefore its own DB connection). Errors are logged
    and reported as ``None`` so one broken index cannot take the rest down.
    """
    try:
        if docsearch is None:
            docsearch = VectorCreator.create_vectorstore(
                settings.VECTOR_STORE, source_id, settings.EMBEDDINGS_KEY
            )
        search_kwargs: Dict[str, Any] = {"k": k}
        if query_vector is not None:
            search_kwargs["query_vector"] = query_vector
        return docsearch.search(query, **search_kwargs)
    except Exception as e:
        logger.error(
            f"Error searching vectorstore {source_id}: {e}",
            exc_info=True,
        )
        return None


def _fetch_sources(
    query: str, source_ids: List[str], k: int
) -> List[Optional[List[Any]]]:
    """Fetch every source's hits: one query embedding, one bounded fan-out.

    Shares :func:`~docsgpt.retriever.fanout.fetch_per_source` with
    ``ClassicRAG`` so the search route and the answer path order, embed and
    degrade identically.
    """
    return fetch_per_source(
        source_ids,
        lambda source_id: VectorCreator.create_vectorstore(
            settings.VECTOR_STORE, source_id, settings.EMBEDDINGS_KEY
        ),
        lambda source_id, docsearch, query_vector: _search_one(
            source_id, docsearch, query, k, query_vector
        ),
        lambda _source_id: query,
    )


def _search_sources(
    query: str, source_ids: List[str], chunks: int
) -> List[Dict[str, Any]]:
    """Search across each source's vectorstore and return up to ``chunks`` hits.

    Per-source errors are logged and skipped so one broken index doesn't
    take down the whole search. Results are de-duplicated by content hash.
    """
    if chunks <= 0 or not source_ids:
        return []

    results: List[Dict[str, Any]] = []
    # Blank ids build no store but still count here, so the per-source budget
    # matches what the serial implementation handed each real source.
    chunks_per_source = max(1, chunks // len(source_ids))
    seen_texts: set[int] = set()

    active_ids = [sid for sid in source_ids if sid and sid.strip()]
    if not active_ids:
        return []

    # Fetch every source up front, then merge serially in source order so the
    # dedupe / cap semantics are exactly what they were.
    fetched = _fetch_sources(query, active_ids, chunks_per_source * 2)

    for source_id, docs in zip(active_ids, fetched):
        if docs is None:
            continue

        try:
            for doc in docs:
                if len(results) >= chunks:
                    break

                if hasattr(doc, "page_content") and hasattr(doc, "metadata"):
                    page_content = doc.page_content
                    metadata = doc.metadata
                else:
                    page_content = doc.get("text", doc.get("page_content", ""))
                    metadata = doc.get("metadata", {})

                text_hash = hash(page_content[:200])
                if text_hash in seen_texts:
                    continue
                seen_texts.add(text_hash)

                title = metadata.get("title", metadata.get("post_title", ""))
                if not isinstance(title, str):
                    title = str(title) if title else ""

                if title:
                    title = title.split("/")[-1]
                else:
                    title = metadata.get("filename", page_content[:50] + "...")

                source = metadata.get("source", source_id)

                hit = {
                    "text": page_content,
                    "title": title,
                    "source": source,
                }
                url = _link_for(metadata.get("source_id") or source_id, source)
                if url:
                    hit["url"] = url
                results.append(hit)

            if len(results) >= chunks:
                break

        except Exception as e:
            logger.error(
                f"Error searching vectorstore {source_id}: {e}",
                exc_info=True,
            )
            continue

    return results[:chunks]


def search(api_key: str, query: str, chunks: int = 5) -> List[Dict[str, Any]]:
    """Resolve an agent by API key and search its sources.

    Args:
        api_key: Agent API key (the opaque string stored on
            ``agents.key`` in Postgres).
        query: Free-text search query.
        chunks: Max number of hits to return.

    Returns:
        List of hit dicts with ``text``, ``title``, ``source`` keys.
        Empty list if the agent has no sources configured.

    Raises:
        InvalidAPIKey: if ``api_key`` does not resolve to an agent.
        SearchFailed: on unexpected DB / infrastructure errors.
    """
    if chunks <= 0:
        return []

    try:
        with db_readonly() as conn:
            agent = AgentsRepository(conn).find_by_key(api_key)
            if not agent:
                raise InvalidAPIKey()
            # Authorize inside the same connection the agent was read on.
            source_ids = _authorized_source_ids(
                conn, agent, _collect_source_ids(agent)
            )
    except InvalidAPIKey:
        raise
    except Exception as e:
        raise SearchFailed("agent lookup failed") from e

    if not source_ids:
        return []

    return _search_sources(query, source_ids, chunks)


# ---------------------------------------------------------------------------
# LOCAL PATCH: document-level hybrid search for the MCP tool.
#
# ``search`` above returns raw chunks in *source order* (every hit of the first
# source, then the second, ...) and the same document often appears several
# times. ``search_documents`` instead:
#   1. per source, fetches vector hits (cosine similarity) and Italian
#      full-text hits (``websearch_to_tsquery('italian', ...)``; upstream's
#      ``keyword_search`` uses the English config, useless on Italian text);
#   2. ranks each list across all sources and fuses them with Reciprocal Rank
#      Fusion, like upstream's HybridRetriever but globally;
#   3. groups chunks by document and returns each document once, with its most
#      relevant passages.
# The Italian keyword search uses the stored column ``tsv_it`` (tsvector
# GENERATED ALWAYS AS to_tsvector('italian', text) STORED, GIN index
# ``documents_tsv_it_idx``) when it exists: ranking then reads the stored
# vector instead of recomputing it for every matching chunk, which on the
# largest sources took the relaxed (OR) query past the timeout (8.1 s vs
# 0.36 s measured) and silently returned no keyword hits. Without the column
# it falls back to the expression, backed by ``documents_text_fts_it_idx``.
# ``/api/search`` keeps using ``search`` unchanged.
# ---------------------------------------------------------------------------

import re as _re

MAX_DOCUMENTS = 20
# Hits fetched per source and per method before fusion: enough for
# MAX_DOCUMENTS distinct documents even when each matches with several chunks.
_CHUNKS_PER_SOURCE = 60
_RRF_K = 60
# Keyword hits found only by the relaxed (OR) query rank after every hit of
# the strict (AND) query, by this many places.
_OR_RANK_PENALTY = 60
_KEYWORD_TIMEOUT_MS = 8000
_MAX_PASSAGES_PER_DOC = 3
# Total characters of passage text returned: every document gets its best
# passage; extra passages are added by relevance until this budget runs out.
_TEXT_BUDGET = 80_000
_DATE_RE = _re.compile(r"(\d{4}-\d{2}-\d{2})")
_TS_CONFIG = "italian"
_EXCLUSION_RE = _re.compile(r"(^|\s)-\S")


# Source catalog, from the source lists kept in the repository (loader/fonti,
# mounted read-only): every ``sorgenti`` entry of every *.yaml there that has a
# ``nome`` gives that source its ``gruppo`` (the entry's, else the file's
# top-level one), ``descrizione`` and ``peso`` (weight of its hits in the fused
# ranking, default 1). Read on every call: the files are small and edits apply
# without a restart.
_CATALOG_DIR = os.environ.get("CATALOGO_FONTI", "/app/fonti")


def _weight(value: Any, path: str) -> float:
    """A catalog ``peso``: a positive number, else 1 (with a warning if set)."""
    if value is None:
        return 1.0
    try:
        weight = float(value)
    except (TypeError, ValueError):
        weight = 0.0
    if weight > 0:
        return weight
    logger.warning(f"Source catalog {path}: invalid peso {value!r}, using 1")
    return 1.0


def _catalog() -> Dict[str, Dict[str, Any]]:
    import glob

    import yaml

    catalog: Dict[str, Dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(_CATALOG_DIR, "*.yaml"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            file_group = data.get("gruppo")
            for entry in data.get("sorgenti") or []:
                if isinstance(entry, dict) and entry.get("nome"):
                    catalog[str(entry["nome"])] = {
                        "gruppo": entry.get("gruppo") or file_group,
                        "descrizione": entry.get("descrizione"),
                        "peso": _weight(entry.get("peso"), path),
                    }
        except Exception as e:
            logger.warning(f"Source catalog {path} unreadable: {e}")
    return catalog


def _agent_sources(api_key: str) -> List[Dict[str, Any]]:
    """The agent's authorized sources, in agent order, with name and group.

    Each item: ``id``, ``nome``, ``gruppo``, ``descrizione``, ``aggiornata``
    (date of the last load, AAAA-MM-GG), ``peso``. Group, description and
    weight come from the catalog; a source missing from it falls back to the
    "<gruppo> - <nome>" naming convention, then to its own name, and weight 1.
    """
    try:
        with db_readonly() as conn:
            agent = AgentsRepository(conn).find_by_key(api_key)
            if not agent:
                raise InvalidAPIKey()
            ids = [s for s in _authorized_source_ids(conn, agent, _collect_source_ids(agent))
                   if s and s.strip()]
            rows = conn.execute(
                _sql("SELECT id::text AS id, name, date FROM sources WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": ids},
            ).mappings().all() if ids else []
    except InvalidAPIKey:
        raise
    except Exception as e:
        raise SearchFailed("agent lookup failed") from e
    by_id = {r["id"]: r for r in rows}
    catalog = _catalog()
    out = []
    for sid in ids:
        row = by_id.get(sid)
        name = row["name"] if row else sid
        entry = catalog.get(name) or {}
        group = entry.get("gruppo") or (name.split(" - ", 1)[0] if " - " in name else name)
        out.append({
            "id": sid,
            "nome": name,
            "gruppo": group,
            "descrizione": entry.get("descrizione"),
            "aggiornata": row["date"].date().isoformat() if row and row["date"] else None,
            "peso": entry.get("peso", 1.0),
        })
    return out


def list_sources(api_key: str) -> List[Dict[str, Any]]:
    """The sources the caller can search, sorted by group and name."""
    sources = _agent_sources(api_key)
    return sorted(({k: v for k, v in s.items() if k not in ("id", "peso")} for s in sources),
                  key=lambda s: (s["gruppo"].casefold(), s["nome"].casefold()))


def _select_sources(sources: List[Dict[str, Any]], wanted: Optional[List[str]]) -> List[Dict[str, Any]]:
    """Sources whose name or group matches one of ``wanted`` (case-insensitive).

    The part of a name before " - " also matches: a source split in parts
    ("Normativa - Decreti", "Normativa - Comunicati", ...) is selected whole
    by its prefix ("Normativa"), whatever its group. ``None`` or empty selects
    everything. A name matching nothing raises
    ``UnknownSource`` listing what is available, so the caller can retry
    instead of reading an empty result as "nothing found".
    """
    wanted = [w.strip() for w in (wanted or []) if w and w.strip()]
    if not wanted:
        return sources
    selected, unknown = [], []
    for w in wanted:
        key = w.casefold()
        hits = [s for s in sources
                if key in (s["nome"].casefold(), s["gruppo"].casefold(),
                           s["nome"].split(" - ", 1)[0].casefold())]
        if not hits:
            unknown.append(w)
        selected += [s for s in hits if s not in selected]
    if unknown:
        names = "; ".join(f'{s["nome"]} (gruppo {s["gruppo"]})' for s in sources)
        raise UnknownSource(
            f"Fonte sconosciuta: {', '.join(unknown)}. Fonti disponibili: {names}"
        )
    return selected


_TSV_COLUMN = "tsv_it"
_tsv_column_cache: Dict[str, bool] = {}


def _has_tsv_column(docsearch: Any, table: str) -> bool:
    """Whether ``table`` has the stored tsvector column (checked once per process)."""
    if table not in _tsv_column_cache:
        conn = docsearch._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
                (table, _TSV_COLUMN),
            )
            _tsv_column_cache[table] = cursor.fetchone() is not None
            conn.commit()
        except Exception as e:
            logger.warning(f"tsvector column check failed on {table}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        finally:
            cursor.close()
    return _tsv_column_cache[table]


# Relaxed (OR) query: lexemes found in more than this fraction of the chunks
# are left out. A natural-language question OR-ed in full ("vendo casa fatto
# pago anni fa ...") matched 63% of Normativa, and ranking all of it took
# 3 s alone and past the timeout under concurrent searches (issue #4); the
# rare terms alone take 0.06-0.2 s, and common terms barely move ts_rank_cd.
_OR_MAX_FREQ = 0.05
# ...but at least this many lexemes are kept (the rarest), so a question
# made only of common words still has a keyword half.
_OR_MIN_TERMS = 2
# Lexeme frequencies come from the column statistics (pg_stats, refreshed by
# ANALYZE): reread after this many seconds.
_FREQ_TTL_S = 3600
_freq_cache: Dict[str, tuple] = {}
_LEXEME_RE = _re.compile(r"'((?:[^']|'')*)'")


def _lexeme_freqs(docsearch: Any, table: str) -> Dict[str, float]:
    """``{lexeme: fraction of rows}`` for the most common lexemes of the tsvector column."""
    import time

    cached = _freq_cache.get(table)
    if cached and time.monotonic() - cached[0] < _FREQ_TTL_S:
        return cached[1]
    freqs: Dict[str, float] = {}
    conn = docsearch._get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT most_common_elems::text::text[], most_common_elem_freqs FROM pg_stats "
            "WHERE tablename = %s AND attname = %s",
            (table, _TSV_COLUMN),
        )
        row = cursor.fetchone()
        if row and row[0] and row[1]:
            # most_common_elem_freqs ends with 3 extra values (min, max, null
            # frequency): zip stops at the last lexeme.
            freqs = dict(zip(row[0], row[1]))
        conn.commit()
    except Exception as e:
        logger.warning(f"Lexeme statistics unavailable on {table}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        cursor.close()
    _freq_cache[table] = (time.monotonic(), freqs)
    return freqs


def _relaxed_tsquery(strict: str, freqs: Dict[str, float]) -> str:
    """OR form of the strict tsquery text, without the most common lexemes.

    Lexemes above ``_OR_MAX_FREQ`` are dropped, keeping at least the
    ``_OR_MIN_TERMS`` rarest; lexemes missing from ``freqs`` count as rare.
    A query with phrases (``<->``) keeps every term and only has its ``&``
    turned into ``|``, as before: those come from keywords written by the
    caller, short and chosen on purpose.
    """
    if "<" in strict:
        return strict.replace(" & ", " | ")
    lexemes: List[str] = []
    for lex in _LEXEME_RE.findall(strict):
        if lex not in lexemes:
            lexemes.append(lex)
    kept = [lex for lex in lexemes if freqs.get(lex.replace("''", "'"), 0.0) <= _OR_MAX_FREQ]
    if len(kept) < min(_OR_MIN_TERMS, len(lexemes)):
        rarest = sorted(lexemes, key=lambda lex: freqs.get(lex.replace("''", "'"), 0.0))
        rarest = set(rarest[:_OR_MIN_TERMS])
        kept = [lex for lex in lexemes if lex in rarest]
    return " | ".join(f"'{lex}'" for lex in kept)


def _keyword_hits(docsearch: Any, source_id: str, text: str, k: int) -> List[tuple]:
    """Italian full-text hits of one pgvector source as ``(text, metadata, relaxed)``.

    Strict query first (all terms, phrases, exclusions as written); when it
    finds nothing, the terms joined by OR without the most common ones (see
    ``_relaxed_tsquery``), flagged ``relaxed`` (skipped when the text has
    exclusions: OR-ing "-term" would match almost anything).
    Non-pgvector stores return ``[]``.
    """
    if not hasattr(docsearch, "_get_connection") or not getattr(docsearch, "_table_name", None):
        return []
    table = docsearch._table_name
    text_col = getattr(docsearch, "_text_column", "text")
    meta_col = getattr(docsearch, "_metadata_column", "metadata")
    has_tsv = _has_tsv_column(docsearch, table)
    tsv = _TSV_COLUMN if has_tsv else f"to_tsvector('{_TS_CONFIG}'::regconfig, {text_col})"
    freqs = _lexeme_freqs(docsearch, table) if has_tsv else {}
    sql = (
        f"SELECT {text_col}, {meta_col} FROM {table}, (SELECT %s::tsquery) AS q(query) "
        f"WHERE source_id = %s AND numnode(q.query) > 0 AND {tsv} @@ q.query "
        f"ORDER BY ts_rank_cd({tsv}, q.query) DESC LIMIT %s"
    )
    conn = docsearch._get_connection()
    cursor = conn.cursor()
    hits: List[tuple] = []
    try:
        cursor.execute(f"SET LOCAL statement_timeout = {int(_KEYWORD_TIMEOUT_MS)}")
        cursor.execute(f"SELECT websearch_to_tsquery('{_TS_CONFIG}'::regconfig, %s)::text", (text,))
        strict = cursor.fetchone()[0] or ""
        attempts = [(strict, False)]
        if not _EXCLUSION_RE.search(text):
            relaxed = _relaxed_tsquery(strict, freqs)
            if relaxed and relaxed != strict:
                attempts.append((relaxed, True))
        for query_text, is_relaxed in attempts:
            cursor.execute(sql, (query_text, source_id, k))
            rows = cursor.fetchall()
            hits = [(t, m or {}, is_relaxed) for t, m in rows]
            if hits:
                break
        conn.commit()
    except Exception as e:
        logger.warning(f"Keyword search failed on {source_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        hits = []
    finally:
        cursor.close()
    return hits


# ---------------------------------------------------------------------------
# Explicit references ("circolare 21/E del 2016", "d.lgs. 472/1997"), issue #5.
# The full-text half cannot tell the document that *is* circolare 21/E 2016
# from the dozens that cite it, but the loader names every file
# "<date>_<Type>_<number>_<title>", so the cited act is found by its path.
# Its first passage goes on top of the results, found_by "riferimento".
# ---------------------------------------------------------------------------

# (pattern, type as written in the file names); decreto-legge before legge.
_REF_TYPES = (
    (r"decreto[\s-]+legge|d\.\s?l\.|dl", "Decreto-legge"),
    (r"decreto\s+legislativo|d\.\s?lgs\.?|dlgs\.?", "Decreto_legislativo"),
    (r"decreto\s+del\s+presidente\s+della\s+repubblica|d\.\s?p\.\s?r\.?|dpr",
     "Decreto_del_Presidente_della_Repubblica"),
    (r"circolar[ei]|circ\.", "Circolare"),
    (r"risoluzion[ei]|ris\.", "Risoluzione"),
    (r"rispost[ae]\s+(?:ad?\s+)?interpell[oi]|interpell[oi]|rispost[ae]", "Interpello"),
    (r"comunicat[oi]\s+stampa|comunicat[oi]", "Comunicato_Stampa"),
    (r"sentenz[ae]|sent\.", "Sentenza"),
    (r"ordinanz[ae]|ord\.", "Ordinanza"),
    (r"legge|l\.", "Legge"),
)
_REF_RE = _re.compile(
    r"(?<![\w.])(?:" + "|".join(f"(?P<t{i}>{p})" for i, (p, _) in enumerate(_REF_TYPES)) + r")"
    r"(?![a-zà-ù])\s*(?:n(?:um(?:ero)?)?\s*[.°º]?\s*)?"
    r"(?P<num>\d{1,6})(?![\d,]|\.\d)"
    r"(?:\s*/\s*(?P<lett>[a-z])(?![a-z]))?"
    r"(?:\s*/\s*(?P<anno>(?:19|20)\d{2})(?!\d))?",
    _re.I,
)
_YEAR_RE = _re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
# How far after the number the year may be: "21/E del 25 maggio 2016".
_REF_YEAR_WINDOW = 40
_REF_MAX = 3
_REF_MAX_DOCS = 5


def _parse_references(text: str) -> List[Dict[str, Optional[str]]]:
    """Acts cited by type and number (and year, when given) in ``text``."""
    matches = list(_REF_RE.finditer(text or ""))
    refs: List[Dict[str, Optional[str]]] = []
    for i, m in enumerate(matches):
        tipo = next(_REF_TYPES[int(g[1:])][1] for g, v in m.groupdict().items() if g.startswith("t") and v)
        anno = m.group("anno")
        if not anno:
            # The year may follow ("del 2016", "del 25/5/2016"), up to the next reference.
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            y = _YEAR_RE.search(text[m.end():min(end, m.end() + _REF_YEAR_WINDOW)])
            anno = y.group(1) if y else None
        lett = m.group("lett")
        ref = {"tipo": tipo, "numero": m.group("num"), "lettera": lett.upper() if lett else None, "anno": anno}
        if ref not in refs:
            refs.append(ref)
    return refs[:_REF_MAX]


def _reference_path_regex(ref: Dict[str, Optional[str]]) -> str:
    """Postgres regex on the document path: "2016-05-25_Circolare_21_..." for circolare 21/E 2016.

    The Agenzia delle Entrate names its acts "21/E" but its files carry the
    bare number; ADM files keep the letter ("Circolare_78-D_").
    """
    year = ref["anno"] or "[0-9]{4}"
    num, lett = ref["numero"], ref["lettera"]
    if lett is None:
        num_rx = f"{num}(-[A-Z])?_"
    elif lett == "E":
        num_rx = f"{num}(-E)?_"
    else:
        num_rx = f"{num}-{lett}_"
    return f"(^|/){year}-[0-9]{{2}}-[0-9]{{2}}_{ref['tipo']}_{num_rx}"


def _reference_hits(docsearch: Any, source_id: str, refs: List[Dict[str, Optional[str]]]) -> List[tuple]:
    """First passage of each document of the source matching a reference, as ``(text, metadata)``."""
    if not refs or not hasattr(docsearch, "_get_connection") or not getattr(docsearch, "_table_name", None):
        return []
    table = docsearch._table_name
    text_col = getattr(docsearch, "_text_column", "text")
    meta_col = getattr(docsearch, "_metadata_column", "metadata")
    path = f"({meta_col}->>'source')"
    sql = (
        f"SELECT {text_col}, {meta_col} FROM ("
        f"SELECT DISTINCT ON ({path}) id, {text_col}, {meta_col} FROM {table} "
        f"WHERE source_id = %s AND {path} ~ %s ORDER BY {path}, id"
        f") d ORDER BY {path} DESC LIMIT %s"
    )
    conn = docsearch._get_connection()
    cursor = conn.cursor()
    hits: List[tuple] = []
    try:
        cursor.execute(f"SET LOCAL statement_timeout = {int(_KEYWORD_TIMEOUT_MS)}")
        for ref in refs:
            cursor.execute(sql, (source_id, _reference_path_regex(ref), _REF_MAX_DOCS))
            hits += [(t, m or {}) for t, m in cursor.fetchall()]
        conn.commit()
    except Exception as e:
        logger.warning(f"Reference lookup failed on {source_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        hits = []
    finally:
        cursor.close()
    return hits


def _search_one_hybrid(
    source_id: str,
    docsearch: Any,
    query: str,
    keywords: str,
    k: int,
    query_vector: Optional[List[float]],
    refs: Optional[List[Dict[str, Optional[str]]]] = None,
) -> Optional[tuple]:
    """``(vector_pairs, keyword_hits, reference_hits)`` for one source, or None on failure."""
    try:
        if docsearch is None:
            docsearch = VectorCreator.create_vectorstore(
                settings.VECTOR_STORE, source_id, settings.EMBEDDINGS_KEY
            )
        kwargs: Dict[str, Any] = {"k": k}
        if query_vector is not None:
            kwargs["query_vector"] = query_vector
        if hasattr(docsearch, "search_with_scores"):
            vector = list(docsearch.search_with_scores(query, **kwargs))
        else:
            vector = [(doc, None) for doc in docsearch.search(query, **kwargs)]
        keyword = _keyword_hits(docsearch, source_id, keywords, k) if keywords else []
        return vector, keyword, _reference_hits(docsearch, source_id, refs or [])
    except Exception as e:
        logger.error(f"Error searching vectorstore {source_id}: {e}", exc_info=True)
        return None


def _doc_fields(doc: Any) -> tuple:
    """``(text, metadata)`` of a hit, whether a Document or a plain dict."""
    if hasattr(doc, "page_content"):
        return doc.page_content, getattr(doc, "metadata", None) or {}
    if isinstance(doc, dict):
        return doc.get("text", doc.get("page_content", "")), doc.get("metadata") or {}
    return "", {}


def _fuse(
    vector_list: List[tuple],
    keyword_list: List[tuple],
    reference_list: List[tuple],
    weight: Any,
) -> Dict[tuple, float]:
    """Fused score of each chunk key.

    ``vector_list``: ``(similarity, key)``; ``keyword_list``: ``(rank in its
    source, source order, key)``; ``reference_list``: keys of the cited acts,
    in order; ``weight(key)``: the ``peso`` of the source the chunk comes from.

    Global ranks. Vector: by similarity across sources. Keyword: ts_rank is
    not comparable across sources, so sources are interleaved by in-source
    rank. The two Reciprocal Rank Fusion terms are multiplied by the source
    weight; the ranks themselves are not, so a weight only reorders hits of
    sources with different weights. The acts the question names come first,
    ahead of any fused score (< 2/_RRF_K), whatever their source's weight.
    """
    rrf: Dict[tuple, float] = {}
    for rank, (_sim, key) in enumerate(sorted(vector_list, key=lambda v: v[0], reverse=True)):
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (_RRF_K + rank)
    for rank, (_r, _s, key) in enumerate(sorted(keyword_list, key=lambda v: (v[0], v[1]))):
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (_RRF_K + rank)
    fused = {key: score * weight(key) for key, score in rrf.items()}
    for rank, key in enumerate(reference_list):
        fused[key] = fused.get(key, 0.0) + 1.0 - rank / 1000.0
    return fused


def search_documents(
    api_key: str,
    query: str,
    max_documents: int = 10,
    keywords: Optional[str] = None,
    sources: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Hybrid search over the agent's sources, returning distinct documents.

    ``keywords`` drives the full-text half (websearch syntax: "exact phrase",
    OR, -exclude); when omitted, ``query`` is used for both halves.
    ``sources`` restricts the search to the named sources or groups (see
    ``_select_sources``); omitted, every source of the agent is searched.

    Each result: ``title``, ``date`` (from the file name, when present),
    ``fonte`` (source name), ``ente`` (archive folder), ``url`` (public link,
    when known), ``source``
    (path inside the DocsGPT source), ``similarity`` (best cosine similarity
    among its passages, when found semantically), ``passages``: the most
    relevant excerpts, each with ``found_by`` (semantica / parole chiave /
    entrambe / riferimento) and ``similarity``. Results are ordered by fused
    rank, after the acts the question cites by type, number and year
    (``found_by`` "riferimento", see ``_parse_references``).
    """
    max_documents = max(1, min(int(max_documents or 10), MAX_DOCUMENTS))
    keywords = (keywords or query or "").strip()
    selected = _select_sources(_agent_sources(api_key), sources)
    source_ids = [s["id"] for s in selected]
    if not source_ids:
        return []
    source_names = {s["id"]: s["nome"] for s in selected}
    refs = _parse_references(f"{query} {keywords}")

    fetched = fetch_per_source(
        source_ids,
        lambda sid: VectorCreator.create_vectorstore(
            settings.VECTOR_STORE, sid, settings.EMBEDDINGS_KEY
        ),
        lambda sid, docsearch, qv: _search_one_hybrid(
            sid, docsearch, query, keywords, _CHUNKS_PER_SOURCE, qv, refs
        ),
        lambda _sid: query,
    )

    # chunk key -> data; chunk key = (source_id, document path, text head)
    chunks: Dict[tuple, Dict[str, Any]] = {}
    vector_list: List[tuple] = []   # (similarity, chunk key)
    keyword_list: List[tuple] = []  # (source order, rank in source, relaxed, chunk key)
    reference_list: List[tuple] = []  # chunk keys of the cited acts, in order

    def _register(sid: str, text: str, metadata: Dict[str, Any]) -> tuple:
        doc_sid = metadata.get("source_id") or sid
        source = metadata.get("source") or sid
        key = (doc_sid, source, text.strip()[:300])
        if key not in chunks:
            chunks[key] = {"text": text, "metadata": metadata, "sid": doc_sid,
                           "source": source, "similarity": None, "by": set(),
                           "found_in": sid}
        return key

    for s_idx, (sid, result) in enumerate(zip(source_ids, fetched)):
        if not result:
            continue
        vector, keyword, reference = result
        for text, metadata in reference:
            if not text:
                continue
            key = _register(sid, text, metadata)
            chunks[key]["by"].add("riferimento")
            reference_list.append(key)
        for doc, score in vector:
            text, metadata = _doc_fields(doc)
            if not text:
                continue
            key = _register(sid, text, metadata)
            sim = None if score is None else round(float(score), 3)
            chunks[key]["similarity"] = sim
            chunks[key]["by"].add("semantica")
            vector_list.append((sim if sim is not None else -1.0, key))
        for rank, (text, metadata, relaxed) in enumerate(keyword):
            if not text:
                continue
            key = _register(sid, text, metadata)
            chunks[key]["by"].add("parole chiave")
            keyword_list.append((rank + (_OR_RANK_PENALTY if relaxed else 0), s_idx, key))

    weights = {s["id"]: s.get("peso", 1.0) for s in selected}
    fused = _fuse(vector_list, keyword_list, reference_list,
                  lambda key: weights.get(chunks[key]["found_in"], 1.0))

    docs: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    extra: List[tuple] = []
    seen_texts: set = set()
    for key in sorted(fused, key=lambda k: fused[k], reverse=True):
        c = chunks[key]
        text_hash = hash(c["text"].strip()[:300])
        if text_hash in seen_texts:
            continue
        seen_texts.add(text_hash)
        by = c["by"]
        passage = {
            "text": c["text"],
            "found_by": ("riferimento" if "riferimento" in by
                         else "entrambe" if len(by) > 1 else next(iter(by))),
            "similarity": c["similarity"],
        }
        doc_key = (c["sid"], c["source"])
        if doc_key in docs:
            extra.append((doc_key, passage))
            continue
        if len(order) >= max_documents:
            continue
        metadata = c["metadata"]
        title = metadata.get("title") or metadata.get("post_title") or str(c["source"])
        title = str(title).split("/")[-1]
        url = _link_for(c["sid"], c["source"])
        ente = url.split("/docs/", 1)[1].split("/", 1)[0] if url and "/docs/" in url else None
        date = _DATE_RE.search(title)
        docs[doc_key] = {
            "title": title,
            "date": date.group(1) if date else None,
            "fonte": source_names.get(c["sid"]),
            "ente": ente,
            "url": url,
            "source": c["source"],
            "similarity": c["similarity"],
            "passages": [passage],
        }
        order.append(doc_key)

    budget = _TEXT_BUDGET - sum(len(docs[k]["passages"][0]["text"]) for k in order)
    for doc_key, passage in extra:
        doc = docs.get(doc_key)
        if doc is None or len(doc["passages"]) >= _MAX_PASSAGES_PER_DOC:
            continue
        if len(passage["text"]) > budget:
            continue
        doc["passages"].append(passage)
        budget -= len(passage["text"])
        sims = [p["similarity"] for p in doc["passages"] if p["similarity"] is not None]
        doc["similarity"] = max(sims) if sims else None

    return [docs[k] for k in order]
