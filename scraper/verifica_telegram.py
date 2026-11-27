"""Controlla i canali Telegram di data/telegram_canali.csv: esistono? il bot e' amministratore?
Non pubblica niente. Lanciato a mano dal workflow "Verifica Telegram"."""
import os
from pathlib import Path

import requests

import telegram

DATA = Path(__file__).resolve().parent.parent / "data"
token = os.environ["TELEGRAM_BOT_TOKEN"]
api = f"https://api.telegram.org/bot{token}/"
me = requests.get(api + "getMe", timeout=20).json()["result"]["id"]
wh = requests.get(api + "getWebhookInfo", timeout=20).json().get("result", {})
print(f"webhook impostato: {'SI' if wh.get('url') else 'no'}")
_u = requests.get(api + "getUpdates", timeout=20).json()
print(f"getUpdates: ok={_u.get('ok')} aggiornamenti={len(_u.get('result', []))} {_u.get('description', '')}")
for _x in _u.get("result", []):
    _tipo = [k for k in _x if k != "update_id"][0]
    _chat = (_x[_tipo].get("chat") or {}) if isinstance(_x[_tipo], dict) else {}
    print(f"   - {_tipo}: {_chat.get('type')} '{_chat.get('title')}'")

canali = telegram.carica_canali(DATA)
risolti = telegram.risolvi_privati(dict(canali), token, DATA)
for (sezione, regione), originale in sorted(canali.items()):
    c = risolti.get((sezione, regione))
    nome = f"{sezione}/{regione}"
    if not c:
        print(f"❌ {nome:28} {originale}: canale privato non ancora visto dal bot")
        continue
    g = requests.get(api + "getChat", params={"chat_id": c}, timeout=20).json()
    if not g.get("ok"):
        print(f"❌ {nome:28} {originale}: {g.get('description')}")
        continue
    m = requests.get(api + "getChatMember", params={"chat_id": c, "user_id": me}, timeout=20).json()
    stato = (m.get("result") or {}).get("status", m.get("description"))
    puo = (m.get("result") or {}).get("can_post_messages")
    ok = stato in ("administrator", "creator") and puo is not False
    print(f"{'✅' if ok else '⚠️'} {nome:28} {originale}: '{g['result'].get('title')}' – bot: {stato}"
          + ("" if puo is not False else " (senza permesso di pubblicare)"))
