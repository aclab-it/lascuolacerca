"""Diagnostica: scarica una pagina e ne descrive la struttura (moduli, tabelle, link). Non salva niente."""
import os, re, sys
import requests
from bs4 import BeautifulSoup
url = os.environ["URL"]
s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; LaScuolaCercaBot/1.0; +https://aclab-it.github.io/lascuolacerca/chi-siamo.html)",
                  "Accept-Language": "it-IT,it;q=0.9"})
r = s.get(url, timeout=30)
print("STATO", r.status_code, r.headers.get("Content-Type"), "byte", len(r.content), "url finale", r.url)
soup = BeautifulSoup(r.text, "html.parser")
print("TITOLO", soup.title.get_text(strip=True) if soup.title else "")
for f in soup.find_all("form"):
    print("FORM", f.get("method"), f.get("action"))
    for i in f.find_all(["input", "select", "textarea"]):
        opts = [o.get("value") for o in i.find_all("option")][:15] if i.name == "select" else ""
        print("   campo", i.name, i.get("type"), i.get("name"), i.get("value"), opts)
for t in soup.find_all("table")[:3]:
    righe = t.find_all("tr")
    print("TABELLA righe", len(righe))
    for tr in righe[:6]:
        print("   ", " | ".join(c.get_text(" ", strip=True)[:60] for c in tr.find_all(["td", "th"])))
links = [(a.get_text(" ", strip=True)[:70], a["href"]) for a in soup.find_all("a", href=True)]
print("LINK", len(links))
for t, h in links[:int(os.environ.get("MAXLINK", "60"))]:
    print("   ", t, "->", h)
testo = re.sub(r"\s+", " ", soup.get_text(" "))
print("TESTO", testo[:1500])
