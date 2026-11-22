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

UA = "AvvisiEspertiScuolaBot/1.0 (+%s)" % (SITO_URL or "aggregatore avvisi pubblici")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})

# ---------------------------------------------------------------- regole
# link verso la sezione albo / bandi della scuola
RE_LINK_ALBO = re.compile(
    r"albo|bandi|avvisi|bacheca|amministrazione[\s_-]*trasparente|gare", re.I)

# il testo deve parlare di una selezione...
RE_SELEZIONE = re.compile(
    r"avviso|selezion|reclutament|manifestazion[ei] di interesse|individuazione|"
    r"bando|procedura comparativa|candidatur|incaric", re.I)

# ...e di una di queste figure (chiave, etichetta)
CATEGORIE = [
    ("psicologo", "Psicologo / sportello d'ascolto",
     r"psicolog|sportello\s+(d['’]\s*|di\s+)?ascolto|supporto psicologico"),
    ("madrelingua", "Esperto madrelingua", r"madre\s*lingua"),
    ("rspp", "RSPP / sicurezza", r"\brspp\b|prevenzione e protezione|medico competente"),
    ("dpo", "DPO / privacy", r"\bdpo\b|protezione dei dati|\brpd\b"),
    ("tutor", "Tutor", r"\btutor"),
    ("collaudo", "Progettista / collaudatore", r"collaudator|progettist"),
    ("asacom", "Assistente comunicazione / educatore",
     r"assistent[ei] (alla |all['’])?(comunicazione|autonomia)|asacom|educator"),
    ("mediatore", "Mediatore linguistico / culturale", r"mediator"),
    ("orientatore", "Orientatore / coach", r"orientator|\bcoach"),
    ("formatore", "Formatore / esperto", r"formator|espert[oi]"),
]
CATEGORIE = [(k, label, re.compile(rx, re.I)) for k, label, rx in CATEGORIE]

# esiti, graduatorie, nomine: non sono avvisi aperti
RE_ESCLUDI = re.compile(
    r"graduatori|esit[oi]\b|verbale|nomina|aggiudicazion|decreto di incarico|"
    r"lettera di incarico|revoca|annullament|rettifica graduatoria|elenco ammessi", re.I)
RE_SOLO_INTERNI = re.compile(r"personale interno|esperti interni|docenti interni", re.I)
RE_ESTERNI = re.compile(r"estern", re.I)


def classifica(testo):
    """Ritorna (chiave, etichetta) se il testo e' un avviso per esperti, altrimenti None."""
    t = " ".join(testo.split())
    if len(t) < 15 or not RE_SELEZIONE.search(t) or RE_ESCLUDI.search(t):
        return None
    if RE_SOLO_INTERNI.search(t) and not RE_ESTERNI.search(t):
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
    u = (u or "").strip().strip("/")
    if not u or u.upper() in ("NON DISPONIBILE", "-", "N.D."):
        return ""
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u
    return u + "/"


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
    istituti = {}
    for path in sorted(DATA.glob("scuole*.csv")):
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        delim = ";" if raw[:2000].count(";") > raw[:2000].count(",") else ","
        for row in csv.DictReader(io.StringIO(raw), delimiter=delim):
            regione = _col(row, "REGIONE").upper()
            if REGIONE != "TUTTE" and regione not in REGIONE.split(","):
                continue
            sito = normalizza_url(_col(row, "SITOWEBSCUOLA"))
            if not sito:
                continue
            codice = _col(row, "CODICEISTITUTORIFERIMENTO") or _col(row, "CODICESCUOLA")
            nome = (_col(row, "DENOMINAZIONEISTITUTORIFERIMENTO")
                    or _col(row, "DENOMINAZIONESCUOLA"))
            if codice in istituti:
                continue
            istituti[codice] = {
                "codice": codice,
                "nome": titolo(nome),
                "comune": _col(row, "DESCRIZIONECOMUNE").title(),
                "provincia": _col(row, "PROVINCIA").title(),
                "regione": regione.title(),
                "sito": sito,
            }
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


def consentito(url):
    host = urlparse(url)
    base = f"{host.scheme}://{host.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = SESSION.get(base + "/robots.txt", timeout=8)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def scarica(url):
    if not consentito(url):
        return None
    r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
    if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html"):
        return None
    r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
    return r.url, BeautifulSoup(r.text, "html.parser")


def testo_link(a):
    parti = [a.get_text(" ", strip=True), a.get("title", "")]
    # spesso l'oggetto dell'atto e' nella riga della tabella, non nel link
    riga = a.find_parent(["tr", "li", "article"])
    if riga is not None:
        parti.append(riga.get_text(" ", strip=True)[:400])
    return " ".join(p for p in parti if p)


def titolo_pulito(a):
    t = a.get_text(" ", strip=True)
    riga = a.find_parent(["tr", "li", "article"])
    if riga is not None and len(t) < 25:
        t = riga.get_text(" ", strip=True)
    t = " ".join(t.split())
    t = re.sub(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s*", "", t)          # data iniziale
    t = re.sub(r"\s*(scarica|download|visualizza|apri|allegat[oi])\s*$", "", t, flags=re.I)
    return t[:300]


def scansiona(ist):
    trovati, errore = [], None
    try:
        res = scarica(ist["sito"])
        if res is None:
            return trovati, "home non raggiungibile"
        home_url, home = res
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
                try:
                    r = scarica(href)
                    if r:
                        pagine.append(r)
                except requests.RequestException:
                    pass
                time.sleep(0.5)
        for url_pagina, soup in pagine:
            for a in soup.find_all("a", href=True):
                cat = classifica(testo_link(a))
                if not cat:
                    continue
                link = urljoin(url_pagina, a["href"])
                trovati.append({
                    "id": hashlib.sha1(link.encode()).hexdigest()[:16],
                    "titolo": titolo_pulito(a),
                    "categoria": cat[0],
                    "categoria_label": cat[1],
                    "link": link,
                    "pagina_albo": url_pagina,
                    "scuola": ist["nome"],
                    "codice": ist["codice"],
                    "comune": ist["comune"],
                    "provincia": ist["provincia"],
                    "regione": ist["regione"],
                })
    except requests.RequestException as e:
        errore = type(e).__name__
    except Exception as e:  # una scuola rotta non deve fermare tutto
        errore = "errore: " + type(e).__name__
    return trovati, errore


# ---------------------------------------------------------------- telegram
def pubblica_telegram(nuovi):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat or not nuovi:
        return
    for av in nuovi[:40]:  # limite per non fare spam al primo giro
        msg = (f"🆕 <b>{av['categoria_label']}</b>\n{av['titolo'][:250]}\n"
               f"🏫 {av['scuola']} – {av['comune']} ({av['provincia']})\n{av['link']}")
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": msg, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=15)
            time.sleep(3)
        except requests.RequestException:
            pass


# ---------------------------------------------------------------- main
def main():
    oggi = dt.date.today().isoformat()
    istituti = carica_istituti()
    if not istituti:
        print("Nessun istituto: carica data/scuole.csv (vedi README).")
        sys.exit(0)
    print(f"Istituti da scansionare: {len(istituti)} (regione: {REGIONE})")

    vecchi = {}
    if OUT.exists():
        try:
            vecchi = {a["id"]: a for a in json.loads(OUT.read_text(encoding="utf-8"))}
        except (json.JSONDecodeError, KeyError):
            vecchi = {}

    trovati, errori = {}, 0
    with cf.ThreadPoolExecutor(max_workers=THREADS) as ex:
        for i, (items, err) in enumerate(ex.map(scansiona, istituti), 1):
            if err:
                errori += 1
            for it in items:
                trovati.setdefault(it["id"], it)
            if i % 100 == 0:
                print(f"  {i}/{len(istituti)} – avvisi finora: {len(trovati)}")

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
        if id_ not in trovati and it.get("trovato_il", oggi) >= limite:
            trovati[id_] = it
    tutti = sorted(trovati.values(), key=lambda a: (a["trovato_il"], a["scuola"]), reverse=True)

    OUT.write_text(json.dumps(tutti, ensure_ascii=False, indent=1), encoding="utf-8")
    STATO.write_text(json.dumps({
        "aggiornato": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "istituti_scansionati": len(istituti),
        "istituti_non_raggiungibili": errori,
        "avvisi_totali": len(tutti),
        "nuovi_oggi": len(nuovi),
        "regione": REGIONE,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Fatto: {len(tutti)} avvisi ({len(nuovi)} nuovi), {errori} scuole non raggiungibili")
    pubblica_telegram(nuovi)


if __name__ == "__main__":
    main()
