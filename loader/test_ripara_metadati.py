#!/usr/bin/env python3
"""Test di ripara_metadati.py (nessuna richiesta di rete).

  python3 -m unittest test_ripara_metadati -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import downloader as d
import ripara_metadati as rm
from test_downloader import giurisprudenza_html

CASS = "Sentenza del 30/12/2014 n. 27513 - Corte di Cassazione - Sezione/Collegio 5"
CGT = "Sentenza del 09/11/2021 n. 5009 - Comm. Trib. Reg. per il Lazio Sezione/Collegio 10"


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def map(self, fn, items):
        return [fn(x) for x in items]

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url not in self.pages:
            raise RuntimeError("pagina non disponibile")
        return SimpleNamespace(url=url, text=self.pages[url])


class RiparaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name)
        self.root = self.target / "giurisprudenza" / "sentenza"
        (self.root / "files" / "2014").mkdir(parents=True)
        (self.root / "files" / "2021").mkdir(parents=True)
        self.rows = []

    def add(self, name, title, date, number, body, url):
        path = self.root / "files" / date[:4] / name
        path.write_bytes(body)
        self.rows.append({
            "source": "giurisprudenza/sentenza", "title": title, "document_date": date, "authority": "",
            "document_type": "Sentenza", "number": number, "detail_url": url, "download_url": url,
            "local_file": str(path.relative_to(self.root)), "sha256": "x", "downloaded_at": "t",
        })

    def write_manifest(self):
        (self.root / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.rows), encoding="utf-8")

    def manifest(self):
        lines = (self.root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        return {r["detail_url"]: r for r in map(json.loads, lines)}

    def run_repair(self, http, dry_run=False):
        store = d.Store(self.target, "giurisprudenza/sentenza", "refresh")
        with mock.patch("builtins.print"):
            return rm.ripara_store(store, http, "giurisprudenza", dry_run=dry_run)

    def html(self, title, estremi):
        return f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title></head><body><h1>{title}</h1><p>{estremi}</p>Testo</body></html>".encode()

    def test_html_organo_dal_file_e_file_rinominato_senza_rete(self):
        self.add("2014-12-30_Sentenza_27513_Iva__aaaa.html", "Iva", "2014-12-30", "27513", self.html("Iva", CASS), "u1")
        self.write_manifest()
        http = FakeHttp({})
        stats = self.run_repair(http)
        row = self.manifest()["u1"]
        self.assertEqual(row["authority"], "Corte di Cassazione")
        self.assertTrue(Path(row["local_file"]).name.startswith("2014-12-30_Sentenza_27513_Corte_di_Cassazione_Iva__"))
        self.assertTrue((self.root / row["local_file"]).is_file())
        self.assertEqual(list((self.root / "files" / "2014").iterdir()), [self.root / row["local_file"]])
        self.assertEqual(http.urls, [])
        self.assertEqual(stats["aggiornati"], 1)

    def test_titolo_segnaposto_diventa_gli_estremi(self):
        self.add("2014-12-30_Sentenza_27513_Non_disponibile__aaaa.html", "Non disponibile", "2014-12-30", "27513",
                 self.html("Non disponibile", CASS), "u1")
        self.write_manifest()
        self.run_repair(FakeHttp({}))
        self.assertEqual(self.manifest()["u1"]["title"], CASS)

    def test_pdf_estremi_dalla_pagina_di_dettaglio(self):
        self.add("2021-11-09_Sentenza_5009_Ici__bbbb.pdf", "Nessuna intitolazione presente", "2021-11-09", "5009", b"%PDF-1.4", "u2")
        self.rows[-1]["download_url"] = "https://def.finanze.it/getContent.do?id=X"
        self.write_manifest()
        http = FakeHttp({"u2": giurisprudenza_html(CGT, "Nessuna intitolazione presente", allegato="true")})
        stats = self.run_repair(http)
        row = self.manifest()["u2"]
        self.assertEqual(row["authority"], "Comm. Trib. Reg. per il Lazio")
        self.assertEqual(row["title"], CGT)
        self.assertTrue(row["local_file"].endswith(".pdf"))
        self.assertEqual((self.root / row["local_file"]).read_bytes(), b"%PDF-1.4")
        self.assertEqual(row["download_url"], "https://def.finanze.it/getContent.do?id=X")
        self.assertEqual(stats["letti_dal_sito"], 1)

    def test_errore_di_rete_lascia_la_riga_com_era(self):
        self.add("2021-11-09_Sentenza_5009_Ici__bbbb.pdf", "Ici", "2021-11-09", "5009", b"%PDF", "u2")
        self.write_manifest()
        stats = self.run_repair(FakeHttp({}))
        self.assertEqual(self.manifest()["u2"], self.rows[0])
        self.assertEqual(stats["errori"], 1)

    def test_prova_non_tocca_nulla(self):
        self.add("2014-12-30_Sentenza_27513_Iva__aaaa.html", "Iva", "2014-12-30", "27513", self.html("Iva", CASS), "u1")
        self.write_manifest()
        before = (self.root / "manifest.jsonl").read_bytes()
        stats = self.run_repair(FakeHttp({}), dry_run=True)
        self.assertEqual((self.root / "manifest.jsonl").read_bytes(), before)
        self.assertTrue((self.root / self.rows[0]["local_file"]).is_file())
        self.assertEqual(stats["aggiornati"], 1)

    def test_seconda_esecuzione_non_cambia_nulla(self):
        self.add("2014-12-30_Sentenza_27513_Iva__aaaa.html", "Iva", "2014-12-30", "27513", self.html("Iva", CASS), "u1")
        self.write_manifest()
        self.run_repair(FakeHttp({}))
        first = self.manifest()
        stats = self.run_repair(FakeHttp({}))
        self.assertEqual(self.manifest(), first)
        self.assertEqual(stats["aggiornati"], 0)

    def test_ripresa_dopo_interruzione_ritrova_il_file_gia_rinominato(self):
        self.add("2014-12-30_Sentenza_27513_Iva__aaaa.html", "Iva", "2014-12-30", "27513", self.html("Iva", CASS), "u1")
        self.write_manifest()
        old = self.root / self.rows[0]["local_file"]
        # Interruzione simulata: file già spostato, manifest non ancora riscritto.
        doc = d.Doc(**{**self.rows[0], "authority": "Corte di Cassazione"})
        store = d.Store(self.target, "giurisprudenza/sentenza", "refresh")
        new = store.choose_path(doc.title, doc.document_date, doc.download_url, ".html", d.file_prefix(doc))
        old.rename(new)
        self.run_repair(FakeHttp({}))
        row = self.manifest()["u1"]
        self.assertEqual(self.root / row["local_file"], new)
        self.assertEqual(row["authority"], "Corte di Cassazione")


if __name__ == "__main__":
    unittest.main()
