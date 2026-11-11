"""Modello di adattatore: copiarlo in adattatori/<nome>.py e completarlo.

Un adattatore conosce SOLO la sua fonte e la riduce a una lista di fonte.Unita.
Scrittura dei file, manifest, aggiornamento incrementale, misure, test di
qualità e caricamento li fa il nucleo: qui non vanno.

Regole:
- tutte le richieste passano da `rete.get(url)` (cache su disco; nei test,
  fixture): mai requests diretto;
- id stabile, unico, ASCII minuscolo con trattini (fonte.ID_RE): diventa il
  nome del file; se la fonte ripete un numero in parti diverse, la parte entra
  nell'id;
- testo pulito, blocchi naturali (commi, paragrafi) separati da "\\n\\n": il
  nucleo divide lì le unità lunghe;
- risposta inattesa (pagina di errore, formato diverso) -> FonteError con un
  messaggio chiaro, mai una lista vuota "valida";
- breve_ammesso=True solo per unità legittimamente corte (es. abrogate);
- dipendenze: requests, beautifulsoup4, defusedxml, PyYAML (niente lxml).

Registrazione, in adattatori/__init__.py:
    from adattatori.<nome> import <Classe>
    registra(<Classe>())
Fixture: fixtures/<nome>/contratto.json (vedi test_contratto.py) con poche
risposte reali, compresi i casi difficili; test propri in test_<nome>.py.
"""

from __future__ import annotations

from fonte import FonteError, Rete, Unita  # noqa: F401


class Modello:
    nome = "modello"                       # = nome del file e della cartella delle fixture
    formati: tuple[str, ...] = ("html",)   # il primo è il predefinito; uno per ogni modo di leggere la fonte
    metadati_obbligatori: tuple[str, ...] = ()   # chiavi di Unita.metadati sempre presenti
    residui: tuple[str, ...] = ()          # regex di sporcizia tipica della fonte (oltre a fonte.RESIDUI_COMUNI)
    candidati_link: tuple[str, ...] = ()   # nomi dei link ufficiali per unità (chiavi di Unita.link_ufficiali)

    def riconosce(self, url: str) -> bool:
        """True se l'URL (della fonte o di una sua pagina) è di questa fonte."""
        raise NotImplementedError

    def versione(self, rete: Rete, cfg: dict) -> str:
        """Data o hash che cambia quando la fonte cambia. Usare
        rete.get(url, fresca=True): deve leggere la fonte, non la cache."""
        raise NotImplementedError

    def attesi(self, rete: Rete, cfg: dict) -> list[str]:
        """Id di TUTTE le unità che la fonte dichiara (indice, elenco, export):
        è il riferimento del conteggio al 100%."""
        raise NotImplementedError

    def unita(self, rete: Rete, cfg: dict, formato: str, solo: set[str] | None = None) -> list[Unita]:
        """Le unità lette con il formato indicato, nell'ordine della fonte.
        Con `solo`, solo quelle (per i campioni: evitare di scaricare il resto
        se il formato è per unità)."""
        raise NotImplementedError

    def casi_limite(self, rete: Rete, cfg: dict) -> list[str]:
        """Id da includere sempre nei campioni: la più lunga, una corta
        ammessa, numerazioni particolari, omonimie tra parti."""
        raise NotImplementedError
