#!/usr/bin/env python3
"""Test di qualita_estrazione.py e valuta_link.py (nessuna rete, nessuna fonte reale).

  python3 -m unittest test_qualita -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import estrai
import fonte
import qualita_estrazione as q
import valuta_link as v
from fonte import Risposta, Unita
from test_supporto import CFG, LUNGO, AdattatoreFinto, registrato

OBBL = ("numero",)
RES = [*fonte.RESIDUI_COMUNI, r"\(\(\d+\)\)"]


def U(i, testo=None, breve=False, titolo=None):
    return Unita(f"u-{i}", titolo if titolo is not None else f"Unità {i}", ["Parte"],
                 testo or f"Testo numero {i} della fonte, con abbastanza parole per non essere vuoto.",
                 {"numero": str(i)}, {"pagina": f"https://fonte.example/u/{i}"}, breve)


def esito(esiti, nome):
    return next(x for x in esiti if x.nome == nome)


class ControllaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.unita = [U(i) for i in range(6)]
        self.attesi = [u.id for u in self.unita]
        self.rif = {u.id: u.testo for u in self.unita}

    def scrivi(self, unita, d=None):
        d = d or self.tmp
        estrai.scrivi_unita(unita, CFG, d, "v1", verifica=False)
        return d

    def controlla(self, d=None, attesi=None, rif=None):
        return q.controlla(d or self.tmp, attesi or self.attesi, rif if rif is not None else self.rif, OBBL, RES)

    def test_tutto_ok(self):
        self.scrivi(self.unita)
        esiti = self.controlla()
        self.assertTrue(all(x.ok for x in esiti), [x for x in esiti if not x.ok])
        self.assertEqual({x.nome for x in esiti}, {"file_e_manifest", "conteggio_unita", "testo_vuoto_o_navigazione",
                                                 "residui", "titolo", "metadati_obbligatori", "confronto_testo"})

    def test_pagina_di_navigazione(self):   # caso issue #3
        self.unita[5] = U(5, "Vai al contenuto Vai alla navigazione Home / Turismo / Alloggi / Contatti e link utili")
        self.scrivi(self.unita)
        e = esito(self.controlla(), "testo_vuoto_o_navigazione")
        self.assertFalse(e.ok)
        self.assertIn("u-5", e.esempi)

    def test_breve_ammesso_non_e_vuoto(self):
        self.unita[2] = U(2, "Abrogato.", breve=True)
        self.scrivi(self.unita)
        self.assertTrue(esito(self.controlla(rif={}), "testo_vuoto_o_navigazione").ok)

    def test_breve_non_ammesso_e_vuoto(self):
        self.unita[2] = U(2, "Abrogato.")
        self.scrivi(self.unita)
        self.assertFalse(esito(self.controlla(rif={}), "testo_vuoto_o_navigazione").ok)

    def test_mancante(self):
        self.scrivi(self.unita)
        e = esito(self.controlla(attesi=self.attesi + ["u-9999"]), "conteggio_unita")
        self.assertFalse(e.ok)
        self.assertEqual(e.esempi, ["u-9999"])

    def test_residuo(self):
        self.unita[1] = U(1, "Testo con marcatore ((12)) rimasto dentro, abbastanza lungo da non essere vuoto.")
        self.scrivi(self.unita)
        self.assertFalse(esito(self.controlla(rif={}), "residui").ok)

    def test_titolo_mancante(self):
        self.unita[0] = U(0, titolo="")
        self.scrivi(self.unita)
        self.assertFalse(esito(self.controlla(), "titolo").ok)

    def test_metadato_mancante(self):
        self.unita[0].metadati = {}
        self.scrivi(self.unita)
        self.assertFalse(esito(self.controlla(), "metadati_obbligatori").ok)

    def test_unita_divisa_ricomposta(self):
        self.unita.append(Unita("u-lunga", "Lunga", ["Parte"], LUNGO, {"numero": "L"}))
        self.scrivi(self.unita)
        e = esito(self.controlla(attesi=self.attesi + ["u-lunga"], rif={**self.rif, "u-lunga": LUNGO}), "confronto_testo")
        self.assertTrue(e.ok, e)

    def test_testo_diverso_dalla_fonte(self):
        self.scrivi(self.unita)
        e = esito(self.controlla(rif={**self.rif, "u-0": "Tutt'altro testo."}), "confronto_testo")
        self.assertFalse(e.ok)
        self.assertEqual(e.esempi, ["u-0"])

    def test_file_orfano(self):
        self.scrivi(self.unita)
        (self.tmp / "files" / "orfano.html").write_text("x")
        self.assertFalse(esito(self.controlla(), "file_e_manifest").ok)

    def test_cli_su_adattatore_finto(self):
        class ReteSpecchio:   # pagina ufficiale = titolo e testo dell'unità
            richieste_rete = byte_rete = 0
            def get(self, url, fresca=False):
                u = next(x for x in AdattatoreFinto().unita(ReteFinta(""), CFG, "intero") if x.link_ufficiali["pagina"] == url)
                return Risposta(url, 200, "text/html", f"<h1>{u.titolo}</h1><p>{u.testo}</p>".encode())
        with registrato(AdattatoreFinto()), mock.patch("qualita_estrazione.fonte.config", return_value=CFG), \
                mock.patch("qualita_estrazione.Rete", return_value=ReteSpecchio()):
            d = fonte.cartella(self.tmp, CFG)
            estrai.scrivi_unita(AdattatoreFinto().unita(fonte.Rete(None), CFG, "intero"), CFG, d, "v1")
            self.assertEqual(q.main(["--target", str(self.tmp), "--id", "prova"]), 0)
            (d / "files" / "u-3.html").unlink()
            self.assertEqual(q.main(["--target", str(self.tmp), "--id", "prova"]), 1)


class AccordoTest(unittest.TestCase):              # I5
    def test_una_unita_diversa_basta_a_fallire(self):
        scritte = {f"u-{i}": {"testo": "uguale " * 20} for i in range(10)}
        altro = {f"u-{i}": "uguale " * 20 for i in range(9)} | {"u-9": "tutt'altro"}
        self.assertFalse(q.accordo_formati(scritte, altro).ok)

    def test_altro_formato_rotto_non_e_ok(self):
        tmp = Path(tempfile.mkdtemp())
        ad = AdattatoreFinto()
        cfg = {**CFG, "formato": "intero"}
        ad.formati = ("intero", "rotto")
        d = fonte.cartella(tmp, cfg)
        estrai.scrivi_unita(ad.unita(fonte.Rete(None), cfg, "intero"), cfg, d, "v1")
        with registrato(ad), mock.patch("qualita_estrazione.Rete", return_value=ReteFinta("")):
            esiti = {x.nome: x for x in q.controlla_fonte(tmp, cfg)}
        self.assertFalse(esiti["accordo_formati"].ok)

    def test_riscontro_link_sempre_anche_locale(self):
        tmp = Path(tempfile.mkdtemp())
        ad = AdattatoreFinto()
        ad.formati = ("intero",)
        d = fonte.cartella(tmp, CFG)
        estrai.scrivi_unita(ad.unita(fonte.Rete(None), CFG, "intero"), CFG, d, "v1")
        with registrato(ad), mock.patch("qualita_estrazione.Rete", return_value=ReteFinta("pagina vuota")):
            esiti = {x.nome: x for x in q.controlla_fonte(tmp, CFG)}
        self.assertFalse(esiti["riscontro_link"].ok)


class ReteFinta:
    richieste_rete = byte_rete = 0

    def __init__(self, testo, status=200):
        self.testo, self.status = testo, status

    def get(self, url, fresca=False):
        return Risposta(url, self.status, "text/html", self.testo.encode())


TRE = [U(1398, "Nell'ipotesi di rappresentanza senza potere il contratto concluso resta inefficace verso il terzo."),
       U(1399, "Nell'ipotesi prevista dall'articolo precedente il contratto puo' essere ratificato dall'interessato."),
       U(1400, "Le speciali forme di rappresentanza nelle imprese agricole e commerciali sono regolate dal libro quinto.")]


class LinkTest(unittest.TestCase):
    def test_precisione_atto(self):
        pagina = "<html><body>" + " ".join(f"<h2>{u.titolo}</h2><p>{u.testo}</p>" for u in TRE) + "</body></html>"
        self.assertEqual(v.precisione(pagina, TRE[1], [TRE[0], TRE[2]]), "atto")

    def test_precisione_unita(self):
        pagina = f"<html><body><h1>{TRE[1].titolo}</h1><p>{TRE[1].testo}</p></body></html>"
        self.assertEqual(v.precisione(pagina, TRE[1], [TRE[0], TRE[2]]), "unita")

    def test_precisione_assente(self):
        self.assertEqual(v.precisione("<html>Pagina di errore</html>", TRE[1], [TRE[0]]), "assente")

    def test_valuta_link_atto(self):
        pagina = " ".join(f"{u.titolo} {u.testo}" for u in TRE) * 5
        ad = AdattatoreFinto(TRE)
        r = v.valuta_link(ad, TRE, ["u-1399"], ReteFinta(pagina))
        self.assertEqual((r["pagina"]["status_200"], r["pagina"]["atto"]), (100.0, 100.0))
        self.assertEqual(r["locale"]["unita"], 100.0)

    def test_valuta_link_errore(self):
        r = v.valuta_link(AdattatoreFinto(TRE), TRE, ["u-1399"], ReteFinta("errore", status=404))
        self.assertEqual((r["pagina"]["status_200"], r["pagina"]["assente"]), (0.0, 100.0))


if __name__ == "__main__":
    unittest.main()
