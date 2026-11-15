#!/usr/bin/env python3
"""Test dell'adattatore Normattiva sulle fixture reali del Codice civile
(R.D. 262/1942): pagina dell'atto ritagliata, articoli HTML e export AKN ritagliato.

  python3 -m unittest test_normattiva -v
"""

import json
import unittest
from pathlib import Path

import adattatori
import fonte

CARTELLA = Path(__file__).resolve().parent / "fixtures" / "normattiva"
CFG = json.loads((CARTELLA / "contratto.json").read_text())["config"]
AD = adattatori.adattatore("normattiva")
URL = CFG["url"]


def unita(formato, *ids):
    return {u.id: u for u in AD.unita(fonte.ReteFixture(CARTELLA), CFG, formato, solo=set(ids))}


class ReteFinta(fonte.ReteFixture):
    """Rete delle fixture con risposte sostituite o in errore finché la pagina dell'atto non è riaperta."""

    def __init__(self, sostituti=None, errore_finche_riaperta=False):
        super().__init__(CARTELLA)
        self.sostituti = sostituti or {}
        self.errore = errore_finche_riaperta
        self.riaperture = 0

    def get(self, url, fresca=False):
        if url == URL and fresca:
            self.riaperture += 1
            self.errore = False
        if url in self.sostituti:
            status, ctype, corpo = self.sostituti[url]
            return fonte.Risposta(url, status, ctype, corpo)
        r = super().get(url, fresca)
        if self.errore and url != URL:
            return fonte.Risposta(url, 500, "text/html", b"<html><title>Normattiva - Errore</title></html>")
        return r


class IndiceTest(unittest.TestCase):
    def test_riconosce(self):
        self.assertTrue(AD.riconosce(URL))
        self.assertTrue(AD.riconosce("https://www.normattiva.it/uri-res/N2Ls?urn:nir:stato:legge:1990-08-07;241"))
        self.assertFalse(AD.riconosce("https://www.normattiva.it/"))
        self.assertFalse(AD.riconosce("https://example.org/uri-res/N2Ls?urn:nir:stato:legge:1990-08-07;241"))

    def test_attesi_senza_versioni_precedenti(self):
        attesi = AD.attesi(fonte.ReteFixture(CARTELLA), CFG)
        # l'articolo 5 ha una ventina di versioni (agg.N, orig.) nell'indice: contano una volta sola
        self.assertEqual(attesi.count("codice-civile-art-5"), 1)
        self.assertEqual(len(attesi), 11)
        self.assertIn("codice-civile-art-42-bis", attesi)       # numerazione bis
        self.assertIn("codice-civile-art-314-2", attesi)        # numerazione con barra
        self.assertIn("atto-art-1", attesi)                     # articolo del decreto stesso
        self.assertIn("disposizioni-sulla-legge-in-generale-art-1", attesi)   # numero ripetuto in un allegato

    def test_percorso_senza_segni_di_modifica(self):
        u = unita("articolo", "codice-civile-art-2426", "codice-civile-art-314-2")
        self.assertEqual(u["codice-civile-art-2426"].percorso, [
            "Codice civile", "Libro quinto - Del lavoro", "Titolo V - Delle società",
            "Capo V - Società per azioni", "Sezione IX - Del bilancio"])
        for x in u.values():
            self.assertFalse(any("((" in p or "))" in p for p in x.percorso), x.percorso)
        self.assertEqual(u["codice-civile-art-314-2"].percorso[2], "Titolo VIII - Dell'adozione di persone maggiori di età")

    def test_percorso_si_azzera_tra_allegati(self):
        u = unita("articolo", "disposizioni-sulla-legge-in-generale-art-17", "atto-art-1")
        self.assertEqual(u["disposizioni-sulla-legge-in-generale-art-17"].percorso,
                         ["Disposizioni sulla legge in generale", "Capo II - Dell'applicazione della legge in generale"])
        self.assertEqual(u["atto-art-1"].percorso, ["Regio decreto 16 marzo 1942, n. 262"])

    def test_versione_cambia_con_la_versione_di_un_articolo(self):
        v1 = AD.versione(fonte.ReteFixture(CARTELLA), CFG)
        pagina = (CARTELLA / "atto.html").read_bytes()
        mod = pagina.replace(b"art.versione=1&amp;art.idGruppo=258", b"art.versione=2&amp;art.idGruppo=258")
        self.assertNotEqual(pagina, mod)
        rete = ReteFinta({URL: (200, "text/html", mod)})
        self.assertNotEqual(v1, AD.versione(rete, CFG))
        self.assertEqual(v1, AD.versione(fonte.ReteFixture(CARTELLA), CFG))
        self.assertRegex(v1, r"^v[0-9a-f]{12}$")


class TestoTest(unittest.TestCase):
    def test_abrogato_e_ammesso_corto(self):
        for formato in AD.formati:
            u = unita(formato, "codice-civile-art-3", "codice-civile-art-314-2", "disposizioni-sulla-legge-in-generale-art-17")
            for x in u.values():
                self.assertTrue(x.breve_ammesso, (formato, x.id))
                self.assertRegex(x.testo, r"^ARTICOLO ABROGATO DALLA")
                self.assertNotIn("((", x.testo)
            self.assertEqual(u["codice-civile-art-314-2"].titolo, "Codice civile, art. 314/2")

    def test_note_di_aggiornamento_e_rimandi_tolti(self):
        for formato in AD.formati:
            t = unita(formato, "codice-civile-art-5")["codice-civile-art-5"].testo
            self.assertTrue(t.startswith("Gli atti di disposizione del proprio corpo"), (formato, t[:80]))
            for brutto in ("AGGIORNAMENTO", "(3a)", "(15a)", "((", "289a", "Corte costituzionale"):
                self.assertNotIn(brutto, t, formato)

    def test_bis_con_testo_tutto_modificato(self):
        for formato in AD.formati:
            u = unita(formato, "codice-civile-art-42-bis")["codice-civile-art-42-bis"]
            self.assertEqual(u.titolo, "Codice civile, art. 42-bis - Trasformazione, fusione e scissione")
            self.assertEqual(u.metadati["articolo"], "42-bis")
            self.assertTrue(u.testo.startswith("Se non "), (formato, u.testo[:60]))
            self.assertNotIn("((", u.testo)
            self.assertGreaterEqual(u.testo.count("\n\n"), 3)       # i commi sono blocchi

    def test_articolo_lungo_a_blocchi(self):
        for formato in AD.formati:
            u = unita(formato, "codice-civile-art-2426")["codice-civile-art-2426"]
            self.assertTrue(u.testo.startswith("Nelle valutazioni devono essere osservati"))
            self.assertGreater(len(u.testo), 9000)
            self.assertGreaterEqual(u.testo.count("\n\n"), 10)
            self.assertNotRegex(u.testo, r"\(\(|\)\)")

    def test_articolo_dell_atto_senza_preambolo(self):
        for formato in AD.formati:
            t = unita(formato, "atto-art-1")["atto-art-1"].testo
            self.assertRegex(t, r"^(È|E') approvato il testo del Codice civile")
            self.assertNotIn("VITTORIO EMANUELE", t)
            self.assertNotIn("AGGIORNAMENTO", t)

    def test_numero_ripetuto_in_allegati_diversi_ha_id_diversi(self):
        u = unita("articolo", "disposizioni-sulla-legge-in-generale-art-1", "codice-civile-art-1")
        self.assertEqual(len(u), 2)
        self.assertTrue(u["disposizioni-sulla-legge-in-generale-art-1"].testo.startswith("Sono fonti del diritto"))
        self.assertNotEqual(u["codice-civile-art-1"].testo, u["disposizioni-sulla-legge-in-generale-art-1"].testo)

    def test_accenti_solo_nel_formato_per_articolo(self):
        self.assertIn("integrità", unita("articolo", "codice-civile-art-5")["codice-civile-art-5"].testo)
        self.assertIn("integrita'", unita("akn-indice", "codice-civile-art-5")["codice-civile-art-5"].testo)

    def test_formati_stessa_gerarchia(self):
        ids = ("codice-civile-art-2043", "codice-civile-art-2969", "codice-civile-art-42-bis")
        a, b = unita("articolo", *ids), unita("akn-indice", *ids)
        for i in ids:
            self.assertEqual(a[i].percorso, b[i].percorso)
            self.assertEqual(a[i].titolo, b[i].titolo)

    def test_link_ufficiali(self):
        u = unita("articolo", "codice-civile-art-5", "codice-civile-art-42-bis", "disposizioni-sulla-legge-in-generale-art-1", "atto-art-1")
        base = "https://www.normattiva.it/uri-res/N2Ls?urn:nir:stato:regio.decreto:1942-03-16;262"
        self.assertEqual(u["codice-civile-art-5"].link_ufficiali, {"normattiva": base + ":2~art5"})
        self.assertEqual(u["codice-civile-art-42-bis"].link_ufficiali, {"normattiva": base + ":2~art42bis"})
        self.assertEqual(u["disposizioni-sulla-legge-in-generale-art-1"].link_ufficiali, {"normattiva": base + ":1~art1"})
        self.assertEqual(u["atto-art-1"].link_ufficiali, {"normattiva": base + "~art1"})

    def test_data_di_entrata_in_vigore_solo_per_articolo(self):
        self.assertEqual(unita("articolo", "codice-civile-art-5")["codice-civile-art-5"].metadati["in_vigore_dal"], "31-1-2018")
        self.assertNotIn("in_vigore_dal", unita("akn-indice", "codice-civile-art-5")["codice-civile-art-5"].metadati)

    def test_articolo_con_barra_link_all_atto(self):
        # "~art314/2" su normattiva apre l'art. 314: il link va alla pagina dell'atto.
        u = unita("articolo", "codice-civile-art-314-2", "codice-civile-art-42-bis")
        link = u["codice-civile-art-314-2"].link_ufficiali["normattiva"]
        self.assertTrue(link.endswith(";262#art314-2"), link)
        self.assertNotIn("~art314", link)
        self.assertTrue(u["codice-civile-art-42-bis"].link_ufficiali["normattiva"].endswith(";262:2~art42bis"))

    def test_casi_limite(self):
        casi = AD.casi_limite(fonte.ReteFixture(CARTELLA), CFG)
        for atteso in ("codice-civile-art-42-bis", "codice-civile-art-314-2", "codice-civile-art-2969", "atto-art-1"):
            self.assertIn(atteso, casi)


class RispostaInattesaTest(unittest.TestCase):
    def test_html_di_errore_su_un_articolo(self):
        v = [x for x in AD._pagina(fonte.ReteFixture(CARTELLA), CFG).voci if x.id == "codice-civile-art-5"][0]
        errore = (200, "text/html", b"<html><title>Normattiva - Errore</title><body>Errore</body></html>")
        rete = ReteFinta({v.url: errore})
        with self.assertRaises(fonte.FonteError) as c:
            AD.unita(rete, CFG, "articolo", solo={"codice-civile-art-5"})
        self.assertIn("inattesa", str(c.exception))

    def test_akn_che_non_e_xml(self):
        ind = AD._pagina(fonte.ReteFixture(CARTELLA), CFG)
        rete = ReteFinta({ind.url_akn: (200, "text/html", b"<html>Errore</html>")})
        with self.assertRaises(fonte.FonteError):
            AD.unita(rete, CFG, "akn-indice")

    def test_pagina_senza_indice(self):
        rete = ReteFinta({URL: (200, "text/html", b"<html><title>Normattiva</title><body>Home</body></html>")})
        with self.assertRaises(fonte.FonteError):
            AD.attesi(rete, CFG)
        rete = ReteFinta({URL: (404, "text/html", b"")})
        with self.assertRaises(fonte.FonteError):
            AD.versione(rete, CFG)

    def test_sessione_scaduta_riapre_la_pagina_e_riprova(self):
        rete = ReteFinta(errore_finche_riaperta=True)
        u = AD.unita(rete, CFG, "articolo", solo={"codice-civile-art-2043"})
        self.assertEqual([x.id for x in u], ["codice-civile-art-2043"])
        self.assertEqual(rete.riaperture, 1)

class ResiduiDelCodiceTest(unittest.TestCase):
    """Casi emersi dall'estrazione completa del Codice civile (articoli non in indice: si legge la pagina diretta)."""

    def _html(self, n):
        voce = adattatori.normattiva.Voce("Codice civile", 2, f"art. {n}", f"https://test.invalid/art{n}", "1")
        corpo = (CARTELLA / f"art-codice-civile-art-{n}.html").read_bytes()
        rete = ReteFinta({voce.url: (200, "text/html", corpo)})
        ind = adattatori.normattiva.Indice("Codice civile", "", [voce])
        return AD._da_html(rete, CFG, ind, voce)

    def _residui(self, testo):
        import re
        return [r for r in AD.residui if re.search(r, testo)]

    def test_articolo_precedente_nel_testo_di_legge_non_e_navigazione(self):
        u = self._html(8)
        self.assertIn("Nel caso previsto dall'articolo precedente, l'azione", u.testo)
        self.assertEqual(self._residui(u.testo), [])
        self.assertIsNone(fonte.difetto_testo(u.testo, u.breve_ammesso))

    def test_note_di_aggiornamento_inline_tagliate_con_i_rimandi(self):
        u = self._html(529)
        self.assertTrue(u.testo.endswith("rendere conto della propria amministrazione."), u.testo[-80:])
        self.assertEqual(self._residui(u.testo), [])

    def test_testo_modificato_inline_non_spezza_la_frase(self):
        u = self._html(2142)
        self.assertIn("riconoscimento di figli. La composizione", u.testo)
        self.assertTrue(u.testo.endswith("libretto colonico."), u.testo[-60:])
        self.assertEqual(self._residui(u.testo), [])

    def test_articolo_breve_non_abrogato(self):
        u = self._html(968)
        self.assertEqual(u.testo, "La subenfiteusi non è ammessa.")
        self.assertIsNone(fonte.difetto_testo(u.testo, u.breve_ammesso))

    def test_la_navigazione_vera_resta_un_residuo(self):
        self.assertTrue(self._residui("Testo\n\narticolo precedente\narticolo successivo"))


if __name__ == "__main__":
    unittest.main()
