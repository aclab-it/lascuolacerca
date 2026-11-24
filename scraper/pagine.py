"""
Genera pagine HTML statiche (indicizzabili da Google) per regione e figura,
piu' sitemap.xml e robots.txt. Chiamato da scan.py dopo ogni aggiornamento.

Struttura:
  avvisi/index.html                       -> indice di tutte le pagine
  avvisi/<regione>/index.html             -> tutti gli avvisi di una regione
  avvisi/figura/<figura>/index.html       -> una figura in tutta Italia
  avvisi/<regione>/<figura>/index.html    -> figura + regione
"""
import html
import re
import shutil
import unicodedata
from collections import defaultdict
from pathlib import Path

NOME_SITO = "La Scuola Cerca"
CF_ANALYTICS = ("<!-- Cloudflare Web Analytics --><script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' "
                "data-cf-beacon='{\"token\": \"c80732ec638842438606fb0b7b7c47bd\"}'></script><!-- End Cloudflare Web Analytics -->")

PLURALE = {
    "psicologo": "psicologi e sportello d'ascolto",
    "formatore": "formatori ed esperti esterni",
    "tutor": "tutor",
    "madrelingua": "esperti madrelingua",
    "rspp": "RSPP e sicurezza",
    "dpo": "DPO e privacy",
    "collaudo": "progettisti e collaudatori",
    "asacom": "assistenti alla comunicazione ed educatori",
    "mediatore": "mediatori linguistici e culturali",
    "orientatore": "orientatori e coach",
}

CSS = """*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#f6f5f1;color:#1d1d1f;line-height:1.5}
header{background:#0f5c4d;color:#fff;padding:22px 18px}header .w{max-width:860px;margin:auto}header a{color:#fff;text-decoration:none;font-weight:600}
main{max-width:860px;margin:auto;padding:16px}h1{font-size:1.45rem;margin:4px 0 8px;letter-spacing:-.01em}.intro{color:#4a4a50;margin:0 0 14px}
.cta{display:inline-block;background:#0f5c4d;color:#fff;padding:9px 14px;border-radius:10px;text-decoration:none;font-weight:600;font-size:.92rem;margin-bottom:14px}
.card{background:#fff;border:1px solid #e4e2dc;border-radius:14px;padding:14px 16px;margin-bottom:10px}.card h2{font-size:1rem;margin:0 0 6px}
.card h2 a{color:#1d1d1f;text-decoration:none}.meta{color:#6b6b70;font-size:.86rem}.tag{display:inline-block;background:#e3f0ec;color:#0f5c4d;font-size:.75rem;font-weight:600;border-radius:6px;padding:2px 8px;margin-bottom:6px}
nav.rel{margin-top:22px}nav.rel h3{font-size:1rem;margin:14px 0 6px}nav.rel a{display:inline-block;margin:0 6px 8px 0;padding:5px 10px;background:#fff;border:1px solid #e4e2dc;border-radius:999px;color:#0f5c4d;text-decoration:none;font-size:.85rem}
.vuoto{color:#6b6b70;background:#fff;border:1px dashed #e4e2dc;border-radius:14px;padding:18px}footer{max-width:860px;margin:24px auto;padding:0 16px 30px;color:#6b6b70;font-size:.8rem}"""


def slug(testo):
    t = unicodedata.normalize("NFKD", testo or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "altro"


def _e(s):
    return html.escape(str(s or ""), quote=True)


def _n(n):
    return "1 avviso" if n == 1 else f"{n} avvisi"


def _data(iso):
    mesi = "gen feb mar apr mag giu lug ago set ott nov dic".split()
    try:
        a, m, g = iso.split("-")
        return f"{int(g)} {mesi[int(m) - 1]} {a}"
    except (ValueError, IndexError, AttributeError):
        return iso or ""


def _pagina(base, profondita, titolo, descrizione, h1, intro, avvisi, correlati, url_canonico):
    su = "../" * profondita
    righe = []
    for a in avvisi[:200]:
        righe.append(
            f'<article class="card"><span class="tag">{_e(a.get("categoria_label"))}</span>'
            f'<h2><a href="{_e(a["link"])}" rel="nofollow noopener" target="_blank">{_e(a["titolo"])}</a></h2>'
            f'<div class="meta">🏫 {_e(a["scuola"])} · 📍 {_e(a["comune"])}'
            f'{" (" + _e(a["provincia"]) + ")" if a.get("provincia") else ""} · trovato il {_data(a.get("trovato_il"))}</div></article>')
    if not righe:
        righe.append('<p class="vuoto">Al momento non ci sono avvisi aperti in questa sezione. '
                     'La pagina si aggiorna ogni mattina.</p>')
    blocchi = []
    for titolo_blocco, links in correlati:
        if links:
            blocchi.append(f"<h3>{_e(titolo_blocco)}</h3>" + "".join(
                f'<a href="{su}{_e(href)}">{_e(testo)}</a>' for href, testo in links))
    canon = f'<link rel="canonical" href="{_e(base + url_canonico)}">' if base else ""
    return f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(titolo)}</title><meta name="description" content="{_e(descrizione)}">{canon}
<style>{CSS}</style></head><body>
<header><div class="w"><a href="{su}">📋 {NOME_SITO}</a></div></header>
<main><h1>{_e(h1)}</h1><p class="intro">{_e(intro)}</p>
<a class="cta" href="{su}">🔍 Cerca e filtra tutti gli avvisi</a>
{"".join(righe)}
<nav class="rel">{"".join(blocchi)}</nav></main>
<footer>Avvisi raccolti automaticamente dagli albi online delle scuole: verifica sempre requisiti e scadenze sul documento ufficiale.
Sito non affiliato al Ministero dell'Istruzione e del Merito. · <a href="{su}privacy.html">Privacy e cookie</a></footer>
{CF_ANALYTICS}</body></html>"""


def genera(avvisi, root: Path, base_url: str, oggi: str):
    base = (base_url or "").rstrip("/") + "/" if base_url else ""
    out = root / "avvisi"
    if out.exists():
        shutil.rmtree(out)  # rigenera da zero: niente pagine vecchie orfane
    pagine = []  # url relativi per la sitemap

    per_reg = defaultdict(list)
    per_cat = defaultdict(list)
    per_reg_cat = defaultdict(list)
    for a in avvisi:
        r, c = a.get("regione") or "Altro", a.get("categoria") or "formatore"
        per_reg[r].append(a)
        per_cat[c].append(a)
        per_reg_cat[(r, c)].append(a)

    regioni = sorted(per_reg)
    link_regioni = [(f"avvisi/{slug(r)}/", r) for r in regioni]
    link_figure = [(f"avvisi/figura/{c}/", PLURALE.get(c, c).capitalize())
                   for c in PLURALE if c in per_cat]

    def scrivi(rel, contenuto):
        p = root / rel / "index.html"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contenuto, encoding="utf-8")
        pagine.append(rel)

    # pagine per regione
    for r in regioni:
        lista = per_reg[r]
        figure_qui = [(f"avvisi/{slug(r)}/{c}/", PLURALE.get(c, c).capitalize())
                      for c in PLURALE if (r, c) in per_reg_cat]
        scrivi(f"avvisi/{slug(r)}/", _pagina(
            base, 2,
            f"Avvisi esperti esterni nelle scuole – {r} | {NOME_SITO}",
            f"{_n(len(lista))} delle scuole in {r} per psicologi, formatori, tutor, madrelingua ed esperti esterni. Aggiornato ogni giorno, gratis.",
            f"Avvisi per esperti esterni nelle scuole in {r}",
            f"Bandi e avvisi pubblicati dalle scuole della regione {r} per psicologi, formatori, tutor, "
            f"madrelingua e altri esperti esterni. {_n(len(lista))}, aggiornati ogni mattina.",
            lista, [("Per figura in " + r, figure_qui), ("Altre regioni", link_regioni)],
            f"avvisi/{slug(r)}/"))

    # pagine per figura (tutta Italia)
    for c, lista in per_cat.items():
        nome = PLURALE.get(c, c)
        reg_qui = [(f"avvisi/{slug(r)}/{c}/", r) for r in regioni if (r, c) in per_reg_cat]
        scrivi(f"avvisi/figura/{c}/", _pagina(
            base, 3,
            f"Bandi scuole per {nome} – tutta Italia | {NOME_SITO}",
            f"{_n(len(lista))} delle scuole italiane per {nome}. Raccolti dagli albi online e aggiornati ogni giorno, gratis.",
            f"Avvisi delle scuole per {nome}",
            f"Tutti gli avvisi pubblicati dalle scuole italiane per {nome}, raccolti in un unico posto. "
            f"{_n(len(lista))}, aggiornati ogni mattina.",
            lista, [("Per regione", reg_qui), ("Altre figure", link_figure)],
            f"avvisi/figura/{c}/"))

    # pagine figura + regione
    for (r, c), lista in per_reg_cat.items():
        nome = PLURALE.get(c, c)
        scrivi(f"avvisi/{slug(r)}/{c}/", _pagina(
            base, 3,
            f"Bandi {nome} nelle scuole – {r} | {NOME_SITO}",
            f"{_n(len(lista))} delle scuole in {r} per {nome}. Aggiornato ogni giorno, gratis.",
            f"Avvisi per {nome} nelle scuole in {r}",
            f"Gli avvisi pubblicati dalle scuole della regione {r} per {nome}. "
            f"{_n(len(lista))}, aggiornati ogni mattina.",
            lista, [(f"Tutti gli avvisi in {r}", [(f"avvisi/{slug(r)}/", r)]),
                    (f"{nome.capitalize()} in altre regioni",
                     [(f"avvisi/{slug(x)}/{c}/", x) for x in regioni if (x, c) in per_reg_cat and x != r])],
            f"avvisi/{slug(r)}/{c}/"))

    # indice
    scrivi("avvisi/", _pagina(
        base, 1,
        f"Avvisi per esperti esterni nelle scuole per regione e figura | {NOME_SITO}",
        "Sfoglia gli avvisi delle scuole italiane per psicologi, formatori, tutor, madrelingua ed esperti esterni, divisi per regione e figura.",
        "Sfoglia gli avvisi per regione e figura",
        f"{_n(len(avvisi))} raccolti dagli albi online delle scuole italiane.",
        [], [("Per regione", link_regioni), ("Per figura", link_figure)], "avvisi/"))

    # sitemap + robots
    if base:
        urls = ["", "interpelli.html", "mappa.html", "copertura.html"] + pagine
        voci = "".join(f"<url><loc>{_e(base + u)}</loc><lastmod>{oggi}</lastmod></url>" for u in urls)
        (root / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{voci}</urlset>\n',
            encoding="utf-8")
        (root / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base}sitemap.xml\n",
                                         encoding="utf-8")
    return len(pagine)
