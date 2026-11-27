"""Prima di salvare: unisce i file di stato Telegram con la versione appena arrivata su GitHub.
Il giro completo e quello rapido degli interpelli possono girare insieme: senza questa unione
il secondo che salva cancellerebbe cosa ha gia' inviato il primo (e i messaggi partirebbero due volte)."""
import json
import subprocess
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
for nome in ("telegram_inviati.json", "telegram_chat.json"):
    locale = DATA / nome
    try:
        remoto = json.loads(subprocess.run(["git", "show", f"origin/main:data/{nome}"],
                                           capture_output=True, text=True, check=True).stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        continue
    try:
        mio = json.loads(locale.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        mio = {}
    unito = dict(remoto)
    for k, v in mio.items():
        unito[k] = max(v, unito[k]) if (nome == "telegram_inviati.json" and k in unito) else v
    locale.write_text(json.dumps(unito, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    print(f"{nome}: {len(mio)} locali + {len(remoto)} remoti -> {len(unito)}")
