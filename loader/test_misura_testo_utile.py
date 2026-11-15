#!/usr/bin/env python3
"""Test di misura_testo_utile.py (nessuna rete).

  python3 -m unittest test_misura_testo_utile -v
"""

import unittest

import misura_testo_utile as m

DOC = "Testo del documento che deve restare nella misura. " * 20
MENU = "<nav>Home Chi siamo Contatti Servizi Notizie</nav>"


class MisuraTest(unittest.TestCase):
    def test_toglie_menu_e_prende_main(self):
        r = m.misura(f"<body>{MENU}<header>Intestazione</header><main><p>{DOC}</p></main><footer>Piede</footer></body>")
        self.assertTrue(r.main)
        self.assertEqual(r.utile, len(DOC.strip()))
        self.assertGreater(r.totale, r.utile)

    def test_non_toglie_antenato_del_main_con_classe_sidebar(self):
        html = f'<body><div class="rt-layout-sidebar main">{MENU}<main><p>{DOC}</p></main></div></body>'
        self.assertEqual(m.misura(html).utile, len(DOC.strip()))

    def test_non_toglie_form_che_avvolge_la_pagina(self):
        html = f'<body><form id="form1">{MENU}<div id="faqs"><p>{DOC}</p></div></form></body>'
        r = m.misura(html)
        self.assertFalse(r.main)
        self.assertEqual(r.utile, len(DOC.strip()))

    def test_pagina_di_login(self):
        html = ('<body><form>Accedi <input name="u"> <input type="password" name="p"></form>'
                '<p>Benvenuto nel portale.</p></body>')
        r = m.misura(html)
        self.assertTrue(r.password)
        self.assertLess(r.utile, 50)

    def test_pagina_a_frame(self):
        # Staytour: <frameset> con la pagina di accesso, testo solo nel ripiego <noframes>.
        html = '<html><frameset><frame src="index_op.php"><noframes><body>Vai alla pagina di accesso</body></noframes></frameset></html>'
        self.assertTrue(m.misura(html).frame)
        self.assertFalse(m.misura(f"<body><main>{DOC}</main></body>").frame)

    def test_sospetti(self):
        self.assertEqual(m.sospetti(m.Misura(totale=5000, utile=4000, password=False, main=True, frame=False)), [])
        motivi = m.sospetti(m.Misura(totale=5000, utile=300, password=True, main=False, frame=True))
        self.assertEqual(len(motivi), 3)
        self.assertIn("300", motivi[0])

    def test_menu_dentro_il_main_tolto(self):
        html = f'<body><main><div class="secondary_menu">{"Voce di menu " * 5}</div><p>{DOC}</p></main></body>'
        self.assertEqual(m.misura(html).utile, len(DOC.strip()))


if __name__ == "__main__":
    unittest.main()
