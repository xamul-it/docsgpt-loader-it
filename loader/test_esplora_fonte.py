#!/usr/bin/env python3
"""Test di esplora_fonte.py (nessuna rete).

  python3 -m unittest test_esplora_fonte -v
"""

import unittest

import esplora_fonte as e
from fonte import Risposta


class ReteFinta:
    def __init__(self, pagine: dict, ctype: str = "text/html", status: int = 200):
        self.pagine, self.ctype, self.status = pagine, ctype, status
        self.richieste_rete = self.byte_rete = 0

    def get(self, url, fresca=False):
        return Risposta(url, self.status, self.ctype, self.pagine[url].encode())


def rete_finta(pagine, ctype="text/html", status=200):
    return ReteFinta(pagine, ctype, status)


class RicognizioneTest(unittest.TestCase):
    def test_formati_e_elenchi(self):
        html = "<html><head><title>Atti</title></head><body><h1>Atti</h1>" + "".join(
            f'<a href="/atto/{i}/dettaglio?id={i}">Atto {i}</a>' for i in range(40)) + '<a href="/export/tutto.xml">XML</a></body></html>'
        r = e.ricognisci(rete_finta({"http://x/": html}), "http://x/")
        self.assertEqual(r.titolo, "Atti")
        self.assertEqual(r.formati["xml"], ["http://x/export/tutto.xml"])
        self.assertEqual(r.elenchi[0]["quanti"], 40)
        self.assertEqual(len(r.elenchi[0]["esempi"]), 3)
        self.assertIsNone(r.adattatore)
        self.assertEqual(r.struttura["h1"], 1)

    def test_avviso_javascript(self):
        html = "<html><body>" + "<script>x()</script>" * 10 + "<div id=app></div></body></html>"
        r = e.ricognisci(rete_finta({"http://x/": html}), "http://x/")
        self.assertTrue(any("JavaScript" in a for a in r.avvisi), r.avvisi)

    def test_avviso_login(self):
        html = "<html><body>" + "testo " * 200 + '<form><input type="password" name="pwd"></form></body></html>'
        r = e.ricognisci(rete_finta({"http://x/": html}), "http://x/")
        self.assertTrue(any("login" in a for a in r.avvisi), r.avvisi)

    def test_avviso_status(self):
        r = e.ricognisci(rete_finta({"http://x/": "<html>errore</html>"}, status=404), "http://x/")
        self.assertTrue(any("404" in a for a in r.avvisi), r.avvisi)

    def test_xml_elenchi(self):
        xml = "<root>" + "<voce><t>a</t></voce>" * 12 + "</root>"
        r = e.ricognisci(rete_finta({"http://x/a.xml": xml}, ctype="text/xml"), "http://x/a.xml")
        self.assertEqual((r.elenchi[0]["schema"], r.elenchi[0]["quanti"]), ("root/voce", 12))

    def test_xml_con_namespace(self):
        xml = '<a:root xmlns:a="urn:x">' + "<a:art/>" * 5 + "</a:root>"
        r = e.ricognisci(rete_finta({"http://x/a.xml": xml}, ctype="application/xml"), "http://x/a.xml")
        self.assertEqual(r.elenchi[0]["schema"], "root/art")

    def test_pdf(self):
        r = e.ricognisci(rete_finta({"http://x/a.pdf": "%PDF-1.4 ..."}, ctype="application/pdf"), "http://x/a.pdf")
        self.assertEqual((r.content_type, r.elenchi), ("application/pdf", []))

    def test_data_trovata(self):
        html = "<html><body><p>Ultimo aggiornamento: 31/10/2026</p>" + "x " * 300 + "</body></html>"
        self.assertEqual(e.ricognisci(rete_finta({"http://x/": html}), "http://x/").data_trovata, "2026-10-31")

    def test_schema_url(self):
        self.assertEqual(e.schema_url("http://x/atto/12/dettaglio?id=5&b=2"), "x/atto/{n}/dettaglio?b&id")


if __name__ == "__main__":
    unittest.main()
