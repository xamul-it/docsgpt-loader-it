"""Shared chunk-label derivation for retrievers.

LOCAL PATCH (mounted over the image's file, see the compose file): adds a
``link`` to each chunk label so the web UI's source boxes open the original
document. The UI only makes a box clickable when the source carries ``link``,
and upstream never sets it for uploaded files. Everything else is upstream's
labels.py as of image arc53/docsgpt 2026-10-01; after an image upgrade, diff
this file against the new upstream one.

Links come from DOC_LINKS_FILE, written by carica_docsgpt.py and the notifier:
``{source_id: {"base": "https://host/docs/<ente>", "depth": 1}}``. A chunk whose
path in the source is ``a/b/c.pdf`` links to ``<base>/a/files/b/c.pdf``:
``depth`` is how many leading components precede the archive's ``files`` folder.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib.parse import quote

_LINKS_FILE = os.environ.get("DOC_LINKS_FILE", "/app/doc_links/doc_links.json")
_links_cache: Dict[str, Any] = {"mtime": None, "data": {}}


def _doc_links() -> Dict[str, Any]:
    try:
        mtime = os.path.getmtime(_LINKS_FILE)
        if mtime != _links_cache["mtime"]:
            with open(_LINKS_FILE, encoding="utf-8") as fh:
                _links_cache["data"] = json.load(fh)
            _links_cache["mtime"] = mtime
    except (OSError, ValueError):
        return {}
    return _links_cache["data"]


def _link_for(source_id: Any, rel_path: Any) -> Optional[str]:
    cfg = _doc_links().get(str(source_id))
    if not cfg or not isinstance(rel_path, str) or "://" in rel_path:
        return None
    parts = [p for p in rel_path.split("/") if p]
    depth = int(cfg.get("depth", 1))
    if len(parts) <= depth:
        return None
    path = "/".join(parts[:depth] + ["files"] + parts[depth:])
    return f"{str(cfg['base']).rstrip('/')}/{quote(path)}"


def labels_from_metadata(
    metadata: Dict[str, Any], text: str, fallback_source: str
) -> Dict[str, str]:
    """Derive ``title``/``source``/``filename`` from a chunk's metadata.

    Falls back to the chunk text for the title and to ``fallback_source`` (the
    vectorstore/source id) when metadata carries no source. Used by both
    ClassicRAG and GraphRAG so citation labels stay identical across retrievers.
    """
    metadata = metadata or {}

    title = metadata.get("title", metadata.get("post_title", text))
    if not isinstance(title, str):
        title = str(title)
    title = title.split("/")[-1]

    filename = (
        metadata.get("filename")
        or metadata.get("file_name")
        or metadata.get("source")
    )
    if isinstance(filename, str):
        filename = os.path.basename(filename) or filename
    else:
        filename = title
    if not filename:
        filename = title

    source = metadata.get("source") or fallback_source
    labels = {"title": title, "source": source, "filename": filename}
    link = _link_for(metadata.get("source_id") or fallback_source, metadata.get("source"))
    if link:
        labels["link"] = link
    return labels
