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


class Riferimenti(unittest.TestCase):
    """Atti citati per tipo, numero e anno (issue #5)."""

    def refs(self, text):
        return [(r["tipo"], r["numero"], r["lettera"], r["anno"]) for r in s._parse_references(text)]

    def test_circolare_con_lettera_e_anno(self):
        self.assertEqual(self.refs("Cosa dice la circolare 21/E del 2016 dell'Agenzia delle Entrate?"),
                         [("Circolare", "21", "E", "2016")])

    def test_numero_con_n_e_data_estesa(self):
        self.assertEqual(self.refs("risoluzione n. 38 del 23 ottobre 2026"), [("Risoluzione", "38", None, "2026")])

    def test_anno_dopo_la_barra(self):
        self.assertEqual(self.refs("art. 13 d.lgs. 472/1997 e legge 190/2014"),
                         [("Decreto_legislativo", "472", None, "1997"), ("Legge", "190", None, "2014")])

    def test_risposta_a_interpello(self):
        self.assertEqual(self.refs("la risposta a interpello n. 543 del 2022"), [("Interpello", "543", None, "2022")])

    def test_decreto_legge_non_e_una_legge(self):
        self.assertEqual(self.refs("decreto-legge 84/2025"), [("Decreto-legge", "84", None, "2025")])

    def test_senza_anno(self):
        self.assertEqual(self.refs("circolare 9/E"), [("Circolare", "9", "E", None)])

    def test_senza_riferimenti(self):
        self.assertEqual(self.refs("reverse charge su console da gioco, art. 17 lett. c"), [])
        self.assertEqual(self.refs("le circolari dell'Agenzia sul concordato del 2025"), [])

    def test_doppioni_tolti(self):
        self.assertEqual(self.refs('circolare 21/E del 2016 "21/E" 2016 circolare 21/E 2016'),
                         [("Circolare", "21", "E", "2016")])

    def test_percorso(self):
        rx = s._reference_path_regex
        self.assertEqual(rx({"tipo": "Circolare", "numero": "21", "lettera": "E", "anno": "2016"}),
                         "(^|/)2016-[0-9]{2}-[0-9]{2}_Circolare_21(-E)?_")
        self.assertEqual(rx({"tipo": "Circolare", "numero": "78", "lettera": "D", "anno": None}),
                         "(^|/)[0-9]{4}-[0-9]{2}-[0-9]{2}_Circolare_78-D_")
        self.assertEqual(rx({"tipo": "Legge", "numero": "190", "lettera": None, "anno": "2014"}),
                         "(^|/)2014-[0-9]{2}-[0-9]{2}_Legge_190(-[A-Z])?_")


if __name__ == "__main__":
    unittest.main()
