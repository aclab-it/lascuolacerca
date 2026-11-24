"""
Coordinate dei comuni per la mappa.

Usa il servizio Nominatim di OpenStreetMap, rispettandone le regole d'uso:
al massimo 1 richiesta al secondo, User-Agent riconoscibile e cache su file,
cosi' ogni comune viene cercato una sola volta (data/comuni_coord.json).
"""
import json
import time
from pathlib import Path

import requests

URL = "https://nominatim.openstreetmap.org/search"
MAX_PER_GIRO = 400  # nuovi comuni geocodificati per esecuzione


def _chiave(comune, provincia):
    return f"{(comune or '').strip().lower()}|{(provincia or '').strip().lower()}"


def aggiungi_coordinate(avvisi, data_dir: Path, user_agent: str):
    cache_file = data_dir / "comuni_coord.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    da_cercare = []
    for a in avvisi:
        k = _chiave(a.get("comune"), a.get("provincia"))
        if a.get("comune") and k not in cache and k not in da_cercare:
            da_cercare.append(k)

    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept-Language": "it"})
    cercati = 0
    for k in da_cercare[:MAX_PER_GIRO]:
        comune, provincia = k.split("|")
        coord = None
        for params in (
            {"city": comune, "county": provincia, "country": "Italia"},
            {"q": f"{comune}, {provincia}, Italia"},
        ):
            try:
                r = s.get(URL, params={**params, "format": "json", "limit": 1}, timeout=20)
                time.sleep(1.1)
                if r.status_code == 200 and r.json():
                    coord = [round(float(r.json()[0]["lat"]), 4), round(float(r.json()[0]["lon"]), 4)]
                    break
            except (requests.RequestException, ValueError, KeyError, IndexError):
                time.sleep(1.1)
        cache[k] = coord  # anche None: non lo ricerca all'infinito
        cercati += 1

    for a in avvisi:
        c = cache.get(_chiave(a.get("comune"), a.get("provincia")))
        if c:
            a["lat"], a["lon"] = c

    cache_file.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    trovati = sum(1 for a in avvisi if "lat" in a)
    print(f"  [mappa] comuni cercati ora: {cercati}, avvisi con coordinate: {trovati}/{len(avvisi)}")
