#!/usr/bin/env python3
"""Test della query rilassata (OR) della ricerca per parole chiave.

Il modulo importa docsgpt, quindi si esegue nel container backend:
  docker cp patches/test_search_service.py docsgpt-backend-1:/tmp/
  docker exec -w /tmp -e PYTHONPATH=/app docsgpt-backend-1 python -m unittest test_search_service -v
"""

import unittest

from docsgpt.services import search_service as s

FREQ = {"cas": 0.30, "8": 0.19, "pag": 0.16, "fatt": 0.14, "anni": 0.09, "tass": 0.07, "vend": 0.054, "fa": 0.043}
DOMANDA = ("'vend' & 'cas' & 'fatt' & 'superbonus' & 'pag' & 'tass' & 'plusvalent' "
           "& 'compr' & '8' & 'anni' & 'fa'")


class QueryRilassata(unittest.TestCase):
    def test_toglie_i_termini_frequenti(self):
        self.assertEqual(s._relaxed_tsquery(DOMANDA, FREQ),
                         "'superbonus' | 'plusvalent' | 'compr' | 'fa'")

    def test_senza_statistiche_mette_tutto_in_or(self):
        self.assertEqual(s._relaxed_tsquery("'a' & 'b'", {}), "'a' | 'b'")

    def test_tutti_frequenti_tiene_i_piu_rari(self):
        self.assertEqual(s._relaxed_tsquery("'cas' & 'anni' & '8' & 'pag'", FREQ), "'anni' | 'pag'")

    def test_un_solo_termine(self):
        self.assertEqual(s._relaxed_tsquery("'cas'", FREQ), "'cas'")

    def test_termini_ripetuti_una_volta(self):
        self.assertEqual(s._relaxed_tsquery("'a' & 'b' & 'a'", {}), "'a' | 'b'")

    def test_apostrofo_nel_lessema(self):
        self.assertEqual(s._relaxed_tsquery("'l''ann' & 'b'", {}), "'l''ann' | 'b'")

    def test_frasi_restano_come_prima(self):
        # "reverse charge" edile -> la frase resta intera, solo & diventa |
        q = "'revers' <-> 'charg' & 'edil'"
        self.assertEqual(s._relaxed_tsquery(q, FREQ), "'revers' <-> 'charg' | 'edil'")

    def test_query_vuota(self):
        self.assertEqual(s._relaxed_tsquery("", FREQ), "")


if __name__ == "__main__":
    unittest.main()
