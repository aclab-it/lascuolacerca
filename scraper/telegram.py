"""
Pubblicazione dei nuovi annunci sui canali Telegram.

- data/telegram_canali.csv: quale canale per quale sezione/regione
    sezione;regione;canale
    interpelli;Piemonte;@lascuolacerca_piemonte
    esperti;*;@lascuolacerca_esperti        (* = tutte le regioni)
  Le righe con canale vuoto sono ignorate.
  Canali privati: nella colonna canale si mette il link d'invito (https://t.me/+...).
  Il bot non puo' scrivere a un link d'invito, gli serve l'id numerico del canale:
  lo ricava da getUpdates (quando viene nominato amministratore Telegram glielo
  comunica) e lo salva in data/telegram_chat.json, abbinandolo per titolo
  ("Interpelli <Regione> - La Scuola Cerca", "Esperti esterni - La Scuola Cerca").
- il token del bot sta nel secret GitHub TELEGRAM_BOT_TOKEN (mai nel codice).
- data/telegram_inviati.json ricorda cosa e' gia' stato inviato, per non ripetere.
  Quando un canale viene attivato, segna come "gia' inviato" tutto quello che c'e',
  cosi' non viene inondato dagli annunci vecchi: da li' in poi arrivano solo i nuovi.
"""
import csv
import datetime as dt
import html
import io
import json
import os
import time
from pathlib import Path

import requests

PAUSA = 3.2              # secondi tra un messaggio e l'altro (limiti di Telegram)
GIORNI_MEMORIA = 60

ETICHETTE = {"psicologo": "Psicologo", "formatore": "Formatore / esperto", "tutor": "Tutor",
             "madrelingua": "Madrelingua", "rspp": "RSPP / sicurezza", "dpo": "DPO / privacy",
             "collaudo": "Progettista / collaudatore", "asacom": "Assistente / educatore",
             "mediatore": "Mediatore", "orientatore": "Orientatore / coach"}


def _e(s):
    return html.escape(str(s or ""), quote=False)


def carica_canali(data_dir: Path):
    try:
        testo = (data_dir / "telegram_canali.csv").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    canali = {}
    for r in csv.DictReader(io.StringIO(testo), delimiter=";"):
        if (r.get("canale") or "").strip():
            canali[(r["sezione"].strip().lower(), (r.get("regione") or "*").strip().lower())] = r["canale"].strip()
    return canali


def _norm(t):
    return "".join(ch for ch in (t or "").lower() if ch.isalnum())


def _parola_regione(regione):
    # "Emilia Romagna" -> "emilia", "Friuli-Venezia G." -> "friuli"
    return _norm((regione or "").replace("-", " ").split()[0]) if regione else ""


def risolvi_privati(canali, token, data_dir: Path):
    """Sostituisce i link d'invito con l'id numerico del canale."""
    privati = {k: v for k, v in canali.items() if "t.me/+" in v or "joinchat" in v}
    if not privati:
        return canali
    f = data_dir / "telegram_chat.json"
    try:
        noti = json.loads(f.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        noti = {}
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
        dati = r.json()
        if not dati.get("ok"):
            print(f"  [telegram] getUpdates non riuscito: {dati.get('description')}")
        for u in dati.get("result", []):
            chat = (u.get("my_chat_member") or u.get("channel_post") or u.get("edited_channel_post") or {}).get("chat") or {}
            if chat.get("type") == "channel" and chat.get("title"):
                noti[chat["title"]] = chat["id"]
        print(f"  [telegram] canali noti al bot: {len(noti)} ({len(dati.get('result', []))} aggiornamenti letti)")
    except (requests.RequestException, ValueError) as e:
        print(f"  [telegram] getUpdates: {type(e).__name__}")
    f.write_text(json.dumps(noti, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    out = dict(canali)
    for (sezione, regione), link in privati.items():
        parola = "esperti" if sezione == "esperti" else _parola_regione(regione)
        trovati = [cid for titolo, cid in noti.items()
                   if parola and parola in _norm(titolo) and (sezione != "interpelli" or "interpell" in _norm(titolo))]
        if trovati:
            out[(sezione, regione)] = str(trovati[0])
        else:
            out.pop((sezione, regione))
            print(f"  [telegram] {sezione}/{regione}: id del canale privato non ancora noto "
                  f"(aggiungi il bot come amministratore e scrivi un messaggio nel canale)")
    return out


def canale_per(canali, sezione, regione):
    return canali.get((sezione, (regione or "").lower())) or canali.get((sezione, "*"))


MAX_RIGHE = 15


def _url(sito, sezione, a=None, regione=None):
    from urllib.parse import urlencode
    pagina = "interpelli.html" if sezione == "interpelli" else ""
    q = {}
    if regione or (a and a.get("regione")):
        q["regione"] = regione or a.get("regione")
    if a and a.get("provincia"):
        q["provincia"] = a["provincia"]
    return f"{sito}{pagina}" + (("?" + urlencode(q)) if q else "")


def riga(sezione, a, sito):
    luogo = a.get("comune") or a.get("provincia") or ""
    if sezione == "interpelli":
        tag = " · ".join([*([f"ATA {a['ata']}"] if a.get("ata") else []), *a.get("classi", [])[:2],
                          *(["Sostegno"] if a.get("sostegno") else []), *a.get("gradi", [])[:1]]) or "Interpello"
        scad = (" · scade il " + dt.date.fromisoformat(a["scadenza"]).strftime("%d/%m")) if a.get("scadenza") else ""
    else:
        tag = ETICHETTE.get(a.get("categoria"), a.get("categoria_label", "Avviso"))
        scad = ""
    return (f'• <b>{_e(tag)}</b> · {_e(luogo)} – {_e(a["scuola"][:45])}{scad} '
            f'<a href="{_e(_url(sito, sezione, a))}">→</a>')


def riepilogo(sezione, lista, sito, regione):
    n = len(lista)
    if sezione == "interpelli":
        titolo = f"📚 <b>{n} {'nuovo interpello' if n == 1 else 'nuovi interpelli'}" + (f" in {_e(regione)}" if regione else "") + "</b>"
    else:
        titolo = f"🧑‍🏫 <b>{n} {'nuovo avviso' if n == 1 else 'nuovi avvisi'} per esperti esterni</b>"
    righe = [titolo, ""] + [riga(sezione, a, sito) for a in lista[:MAX_RIGHE]]
    if n > MAX_RIGHE:
        righe.append(f"…e altri {n - MAX_RIGHE}")
    righe += ["", f'👉 <a href="{_e(_url(sito, sezione, regione=regione))}">Vedili tutti su La Scuola Cerca</a>']
    return "\n".join(righe)


def pubblica(sezione, elementi, data_dir: Path, sito=""):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    canali = carica_canali(data_dir)
    if not token or not canali:
        return
    canali = risolvi_privati(canali, token, data_dir)
    file_inviati = data_dir / "telegram_inviati.json"
    oggi = dt.date.today().isoformat()
    try:
        inviati = json.loads(file_inviati.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        inviati = {}
    chiave = lambda a: f"{sezione}:{a['id']}"

    per_canale = {}
    for a in elementi:
        c = canale_per(canali, sezione, a.get("regione"))
        if c:
            per_canale.setdefault(c, []).append(a)
    inviati_ora = 0
    for c, lista in per_canale.items():
        if f"_init:{sezione}:{c}" not in inviati:
            # canale appena attivato: non inondarlo con l'arretrato
            try:
                g = requests.get(f"https://api.telegram.org/bot{token}/getChat", params={"chat_id": c}, timeout=20).json()
                if not g.get("ok"):
                    print(f"  [telegram] {c}: ATTENZIONE canale non raggiungibile: {g.get('description')}")
                    continue
            except (requests.RequestException, ValueError):
                pass
            for a in lista:
                inviati[chiave(a)] = oggi
            inviati[f"_init:{sezione}:{c}"] = "9999-12-31"
            print(f"  [telegram] {c}: attivato, {len(lista)} {sezione} gia' presenti non verranno inviati")
            continue
        nuovi = [x for x in lista if chiave(x) not in inviati]
        if not nuovi:
            continue
        regioni = {x.get("regione") for x in nuovi}
        regione = regioni.pop() if len(regioni) == 1 and sezione == "interpelli" else None
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data={
                "chat_id": c, "text": riepilogo(sezione, nuovi, sito, regione), "parse_mode": "HTML",
                "disable_web_page_preview": "true"})
            if r.status_code == 200:
                for x in nuovi:
                    inviati[chiave(x)] = oggi
                inviati_ora += 1
            else:
                print(f"  [telegram] {c}: errore {r.status_code} {r.text[:150]}")
        except requests.RequestException as e:
            print(f"  [telegram] {c}: {type(e).__name__}")
        time.sleep(PAUSA)
    print(f"  [telegram] {sezione}: {inviati_ora} riepiloghi inviati su {len(per_canale)} canali")

    limite = (dt.date.today() - dt.timedelta(days=GIORNI_MEMORIA)).isoformat()
    inviati = {k: v for k, v in inviati.items() if v >= limite}
    file_inviati.write_text(json.dumps(inviati, sort_keys=True, indent=0), encoding="utf-8")
