#!/usr/bin/env python3
"""Test di estrai.py (nessuna rete, nessuna fonte reale).

  python3 -m unittest test_estrai -v
"""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import carica_docsgpt
import estrai as e
from fonte import FonteError, Unita
from test_supporto import CFG, LUNGO, AdattatoreFinto, registrato


def U(id, titolo="Titolo", percorso=None, testo=None, metadati=None, link_ufficiali=None, breve=False):
    return Unita(id, titolo, percorso or ["Parte"], testo or f"Testo di {id}. " + "parole " * 20,
                 metadati or {}, link_ufficiali or {}, breve)


TRE = [U("a"), U("b"), U("c")]


def manifest(d: Path) -> list[dict]:
    return [json.loads(x) for x in (d / "manifest.jsonl").read_text().splitlines() if x.strip()]


class DividiTest(unittest.TestCase):
    def test_corto_non_diviso(self):
        self.assertEqual(e.dividi("a\n\nb", 4000), ["a\n\nb"])

    def test_blocchi_accumulati(self):
        parti = e.dividi("\n\n".join(["x" * 1500] * 6), 4000)
        self.assertEqual(len(parti), 3)
        self.assertTrue(all(len(p) <= 4000 for p in parti))

    def test_blocco_troppo_lungo_tagliato_al_punto(self):
        testo = ("Frase lunga. " * 500).strip()
        parti = e.dividi(testo, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parti))
        self.assertTrue(all(p.endswith(".") for p in parti))
        self.assertEqual(" ".join(parti).replace("  ", " "), testo)


class ScriviTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_id_duplicati_fermano(self):
        with self.assertRaises(FonteError) as c:
            e.scrivi_unita([U("a"), U("a")], CFG, self.tmp, "v1")
        self.assertIn("a", str(c.exception))
        self.assertFalse((self.tmp / "manifest.jsonl").exists())

    def test_id_non_valido_ferma(self):
        with self.assertRaises(FonteError):
            e.scrivi_unita([U("Art 1")], CFG, self.tmp, "v1")

    def test_titolo_con_percorso(self):
        e.scrivi_unita([U("x-1401", titolo="Art. 1401 – Nomina", percorso=["LIBRO QUARTO", "CAPO VII X"])], CFG, self.tmp, "v1")
        row = manifest(self.tmp)[0]
        self.assertIn("CAPO VII", row["title"])
        self.assertEqual(row["unit_id"], "x-1401")
        html = (self.tmp / "files" / "x-1401.html").read_text()
        self.assertIn("<title>Fonte finta › LIBRO QUARTO › CAPO VII X › Art. 1401 – Nomina</title>", html)
        self.assertIn('class="percorso"', html)

    def test_campi_esistenti_del_manifest(self):
        e.scrivi_unita(TRE, CFG, self.tmp, "v1")
        row = manifest(self.tmp)[0]
        for k in ("source", "title", "document_date", "authority", "document_type", "number", "detail_url",
                  "download_url", "local_file", "sha256", "downloaded_at", "versione", "breve_ammesso"):
            self.assertIn(k, row)
        self.assertEqual((row["source"], row["authority"], row["local_file"]), ("gruppo/prova", "Ente finto", "files/a.html"))

    def test_divisione_unita_lunga(self):
        testo = "\n\n".join(["Comma " + "x" * 1500 + "."] * 6)
        r = e.scrivi_unita([U("art-9999", titolo="Art. 9999", testo=testo)], CFG, self.tmp, "v1")
        parti = sorted((self.tmp / "files").glob("art-9999-p*.html"))
        self.assertGreaterEqual(len(parti), 3)
        self.assertEqual(r.divise, 1)
        self.assertTrue(all("Art. 9999" in p.read_text() for p in parti))
        self.assertEqual(manifest(self.tmp)[0]["parte"], f"1/{len(parti)}")

    def test_rieseguo_invariato(self):
        e.scrivi_unita(TRE, CFG, self.tmp, "v1")
        mtime = (self.tmp / "files" / "a.html").stat().st_mtime_ns
        self.assertEqual(e.scrivi_unita(TRE, CFG, self.tmp, "v1"), e.Rapporto(0, 3, 0, 0))
        self.assertEqual((self.tmp / "files" / "a.html").stat().st_mtime_ns, mtime)

    def test_rieseguo_una_cambiata_una_sparita(self):
        e.scrivi_unita(TRE, CFG, self.tmp, "v1")
        r = e.scrivi_unita([replace(TRE[0], testo="Testo nuovo. " + "parole " * 20), TRE[1]], CFG, self.tmp, "v2")
        self.assertEqual((r.scritte, r.invariate, r.rimosse), (1, 1, 1))
        self.assertFalse((self.tmp / "files" / "c.html").exists())
        self.assertEqual(len(manifest(self.tmp)), 2)

    def test_risposta_vuota_non_cancella(self):
        e.scrivi_unita(TRE, CFG, self.tmp, "v1")
        with self.assertRaises(FonteError):
            e.scrivi_unita([], CFG, self.tmp, "v2")
        with self.assertRaises(FonteError):
            e.scrivi_unita([TRE[0]], CFG, self.tmp, "v2")   # 1 su 3: meno della metà
        self.assertEqual(len(manifest(self.tmp)), 3)

    def test_link_scelto_nel_manifest_e_nel_testo(self):
        e.scrivi_unita([U("a", link_ufficiali={"urn": "http://u"})], {**CFG, "link": "entrambi:urn"}, self.tmp, "v1")
        self.assertEqual(manifest(self.tmp)[0]["link"], "http://u")
        self.assertIn('href="http://u"', (self.tmp / "files" / "a.html").read_text())

    def test_link_non_http_rifiutato(self):
        with self.assertRaises(FonteError):
            e.scrivi_unita([U("a", link_ufficiali={"urn": "javascript:alert(1)"})], {**CFG, "link": "urn"}, self.tmp, "v1")
        with self.assertRaises(FonteError):
            e.scrivi_unita([U("a", link_ufficiali={"urn": "javascript:alert(1)"})], CFG, self.tmp, "v1")

    def test_link_locale_niente_link_nel_testo(self):
        e.scrivi_unita([U("a", link_ufficiali={"urn": "http://u"})], CFG, self.tmp, "v1")
        self.assertEqual(manifest(self.tmp)[0]["link"], "")
        self.assertNotIn("http://u", (self.tmp / "files" / "a.html").read_text())

    def test_detail_url_unico_anche_locale(self):
        e.scrivi_unita(TRE + [U("lunga", testo=LUNGO)], CFG, self.tmp, "v1")
        urls = [r["detail_url"] for r in manifest(self.tmp)]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertGreater(len(urls), 4)

    def test_metadati_piatti(self):
        e.scrivi_unita([U("a", metadati={"rubrica": "R"})], CFG, self.tmp, "v1")
        self.assertEqual(manifest(self.tmp)[0]["rubrica"], "R")
        with self.assertRaises(FonteError):
            e.scrivi_unita([U("b", metadati={"title": "x"})], CFG, self.tmp / "altro", "v2")

    def test_testo_escapato(self):
        e.scrivi_unita([U("a", testo="a < b & c > d. " + "x " * 30)], CFG, self.tmp, "v1")
        self.assertIn("a &lt; b &amp; c &gt; d", (self.tmp / "files" / "a.html").read_text())

    def test_carica_docsgpt_vede_i_file(self):
        d = self.tmp / "gruppo" / "prova"
        e.scrivi_unita(TRE, CFG, d, "v1")
        docs = carica_docsgpt.ente_documents(d)
        self.assertEqual(sorted(a for _, a in docs), ["a.html", "b.html", "c.html"])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def esegui(self, ad, *args):
        with registrato(ad), mock.patch("estrai.fonte.config", return_value=CFG), \
                mock.patch("estrai.fonte.configs", return_value=[CFG]):
            return e.main(["--target", str(self.tmp), *args])

    def test_estrai_poi_invariato(self):
        self.assertEqual(self.esegui(AdattatoreFinto(), "--id", "prova"), 0)
        d = self.tmp / "gruppo" / "prova"
        self.assertEqual(json.loads((d / "stato.json").read_text())["versione"], "v1")
        n = len(manifest(d))
        with mock.patch("estrai.scrivi_unita") as scrivi:
            self.assertEqual(self.esegui(AdattatoreFinto(), "--id", "prova"), 0)
            scrivi.assert_not_called()
        self.assertEqual(len(manifest(d)), n)

    def test_versione_nuova_riestrae(self):
        self.esegui(AdattatoreFinto(), "--id", "prova")
        self.assertEqual(self.esegui(AdattatoreFinto(versione="v2"), "--gruppo", "gruppo"), 0)
        self.assertEqual(json.loads((self.tmp / "gruppo" / "prova" / "stato.json").read_text())["versione"], "v2")

    def test_errore_fonte_exit_1(self):
        cfg = {**CFG, "formato": "rotto"}
        with registrato(AdattatoreFinto()), mock.patch("estrai.fonte.config", return_value=cfg):
            self.assertEqual(e.main(["--target", str(self.tmp), "--id", "prova"]), 1)
        self.assertFalse((self.tmp / "gruppo" / "prova" / "manifest.jsonl").exists())


class CaricaDocsgptTest(unittest.TestCase):
    def test_nomi_e_gruppi_da_pipeline(self):
        tmp = Path(tempfile.mkdtemp())
        import yaml
        (tmp / "p.yaml").write_text(yaml.safe_dump({"sorgenti": [CFG]}))
        nomi = carica_docsgpt.pipeline_names(tmp / "p.yaml")
        self.assertEqual(nomi, {"gruppo/prova": "Fonte finta"})


if __name__ == "__main__":
    unittest.main()
