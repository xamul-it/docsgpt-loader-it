#!/usr/bin/env python3
"""Test di misura_formati.py (nessuna rete).

  python3 -m unittest test_misura_formati -v
"""

import unittest

import fonte
import misura_formati as m
from fonte import Unita

LUNGO = "Testo lungo abbastanza per non essere vuoto. "


class CampioneTest(unittest.TestCase):
    def test_campione_deterministico(self):
        ids = [f"u-{i}" for i in range(500)] + ["u-42-bis"]
        a = m.campione(ids, obbligatori=["u-42-bis"])
        self.assertEqual(a, m.campione(ids, obbligatori=["u-42-bis"]))
        self.assertIn("u-42-bis", a)
        self.assertEqual(len(a), 30)
        self.assertEqual(len(set(a)), 30)

    def test_campione_piccolo(self):
        self.assertEqual(sorted(m.campione(["a", "b"])), ["a", "b"])

    def test_obbligatori_fuori_elenco_ignorati(self):
        self.assertNotIn("zz", m.campione(["a", "b", "c"], n=2, obbligatori=["zz"]))


class MisuraTest(unittest.TestCase):
    def test_misura_conta_residui_vuoti_titoli(self):
        us = [Unita("a", "T", ["P"], LUNGO + "((3))", {"k": "v"}), Unita("b", "", [], "Vai al contenuto")]
        r = m.misura("x", us, attese=["a", "b", "c"], obbligatori=("k",),
                     residui=fonte.RESIDUI_COMUNI + [r"\(\(\d+\)\)"])
        self.assertEqual((r.attese, r.trovate, r.residui, r.con_titolo, r.vuote), (3, 2, 2, 1, 1))
        self.assertEqual((r.con_percorso, r.con_metadati), (1, 1))

    def test_breve_ammesso_non_vuoto(self):
        r = m.misura("x", [Unita("a", "T", [], "Abrogato", breve_ammesso=True)], ["a"], (), [])
        self.assertEqual(r.vuote, 0)

    def test_breve_non_ammesso_vuoto(self):
        r = m.misura("x", [Unita("a", "T", [], "Abrogato")], ["a"], (), [])
        self.assertEqual(r.vuote, 1)

    def test_lunghezze(self):
        us = [Unita(str(i), "T", [], "x" * (i * 100)) for i in range(1, 21)]
        r = m.misura("x", us, [u.id for u in us], (), [])
        self.assertEqual((r.lunghezza_max, r.lunghezza_p95), (2000, 1900))

    def test_trovate_solo_tra_attese(self):
        r = m.misura("x", [Unita("a", "T", [], LUNGO), Unita("estranea", "T", [], LUNGO)], ["a"], (), [])
        self.assertEqual(r.trovate, 1)


class MisuraFonteTest(unittest.TestCase):
    def test_tutti_i_formati_misurati_con_errore_isolato(self):
        import json, tempfile
        from pathlib import Path
        from test_supporto import CFG, AdattatoreFinto, registrato
        tmp = Path(tempfile.mkdtemp())
        with registrato(AdattatoreFinto()):
            mis = {x.formato: x for x in m.misura_fonte(tmp, CFG, n=5)}
        self.assertEqual(set(mis), {"intero", "per-unita", "rotto"})
        self.assertIn("HTML", mis["rotto"].errore)
        self.assertEqual(mis["intero"].trovate, 5)
        self.assertEqual(mis["intero"].stima_richieste_totali, 1)
        self.assertEqual(mis["per-unita"].stima_richieste_totali, 12)   # 5 nel campione, 12 attese
        salvate = json.loads((tmp / "gruppo" / "prova" / "misure.json").read_text())
        self.assertIn("u-lunga", salvate["campione"])
        self.assertEqual(len(m.tabella(list(mis.values()))), 4)


if __name__ == "__main__":
    unittest.main()
