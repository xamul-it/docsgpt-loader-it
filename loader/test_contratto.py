#!/usr/bin/env python3
"""Test di contratto: valgono per ogni adattatore registrato, su fixture, senza rete.

Ogni adattatore deve avere fixtures/<nome>/contratto.json:
  {"config": {...voce di fonti/pipeline.yaml...},
   "rete": {"<url>": "<file nella cartella>", ...},
   "campione": {"<formato>": ["<id unità>", ...], ...}}

  python3 -m unittest test_contratto -v
"""

import json
import re
import unittest
from pathlib import Path

import adattatori
import fonte

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ContrattoTest(unittest.TestCase):
    def test_adattatori(self):
        for nome, ad in sorted(adattatori.REGISTRO.items()):
            with self.subTest(adattatore=nome):
                self.verifica(nome, ad)

    def verifica(self, nome: str, ad):
        cartella = FIXTURES / nome
        if not (cartella / "contratto.json").exists():
            self.fail(f"{nome}: manca fixtures/{nome}/contratto.json")
        spec = json.loads((cartella / "contratto.json").read_text())
        cfg = spec["config"]
        rete = fonte.ReteFixture(cartella)

        self.assertEqual(ad.nome, nome)
        self.assertTrue(ad.formati, f"{nome}: nessun formato")
        self.assertTrue(ad.riconosce(cfg["url"]), f"{nome}: non riconosce {cfg['url']}")
        self.assertTrue(ad.versione(rete, cfg), f"{nome}: versione vuota")
        attesi = ad.attesi(rete, cfg)
        self.assertTrue(attesi, f"{nome}: nessuna unità attesa")
        self.assertEqual(len(attesi), len(set(attesi)), f"{nome}: id attesi duplicati")
        self.assertLessEqual(set(ad.casi_limite(rete, cfg)), set(attesi), f"{nome}: casi limite fuori dagli attesi")
        residui = [re.compile(r) for r in [*fonte.RESIDUI_COMUNI, *ad.residui]]
        self.assertTrue(spec["campione"], f"{nome}: campione vuoto")
        for formato, ids in spec["campione"].items():
            self.assertIn(formato, ad.formati)
            unita = ad.unita(rete, cfg, formato, solo=set(ids))
            self.assertEqual(sorted(u.id for u in unita), sorted(ids), f"{nome}/{formato}: id diversi dal campione")
            for u in unita:
                where = f"{nome}/{formato}/{u.id}"
                self.assertRegex(u.id, fonte.ID_RE, where)
                self.assertIn(u.id, attesi, where)
                self.assertTrue(u.titolo.strip(), f"{where}: titolo vuoto")
                self.assertTrue(u.testo.strip(), f"{where}: testo vuoto")
                self.assertIsInstance(u.percorso, list, where)
                for campo in ad.metadati_obbligatori:
                    self.assertTrue(u.metadati.get(campo), f"{where}: manca il metadato {campo}")
                self.assertEqual(set(u.link_ufficiali), set(ad.candidati_link), f"{where}: link ufficiali")
                for r in residui:
                    self.assertIsNone(r.search(u.testo), f"{where}: residuo {r.pattern}")


if __name__ == "__main__":
    unittest.main()
