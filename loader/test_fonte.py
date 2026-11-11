#!/usr/bin/env python3
"""Test del nucleo della pipeline (fonte.py, registro degli adattatori). Nessuna rete.

  python3 -m unittest test_fonte -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adattatori
import fonte as f


def resp(content: bytes, status: int = 200, ctype: str = "text/html"):
    r = mock.Mock(status_code=status, content=content, headers={"Content-Type": ctype}, url="http://u")
    return r


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def scrivi_yaml(self, sorgenti):
        import yaml
        (self.tmp / "p.yaml").write_text(yaml.safe_dump({"sorgenti": sorgenti}))

    def test_config_campo_mancante(self):
        self.scrivi_yaml([{"id": "x", "gruppo": "g"}])
        with self.assertRaises(f.FonteError):
            f.config("x", self.tmp / "p.yaml")

    def test_config_completa(self):
        voce = {k: "v" for k in f.CAMPI_CONFIG} | {"id": "x", "soglia_caratteri": 4000}
        self.scrivi_yaml([voce])
        self.assertEqual(f.config("x", self.tmp / "p.yaml")["soglia_caratteri"], 4000)
        self.assertEqual([c["id"] for c in f.configs(self.tmp / "p.yaml")], ["x"])

    def test_config_assente(self):
        self.scrivi_yaml([])
        with self.assertRaises(f.FonteError):
            f.config("x", self.tmp / "p.yaml")

    def test_cartella(self):
        self.assertEqual(f.cartella(Path("/a"), {"gruppo": "codici", "id": "codice-civile"}), Path("/a/codici/codice-civile"))

    def test_pipeline_yaml_valido(self):
        self.assertIsInstance(f.configs(), list)


class ReteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_rete_usa_la_cache(self):
        with mock.patch("fonte.requests.Session.get", return_value=resp(b"x")) as get:
            r = f.Rete(self.tmp, pausa=0)
            a = r.get("http://u")
            b = r.get("http://u")
            self.assertEqual((get.call_count, r.richieste_rete, r.byte_rete), (1, 1, 1))
            self.assertEqual((a.content, b.content, b.status, b.content_type), (b"x", b"x", 200, "text/html"))
            r.get("http://u", fresca=True)
            self.assertEqual(get.call_count, 2)

    def test_rete_cache_sopravvive(self):
        with mock.patch("fonte.requests.Session.get", return_value=resp(b"y")):
            f.Rete(self.tmp, pausa=0).get("http://u")
        with mock.patch("fonte.requests.Session.get") as get:
            self.assertEqual(f.Rete(self.tmp, pausa=0).get("http://u").text, "y")
            get.assert_not_called()

    def test_rete_senza_cache(self):
        with mock.patch("fonte.requests.Session.get", return_value=resp(b"x")) as get:
            r = f.Rete(None, pausa=0)
            r.get("http://u"); r.get("http://u")
            self.assertEqual(get.call_count, 2)

    def test_rete_fixture(self):
        (self.tmp / "a.html").write_bytes(b"ciao")
        (self.tmp / "contratto.json").write_text(json.dumps({"config": {}, "rete": {"http://a": "a.html"}, "campione": {}}))
        r = f.ReteFixture(self.tmp)
        self.assertEqual(r.get("http://a").text, "ciao")
        with self.assertRaises(f.ReteMancante):
            r.get("http://non-previsto")


class RegistroTest(unittest.TestCase):
    def test_trova_sconosciuto(self):
        self.assertIsNone(adattatori.trova("https://www.example.org/x"))

    def test_adattatore_sconosciuto(self):
        with self.assertRaises(f.FonteError):
            adattatori.adattatore("non-esiste")

    def test_modello_non_registrato(self):
        self.assertNotIn("_modello", adattatori.REGISTRO)


if __name__ == "__main__":
    unittest.main()
