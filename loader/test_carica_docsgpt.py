#!/usr/bin/env python3
"""Test della divisione di una cartella in più sorgenti (issue #7), senza rete.

  python3 -m unittest test_carica_docsgpt -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import carica_docsgpt as c

CATALOGO = """
gruppo: "Fisco"
sorgenti:
  - nome: "Agenzia Entrate"
  - nome: "Normativa - Leggi"
    cartella: normativa
    tipologie: [legge, decreto-legge]
  - nome: "Normativa - Atti UE"
    cartella: normativa
    tipologie: [regolamento-unione-europea]
  - nome: "Normativa - Altri atti"
    cartella: normativa
    resto: true
"""


def scrivi_tipologia(root: Path, tipologia: str, nomi: list[str]) -> None:
    src = root / tipologia
    (src / "files" / "2020").mkdir(parents=True)
    rows = []
    for nome in nomi:
        (src / "files" / "2020" / nome).write_text("testo")
        rows.append(json.dumps({"local_file": f"files/2020/{nome}"}))
    (src / "manifest.jsonl").write_text("\n".join(rows) + "\n")


class Divisione(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "fisco.yaml").write_text(CATALOGO)
        self.splits = c.split_sources(self.tmp / "fisco.yaml")
        patcher = mock.patch.object(c, "SPLITS", self.splits)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.archivio = self.tmp / "archivio"
        norm = self.archivio / "normativa"
        scrivi_tipologia(norm, "legge", ["l1.html", "l2.html"])
        scrivi_tipologia(norm, "decreto-legge", ["dl1.html"])
        scrivi_tipologia(norm, "regolamento-unione-europea", ["ue1.html"])
        scrivi_tipologia(norm, "comunicato", ["c1.html"])
        scrivi_tipologia(norm, "tipologia-nuova", ["n1.html"])

    def test_chiavi_dal_nome_dopo_il_trattino(self):
        self.assertEqual(sorted(self.splits),
                         ["normativa:altri-atti", "normativa:atti-ue", "normativa:leggi"])
        self.assertEqual(self.splits["normativa:atti-ue"]["nome"], "Normativa - Atti UE")

    def test_voci_senza_cartella_non_sono_divise(self):
        self.assertNotIn("Agenzia Entrate", {v["nome"] for v in self.splits.values()})

    def test_ogni_documento_in_una_sola_parte(self):
        parti = {e: [arc for _, arc in c.documents_of(self.archivio, e)] for e in self.splits}
        self.assertEqual(parti["normativa:leggi"],
                         ["decreto-legge/2020/dl1.html", "legge/2020/l1.html", "legge/2020/l2.html"])
        self.assertEqual(parti["normativa:atti-ue"], ["regolamento-unione-europea/2020/ue1.html"])
        # Il resto prende anche le tipologie non elencate da nessuna parte.
        self.assertEqual(parti["normativa:altri-atti"],
                         ["comunicato/2020/c1.html", "tipologia-nuova/2020/n1.html"])
        tutti = sorted(arc for _, arc in c.ente_documents(self.archivio / "normativa"))
        self.assertEqual(sorted(sum(parti.values(), [])), tutti)

    def test_enti_non_divisi_invariati(self):
        self.assertTrue(c.wants_tipologia("agenzia-entrate", "circolari"))
        self.assertEqual(c.ente_dir("locazioni/bologna"), "locazioni/bologna")
        self.assertEqual(c.ente_dir("normativa:leggi"), "normativa")

    def test_tipologia_in_due_parti_e_un_errore(self):
        (self.tmp / "doppia.yaml").write_text(CATALOGO.replace("[regolamento-unione-europea]", "[legge]"))
        with self.assertRaisesRegex(ValueError, "normativa/legge"):
            c.split_sources(self.tmp / "doppia.yaml")

    def test_due_resti_sono_un_errore(self):
        (self.tmp / "resti.yaml").write_text(
            CATALOGO.replace("tipologie: [regolamento-unione-europea]", "resto: true"))
        with self.assertRaisesRegex(ValueError, "resto"):
            c.split_sources(self.tmp / "resti.yaml")

    def test_caricamento_di_una_parte(self):
        """Link sulla cartella divisa (depth 1) e novità tolte solo per le sue tipologie."""
        with mock.patch.dict(c.SOURCE_NAMES, {"normativa:leggi": "Normativa - Leggi"}), \
                mock.patch.object(c, "list_sources", return_value={}), \
                mock.patch.object(c, "upload_zip", return_value=("task", "nuova")) as upload, \
                mock.patch.object(c, "wait_task"), \
                mock.patch.object(c, "agents_using", return_value=[]), \
                mock.patch.object(c, "register_links") as links, \
                mock.patch.object(c, "clear_novita") as novita:
            ok = c.load_ente("http://x", self.archivio, "normativa:leggi", self.tmp / "zip",
                             False, "https://h/docs", self.tmp / "links.json")
        self.assertTrue(ok)
        self.assertEqual(upload.call_args.args[1], "Normativa - Leggi")
        self.assertEqual(upload.call_args.args[2].name, "normativa_leggi.zip")
        links.assert_called_once_with(self.tmp / "links.json", "https://h/docs", "normativa",
                                      "nuova", [], depth=1)
        self.assertEqual([call.args[1] for call in novita.call_args_list],
                         ["normativa/decreto-legge", "normativa/legge"])


class CatalogoVero(unittest.TestCase):
    """Il catalogo del repository divide la normativa e il gruppo la carica tutta."""

    def test_normativa_e_un_gruppo(self):
        self.assertNotIn("normativa", c.SOURCE_NAMES)
        self.assertGreater(len(c.GROUPS["normativa"]), 1)
        self.assertTrue(all(c.SOURCE_NAMES[e].startswith("Normativa - ") for e in c.GROUPS["normativa"]))
        self.assertEqual(sum(v["resto"] for v in c.SPLITS.values()), 1)


if __name__ == "__main__":
    unittest.main()
