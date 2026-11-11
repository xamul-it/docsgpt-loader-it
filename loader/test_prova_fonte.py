#!/usr/bin/env python3
"""Test di prova_fonte.py (nessuna chiamata a DocsGPT).

  python3 -m unittest test_prova_fonte -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import prova_fonte as p
from test_supporto import CFG


class EsitoTest(unittest.TestCase):
    def test_esito_soglia(self):
        righe = [{"mode": "B", "ranks": [r]} for r in (1, 2, 3, 3, None)]
        self.assertEqual(p.esito_benchmark(righe), (True, 0.8))
        righe[0]["ranks"] = [4]
        self.assertFalse(p.esito_benchmark(righe)[0])

    def test_esito_ignora_modalita_a(self):
        self.assertEqual(p.esito_benchmark([{"mode": "A", "ranks": [None]}, {"mode": "B", "ranks": [1]}]), (True, 1.0))

    def test_esito_senza_righe(self):
        self.assertEqual(p.esito_benchmark([]), (False, 0.0))

    def test_domande_della_fonte(self):
        dom = {"domande": [{"id": "prova-01"}, {"id": "rif-01"}, {"id": "prova-02"}]}
        self.assertEqual([d["id"] for d in p.domande_fonte(dom, "prova")], ["prova-01", "prova-02"])


class AgenteTest(unittest.TestCase):
    def test_create_agent_payload(self):
        with mock.patch("prova_fonte.requests.post") as post:
            post.return_value.json.return_value = {"id": "a1", "key": "k"}
            post.return_value.status_code = 201
            self.assertEqual(p.crea_agente_prova("http://api", "codice-civile", "s1"), ("a1", "k"))
            body = post.call_args.kwargs["json"]
            self.assertEqual((body["name"], body["sources"], body["retriever"], body["status"]),
                             ("prova-codice-civile", ["s1"], "classic", "published"))
            self.assertEqual(body["prompt_id"], "default")


class PulisciTest(unittest.TestCase):
    def test_pulisci_idempotente(self):
        tmp = Path(tempfile.mkdtemp())
        links = tmp / "doc_links.json"
        links.write_text(json.dumps({"s1": {"base": "x", "depth": 0}, "altra": {"base": "y", "depth": 1}}))
        d = tmp / "gruppo" / "prova"
        d.mkdir(parents=True)
        (d / "prova.json").write_text(json.dumps({"source_id": "s1", "agent_id": "a1", "key": "k"}))
        with mock.patch("prova_fonte.requests.delete") as delete, \
                mock.patch("prova_fonte.carica_docsgpt.delete_source") as del_src, \
                mock.patch("prova_fonte.carica_docsgpt.list_sources", return_value={}), \
                mock.patch("prova_fonte.requests.get") as get:
            get.return_value.json.return_value = []
            p.pulisci("http://api", tmp, CFG, links)
            delete.assert_called_once()
            self.assertIn("id=a1", delete.call_args.args[0])
            del_src.assert_called_once_with("http://api", "s1")
            self.assertEqual(set(json.loads(links.read_text())), {"altra"})
            self.assertFalse((d / "prova.json").exists())
            p.pulisci("http://api", tmp, CFG, links)   # seconda volta: niente da fare, nessun errore


if __name__ == "__main__":
    unittest.main()
