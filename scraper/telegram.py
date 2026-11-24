"""
Pubblicazione dei nuovi annunci sui canali Telegram.

- data/telegram_canali.csv: quale canale per quale sezione/regione
    sezione;regione;canale
    interpelli;Piemonte;@lascuolacerca_piemonte
    esperti;*;@lascuolacerca_esperti        (* = tutte le regioni)
  Le righe con canale vuoto sono ignorate.
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

MAX_PER_CANALE = 25      # messaggi massimi per canale a ogni giro
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


def canale_per(canali, sezione, regione):
    return canali.get((sezione, (regione or "").lower())) or canali.get((sezione, "*"))


def messaggio(sezione, a, sito):
    luogo = f"{a.get('comune')} ({a.get('provincia')})" if a.get("comune") else f"Provincia di {a.get('provincia')}"
    if sezione == "interpelli":
        tag = " · ".join([*([f"ATA · {a['ata']}"] if a.get("ata") else []), *a.get("classi", []),
                          *(["Sostegno"] if a.get("sostegno") else []), *a.get("gradi", [])[:1]])
        righe = [f"📚 <b>{_e(tag or 'Interpello')}</b>", _e(a["titolo"][:250]), f"🏫 {_e(a['scuola'])} – {_e(luogo)}"]
        if a.get("scadenza"):
            righe.append("⏰ Scade il " + dt.date.fromisoformat(a["scadenza"]).strftime("%d/%m"))
    else:
        righe = [f"🧑‍🏫 <b>{_e(ETICHETTE.get(a.get('categoria'), a.get('categoria_label', 'Avviso')))}</b>",
                 _e(a["titolo"][:250]), f"🏫 {_e(a['scuola'])} – {_e(luogo)}"]
    righe.append(_e(a["link"]))
    if sito:
        righe.append(f'<a href="{_e(sito)}{"interpelli.html" if sezione == "interpelli" else ""}">Tutti gli annunci su La Scuola Cerca</a>')
    return "\n".join(righe)


def pubblica(sezione, elementi, data_dir: Path, sito=""):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    canali = carica_canali(data_dir)
    if not token or not canali:
        return
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
            for a in lista:
                inviati[chiave(a)] = oggi
            inviati[f"_init:{sezione}:{c}"] = "9999-12-31"
            print(f"  [telegram] {c}: attivato, {len(lista)} {sezione} gia' presenti non verranno inviati")
            continue
        for a in [x for x in lista if chiave(x) not in inviati][:MAX_PER_CANALE]:
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data={
                    "chat_id": c, "text": messaggio(sezione, a, sito), "parse_mode": "HTML",
                    "disable_web_page_preview": "true"})
                if r.status_code == 200:
                    inviati[chiave(a)] = oggi
                    inviati_ora += 1
                elif r.status_code == 429:  # troppi messaggi: il resto al prossimo giro
                    time.sleep(int(r.json().get("parameters", {}).get("retry_after", 30)))
                else:
                    print(f"  [telegram] {c}: errore {r.status_code} {r.text[:120]}")
                    break  # canale sbagliato o bot non amministratore
            except requests.RequestException:
                pass
            time.sleep(PAUSA)
    print(f"  [telegram] {sezione}: inviati {inviati_ora} messaggi su {len(per_canale)} canali")

    limite = (dt.date.today() - dt.timedelta(days=GIORNI_MEMORIA)).isoformat()
    inviati = {k: v for k, v in inviati.items() if v >= limite}
    file_inviati.write_text(json.dumps(inviati, sort_keys=True, indent=0), encoding="utf-8")
