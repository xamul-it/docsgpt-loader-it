#!/usr/bin/env python3
"""Test dei controlli locali di quadratura.py (nessuna richiesta di rete).

  python3 -m unittest test_quadratura -v
"""

import unittest

import quadratura as q


class WrongTypeTest(unittest.TestCase):
    CFG = {"document_type": "Circolare"}

    def test_comunicato_tra_le_circolari_e_segnalato(self):
        rows = [
            {"document_type": "Circolare", "local_file": "files/a.pdf"},
            {"document_type": "Comunicato Stampa", "local_file": "files/b.pdf"},
        ]
        self.assertEqual(q.wrong_type(rows, self.CFG), ["files/b.pdf (Comunicato Stampa)"])

    def test_titolo_che_cita_un_comunicato_non_conta(self):
        rows = [{"document_type": "Circolare", "title": "Trasmissione comunicato stampa", "local_file": "files/c.html"}]
        self.assertEqual(q.wrong_type(rows, self.CFG), [])

    def test_source_senza_tipo_non_si_controlla(self):
        self.assertEqual(q.wrong_type([{"document_type": "Legge", "local_file": "x"}], {}), [])


if __name__ == "__main__":
    unittest.main()
