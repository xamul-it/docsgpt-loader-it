"""Supporto ai test del nucleo: un adattatore finto in memoria, senza rete.

Non è una fonte reale: produce unità sintetiche con i casi che il nucleo deve
gestire (id simili, unità lunga, unità corta ammessa).
"""

from __future__ import annotations

from contextlib import contextmanager

import adattatori
from fonte import FonteError, Unita

LUNGO = "\n\n".join(f"Blocco {i}. " + "parola " * 250 for i in range(8))   # ~ 14.000 caratteri


def unita_finte(n: int = 10) -> list[Unita]:
    us = [Unita(f"u-{i}", f"Unità {i}", ["Parte A", f"Sezione {i // 5}"],
                f"Testo dell'unità {i}. " + "contenuto " * 20 + f"\n\nSecondo blocco dell'unità {i}.",
                {"numero": str(i)}, {"pagina": f"https://fonte.example/u/{i}"}) for i in range(n)]
    us.append(Unita("u-lunga", "Unità lunga", ["Parte A"], LUNGO, {"numero": "L"}, {"pagina": "https://fonte.example/u/L"}))
    us.append(Unita("u-corta", "Unità corta", ["Parte A"], "Abrogata.", {"numero": "C"},
                    {"pagina": "https://fonte.example/u/C"}, breve_ammesso=True))
    return us


class AdattatoreFinto:
    nome = "finto"
    formati = ("intero", "per-unita", "rotto")
    metadati_obbligatori = ("numero",)
    residui = (r"\(\(\d+\)\)",)
    candidati_link = ("pagina",)

    def __init__(self, unita: list[Unita] | None = None, versione: str = "v1"):
        self._unita = unita if unita is not None else unita_finte()
        self._versione = versione

    def riconosce(self, url: str) -> bool:
        return url.startswith("https://fonte.example/")

    def versione(self, rete, cfg) -> str:
        return self._versione

    def attesi(self, rete, cfg) -> list[str]:
        return [u.id for u in self._unita]

    def unita(self, rete, cfg, formato, solo=None):
        if formato == "rotto":
            raise FonteError("risposta HTML invece del formato atteso")
        us = [u for u in self._unita if solo is None or u.id in solo]
        if formato == "per-unita":
            rete.richieste_rete += len(us)
            rete.byte_rete += sum(len(u.testo) for u in us)
        else:
            rete.richieste_rete += 1
            rete.byte_rete += sum(len(u.testo) for u in self._unita)
        return us

    def casi_limite(self, rete, cfg) -> list[str]:
        return ["u-lunga", "u-corta"]


CFG = {"id": "prova", "gruppo": "gruppo", "nome": "Fonte finta", "adattatore": "finto",
       "url": "https://fonte.example/", "formato": "intero", "link": "locale", "soglia_caratteri": 4000,
       "ente": "Ente finto", "tipo_documento": "scheda"}


@contextmanager
def registrato(ad: AdattatoreFinto):
    adattatori.REGISTRO[ad.nome] = ad
    try:
        yield ad
    finally:
        adattatori.REGISTRO.pop(ad.nome, None)
