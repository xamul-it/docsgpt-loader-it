#!/usr/bin/env python3
"""Test del ritmo per host e del pool di downloader.py (nessuna richiesta di rete).

  python3 -m unittest test_downloader -v
"""

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import downloader as d


class HostLimiterTest(unittest.TestCase):
    def test_piu_thread_non_superano_il_ritmo(self):
        lim = d.HostLimiter(50)
        starts = []
        lock = threading.Lock()

        def worker():
            for _ in range(5):
                lim.wait()
                with lock:
                    starts.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 40 richieste a 50/s: la prima parte subito, l'ultima dopo 39 intervalli.
        self.assertGreaterEqual(time.monotonic() - t0, 39 / 50 - 0.01)
        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertGreaterEqual(min(gaps), 1 / 50 - 0.01)

    def test_slow_down_dimezza_una_volta_per_raffica(self):
        lim = d.HostLimiter(8)
        # 4 thread che ricevono lo stesso 429 non devono dimezzare 4 volte.
        for _ in range(4):
            lim.slow_down()
        self.assertEqual(lim.rate, 4)

    def test_slow_down_non_scende_sotto_il_minimo(self):
        lim = d.HostLimiter(1)
        for _ in range(10):
            lim.last_slow_down = 0.0
            lim.slow_down()
        self.assertEqual(lim.rate, d.MIN_RATE)

    def test_retry_after_sospende_tutti_i_thread(self):
        lim = d.HostLimiter(100)
        lim.slow_down(retry_after=0.2)
        t0 = time.monotonic()
        lim.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 0.19)

    def test_il_ritmo_risale_fino_al_massimo(self):
        lim = d.HostLimiter(10)
        lim.slow_down()
        self.assertEqual(lim.rate, 5)
        lim.speed_up()
        self.assertGreater(lim.rate, 5)
        for _ in range(1000):
            lim.speed_up()
        self.assertEqual(lim.rate, 10)


class HttpPoolTest(unittest.TestCase):
    def tearDown(self):
        d.WORKERS = 4

    def test_un_limiter_per_host(self):
        http = d.Http()
        a = http.limiter("https://def.finanze.it/DocTribFrontend/x.do")
        self.assertIs(a, http.limiter("https://def.finanze.it/altro"))
        self.assertIsNot(a, http.limiter("https://www.adm.gov.it/portale/"))

    def test_una_sessione_per_thread(self):
        http = d.Http()
        main = http.s
        self.assertIs(main, http.s)
        other = http.map(lambda _: http.s, [0])[0]
        self.assertIsNot(main, other)

    def test_map_conserva_ordine_e_usa_piu_thread(self):
        http = d.Http()
        names = set()

        def job(x):
            names.add(threading.current_thread().name)
            time.sleep(0.05)
            return x * 2

        self.assertEqual(http.map(job, range(8)), [x * 2 for x in range(8)])
        self.assertGreater(len(names), 1)

    def test_un_worker_resta_nel_thread_chiamante(self):
        d.WORKERS = 1
        http = d.Http()
        self.assertEqual(http.map(lambda _: threading.current_thread(), [0]), [threading.current_thread()])


class FakeHttp:
    """Registra le richieste e fa fallire il dettaglio: qui conta solo se parte."""

    def __init__(self):
        self.urls = []

    def map(self, fn, items):
        return [fn(x) for x in items]

    def get(self, url, **kwargs):
        self.urls.append(url)
        raise RuntimeError("rete non disponibile nei test")


class SkipExistingTest(unittest.TestCase):
    GUID = "{AAAA-1111}"
    DETAIL = "https://def.finanze.it/DocTribFrontend/getAttoNormativoDetail.do?id=%7BAAAA-1111%7D"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, d, "SKIP_EXISTING", False)
        root = Path(self.tmp.name) / "normativa"
        (root / "files").mkdir(parents=True)
        (root / "files" / "atto.html").write_text("<html>testo</html>", encoding="utf-8")
        row = {"source": "normativa", "title": "Atto", "detail_url": self.DETAIL,
               "download_url": self.DETAIL, "local_file": "files/atto.html", "sha256": "x"}
        (root / "manifest.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    def walk(self, mode="refresh"):
        http = FakeHttp()
        store = d.Store(Path(self.tmp.name), "normativa", "refresh")
        pagination = {"pagina": 1, "ultimaPagina": 1, "totale": 1, "ulteriori": False}
        first = SimpleNamespace(url="https://def.finanze.it/DocTribFrontend/executeSearch.do", text="")
        with mock.patch.object(d, "cedef_listing_from_html", return_value=([(self.GUID, "estremi", "Atto")], pagination)), \
             mock.patch.object(d, "cedef_next_links", return_value=[]), \
             mock.patch.object(d, "VERBOSE", 0):
            stats = d.walk_cedef_results(http, store, "normativa", d.SOURCES["normativa"], "normativa", first, mode, 2026)
        return http.urls, stats

    def test_senza_opzione_il_dettaglio_viene_riletto(self):
        urls, _ = self.walk()
        self.assertEqual(urls, [self.DETAIL])

    def test_con_opzione_il_documento_presente_non_genera_richieste(self):
        d.SKIP_EXISTING = True
        urls, stats = self.walk()
        self.assertEqual(urls, [])
        self.assertEqual((stats["accepted"], stats["skipped_existing"], stats["downloaded"]), (1, 1, 0))

    def test_con_opzione_il_file_mancante_viene_riscaricato(self):
        d.SKIP_EXISTING = True
        (Path(self.tmp.name) / "normativa" / "files" / "atto.html").unlink()
        urls, _ = self.walk()
        self.assertEqual(urls, [self.DETAIL])

    def test_in_rewrite_l_opzione_non_vale(self):
        d.SKIP_EXISTING = True
        urls, _ = self.walk(mode="rewrite")
        self.assertEqual(urls, [self.DETAIL])


class BatchPaginationTest(unittest.TestCase):
    """CeRDEF: ogni GET di paginatorXml.do restituisce il batch successivo (cumulativo)."""

    BASE = "https://def.finanze.it/DocTribFrontend/"
    PAGE = (
        '<a href="#" class="avanti">Avanti</a>'
        '<a href="javascript:void(0)" class="ulteriori" '
        "onClick=\"window.location.href='paginatorXml.do'\">Avanti</a>"
    )

    def test_il_link_al_batch_successivo_vale_anche_dalla_pagina_del_batch(self):
        url = self.BASE + "paginatorXml.do"
        soup = d.BeautifulSoup(self.PAGE, "html.parser")
        self.assertEqual(list(d.cedef_next_links(soup, url)), [url])

    def test_vengono_letti_tutti_i_batch_oltre_il_secondo(self):
        batches = [[(f"{{G{i}}}", "estremi", f"Doc {i}") for i in range(n)] for n in (2, 4, 5)]
        listings = [
            (items, {"pagina": 1, "ultimaPagina": 1, "totale": len(items), "ulteriori": i < 2})
            for i, items in enumerate(batches)
        ]

        class Http(FakeHttp):
            def get(self, url, **kwargs):
                if url.endswith("paginatorXml.do"):
                    self.urls.append(url)
                    return SimpleNamespace(url=url, text=BatchPaginationTest.PAGE)
                return super().get(url, **kwargs)

        http = Http()
        with tempfile.TemporaryDirectory() as tmp:
            store = d.Store(Path(tmp), "normativa", "refresh")
            first = SimpleNamespace(url=self.BASE + "executeSearch.do", text=self.PAGE)
            with mock.patch.object(d, "cedef_listing_from_html", side_effect=listings), \
                 mock.patch.object(d, "VERBOSE", 0):
                stats = d.walk_cedef_results(http, store, "normativa", d.SOURCES["normativa"], "normativa", first, "refresh", 2026)
        self.assertEqual(stats["detail_links"], 5)
        self.assertEqual(stats["pages"], 3)


class SplitStoreTest(unittest.TestCase):
    """Normativa e giurisprudenza: una cartella (con il suo manifest) per tipologia."""

    def test_tipo_dagli_estremi(self):
        self.assertEqual(d._doctype_from_estremi("Decreto del Presidente della Repubblica del 26/10/1972 n. 633", ""), "Decreto del Presidente della Repubblica")
        self.assertEqual(d._doctype_from_estremi("Decreto-legge del 02/03/2012 n. 16", ""), "Decreto-legge")
        self.assertEqual(d._doctype_from_estremi("Circolare del 11/05/2001 n. 46 - Agenzia delle Entrate", ""), "Circolare")
        self.assertEqual(d.tipologia_folder("Decreto legislativo"), "decreto-legislativo")
        self.assertEqual(d.tipologia_folder(""), "non-classificati")

    def test_i_documenti_finiscono_nella_cartella_del_loro_tipo(self):
        items = [("{L1}", "Legge del 30/12/2024 n. 207", "Bilancio"), ("{D1}", "Decreto legislativo del 05/08/2026 n. 141", "Testo unico")]
        pagination = {"pagina": 1, "ultimaPagina": 1, "totale": 2, "ulteriori": False}

        def detail(http, source, channel, url, html):
            tipo = "Legge" if "L1" in url else "Decreto legislativo"
            doc = d.Doc(source=source, title="Atto " + tipo, document_date="2024-12-30", document_type=tipo, number="1", detail_url=url, download_url=url)
            return doc, b"<html>testo</html>"

        http = SimpleNamespace(map=lambda fn, jobs: [fn(j) for j in jobs], get=lambda url, **kw: SimpleNamespace(url=url, text=""))
        with tempfile.TemporaryDirectory() as tmp:
            store = d.SplitStore(Path(tmp), "normativa", "refresh")
            first = SimpleNamespace(url="https://def.finanze.it/DocTribFrontend/executeSearch.do", text="")
            with mock.patch.object(d, "cedef_listing_from_html", return_value=(items, pagination)), \
                 mock.patch.object(d, "cedef_next_links", return_value=[]), \
                 mock.patch.object(d, "parse_cedef_detail", side_effect=detail), \
                 mock.patch.object(d, "VERBOSE", 0), mock.patch("builtins.print"):
                stats = d.walk_cedef_results(http, store, "normativa", d.SOURCES["normativa"], "normativa", first, "refresh", 2024)
            self.assertEqual(stats["downloaded"], 2)
            for folder in ("legge", "decreto-legislativo"):
                root = Path(tmp) / "normativa" / folder
                rows = [json.loads(l) for l in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual([r["source"] for r in rows], [f"normativa/{folder}"])
                self.assertTrue((root / rows[0]["local_file"]).is_file())
                self.assertTrue(rows[0]["local_file"].startswith("files/2024/"))


CURATED_YAML = """
sorgenti:
  - id: comune-prova
    nome: "Locazioni - Comune di Prova"
    descrizione: "Sorgente di prova."
    voci:
      - titolo: "Regolamento imposta di soggiorno"
        ente: "Comune di Prova"
        livello: comune
        ambito: affitti-brevi
        tipo: regolamento
        url: "https://www.comune.prova.it/regolamento"
        formato: html
        data_documento: "2024-01-15"
        aggiornamento_atteso: annuale
        verificato: "si"
        note: ""
      - titolo: "Tariffe 2026"
        ente: "Comune di Prova"
        livello: comune
        ambito: affitti-brevi
        tipo: tariffe
        url: "https://www.comune.prova.it/tariffe.pdf"
        formato: pdf
        data_documento: "2026-01-01"
        aggiornamento_atteso: annuale
        verificato: "si"
        note: ""
      - titolo: "Pagina non controllata"
        ente: "Comune di Prova"
        livello: comune
        ambito: affitti-brevi
        tipo: faq
        url: "https://www.comune.prova.it/faq"
        formato: html
        data_documento: ""
        aggiornamento_atteso: raro
        verificato: "no"
        note: ""
  - id: regione-prova
    nome: "Locazioni - Regione Prova"
    descrizione: "Altra sorgente."
    voci: []
"""


class FakeFetcher:
    """Risponde alle GET con i contenuti dati (url -> bytes); un'eccezione se il valore e' un errore."""

    def __init__(self, pages):
        self.pages = dict(pages)
        self.urls = []

    def map(self, fn, items):
        return [fn(x) for x in items]

    def get(self, url, **kwargs):
        self.urls.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return SimpleNamespace(url=url, content=page, headers={}, text=page.decode("utf-8", "ignore"))


class CuratedTest(unittest.TestCase):
    """Kind "curated": elenco curato in YAML, una sorgente per amministrazione."""

    HTML = b"<html><body>" + b"Regolamento dell'imposta di soggiorno. " * 10 + b"</body></html>"
    PDF = b"%PDF-1.4 tariffe"
    URL_HTML = "https://www.comune.prova.it/regolamento"
    URL_PDF = "https://www.comune.prova.it/tariffe.pdf"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.yaml_path = self.root / "locazioni.yaml"
        self.yaml_path.write_text(CURATED_YAML, encoding="utf-8")
        self.sources = d.curated_sources(self.yaml_path)
        self.addCleanup(setattr, d, "VERBOSE", d.VERBOSE)
        d.VERBOSE = 0

    def run_source(self, pages, mode="refresh", name="locazioni/comune-prova"):
        http = FakeFetcher(pages)
        store = d.Store(self.root / "archivio", name, mode)
        with mock.patch("builtins.print"):
            stats = d.curated_source(http, store, name, self.sources[name], mode)
        store.compact()
        return http, store, stats

    def manifest(self, name="locazioni/comune-prova"):
        path = self.root / "archivio" / name / "manifest.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]

    def test_una_sorgente_per_id_con_nome_locazioni(self):
        self.assertEqual(set(self.sources), {"locazioni/comune-prova", "locazioni/regione-prova"})
        cfg = self.sources["locazioni/comune-prova"]
        self.assertEqual(cfg["kind"], "curated")
        self.assertEqual(cfg["label"], "Locazioni - Comune di Prova")
        self.assertEqual(len(cfg["voci"]), 3)

    def test_il_file_vero_registra_le_sorgenti_e_il_gruppo(self):
        real = d.curated_sources()
        self.assertEqual(len(real), 10)
        self.assertIn("locazioni/comune-bologna", real)
        self.assertTrue(all(n in d.SOURCES for n in real))
        self.assertEqual(sorted(d.expand_source("locazioni")), sorted(real))
        self.assertIn("locazioni", d.source_choices())

    def test_le_voci_non_verificate_sono_saltate_e_registrate(self):
        http, store, stats = self.run_source({self.URL_HTML: self.HTML, self.URL_PDF: self.PDF})
        self.assertNotIn("https://www.comune.prova.it/faq", http.urls)
        self.assertEqual((stats["downloaded"], stats["skipped_unverified"], stats["errors"]), (2, 1, 0))
        log = (self.root / "archivio" / "locazioni/comune-prova" / "errors.log").read_text(encoding="utf-8")
        self.assertIn("Pagina non controllata", log)
        self.assertIn("non verificata, saltata", log)

    def test_manifest_con_i_campi_delle_altre_sorgenti_e_quelli_curati(self):
        self.run_source({self.URL_HTML: self.HTML, self.URL_PDF: self.PDF})
        rows = {r["title"]: r for r in self.manifest()}
        r = rows["Regolamento imposta di soggiorno"]
        self.assertEqual(r["source"], "locazioni/comune-prova")
        self.assertEqual(r["authority"], "Comune di Prova")
        self.assertEqual(r["document_type"], "regolamento")
        self.assertEqual(r["document_date"], "2024-01-15")
        self.assertEqual(r["detail_url"], self.URL_HTML)
        self.assertEqual(r["download_url"], self.URL_HTML)
        self.assertEqual((r["livello"], r["ambito"], r["aggiornamento_atteso"]), ("comune", "affitti-brevi", "annuale"))
        self.assertEqual(r["sha256"], hashlib.sha256(self.HTML).hexdigest())
        self.assertTrue(r["downloaded_at"])

    def test_estensione_dal_contenuto_non_dal_formato_dichiarato(self):
        # Il YAML dice pdf per la seconda voce: qui il server restituisce html, e viceversa.
        self.run_source({self.URL_HTML: self.PDF, self.URL_PDF: self.HTML})
        rows = {r["title"]: r for r in self.manifest()}
        self.assertEqual(rows["Regolamento imposta di soggiorno"]["local_file"], "files/regolamento-imposta-di-soggiorno.pdf")
        self.assertEqual(rows["Tariffe 2026"]["local_file"], "files/tariffe-2026.html")
        root = self.root / "archivio" / "locazioni/comune-prova"
        self.assertEqual((root / "files/tariffe-2026.html").read_bytes(), self.HTML)

    def test_refresh_con_sha_invariato_non_riscrive_nulla(self):
        pages = {self.URL_HTML: self.HTML, self.URL_PDF: self.PDF}
        self.run_source(pages)
        before = self.manifest()
        _, _, stats = self.run_source(pages)
        self.assertEqual((stats["downloaded"], stats["updated"], stats["unchanged"]), (0, 0, 2))
        self.assertEqual(self.manifest(), before)

    def test_refresh_con_sha_cambiato_sovrascrive_e_registra_aggiornato(self):
        self.run_source({self.URL_HTML: self.HTML, self.URL_PDF: self.PDF})
        nuovo = b"<html><body>" + b"Regolamento modificato. " * 20 + b"</body></html>"
        _, _, stats = self.run_source({self.URL_HTML: nuovo, self.URL_PDF: self.PDF})
        self.assertEqual((stats["downloaded"], stats["updated"], stats["unchanged"]), (0, 1, 1))
        root = self.root / "archivio" / "locazioni/comune-prova"
        self.assertEqual((root / "files/regolamento-imposta-di-soggiorno.html").read_bytes(), nuovo)
        rows = {r["title"]: r for r in self.manifest()}
        self.assertEqual(len(rows), 3 - 1)
        self.assertEqual(rows["Regolamento imposta di soggiorno"]["sha256"], hashlib.sha256(nuovo).hexdigest())
        self.assertIn("aggiornato", (root / "errors.log").read_text(encoding="utf-8"))

    def test_se_cambia_il_tipo_di_contenuto_il_vecchio_file_sparisce(self):
        self.run_source({self.URL_HTML: self.HTML, self.URL_PDF: self.PDF})
        self.run_source({self.URL_HTML: self.PDF, self.URL_PDF: self.PDF})
        files = sorted(p.name for p in (self.root / "archivio" / "locazioni/comune-prova" / "files").iterdir())
        self.assertEqual(files, ["regolamento-imposta-di-soggiorno.pdf", "tariffe-2026.pdf"])

    def test_un_errore_di_rete_non_ferma_le_altre_voci(self):
        _, _, stats = self.run_source({self.URL_HTML: RuntimeError("HTTP 404"), self.URL_PDF: self.PDF})
        self.assertEqual((stats["downloaded"], stats["errors"]), (1, 1))
        log = (self.root / "archivio" / "locazioni/comune-prova" / "errors.log").read_text(encoding="utf-8")
        self.assertIn("HTTP 404", log)
        self.assertEqual([r["title"] for r in self.manifest()], ["Tariffe 2026"])

    def test_pagina_html_senza_testo_e_un_errore_non_un_documento(self):
        # Es. il modulo di reindirizzamento di Airbnb: nessun contenuto da indicizzare.
        guscio = b'<html><body onload="document.forms[0].submit()"><form><input type="hidden" value="x"></form></body></html>'
        _, _, stats = self.run_source({self.URL_HTML: guscio, self.URL_PDF: self.PDF})
        self.assertEqual((stats["downloaded"], stats["errors"]), (1, 1))
        log = (self.root / "archivio" / "locazioni/comune-prova" / "errors.log").read_text(encoding="utf-8")
        self.assertIn("senza testo", log)

    def test_sorgente_senza_voci_non_fallisce(self):
        _, _, stats = self.run_source({}, name="locazioni/regione-prova")
        self.assertEqual(stats["downloaded"], 0)

    def test_voce_senza_campi_obbligatori_e_rifiutata(self):
        self.yaml_path.write_text("sorgenti:\n  - id: x\n    nome: X\n    voci:\n      - titolo: senza url\n        verificato: 'si'\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            d.curated_sources(self.yaml_path)


if __name__ == "__main__":
    unittest.main()
