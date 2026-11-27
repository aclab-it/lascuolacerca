#!/usr/bin/env python3
"""
Scanner degli avvisi per ESPERTI ESTERNI pubblicati dalle scuole italiane.

Flusso:
 1. legge l'anagrafe scuole (CSV open data MIM) in data/scuole.csv
    e/o una lista manuale di siti in data/siti_extra.txt
 2. per ogni istituto apre la home, trova i link "albo / bandi / avvisi"
 3. in quelle pagine cerca i link il cui testo parla di selezione di
    psicologi, formatori, esperti, tutor, madrelingua, RSPP, DPO, ...
 4. salva tutto in data/avvisi.json (il sito legge questo file)
 5. (opzionale) pubblica i nuovi avvisi su un canale Telegram

Variabili d'ambiente:
  REGIONE              es. "LAZIO" oppure "LAZIO,UMBRIA" oppure "TUTTE" (default)
  MAX_ISTITUTI         limite per test (default 0 = nessun limite)
  THREADS              richieste in parallelo (default 10)
  TELEGRAM_BOT_TOKEN   opzionale
  TELEGRAM_CHAT_ID     opzionale (es. @mio_canale)
  SITO_URL             opzionale, usato nello User-Agent e nei post Telegram
"""
import csv
import collections
import concurrent.futures as cf
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from sigle import SIGLE as SIGLE_PROVINCE

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "avvisi.json"
STATO = DATA / "stato.json"

REGIONE = os.environ.get("REGIONE", "TUTTE").upper()
MAX_ISTITUTI = int(os.environ.get("MAX_ISTITUTI", "0") or 0)
THREADS = int(os.environ.get("THREADS", "10") or 10)
SITO_URL = os.environ.get("SITO_URL", "")
GIORNI_CONSERVAZIONE = 120
TIMEOUT = 15
MAX_PAGINE_ALBO = 4

UA = "LaScuolaCercaBot/1.0 (+%s)" % (SITO_URL or "aggregatore avvisi pubblici")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
_ultima_risposta = __import__("threading").local()   # stato HTTP dell'ultima pagina scaricata (per il report)

# ---------------------------------------------------------------- regole
# link verso la sezione albo / bandi della scuola
RE_LINK_ALBO = re.compile(
    r"albo|bandi|avvisi|bacheca|amministrazione[\s_-]*trasparente|gare", re.I)

# il testo deve parlare di una selezione aperta...
RE_SELEZIONE = re.compile(
    r"avviso|selezion|reclutament|manifestazion[ei] di interesse|bando|"
    r"procedura comparativa|reperimento|ricerca di|candidature", re.I)

# ...e di una di queste figure (chiave, etichetta)
CATEGORIE = [
    ("psicologo", "Psicologo / sportello d'ascolto",
     r"psicolog|sportello\s+(d['’]\s*|di\s+)?ascolto|supporto psicologico"),
    ("madrelingua", "Esperto madrelingua", r"madre\s*lingua"),
    ("rspp", "RSPP / sicurezza", r"\br\.?s\.?p\.?p\b|prevenzione e protezione|medico competente"),
    ("dpo", "DPO / privacy", r"\bdpo\b|protezione dei dati|\brpd\b"),
    ("tutor", "Tutor", r"\btutor(?!ial)"),
    ("collaudo", "Progettista / collaudatore", r"collaudator|progettist"),
    ("asacom", "Assistente comunicazione / educatore",
     r"assistent[ei] (alla |all['’])?(comunicazione|autonomia)|asacom|educator"),
    ("mediatore", "Mediatore linguistico / culturale", r"mediator"),
    ("orientatore", "Orientatore / coach", r"orientator|\bcoach"),
    ("formatore", "Formatore / esperto", r"formator|espert[oi]"),
]
CATEGORIE = [(k, label, re.compile(rx, re.I)) for k, label, rx in CATEGORIE]

# atti che NON sono avvisi aperti: esiti, nomine, determine, contratti...
RE_ESCLUDI = re.compile(
    r"graduatori|esit[oi]\b|verbal[ei]|nomina|aggiudicazion|decret[oi]|determin|"
    r"lettera\W{0,3}(di\W{0,3})?incarico|contratt|revoca|annullament|rettifica|"
    r"elenco (ammessi|dei partecipanti|partecipanti|candidat)|convocazione|commission[ei]|individuazione|affid|"
    r"conferimento incarico (a|al|alla|prof|dott|sig)|accettazione|"
    r"proroga|liquidazion|attestazione|dichiarazion[ei]|interpell", re.I)
RE_INTERNI = re.compile(r"\binterni\b|\binterna\b|\binterno\b|\binterne\b", re.I)
RE_ESTERNI = re.compile(r"estern", re.I)
# testo che in realta' e' un menu del sito
RE_MENU = re.compile(r"amministrazione trasparente|albo on\W?line|registro elettronico|"
                     r"\bptof\b|privacy policy|cookie|area riservata|home\s*page", re.I)

# --- date: tiene solo avvisi recenti
MESI = {m: i for i, m in enumerate(
    "gen feb mar apr mag giu lug ago set ott nov dic".split(), 1)}
RE_DATA_NUM = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d\d)\b")
RE_DATA_NUVOLA = re.compile(r"\b(20\d\d)\s+(\d{1,2})\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)[a-z]*\b", re.I)
RE_DATA_TXT = re.compile(r"\b(\d{1,2})\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)[a-z]*\.?\s+(20\d\d)\b", re.I)
RE_DATA_ISO = re.compile(r"\b(20\d\d)[-_](\d{2})[-_](\d{2})\b")
RE_DATA_WP = re.compile(r"/(20\d\d)/(0[1-9]|1[0-2])/")   # es. /wp-content/uploads/2024/09/
RE_ANNO = re.compile(r"(?<!\d)(20[12]\d)(?!\d)")
GIORNI_VALIDITA = 75


def data_piu_recente(testo):
    """Ritorna (data_iso o None, anno_max o None) dalle date citate nel testo."""
    date = []
    for g, m, a in RE_DATA_NUM.findall(testo):
        date.append((int(a), int(m), int(g)))
    for g, m, a in RE_DATA_TXT.findall(testo):
        date.append((int(a), MESI[m.lower()[:3]], int(g)))
    for a, m, g in RE_DATA_ISO.findall(testo):
        date.append((int(a), int(m), int(g)))
    for a, g, m in RE_DATA_NUVOLA.findall(testo):
        date.append((int(a), MESI[m.lower()[:3]], int(g)))
    for a, m in RE_DATA_WP.findall(testo):
        date.append((int(a), int(m), 28))
    valide = []
    for a, m, g in date:
        try:
            valide.append(dt.date(a, m, g))
        except ValueError:
            pass
    oggi = dt.date.today()
    valide = [d for d in valide if d <= oggi + dt.timedelta(days=400)]
    anni = [int(x) for x in RE_ANNO.findall(testo)]
    anno = max(anni) if anni else None
    # le scadenze stanno nel futuro: conta la data piu' recente fino a oggi + margine
    return (max(valide).isoformat() if valide else None), anno


def recente(testo):
    """False se il testo cita solo date/anni ormai vecchi."""
    data, anno = data_piu_recente(testo)
    oggi = dt.date.today()
    if data:
        return data >= (oggi - dt.timedelta(days=GIORNI_VALIDITA)).isoformat()
    if anno:
        # da settembre valgono anche gli avvisi dell'a.s. che inizia (es. 2026/27)
        return anno >= (oggi.year if oggi.month >= 9 else oggi.year - 1)
    return True  # nessuna data: nel dubbio lo tiene


def classifica(testo):
    """Ritorna (chiave, etichetta) se il testo e' un avviso per esperti, altrimenti None."""
    t = " ".join(testo.split())
    if len(t) < 15 or not RE_SELEZIONE.search(t) or RE_ESCLUDI.search(t):
        return None
    if re.match(r"\s*(domanda|allegat|modell|modulistica|scheda|griglia|informativa|video)", t, re.I):
        return None
    if len(RE_MENU.findall(t)) >= 2 or re.search(r"commm|istituzione commiss", t, re.I):
        return None
    if RE_LINK_GENERICO.match(t.split(" http")[0]):
        return None
    if RE_INTERNI.search(t) and not RE_ESTERNI.search(t):
        return None
    if not recente(t):
        return None
    for chiave, label, rx in CATEGORIE:
        if rx.search(t):
            return chiave, label
    return None


# ---------------------------------------------------------------- anagrafe
def _col(row, *nomi):
    for n in nomi:
        for k, v in row.items():
            if k and k.strip().upper() == n:
                return (v or "").strip()
    return ""


SIGLE = {"Ic", "Iis", "Itis", "Itc", "Ipsia", "Ipseoa", "Isis", "Iiss", "Itt", "Ite", "Cpia",
         "Ddc", "Dd", "Smi", "Ss", "Itet", "Ipsar", "Ipssar", "Ipsc", "Ist", "Is", "Isiss"}


def titolo(nome):
    return " ".join(w.upper() if w in SIGLE or w.strip(".") in SIGLE else w
                    for w in nome.title().split())


def normalizza_url(u):
    """Ripulisce gli indirizzi dell'anagrafe, che spesso sono scritti male:
    'https//www.x.it', 'http:/x.it', 'https://https//x.it', 'wwwx.edu.it', 'www,x.it', spazi..."""
    u = re.sub(r"\s+", "", (u or "")).strip("/")
    if not u or u.upper() in ("NONDISPONIBILE", "-", "N.D.", "ND"):
        return ""
    proto = "http" if re.match(r"http:", u, re.I) and not re.match(r"https", u, re.I) else "https"
    # toglie uno o piu' prefissi di protocollo, anche storpiati (htpp, https//, http:/, ...)
    u = re.sub(r"^(?:h?t{1,2}p{1,2}s?[:;]*/*)+", "", u, flags=re.I)
    u = u.replace(",", ".").lstrip("./")
    host, _, resto = u.partition("/")
    host = host.lower().rstrip(".")
    if re.match(r"www[^.]", host) and host.count(".") >= 2 and not host.startswith("www-"):
        host = "www." + host[3:]          # 'wwwscuola.edu.it' -> 'www.scuola.edu.it'
    host = re.sub(r"\.{2,}", ".", host)
    if not host or "." not in host:
        return ""
    return f"{proto}://{host}/" + (resto.strip("/") + "/" if resto.strip("/") else "")


ANAGRAFI = [  # (dataset MIM, nome file locale)
    ("DS0400SCUANAGRAFESTAT", "scuole.csv"),
    ("DS0410SCUANAGRAFEPAR", "scuole_paritarie.csv"),
]
CATALOGO = ("https://dati.istruzione.it/opendata/opendata/catalogo/elements1/leaf/"
            "?area=Scuole&datasetId={}")


def scarica_anagrafe():
    """Se manca l'elenco scuole, prova a scaricarlo dal portale open data del MIM."""
    for dataset, nome in ANAGRAFI:
        dest = DATA / nome
        if dest.exists():
            continue
        try:
            pagina = CATALOGO.format(dataset)
            html = SESSION.get(pagina, timeout=30).text
            sigla = dataset[6:]  # es. SCUANAGRAFESTAT
            link = sorted(set(re.findall(r'href="([^"]*%s\d*\.csv)"' % sigla, html, re.I)))
            if not link:
                print(f"  [anagrafe] nessun CSV trovato per {dataset}: caricalo a mano in data/{nome}")
                continue
            url = urljoin(pagina, link[-1])  # il piu' recente
            r = SESSION.get(url, timeout=120)
            r.raise_for_status()
            dest.write_bytes(r.content)
            print(f"  [anagrafe] scaricato {url} -> data/{nome}")
        except requests.RequestException as e:
            print(f"  [anagrafe] download fallito per {dataset} ({type(e).__name__}): "
                  f"caricalo a mano in data/{nome}")


def carica_istituti():
    scarica_anagrafe()
    istituti, senza_sito = {}, {}
    for path in sorted(DATA.glob("scuole*.csv")):
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        delim = ";" if raw[:2000].count(";") > raw[:2000].count(",") else ","
        for row in csv.DictReader(io.StringIO(raw), delimiter=delim):
            regione = _col(row, "REGIONE").upper()
            if REGIONE != "TUTTE" and regione not in REGIONE.split(","):
                continue
            sito = normalizza_url(_col(row, "SITOWEBSCUOLA"))
            codice = _col(row, "CODICEISTITUTORIFERIMENTO") or _col(row, "CODICESCUOLA")
            nome = (_col(row, "DENOMINAZIONEISTITUTORIFERIMENTO")
                    or _col(row, "DENOMINAZIONESCUOLA"))
            if not sito:
                # scuola statale senza indirizzo (o scritto in modo illeggibile): quasi tutte
                # hanno il dominio <codice>.edu.it; lo proviamo solo se nessun'altra riga
                # dello stesso istituto ha un indirizzo vero
                if re.fullmatch(r"[A-Z]{2}[A-Z]{2}[0-9A-Z]{6}", codice or "") and not re.match(r"[A-Z]{2}1", codice):
                    senza_sito.setdefault(codice, (row, nome))
                continue
            if codice in istituti:
                continue
            istituti[codice] = {
                "codice": codice,
                "nome": titolo(nome),
                "comune": _col(row, "DESCRIZIONECOMUNE").title(),
                "provincia": _col(row, "PROVINCIA").title(),
                "regione": re.sub(r"\bD'", "d'", regione.title()),
                "sito": sito,
            }
    # (le statali senza indirizzo provate su <codice>.edu.it non rispondevano mai: tolte)
    extra = DATA / "siti_extra.txt"
    if extra.exists():
        # formato: url ; nome scuola ; comune ; provincia ; regione
        for line in extra.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            p = [x.strip() for x in line.split(";")] + [""] * 5
            sito = normalizza_url(p[0])
            if sito:
                istituti["EXTRA-" + sito] = {
                    "codice": "", "nome": p[1] or urlparse(sito).netloc,
                    "comune": p[2], "provincia": p[3], "regione": p[4], "sito": sito}
    lista = list(istituti.values())
    if MAX_ISTITUTI:
        lista = lista[:MAX_ISTITUTI]
    return lista


# ---------------------------------------------------------------- rete
_robots = {}


NOME_BOT = "lascuolacercabot"


def consentito(url):
    """Ignora i blocchi generici del robots.txt (spesso impostazioni di default delle piattaforme),
    ma rispetta quelli rivolti esplicitamente a LaScuolaCercaBot: li' la scuola ha scelto di non comparire."""
    host = urlparse(url)
    base = f"{host.scheme}://{host.netloc}"
    if base not in _robots:
        regole = None
        try:
            with _Turno(base):
                r = SESSION.get(base + "/robots.txt", timeout=8)
            if r.status_code == 200 and re.search(r"(?im)^\s*user-agent\s*:\s*" + NOME_BOT + r"\b", r.text):
                regole = urllib.robotparser.RobotFileParser()
                regole.parse(r.text.splitlines())
        except requests.RequestException:
            pass
        _robots[base] = regole
    return _robots[base] is None or _robots[base].can_fetch(UA, url)


class BloccatoRobots(Exception):
    pass


# ---- gentilezza verso i server condivisi
# Molte scuole stanno sullo stesso server (stesso hosting o stessa piattaforma di albo).
# Le richieste allo stesso server non partono mai piu' di PER_SERVER alla volta e sono
# distanziate di almeno PAUSA_SERVER secondi, anche se arrivano da scuole diverse.
import socket
import threading
PER_SERVER = int(os.environ.get("PER_SERVER", "2") or 2)
PAUSA_SERVER = float(os.environ.get("PAUSA_SERVER", "0.4") or 0.4)
_ip_host, _sem_server, _ultimo_server = {}, {}, {}
_lock_server = threading.Lock()


def _server(url):
    host = (urlparse(url).hostname or "").lower()
    if host not in _ip_host:
        try:
            _ip_host[host] = socket.gethostbyname(host)
        except OSError:
            _ip_host[host] = host
    return _ip_host[host]


class _Turno:
    """Aspetta il proprio turno sul server di destinazione."""
    def __init__(self, url):
        self.srv = _server(url)
        with _lock_server:
            self.sem = _sem_server.setdefault(self.srv, threading.Semaphore(PER_SERVER))

    def __enter__(self):
        self.sem.acquire()
        with _lock_server:
            attesa = _ultimo_server.get(self.srv, 0) + PAUSA_SERVER - time.monotonic()
            _ultimo_server[self.srv] = max(time.monotonic(), _ultimo_server.get(self.srv, 0) + PAUSA_SERVER)
        if attesa > 0:
            time.sleep(attesa)

    def __exit__(self, *exc):
        self.sem.release()


def scarica(url):
    if not consentito(url):
        raise BloccatoRobots(url)
    _ultima_risposta.stato = None
    with _Turno(url):
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
    if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html"):
        _ultima_risposta.stato = f"HTTP {r.status_code}" if r.status_code != 200 else "non html"
        return None
    r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
    return r.url, BeautifulSoup(r.text, "html.parser")


def _riga(a):
    """La riga (tabella/lista) che contiene il link, se e' una riga vera e non un menu."""
    riga = a.find_parent(["tr", "li", "article"])
    if riga is None:
        return ""
    t = riga.get_text(" ", strip=True)
    return t if len(t) <= 600 else ""


def in_menu(a):
    return a.find_parent(["nav", "header", "footer"]) is not None or any(
        re.search(r"menu|navbar|breadcrumb|footer|sidebar", " ".join(p.get("class", []) + [p.get("id") or ""]), re.I)
        for p in a.parents if getattr(p, "get", None) and p.name in ("div", "ul", "aside"))


def testo_link(a):
    parti = [a.get_text(" ", strip=True), a.get("title", ""), _riga(a)]
    # spesso l'oggetto dell'atto e' nella riga della tabella, non nel link
    return " ".join(p for p in parti if p)


RE_LINK_GENERICO = re.compile(r"^(scopri di pi\S*|leggi (tutto|di pi\S*|l'articolo)|continua( a leggere)?|dettagli|"
                              r"visualizza|download|scarica|apri|vai (a|al|ai|alla)\b.*|clicca qui|qui|link( alla?\b.*)?|"
                              r"allegato|pdf)\W*$", re.I)


def titolo_pulito(a):
    t = a.get_text(" ", strip=True)
    if RE_LINK_GENERICO.match(t) and len(t) < 40:
        riga = a.find_parent(["tr", "li", "article", "div"])
        t = riga.get_text(" ", strip=True)[:300] if riga is not None else ""
        t = re.sub(r"\s*(scopri di pi\S*|leggi tutto|continua a leggere)\s*$", "", t, flags=re.I)
    if len(t) < 25 and _riga(a):
        t = _riga(a)
    t = " ".join(t.split())
    t = re.sub(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s*", "", t)          # data iniziale
    t = re.sub(r"\s*(scarica|download|visualizza|apri|allegat[oi])\s*$", "", t, flags=re.I)
    return t[:300]


def alternative(ist):
    """Indirizzi da provare se quello dell'anagrafe non risponde."""
    u = urlparse(ist["sito"])
    host = u.netloc.lower()
    nudo = host[4:] if host.startswith("www.") else host
    cand = [ist["sito"],
            f"https://{'www.' + nudo if host == nudo else nudo}/",
            f"http://{host}/"]
    cod = (ist.get("codice") or "").lower()
    if re.fullmatch(r"[a-z]{4}[0-9a-z]{6}", cod):
        # molte scuole sono passate al dominio <codice>.edu.it
        cand += [f"https://www.{cod}.edu.it/", f"https://{cod}.edu.it/"]
    if not re.search(r"\.[a-z]{2,}$", nudo):     # dominio senza finale (es. 'www.scuolaxyz')
        cand += [f"https://{host}.it/", f"https://{nudo}.edu.it/"]
    base = re.sub(r"\.(gov\.it|it|eu|org|net|com|info)$", "", nudo)
    if not nudo.endswith(".edu.it") and "." in nudo:
        cand.append(f"https://www.{base}.edu.it/")
    visti, out = set(), []
    for c in cand:
        if c not in visti:
            visti.add(c)
            out.append(c)
    return out


# quale motivo mostrare se nessun indirizzo funziona: il piu' informativo, non l'ultimo provato
# (prima "ConnectionError" dell'ultima variante copriva un 403 del sito vero)
PRIORITA_ERRORE = ["HTTP 403", "HTTP 401", "HTTP 429", "HTTP 503", "HTTP 5xx", "HTTP 4xx", "non html",
                   "SSLError", "ReadTimeout", "ConnectTimeout", "TooManyRedirects", "ConnectionError"]


def _rango(err):
    e = err if err in PRIORITA_ERRORE else (err[:6] + "xx" if err.startswith("HTTP ") else err)
    return PRIORITA_ERRORE.index(e) if e in PRIORITA_ERRORE else len(PRIORITA_ERRORE)


def apri_home(ist):
    errori = []
    for url in alternative(ist):
        try:
            res = scarica(url)
            if res is not None:
                return res, None
            errori.append(getattr(_ultima_risposta, "stato", None) or "home non raggiungibile")
        except BloccatoRobots:
            return None, "robots"
        except requests.exceptions.SSLError:
            errori.append("SSLError")
        except requests.RequestException as e:
            errori.append(type(e).__name__)
    if not errori:
        return None, "home non raggiungibile"
    return None, min(errori, key=_rango)


def estrai(pagine, ist, solo_interpelli=False):
    """Dalle pagine lette ricava (avvisi per esperti, interpelli)."""
    import interpelli as ip_mod
    esperti, interp = [], []
    for url_pagina, soup in pagine:
        for a in soup.find_all("a", href=True):
            if in_menu(a):
                continue
            link = urljoin(url_pagina, a["href"])
            testo = testo_link(a) + " " + link
            titolo = titolo_pulito(a)
            if len(titolo) < 12:
                continue
            base = {
                "id": hashlib.sha1(link.encode()).hexdigest()[:16],
                "titolo": titolo,
                "link": link,
                "pagina_albo": url_pagina,
                "scuola": ist["nome"],
                "codice": ist["codice"],
                "comune": ist["comune"],
                "provincia": ist["provincia"],
                "regione": ist["regione"],
                "data_atto": data_piu_recente(testo)[0],
            }
            nome_file = urlparse(link).path.rsplit("/", 1)[-1]
            ip = ip_mod.classifica(testo, titolo + " " + nome_file)
            if ip:
                if recente(testo) or ip.get("scadenza"):
                    interp.append({**base, **ip, "titolo": ip_mod.pulisci(titolo)})
                continue
            if ip_mod.RE_INTERPELLO.search(titolo):
                continue  # interpello generico/menu: non e' nemmeno un avviso per esperti
            if solo_interpelli:
                continue
            cat = classifica(testo)
            if cat:
                esperti.append({**base, "categoria": cat[0], "categoria_label": cat[1]})
    return esperti, interp


ALBI_NOTI = {}          # codice scuola -> pagine albo trovate nei giri precedenti (data/albi.json)
ERRORI_SOTTOPAGINE = collections.Counter()


def scarica_sottopagina(url):
    """Come scarica(), ma con un secondo tentativo: molti siti scolastici sono sugli stessi
    server condivisi e ogni tanto rifiutano una richiesta se ne arrivano tante insieme."""
    for tentativo in (1, 2):
        try:
            return scarica(url)
        except BloccatoRobots:
            return None
        except (requests.ConnectionError, requests.Timeout) as e:
            ERRORI_SOTTOPAGINE[type(e).__name__] += 1
            if tentativo == 1:
                time.sleep(3)
        except requests.RequestException as e:
            ERRORI_SOTTOPAGINE[type(e).__name__] += 1
            return None
    return None


def scansiona(ist):
    """Ritorna (avvisi trovati, errore o None, info per il report di copertura)."""
    trovati, errore = [], None
    info = {"url": None, "albo": 0, "interpelli": [], "albi": []}
    albo_letti = 0
    try:
        res, err = apri_home(ist)
        if res is None:
            return trovati, err, info
        home_url, home = res
        info["url"] = home_url
        pagine = [(home_url, home)]
        visti = {home_url}
        for a in home.find_all("a", href=True):
            if len(pagine) > MAX_PAGINE_ALBO:
                break
            href = urljoin(home_url, a["href"])
            if href in visti or not href.startswith("http"):
                continue
            if RE_LINK_ALBO.search(a.get_text(" ", strip=True)) or RE_LINK_ALBO.search(a["href"]):
                visti.add(href)
                r = scarica_sottopagina(href)
                if r:
                    pagine.append(r)
                    if RE_LINK_ALBO.search(r[0]) or RE_LINK_ALBO.search(a.get_text(" ", strip=True)):
                        albo_letti += 1
                time.sleep(0.5)
        if albo_letti == 0:
            # la home non ha dato link all'albo (sito lento, menu caricato in modo diverso...):
            # riprova con le pagine albo trovate nei giri precedenti
            for href in ALBI_NOTI.get(ist["codice"] or ist["sito"], [])[1:MAX_PAGINE_ALBO + 1]:
                if href in visti:
                    continue
                visti.add(href)
                r = scarica_sottopagina(href)
                if r:
                    pagine.append(r)
                    albo_letti += 1
                time.sleep(0.5)
        trovati, info["interpelli"] = estrai(pagine, ist)
        info["albi"] = [u for u, _ in pagine]
    except requests.RequestException as e:
        errore = type(e).__name__
    except Exception as e:  # una scuola rotta non deve fermare tutto
        errore = "errore: " + type(e).__name__
    info["albo"] = albo_letti
    return trovati, errore, info


MOTIVI = {
    "ConnectionError": "indirizzo inesistente o sito spento",
    "InvalidURL": "indirizzo non valido",
    "errore: LocationParseError": "indirizzo non valido",
    "SSLError": "certificato di sicurezza del sito non valido",
    "ConnectTimeout": "il sito non risponde (troppo lento)",
    "ReadTimeout": "il sito non risponde (troppo lento)",
    "TooManyRedirects": "il sito rimanda all'infinito ad altre pagine",
    "home non raggiungibile": "il sito risponde con un errore o blocca le visite automatiche",
    "robots": "la scuola ha chiesto di non comparire su questo sito",
    "HTTP 403": "il sito blocca le visite automatiche (errore 403)",
    "HTTP 401": "il sito chiede una password",
    "HTTP 429": "il sito limita le visite troppo frequenti (errore 429)",
    "HTTP 404": "la pagina indicata nell'anagrafe non esiste più (errore 404)",
    "HTTP 503": "il sito è temporaneamente non disponibile (errore 503)",
    "HTTP 509": "il sito ha esaurito il traffico concesso dal suo hosting (errore 509)",
    "non html": "all'indirizzo non c'è una pagina web (es. un file)",
}


def voce_copertura(ist, err, info, n_avvisi):
    if err or not info["url"]:
        esito, motivo = "err", MOTIVI.get(err) or (f"il sito risponde con un errore ({err})" if str(err).startswith("HTTP ") else "errore tecnico del sito")
    elif info["albo"] == 0:
        esito, motivo = "home", "sito raggiunto, ma la sezione albo/bandi non è stata trovata"
    else:
        esito, motivo = "ok", ""
    return {"c": ist["codice"], "n": ist["nome"], "m": ist["comune"], "p": ist["provincia"],
            "r": ist["regione"], "s": info["url"] or ist["sito"], "e": esito, "x": motivo,
            "a": n_avvisi}


# ---------------------------------------------------------------- telegram
def pubblica_telegram(sezione, elementi):
    try:
        import telegram
        telegram.pubblica(sezione, elementi, DATA, SITO_URL)
    except Exception as e:  # Telegram non deve mai bloccare l'aggiornamento
        print(f"  [telegram] errore: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- main
def main():
    oggi = dt.date.today().isoformat()
    istituti = carica_istituti()
    if not istituti:
        print("Nessun istituto: carica data/scuole.csv (vedi README).")
        sys.exit(0)
    print(f"Istituti da scansionare: {len(istituti)} (regione: {REGIONE})")
    # ordine mescolato: le scuole dello stesso hosting (spesso vicine nell'elenco) non arrivano tutte insieme
    __import__("random").Random(oggi).shuffle(istituti)

    vecchi = {}
    if OUT.exists():
        try:
            vecchi = {a["id"]: a for a in json.loads(OUT.read_text(encoding="utf-8"))}
        except (json.JSONDecodeError, KeyError):
            vecchi = {}

    try:
        ALBI_NOTI.update({k: v.get("albi", []) for k, v in
                          json.loads((DATA / "albi.json").read_text(encoding="utf-8")).items()})
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        pass

    trovati, errori = {}, 0
    tipi_errore = collections.Counter()
    with cf.ThreadPoolExecutor(max_workers=THREADS) as ex:
        copertura, tutti_interpelli, albi = [], [], {}
        for i, (ist, (items, err, info)) in enumerate(zip(istituti, ex.map(scansiona, istituti)), 1):
            tutti_interpelli.extend(info.get("interpelli", []))
            chiave_albi = ist["codice"] or ist["sito"]
            if len(info.get("albi") or []) <= 1 and len(ALBI_NOTI.get(chiave_albi, [])) > 1:
                # giro andato male per questa scuola: non dimenticare le pagine albo note
                albi[chiave_albi] = {**ist, "albi": ALBI_NOTI[chiave_albi]}
            elif info.get("albi"):
                albi[chiave_albi] = {**ist, "albi": info["albi"]}
            if err:
                errori += 1
                tipi_errore[err] += 1
            copertura.append(voce_copertura(ist, err, info, len({x["id"] for x in items})))
            for it in items:
                trovati.setdefault(it["id"], it)
            if i % 100 == 0:
                print(f"  {i}/{len(istituti)} – avvisi finora: {len(trovati)}")
    print(f"  errori sulle pagine interne (dopo il secondo tentativo conta 1 per tentativo): {dict(ERRORI_SOTTOPAGINE)}")

    nuovi = []
    for id_, it in trovati.items():
        if id_ in vecchi:
            it["trovato_il"] = vecchi[id_].get("trovato_il", oggi)
        else:
            it["trovato_il"] = oggi
            nuovi.append(it)
        it["visto_il"] = oggi

    limite = (dt.date.today() - dt.timedelta(days=GIORNI_CONSERVAZIONE)).isoformat()
    # tiene anche gli avvisi non piu' visti (scuole irraggiungibili oggi), finche' recenti
    for id_, it in vecchi.items():
        if (id_ not in trovati and it.get("trovato_il", oggi) >= limite
                and len(it.get("titolo", "").strip()) >= 12
                and classifica(it.get("titolo", "") + " " + it.get("link", ""))):  # riapplica le regole aggiornate
            trovati[id_] = it
    tutti = sorted(trovati.values(), key=lambda a: (a["trovato_il"], a["scuola"]), reverse=True)

    try:
        import geo
        geo.aggiungi_coordinate(tutti, DATA, UA)
    except Exception as e:  # la mappa non deve mai bloccare l'aggiornamento
        print(f"  [mappa] coordinate non aggiunte: {type(e).__name__}: {e}")
    OUT.write_text(json.dumps(tutti, ensure_ascii=False, indent=1), encoding="utf-8")
    copertura.sort(key=lambda c: (c["r"], c["p"], c["n"]))
    (DATA / "copertura.json").write_text(
        json.dumps({"aggiornato": oggi, "scuole": copertura}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    esiti = collections.Counter(c["e"] for c in copertura)
    STATO.write_text(json.dumps({
        "aggiornato": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "istituti_scansionati": len(istituti),
        "istituti_non_raggiungibili": errori,
        "avvisi_totali": len(tutti),
        "nuovi_oggi": len(nuovi),
        "regione": REGIONE,
        "tipi_errore": dict(tipi_errore.most_common()),
        "copertura": {"albo_letto": esiti["ok"], "albo_non_trovato": esiti["home"],
                      "non_raggiungibili": esiti["err"]},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Fatto: {len(tutti)} avvisi ({len(nuovi)} nuovi), {errori} scuole non raggiungibili")
    try:
        import pagine
        n = pagine.genera(tutti, ROOT, SITO_URL, oggi)
        print(f"Pagine statiche generate: {n}")
    except Exception as e:  # le pagine SEO non devono mai bloccare l'aggiornamento
        print(f"  [pagine] generazione fallita: {type(e).__name__}: {e}")
    pubblica_telegram("esperti", tutti)
    (DATA / "albi.json").write_text(json.dumps(albi, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    salva_interpelli(unisci_fonti(tutti_interpelli, leggi_usp()[0] + leggi_piemonte()), len(istituti), completo=True)


# ---------------------------------------------------------------- interpelli
INTERPELLI = DATA / "interpelli.json"


def salva_interpelli(trovati_lista, n_scuole, completo):
    import interpelli as ip_mod
    oggi = dt.date.today().isoformat()
    try:
        vecchi = {a["id"]: a for a in json.loads(INTERPELLI.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        vecchi = {}
    trovati = {}
    for it in trovati_lista:
        it["trovato_il"] = vecchi.get(it["id"], {}).get("trovato_il", oggi)
        trovati.setdefault(it["id"], it)
    nuovi = [it for id_, it in trovati.items() if id_ not in vecchi]
    for id_, it in vecchi.items():  # scuole non lette in questo giro: li tiene finche' validi
        nome_file = urlparse(it.get("link", "")).path.rsplit("/", 1)[-1]
        if id_ not in trovati and ip_mod.classifica(it.get("titolo", ""), it.get("titolo", "") + " " + nome_file):
            trovati[id_] = it
    validi = togli_doppioni([rilocalizza(it) for it in trovati.values() if ip_mod.ancora_valido(it)])
    validi.sort(key=lambda a: (a.get("scadenza") or "9999", a["trovato_il"]))
    try:
        import geo
        geo.aggiungi_coordinate(validi, DATA, UA)
    except Exception as e:
        print(f"  [interpelli] coordinate non aggiunte: {type(e).__name__}: {e}")
    INTERPELLI.write_text(json.dumps(validi, ensure_ascii=False, indent=1), encoding="utf-8")
    aggiorna_pagine_interpelli(trovati_lista, oggi)
    pubblica_telegram("interpelli", validi)
    (DATA / "interpelli_stato.json").write_text(json.dumps({
        "aggiornato": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "giro": "completo" if completo else "rapido",
        "scuole_controllate": n_scuole,
        "interpelli": len(validi),
        "nuovi": len(nuovi),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Interpelli: {len(validi)} aperti ({len(nuovi)} nuovi)")


# ---------------------------------------------------------------- USP
# nome della scuola nel titolo: sigle e parole tipiche, a inizio parola
RE_SCUOLA_TITOLO = re.compile(
    r"(?:^|[–\-|:,]\s*|\bc/o\s+|\bpresso\s+(?:l['’])?)"
    r"((?:I\.\s?C\.|IC|I\.\s?I\.\s?S\.(?:\s?S\.)?|IIS|IISS|ITIS|ITI|ITC|ITT|ITE|ITET|IPSIA|IPSEOA|IPSSAR|ISIS|CPIA|D\.\s?D\.|"
    r"Istituto|"
    r"Liceo|Convitto|Circolo Didattico|Educandato|Primo IC|Secondo IC|\d+°?\s?I\.?C\.?)\b[^–|,;()]{2,60})",
    re.I)
RE_SIGLA = re.compile(r"\(([A-Z]{2})\)")
RE_LINK_NAVIGAZIONE = re.compile(r"/(tag|category|categoria|argomento|author|page)/", re.I)


def province_regioni():
    mappa = {}
    for path in DATA.glob("scuole*.csv"):
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        delim = ";" if raw[:2000].count(";") > raw[:2000].count(",") else ","
        for row in csv.DictReader(io.StringIO(raw), delimiter=delim):
            p, r = _col(row, "PROVINCIA").title(), _col(row, "REGIONE").title()
            if p and r:
                mappa.setdefault(p.lower(), (p, re.sub(r"\bD'", "d'", r)))
    return mappa


def leggi_usp():
    """Interpelli dalle pagine degli Uffici scolastici provinciali (data/usp.csv)."""
    import interpelli as ip_mod
    try:
        righe = list(csv.DictReader(io.StringIO((DATA / "usp.csv").read_text(encoding="utf-8")), delimiter=";"))
    except FileNotFoundError:
        return [], []
    pr = province_regioni()
    trovati, stato, visti = [], [], set()
    for riga in righe:
        url, prov_testo = riga["url"].strip(), riga["provincia"].strip()
        if not url or url in visti:
            continue
        visti.add(url)
        prima = prov_testo.split("/")[0].strip()
        provincia, regione = pr.get(prima.lower(), (prima, ""))
        esito = {"provincia": prov_testo, "url": url, "esito": "ok", "interpelli": 0}
        try:
            r = scarica(url)
            if not r:
                esito["esito"] = "pagina non leggibile"
            else:
                url_pagina, soup = r
                for a in soup.find_all("a", href=True):
                    if in_menu(a):
                        continue
                    link = urljoin(url_pagina, a["href"])
                    if RE_LINK_NAVIGAZIONE.search(urlparse(link).path):
                        continue
                    titolo = ip_mod.pulisci(titolo_pulito(a))
                    if len(titolo) < 15:
                        continue
                    testo = testo_link(a) + " " + link
                    nome_file = urlparse(link).path.rsplit("/", 1)[-1]
                    ip = ip_mod.classifica(testo, titolo + " " + nome_file)
                    if not ip or not (recente(testo) or ip.get("scadenza")):
                        continue
                    m = RE_SCUOLA_TITOLO.search(titolo)
                    prov_it, reg_it = provincia, regione
                    sig = [x for x in RE_SIGLA.findall(titolo) if x in SIGLE_PROVINCE]
                    if sig:  # alcuni uffici ripubblicano interpelli di altre province
                        prov_it, reg_it = pr.get(SIGLE_PROVINCE[sig[-1]].lower(), (SIGLE_PROVINCE[sig[-1]], ""))
                    trovati.append({
                        "id": hashlib.sha1(link.encode()).hexdigest()[:16],
                        "titolo": titolo, "link": link, "pagina_albo": url_pagina,
                        "scuola": (m.group(1).strip(" .") if m else f"Ufficio scolastico di {prov_testo}"),
                        "codice": "", "comune": "", "provincia": prov_it, "regione": reg_it,
                        "data_atto": data_piu_recente(testo)[0], "fonte": "USP", **ip,
                    })
                    esito["interpelli"] += 1
        except BloccatoRobots:
            esito["esito"] = "l'ufficio ha chiesto di non comparire su questo sito"
        except requests.RequestException as e:
            esito["esito"] = MOTIVI.get(type(e).__name__, type(e).__name__)
        stato.append(esito)
        time.sleep(0.5)
    (DATA / "usp_stato.json").write_text(json.dumps(stato, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for e in stato if e["interpelli"])
    print(f"USP: {len(stato)} pagine, {ok} con interpelli, {len(trovati)} interpelli")
    return trovati, stato


# ---- Piemonte: elenco ufficiale degli interpelli dell'USR, una tabella per provincia
PIEMONTE_AMBITI = {"al": "Alessandria", "at": "Asti", "bi": "Biella", "cn": "Cuneo", "no": "Novara",
                   "to": "Torino", "vb": "Verbano-Cusio-Ossola", "vc": "Vercelli"}
PIEMONTE_URL = "https://servizi.istruzionepiemonte.it/interpello{anno}/ric_interpello_ambito_{sigla}.php"
CDC_GRADI = {"AAAA": ("infanzia", False), "EEEE": ("primaria", False), "ADAA": ("infanzia", True),
             "ADEE": ("primaria", True), "ADMM": ("secondaria I grado", True), "ADSS": ("secondaria II grado", True)}


def _data_it(t):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def leggi_piemonte(istituti_per_codice=None):
    """Interpelli 'aperti' dalle tabelle di servizi.istruzionepiemonte.it (tutte le scuole statali piemontesi)."""
    import interpelli as ip_mod
    if istituti_per_codice is None:
        istituti_per_codice = {i["codice"]: i for i in carica_istituti() if i.get("codice")}
    oggi = dt.date.today()
    anni = [oggi.year, oggi.year + 1] if oggi.month >= 9 else [oggi.year, oggi.year - 1]
    trovati = []
    for sigla, provincia in PIEMONTE_AMBITI.items():
        soup = url = None
        for a in anni:
            try:
                r = scarica(PIEMONTE_URL.format(anno=a, sigla=sigla))
            except (requests.RequestException, BloccatoRobots):
                r = None
            if r and r[1].find("table"):
                url, soup = r
                break
        if not soup:
            print(f"  Piemonte {provincia}: elenco non raggiungibile")
            continue
        n = 0
        for tr in soup.find_all("tr"):
            celle = tr.find_all("td")
            if len(celle) < 10:
                continue
            testi = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in celle]
            codice, nome, cdc, cattedra, corso, durata, data_ip, _, stato, scad = testi[:10]
            if stato.lower() != "aperto":
                continue
            scadenza = _data_it(scad)
            if scadenza and scadenza < oggi.isoformat():
                continue
            prog = tr.find("input", attrs={"name": "progr"})
            sigla_cdc = cdc.split(" - ")[0].strip().upper()
            classi, gradi, sostegno = [], [], False
            if sigla_cdc in CDC_GRADI:
                g, sostegno = CDC_GRADI[sigla_cdc]
                gradi = [g]
            elif re.fullmatch(r"[A-B][A-Z0-9]\d{2}", sigla_cdc):
                classi = [sigla_cdc]
                gradi = ip_mod.classifica(cdc, cdc).get("gradi", []) if ip_mod.classifica(cdc, cdc) else []
            cattedra = re.sub(r"\s*(Interna|Esterna|Spezzone|Cattedra)\s*$", "", cattedra).strip() or cattedra
            if cattedra.lower() in ("interna", "esterna"):
                cattedra = "cattedra " + cattedra.lower()
            descr = cdc.split(" - ", 1)[-1].strip().capitalize()
            durata = re.sub(r"(\d{4})-(\d{2})-(\d{2})", lambda m: f"{m.group(3)}/{m.group(2)}/{m.group(1)}", durata)
            durata = durata[:1].lower() + durata[1:]
            titolo = (f"Interpello {descr}" if sigla_cdc in CDC_GRADI else f"Interpello {sigla_cdc} {descr}") \
                + f" – {cattedra}, {durata}"
            ist = istituti_per_codice.get(codice, {})
            chiave = f"{url}#interpello-{prog['value'] if prog else codice + data_ip + sigla_cdc}"
            trovati.append({
                "id": hashlib.sha1(chiave.encode()).hexdigest()[:16],
                "titolo": titolo, "link": chiave, "pagina_albo": url,
                "scuola": ist.get("nome") or nome.title(), "codice": codice,
                "comune": ist.get("comune", ""), "provincia": ist.get("provincia") or provincia,
                "regione": "Piemonte", "data_atto": _data_it(data_ip), "fonte": "USP",
                "classi": classi, "sostegno": sostegno, "gradi": gradi, "scadenza": scadenza, "ata": None,
            })
            n += 1
        print(f"  Piemonte {provincia}: {n} interpelli aperti")
    return trovati


# ---- interpelli ripubblicati: "presso l'IIS X di Carbonia" -> provincia del Sud Sardegna
RE_PRESSO = re.compile(r"\bpresso\s+(?:l['’]\s*|la\s+|il\s+)?(.{3,90}?)\s+di\s+([A-ZÀ-Ý][\w'’À-ÿ]+(?:[ -][A-ZÀ-Ýa-z][\w'’À-ÿ]+){0,3}?)"
                       r"(?=\s*[.,;()\-–]|\s+\d|\s*$)")
CAPOLUOGHI_AMBIGUI = {"fermo", "prato", "massa", "lodi", "enna", "como", "asti", "terni", "sondrio"}
_geo_cache = {}


def _geo():
    if not _geo_cache:
        import geo
        _geo_cache["istat"] = geo.carica_istat(DATA)
        _geo_cache["norm"] = geo.norm
        _geo_cache["pr"] = {geo.norm(k): v for k, v in province_regioni().items()}
        cap = {geo.norm(v.split("-")[0].split(" e ")[0]): v for v in SIGLE_PROVINCE.values()}
        for citta, prov in {"Carbonia": "Sulcis Iglesiente", "Iglesias": "Sulcis Iglesiente", "Olbia": "Gallura Nord-Est Sardegna",
                            "Tempio Pausania": "Gallura Nord-Est Sardegna", "Sanluri": "Medio Campidano", "Tortoli": "Ogliastra",
                            "Lanusei": "Ogliastra", "Cesena": "Forli'-Cesena", "Forli": "Forli'-Cesena", "Urbino": "Pesaro e Urbino",
                            "Pesaro": "Pesaro e Urbino", "Andria": "Barletta-Andria-Trani", "Barletta": "Barletta-Andria-Trani",
                            "Trani": "Barletta-Andria-Trani", "Carrara": "Massa-Carrara", "Verbania": "Verbano-Cusio-Ossola",
                            "Monza": "Monza e della Brianza"}.items():
            cap[geo.norm(citta)] = prov
        _geo_cache["capoluoghi"] = cap
    return _geo_cache


def rilocalizza(it):
    """Se il titolo indica una scuola di un'altra provincia, sposta l'interpello li'."""
    g = _geo()
    titolo = it.get("titolo", "")
    nuova = None
    m = RE_PRESSO.search(titolo)
    if m:
        comune = m.group(2).strip()
        cand = g["istat"].get(g["norm"](comune))
        if cand and len({c[2] for c in cand}) == 1:
            nuova = (cand[0][2], comune.title() if comune.isupper() else comune)
            scuola = re.sub(r"[“”\"«»]", "", m.group(1)).strip(" '")
            if 3 <= len(scuola) <= 90:
                it["scuola"] = scuola
    if not nuova:
        for parola in re.findall(r"\b[A-Z][a-zà-ÿ']+(?:\s[A-Z][a-zà-ÿ']+)?\b", titolo):
            k = g["norm"](parola)
            if k in g["capoluoghi"] and k not in CAPOLUOGHI_AMBIGUI:
                nuova = (g["norm"](g["capoluoghi"][k]), "")
                m2 = re.search(r"\bpresso\s+(?:l['’]\s*)?(.+)$", titolo)
                if m2:
                    sc = re.sub(r"\s+\d{1,2}\s+[A-Za-z]+\s+20\d\d.*$", "", m2.group(1))  # data e autore in coda
                    it["scuola"] = re.sub(r"[“”\"«»]", "", sc).strip(" .'")[:60]
                break
    if not nuova:
        return it
    prov_norm, comune = nuova
    if prov_norm == g["norm"](it.get("provincia", "")):
        return it
    prov, reg = g["pr"].get(prov_norm, (prov_norm.title(), it.get("regione", "")))
    it.update({"provincia": prov, "regione": reg, "comune": comune, "codice": ""})
    it.pop("lat", None), it.pop("lon", None)
    return it


def togli_doppioni(lista):
    """Lo stesso interpello ripubblicato da piu' uffici/siti diventa uno (stessa provincia, scuola, classe).
    Due interpelli dello stesso sito restano sempre separati."""
    def k(a):
        sc = re.sub(r"[^a-z0-9]", "", (a.get("scuola") or "").lower().replace("istitutodiistruzionesuperiore", "iis"))[:18]
        return (a.get("provincia", "").lower(), sc, tuple(a.get("classi") or []), bool(a.get("sostegno")),
                tuple(a.get("gradi") or []))
    host = lambda a: urlparse(a.get("pagina_albo") or a.get("link", "")).netloc.lower().removeprefix("www.")
    siti, out = {}, []
    for a in sorted(lista, key=lambda a: a.get("fonte") == "USP"):  # prima le versioni delle scuole
        kk = k(a)
        generico = kk[1].startswith("ufficioscolastico") or not (kk[2] or kk[3])
        if not generico and kk in siti and host(a) not in siti[kk]:
            continue
        siti.setdefault(kk, set()).add(host(a))
        out.append(a)
    return out


def unisci_fonti(da_scuole, da_usp):
    """Lo stesso interpello puo' arrivare da scuola e USP (o due volte, con http e https): uno solo."""
    def chiave(a):
        return (a.get("provincia", "").lower(), re.sub(r"\W+", " ", a["titolo"].lower())[:70])

    def link(a):
        return re.sub(r"^https?://(www\.)?", "", a.get("link", "")).rstrip("/")

    noti, link_noti, out = {chiave(a) for a in da_scuole}, {link(a) for a in da_scuole}, list(da_scuole)
    for a in da_usp:
        if chiave(a) in noti or link(a) in link_noti:
            continue
        noti.add(chiave(a))
        link_noti.add(link(a))
        out.append(a)
    return out


PAGINE_IP = DATA / "pagine_interpelli.json"
GIORNI_PAGINE_IP = 30


def aggiorna_pagine_interpelli(lista, oggi):
    """Ricorda su quali pagine degli albi compaiono interpelli (per il giro rapido)."""
    try:
        pagine = json.loads(PAGINE_IP.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pagine = {}
    for it in lista:
        if it.get("fonte") == "USP" or not it.get("pagina_albo"):
            continue
        pagine[it["pagina_albo"]] = {
            "visto": oggi, "nome": it["scuola"], "codice": it.get("codice", ""),
            "comune": it.get("comune", ""), "provincia": it.get("provincia", ""), "regione": it.get("regione", ""),
        }
    limite = (dt.date.fromisoformat(oggi) - dt.timedelta(days=GIORNI_PAGINE_IP)).isoformat()
    pagine = {u: v for u, v in pagine.items() if v.get("visto", oggi) >= limite}
    PAGINE_IP.write_text(json.dumps(pagine, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


def leggi_albi(voce):
    """Giro rapido: rilegge solo le pagine albo gia' note di una scuola."""
    pagine = []
    for u in voce.get("albi", [])[:MAX_PAGINE_ALBO + 1]:
        try:
            r = scarica(u)
            if r:
                pagine.append(r)
        except (requests.RequestException, BloccatoRobots):
            pass
        time.sleep(0.3)
    try:
        return estrai(pagine, voce, solo_interpelli=True)[1]
    except Exception:
        return []


def main_rapido():
    """Giro rapido: solo le pagine dove negli ultimi 30 giorni sono comparsi interpelli, piu' gli USP."""
    try:
        pagine = json.loads(PAGINE_IP.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pagine = {}
    voci = {}
    if pagine:
        for url, info in pagine.items():
            k = info.get("codice") or info.get("nome")
            v = voci.setdefault(k, {**info, "nome": info.get("nome", ""), "albi": []})
            v["albi"].append(url)
    else:  # prima volta: usa tutti gli albi noti
        try:
            voci = json.loads((DATA / "albi.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            print("Nessun albo noto: serve prima un giro completo.")
            return
    voci = list(voci.values())
    if REGIONE != "TUTTE":
        voci = [v for v in voci if (v.get("regione") or "").upper() in REGIONE.split(",")]
    if MAX_ISTITUTI:
        voci = voci[:MAX_ISTITUTI]
    print(f"Giro rapido interpelli: {len(voci)} scuole, {sum(len(v['albi']) for v in voci)} pagine")
    trovati = []
    with cf.ThreadPoolExecutor(max_workers=THREADS) as ex:
        for lista in ex.map(leggi_albi, voci):
            trovati.extend(lista)
    salva_interpelli(unisci_fonti(trovati, leggi_usp()[0] + leggi_piemonte()), len(voci), completo=False)


if __name__ == "__main__":
    if os.environ.get("MODO", "").lower() == "interpelli":
        main_rapido()
    else:
        main()
