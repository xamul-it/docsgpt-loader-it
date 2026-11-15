"""Adattatore Normattiva (www.normattiva.it): un atto, un articolo per unità.

Vale per qualunque atto raggiungibile con l'URL `.../uri-res/N2Ls?urn:nir:...`
(legge, decreto, codice): la pagina dell'atto contiene l'indice di tutti gli
articoli (albero con libri/titoli/capi) e, per gli atti che li hanno, degli
allegati (nel Codice civile: "Disposizioni sulla legge in generale" e "CODICE
CIVILE"). Il testo dell'articolo si legge in due modi:

- `articolo`: una richiesta per articolo (`/atto/caricaArticolo`), HTML con le
  lettere accentate;
- `akn-indice`: l'export Akoma Ntoso dell'intero atto (una richiesta, ASCII con
  l'apostrofo al posto degli accenti) per il testo + la gerarchia dall'indice
  della pagina dell'atto.

Il server risponde alle richieste dinamiche solo con il cookie di sessione
preso aprendo la pagina dell'atto: se una risposta è un errore, l'adattatore
riapre la pagina (senza cache) e riprova una volta.

Scelte sul testo (vedi il rapporto della fonte): le note "AGGIORNAMENTO (n)" e i
rimandi alle note "(3a)", "((12))" sono tolti; i segni `((` `))` di testo
modificato sono tolti e resta il testo vigente.
"""

from __future__ import annotations

import hashlib
import re
import warnings
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from fonte import FonteError, Rete, Unita, xml_fromstring

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

BASE = "https://www.normattiva.it"
LIVELLI = {"PARTE": 0, "LIBRO": 1, "TITOLO": 2, "CAPO": 3, "SEZIONE": 4, "PARAGRAFO": 5}
RE_LIVELLO = re.compile(r"^(" + "|".join(LIVELLI) + r")\b", re.I)
RE_ROMANO = re.compile(r"^[IVXLCDM]+$")
RE_ETICHETTA_ATTUALE = re.compile(r"^(?!agg\.|orig\.)\S")
RE_NOTA_DOPPIA = re.compile(r"\(\(\d+[a-z]?\)\)")
RE_RIGA_NOTE = re.compile(r"(?m)^[ \t]*(\(\d+[a-z]?\)[ \t]*)+$")
RE_TAGLIO_NOTE = re.compile(r"(?m)^[ \t]*-{5,}[ \t]*$")      # riga di trattini: da qui le note di aggiornamento
RE_NOTA_RIF = re.compile(r"AGGIORNAMENTO \((\d+[a-z]?)\)")
RE_ART = re.compile(r"^Art\.?\s*(\S.*?)\.?\s*$", re.I)


@dataclass
class Voce:
    allegato: str            # titolo dell'allegato o dell'atto (flag 0)
    flag: int                # 0 = articoli dell'atto, 1.. = allegato n.
    etichetta: str           # "art. 42 bis", "art. 314/2", "1"
    url: str                 # richiesta dell'articolo (assoluta)
    versione: str
    percorso: list[str] = field(default_factory=list)

    @property
    def numero(self) -> str:
        return re.sub(r"^art\.?\s*", "", self.etichetta, flags=re.I).strip()

    @property
    def id(self) -> str:
        prefisso = "atto" if self.flag == 0 else _slug(self.allegato)
        return f"{prefisso}-art-{_slug(self.numero)}"


@dataclass
class Indice:
    titolo: str
    url_akn: str
    voci: list[Voce]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _frase(s: str) -> str:
    """MAIUSCOLO -> Maiuscolo (prima lettera maiuscola, il resto minuscolo)."""
    s = s.strip()
    return s[:1].upper() + s[1:].lower() if s.isupper() else s


def _livello(token: str) -> str:
    """'LIBRO PRIMO' -> 'Libro primo'; 'TITOLO II' -> 'Titolo II'."""
    parole = token.split()
    out = [parole[0].capitalize()]
    out += [p if RE_ROMANO.match(p) else p.lower() for p in parole[1:]]
    return " ".join(out)


def _aggiorna_percorso(pila: list[tuple[int, str, object]], token: list[str], nodo=None) -> None:
    """Applica un blocco di intestazione ('LIBRO PRIMO', 'DELLE PERSONE', 'TITOLO I', …)
    alla pila (rango, testo, nodo)."""
    i = 0
    while i < len(token):
        m = RE_LIVELLO.match(token[i])
        if not m:
            i += 1
            continue
        rango = LIVELLI[m.group(1).upper()]
        testo = _livello(token[i])
        if i + 1 < len(token) and not RE_LIVELLO.match(token[i + 1]):
            testo += " - " + _frase(token[i + 1])
            i += 1
        while pila and pila[-1][0] >= rango:
            pila.pop()
        pila.append((rango, testo, nodo))
        i += 1


def _pulito(testo: str) -> str:
    return re.sub(r"[ \t ]+", " ", testo).strip()


def _senza_segni(testo: str) -> str:
    """Toglie i segni `((` `))` di testo modificato."""
    return _pulito(testo.replace("((", "").replace("))", ""))


def _suppa(contenuto: bytes) -> BeautifulSoup:
    t = contenuto.decode("utf-8", "replace")
    return BeautifulSoup(re.sub(r"^\s*<\?xml[^>]*\?>", "", t), "html.parser")


def _ripulisci(grezzo: str, etichetta: str) -> tuple[str, str, str]:
    """Testo grezzo dell'articolo -> (numero, rubrica, testo a blocchi)."""
    note = set(RE_NOTA_RIF.findall(grezzo))
    taglio = RE_TAGLIO_NOTE.search(grezzo)
    t = grezzo[:taglio.start()] if taglio else grezzo
    t = RE_NOTA_DOPPIA.sub("", t)
    for n in note:                                  # rimando attaccato al testo: "...amministrazione.(111)"
        t = re.sub(rf"(?<=\S)\({n}\)", "", t)
    t = RE_RIGA_NOTE.sub("", t)
    t = t.replace("((", "").replace("))", "")
    righe = [_pulito(r) for r in t.split("\n")]
    righe = [r for r in righe if r]
    numero = ""
    for i, r in enumerate(righe[:4]):
        m = RE_ART.match(r)
        if m:
            numero = m.group(1)
            righe = righe[i + 1:]
            break
    rubrica = ""
    if righe and re.fullmatch(r"\(.{1,200}\)\.?", righe[0]):
        rubrica = righe[0].rstrip(".")[1:-1].strip()
        righe = righe[1:]
    if not numero:
        numero = re.sub(r"^art\.?\s*", "", etichetta, flags=re.I).strip()
    return numero, rubrica, "\n\n".join(righe)


class Normattiva:
    nome = "normattiva"
    formati: tuple[str, ...] = ("articolo", "akn-indice")
    metadati_obbligatori: tuple[str, ...] = ("articolo",)
    residui: tuple[str, ...] = (
        r"(?mi)^\s*articolo (precedente|successivo)\s*$",   # solo come riga a sé: nel testo di legge è un rimando
        r"Testo in vigore dal",
        r"AGGIORNAMENTO \(",
        r"\(\(",
        r"(?m)^\s*\(\d+[a-z]?\)\s*$",
        r"(?i)\b(?:e|piu|perche|cioe|gia|puo|cosi)'(?=\s)",   # accenti scritti con l'apostrofo (export AKN)
    )
    candidati_link: tuple[str, ...] = ("normattiva",)

    # --- rete -----------------------------------------------------------

    def riconosce(self, url: str) -> bool:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return (host == "normattiva.it" or host.endswith(".normattiva.it")) and "urn:nir:" in unquote(url)

    def _pagina(self, rete: Rete, cfg: dict, fresca: bool = False) -> Indice:
        r = rete.get(cfg["url"], fresca=fresca)
        if r.status != 200 or b'id="albero"' not in r.content:
            raise FonteError(f"normattiva: la pagina dell'atto non ha l'indice ({r.status}, {r.content_type}): {cfg['url']}")
        return self._indice(r.content, cfg)

    def _con_sessione(self, rete: Rete, cfg: dict, url: str, valida) -> bytes:
        r = rete.get(url)
        if not (r.status == 200 and valida(r.content)):
            rete.get(cfg["url"], fresca=True)       # riapre la pagina: ripristina il cookie di sessione
            r = rete.get(url, fresca=True)
            if not (r.status == 200 and valida(r.content)):
                raise FonteError(f"normattiva: risposta inattesa ({r.status}, {r.content_type}) per {url}")
        return r.content

    # --- indice -----------------------------------------------------------

    def _indice(self, contenuto: bytes, cfg: dict) -> Indice:
        sp = _suppa(contenuto)
        titolo = (sp.title.get_text() if sp.title else "").replace("- Normattiva", "").strip()
        titolo = re.sub(r"^([A-ZÀ-Ý][A-ZÀ-Ý ]+?)(?=\s\d)", lambda m: m.group(1).capitalize(), titolo)
        a = sp.find("a", href=re.compile(r"caricaAKN"))
        url_akn = BASE + a["href"].replace("&amp;", "&") if a else ""
        albero = sp.find(id="albero")
        if albero is None:
            raise FonteError("normattiva: indice degli articoli non trovato")
        voci: list[Voce] = []
        pila: list[tuple[int, str, object]] = []
        allegato = titolo
        for el in albero.select("li.box_allegati, li.box_allegati_small, li.singolo_risultato_collapse, a.numero_articolo"):
            if el.name == "li" and "singolo_risultato_collapse" not in el.get("class", []):
                allegato = _frase(el.get_text(" ", strip=True))
                pila = []
            elif el.name == "li":
                token = [el.select_one(".collapse-header").get_text(" ", strip=True)]
                corpo = el.select_one(".snippets div")
                if corpo is not None:
                    token += [_senza_segni(s) for s in corpo.get_text("\n").split("\n") if s.strip()]
                _aggiorna_percorso(pila, token)
            else:
                etichetta = _pulito(el.get_text())
                m = re.search(r"showArticle\('([^']+)'", el.get("onclick", ""))
                if not etichetta or not RE_ETICHETTA_ATTUALE.match(etichetta) or not m:
                    continue
                href = m.group(1).replace("&amp;", "&")
                flag = int(re.search(r"flagTipoArticolo=(\d+)", href).group(1))
                ver = re.search(r"art\.versione=(\d+)", href)
                nome = titolo if flag == 0 else allegato
                voci.append(Voce(nome, flag, etichetta, BASE + href, ver.group(1) if ver else "",
                                 [nome] + [p[1] for p in pila]))
        if not voci:
            raise FonteError("normattiva: nessun articolo nell'indice")
        ids = [v.id for v in voci]
        doppi = sorted({i for i in ids if ids.count(i) > 1})
        if doppi:
            raise FonteError(f"normattiva: id duplicati nell'indice: {', '.join(doppi[:5])}")
        return Indice(titolo, url_akn, voci)

    # --- interfaccia ------------------------------------------------------

    def versione(self, rete: Rete, cfg: dict) -> str:
        ind = self._pagina(rete, cfg, fresca=True)
        h = hashlib.sha1("\n".join(f"{v.flag}|{v.etichetta}|{v.versione}" for v in ind.voci).encode()).hexdigest()
        return "v" + h[:12]

    def attesi(self, rete: Rete, cfg: dict) -> list[str]:
        return [v.id for v in self._pagina(rete, cfg).voci]

    def casi_limite(self, rete: Rete, cfg: dict) -> list[str]:
        voci = self._pagina(rete, cfg).voci
        casi: list[str] = []
        visti_flag: set[int] = set()
        for v in voci:                                   # il primo di ogni parte
            if v.flag not in visti_flag:
                visti_flag.add(v.flag)
                casi.append(v.id)
        casi.append(voci[-1].id)                         # l'ultimo
        for pred in (lambda n: re.search(r"[a-z]", n, re.I), lambda n: "/" in n):   # bis/ter…, numerazione con barra
            v = next((v for v in voci if pred(v.numero)), None)
            if v:
                casi.append(v.id)
        casi += [i for i in cfg.get("casi_limite", []) if i in {v.id for v in voci}]
        return list(dict.fromkeys(casi))

    def unita(self, rete: Rete, cfg: dict, formato: str, solo: set[str] | None = None) -> list[Unita]:
        if formato not in self.formati:
            raise FonteError(f"normattiva: formato sconosciuto: {formato}")
        ind = self._pagina(rete, cfg)
        voci = [v for v in ind.voci if solo is None or v.id in solo]
        if formato == "articolo":
            return [self._da_html(rete, cfg, ind, v) for v in voci]
        testi = self._akn(rete, cfg, ind)
        out = []
        for v in voci:
            if v.id in testi:
                out.append(self._unita(cfg, v, testi[v.id], {}))
        return out

    # --- formati ----------------------------------------------------------

    def _da_html(self, rete: Rete, cfg: dict, ind: Indice, v: Voce) -> Unita:
        contenuto = self._con_sessione(rete, cfg, v.url, lambda c: b"bodyTesto" in c)
        sp = _suppa(contenuto)
        corpo = sp.select_one(".bodyTesto")
        for n in corpo.select("[class*=preamble], [class*=formula], .art_aggiornamento-akn, script"):
            n.decompose()
        for br in corpo.find_all("br"):
            br.replace_with("\n")
        for b in corpo.find_all(["div", "p", "h2", "h3"]):
            if "ins-akn" in b.get("class", []):       # testo modificato: resta dentro la frase
                continue
            b.insert(0, "\n")
            b.append("\n")
        extra = {}
        inizio = sp.select_one("#artInizio")
        if inizio and _pulito(inizio.get_text()):
            extra["in_vigore_dal"] = _pulito(inizio.get_text())
        return self._unita(cfg, v, corpo.get_text(), extra)

    def _akn(self, rete: Rete, cfg: dict, ind: Indice) -> dict[str, str]:
        if not ind.url_akn:
            raise FonteError("normattiva: la pagina dell'atto non offre l'export AKN")
        dati = self._con_sessione(rete, cfg, ind.url_akn, lambda c: c[:300].lstrip().startswith(b"<?xml") and b"akomaNtoso" in c[:2000])
        radice = xml_fromstring(dati)

        def nome(e):
            return e.tag.rsplit("}", 1)[-1]

        def grezzo(elem) -> str:
            parti = []
            for par in (e for e in elem.iter() if nome(e) == "paragraph"):
                ps = [e for e in par.iter() if nome(e) == "p"]
                testi = ["".join(p.itertext()) for p in ps]
                if testi and re.fullmatch(r"\s*-{5,}\s*", testi[0]):      # da qui le note di aggiornamento
                    break
                parti.append("\n".join(testi))
            return "\n".join(parti)

        per_nome: dict[str, str] = {}
        for e in radice.iter():
            if nome(e) == "doc" and e.get("name"):
                per_nome[e.get("name")] = grezzo(e)
            elif nome(e) == "article" and (e.get("eId") or "").startswith("art_"):
                num = next((c for c in e if nome(c) == "num"), None)
                per_nome["atto|" + (e.get("eId")[4:])] = ("".join(num.itertext()) if num is not None else "") + "\n" + grezzo(e)
        out = {}
        for v in ind.voci:
            chiave = f"atto|{v.numero}" if v.flag == 0 else f"{v.allegato.upper()}-{v.etichetta}"
            g = per_nome.get(chiave)
            if g is None:                                     # il titolo dell'allegato può avere un'altra maiuscola
                g = next((t for n, t in per_nome.items() if n.lower() == f"{v.allegato}-{v.etichetta}".lower()), None)
            if g is not None:
                out[v.id] = g
        return out

    def _unita(self, cfg: dict, v: Voce, grezzo: str, extra: dict[str, str]) -> Unita:
        numero, rubrica, testo = _ripulisci(grezzo, v.etichetta)
        if not testo:
            raise FonteError(f"normattiva: articolo senza testo: {v.id}")
        titolo = f"{v.allegato}, art. {numero}" + (f" - {rubrica}" if rubrica else "")
        urn = re.search(r"urn:nir:[^&\s#]*?(?=~|$)", unquote(cfg["url"]))
        link = {}
        num = re.sub(r"\s+", "", v.numero).lower()
        # Gli articoli con la barra (314/2 … 314/28) non hanno una URN: "~art314/2"
        # apre l'art. 314. Per loro il link è la pagina dell'atto (indice), con
        # un'ancora che lo rende unico: meglio dell'articolo sbagliato.
        if urn and "/" in num:
            link["normattiva"] = f"{BASE}/uri-res/N2Ls?{urn.group(0)}#art{num.replace('/', '-')}"
        elif urn:
            parte = f":{v.flag}" if v.flag else ""
            link["normattiva"] = f"{BASE}/uri-res/N2Ls?{urn.group(0)}{parte}~art{num}"
        return Unita(
            id=v.id, titolo=titolo, percorso=v.percorso, testo=testo,
            metadati={"articolo": numero, "allegato": v.allegato, **extra},
            link_ufficiali=link,
            breve_ammesso=len(testo) < 300 and (bool(re.search(r"\babrogat", testo, re.I)) or testo.rstrip().endswith(".")),
        )
