"""Registro degli adattatori di fonte.

Per aggiungerne uno: copiare _modello.py in <nome>.py, completarlo, creare
fixtures/<nome>/contratto.json e registrarlo qui sotto. test_contratto.py lo
verifica automaticamente.
"""

from __future__ import annotations

from fonte import Adattatore, FonteError

REGISTRO: dict[str, Adattatore] = {}


def registra(ad: Adattatore) -> Adattatore:
    REGISTRO[ad.nome] = ad
    return ad


def adattatore(nome: str) -> Adattatore:
    if nome not in REGISTRO:
        raise FonteError(f"adattatore sconosciuto: {nome} (registrati: {', '.join(sorted(REGISTRO)) or 'nessuno'})")
    return REGISTRO[nome]


def trova(url: str) -> Adattatore | None:
    """Il primo adattatore che riconosce l'URL, o None (= da scrivere)."""
    return next((ad for ad in REGISTRO.values() if ad.riconosce(url)), None)


from adattatori.normattiva import Normattiva  # noqa: E402

registra(Normattiva())
