"""
Riconoscimento degli INTERPELLI per supplenze (docenti) negli albi delle scuole.

Per ogni interpello estrae: classi di concorso, tipo di posto (sostegno, posto comune,
classe di concorso), grado di scuola e scadenza per candidarsi.
"""
import datetime as dt
import re

RE_INTERPELLO = re.compile(r"interpell", re.I)

# atti che non sono interpelli aperti: esiti, individuazioni, nomine, revoche...
RE_ESCLUDI = re.compile(
    r"esit[oi]\b|individuazion|individuat|nomina|graduatori|decreto di (conferimento|individuazione)|"
    r"revoca|annullament|rettifica|accettazione|contratt|presa di servizio|convocazion|"
    r"elenco (candidati|domande|ammessi)|verbale|deserto", re.I)

# classi di concorso: A022, A-22, A 022, B015, AB24, AJ56 (strumento), ecc.
RE_CDC = re.compile(r"\b(A[A-Z]?|B[A-Z]?)-?(\d{2,3})\b")
RE_CDC_ESTESA = re.compile(r"(?:classe di concorso|classi di concorso|cdc|c\.d\.c\.)\s*:?\s*(A[A-Z]?|B)\s?-?\s?(\d{2,3})\b", re.I)
RE_SOSTEGNO = re.compile(r"sostegno|\bAD(SS|MM|EE|AA)\b", re.I)
GRADI = [
    ("infanzia", re.compile(r"infanzia|\bAAAA\b|\bADAA\b", re.I)),
    ("primaria", re.compile(r"primaria|\bEEEE\b|\bADEE\b|elementare", re.I)),
    ("secondaria I grado", re.compile(r"(secondaria|scuola) (di )?(i|1|primo)\s*(°|grado)|\bADMM\b|scuola media", re.I)),
    ("secondaria II grado", re.compile(r"(secondaria|scuola) (di )?(ii|2|secondo)\s*(°|grado)|\bADSS\b|liceo|istituto tecnico|professionale", re.I)),
]

MESI = {m: i for i, m in enumerate("gen feb mar apr mag giu lug ago set ott nov dic".split(), 1)}
# "entro il 26/11/2026", "scadenza: 26.11.2026", "entro le ore 12:00 del 26 novembre 2026"
RE_SCAD = re.compile(
    r"(?:entro|scadenza|scade|termine|fino al|non oltre)[^0-9]{0,40}?"
    r"(?:ore\s*\d{1,2}[:.]\d{2}\s*(?:del(?:\s+giorno)?\s*)?)?"
    r"(\d{1,2})\s*(?:[/.-]\s*(\d{1,2})|\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)[a-z]*)"
    r"(?:\s*[/.-]?\s*(20\d\d|\d{2}))?", re.I)


def classi_concorso(testo):
    out = []
    testo = re.sub(r"\bA[oO](\d{2})\b", r"A0\1", testo or "")  # "Ao41" -> "A041"
    for pref, num in RE_CDC.findall(testo) + RE_CDC_ESTESA.findall(testo):
        pref = pref.upper()
        num = num.zfill(3) if pref in ("A", "B") else num.zfill(2)
        cdc = f"{pref.upper()}{num}"
        if cdc not in out:
            out.append(cdc)
    return out[:6]


def grado(testo):
    return [g for g, rx in GRADI if rx.search(testo or "")]


def scadenza(testo, oggi=None):
    """Data di scadenza (ISO) se indicata nel testo, altrimenti None."""
    oggi = oggi or dt.date.today()
    for g, m_num, m_txt, a in RE_SCAD.findall(testo or ""):
        try:
            mese = int(m_num) if m_num else MESI[m_txt.lower()[:3]]
            anno = int(a) + 2000 if a and len(a) == 2 else int(a) if a else oggi.year
            d = dt.date(anno, mese, int(g))
            if not a and d < oggi - dt.timedelta(days=180):  # "entro il 3/1" detto a dicembre
                d = dt.date(anno + 1, mese, int(g))
            if oggi - dt.timedelta(days=400) <= d <= oggi + dt.timedelta(days=120):
                return d.isoformat()
        except (ValueError, KeyError):
            continue
    return None


# link di menu/sezione o pagine di servizio, non singoli interpelli
RE_GENERICO = re.compile(
    r"\bmad\b|messa a disposizione|come (mettersi|candidarsi|fare|presentare)|servizio dedicato|"
    r"informativa|modell|modulo|domanda di|vai al|footer|menu|\bargo\b|istruzioni|guida|faq|"
    r"tutti gli interpelli|archivio|sezione", re.I)
# un interpello vero cita almeno uno di questi elementi specifici
RE_SPECIFICO = re.compile(
    r"supplenz|\bposto\b|\bposti\b|\bore\b|cattedr|spezzone|sostegno|classe di concorso|\bcdc\b|"
    r"\bn\.?\s*\d|\bprot|\bAAAA\b|\bEEEE\b|\bAD(MM|SS|EE|AA)\b|\b[AB]\d{2,3}\b|\bA[A-Z]\d{2}\b|"
    r"docente di|insegnante|infanzia|primaria|secondaria|lingua|matematica|italiano|inglese|"
    r"musica|strumento|tecnologia|religione|educazione", re.I)


# per le voci al plurale ("Interpelli ...") serve un dettaglio forte: altrimenti e' una sezione
RE_FORTE = re.compile(
    r"\b(posto|posti|ore|h)\b|\d+\s*h\b|sostegno|\bAAAA\b|\bEEEE\b|\bAD(MM|SS|EE|AA)\b|"
    r"\b[AB]\d{2,3}\b|\bA[A-Z0O]\d{2}\b|classe di concorso|\bn\.?\s*\d", re.I)
RE_MENU = re.compile(r"ptof|blog|calendario|registro|circolari|organigramma|modulistica|contatti|"
                     r"amministrazione trasparente|albo on\W?line|news|home", re.I)


def pulisci(t):
    """Nomi di file come 'Avviso-Interpello_Supplenza_AAAA-2.pdf' -> testo leggibile."""
    t = re.sub(r"\.(pdf|docx?|odt)(\.pades|\.p7m)?\b.*$", "", t, flags=re.I)
    t = re.sub(r"[_]+|(?<=[A-Za-z])-(?=[A-Za-z])", " ", t)
    return " ".join(t.split())


def classifica(testo, titolo=None):
    """Dati dell'interpello se il testo e' un interpello aperto, altrimenti None.
    Se c'e' il titolo, la parola 'interpello' deve stare li' (o nel nome del file), non solo nella riga."""
    t = " ".join(pulisci(testo or "").split())
    tit = pulisci(titolo) if titolo is not None else t
    if len(tit) < 15 or not RE_INTERPELLO.search(tit) or RE_ESCLUDI.search(t):
        return None
    if RE_GENERICO.search(tit) and not RE_CDC.search(tit):
        return None
    if not RE_SPECIFICO.search(tit):
        return None
    if re.search(r"\binterpelli\b", tit, re.I) and not RE_FORTE.search(tit):
        return None
    if len(RE_MENU.findall(tit)) >= 2:
        return None
    cdc = classi_concorso(t)
    sostegno = bool(RE_SOSTEGNO.search(t))
    return {
        "classi": cdc,
        "sostegno": sostegno,
        "gradi": grado(t),
        "scadenza": scadenza(t),
    }


def ancora_valido(it, oggi=None):
    """Un interpello resta in elenco finche' non scade; senza scadenza, per 5 giorni."""
    oggi = (oggi or dt.date.today()).isoformat()
    if it.get("scadenza"):
        return it["scadenza"] >= oggi
    rif = it.get("data_atto") or it.get("trovato_il") or oggi
    limite = (dt.date.fromisoformat(oggi) - dt.timedelta(days=5)).isoformat()
    return rif >= limite
