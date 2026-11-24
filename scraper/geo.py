"""
Coordinate dei comuni per la mappa.

1) file fisso data/comuni_istat.json: centro di tutti i ~7.900 comuni, calcolato dai
   confini ISTAT pubblicati da Openpolis (github.com/openpolis/geojson-italy, CC-BY-4.0);
2) solo per i rari comuni non trovati li', riserva con il servizio Nominatim di OpenStreetMap, rispettandone le regole d'uso:
al massimo 1 richiesta al secondo, User-Agent riconoscibile e cache su file,
cosi' ogni comune viene cercato una sola volta (data/comuni_coord.json).
"""
import json
import re
import time
import unicodedata
from pathlib import Path

import requests

URL = "https://nominatim.openstreetmap.org/search"
MAX_PER_GIRO = 400  # nuovi comuni geocodificati per esecuzione


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.split("/")[0]
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def carica_istat(data_dir: Path):
    try:
        return json.loads((data_dir / "comuni_istat.json").read_text(encoding="utf-8"))["comuni"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {}


def da_istat(istat, comune, provincia):
    """Cerca il comune nel file ISTAT; gestisce nomi troncati a 30 caratteri e bilingui."""
    n, p = norm(comune), norm(provincia)
    if not n:
        return None
    cand = istat.get(n)
    if not cand:
        cand = [c for k, v in istat.items() if len(n) >= 5 and (k.startswith(n) or (len(k) >= 8 and n.startswith(k)))
                for c in v]
    if not cand:
        return None
    for lat, lon, prov in cand:
        if prov == p or p in prov or prov in p:
            return [lat, lon]
    return [cand[0][0], cand[0][1]] if len(cand) == 1 else None


def _chiave(comune, provincia):
    return f"{(comune or '').strip().lower()}|{(provincia or '').strip().lower()}"


def aggiungi_coordinate(avvisi, data_dir: Path, user_agent: str):
    cache_file = data_dir / "comuni_coord.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    istat = carica_istat(data_dir)
    da_cercare = []
    for a in avvisi:
        k = _chiave(a.get("comune"), a.get("provincia"))
        if not a.get("comune") or k in cache:
            continue
        c = da_istat(istat, a.get("comune"), a.get("provincia"))
        if c:
            cache[k] = c
        elif k not in da_cercare:
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
