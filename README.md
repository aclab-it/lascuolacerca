# La Scuola Cerca

Sito gratuito che raccoglie ogni giorno gli avvisi delle scuole italiane per
**psicologi, formatori, esperti esterni, tutor, madrelingua, RSPP, DPO**.

- `index.html` – il sito (una sola pagina, legge `data/avvisi.json`)
- `scraper/scan.py` – lo script che legge gli albi delle scuole
- `.github/workflows/aggiorna.yml` – esegue lo script ogni mattina, gratis, su GitHub
- `data/` – qui finiscono l'elenco delle scuole e gli avvisi trovati

## Pubblicazione (gratis, circa 10 minuti, meglio da PC)

1. **Account**: crea un account gratuito su github.com.
2. **Repository**: in alto a destra fai **+ → New repository**, chiamalo `Sito-professionisti`,
   scegli **Public** (Pages gratis richiede un repo pubblico) e fai **Create repository**.
3. **Carica i file**: clicca *uploading an existing file*, trascina `index.html`,
   `README.md` e le cartelle `scraper` e `data`, poi fai **Commit changes**.
4. **Automazione**: la cartella `.github` è nascosta e spesso non si trascina.
   Fai **Add file → Create new file**, scrivi come nome
   `.github/workflows/aggiorna.yml`, incolla il contenuto del file omonimo
   e fai **Commit changes**.
5. **Elenco scuole**: viene scaricato in automatico dal portale open data del Ministero al primo giro.
   Se nel log compare "caricalo a mano", scarica da **dati.istruzione.it/opendata** il CSV
   *"Informazioni anagrafiche scuole statali"*, rinominalo `scuole.csv` e caricalo nella cartella `data`.
6. **Accendi il sito**: vai in **Settings → Pages → Branch: main / (root) → Save**.
   Dopo un minuto il sito è online su `https://carineapig.github.io/Sito-professionisti/`.
7. **Primo aggiornamento**: nella scheda **Actions**, se richiesto abilita i workflow,
   poi apri **Aggiorna avvisi → Run workflow**. Dopo, gira da solo ogni mattina.

### Consiglio per il primo giro
In **Settings → Secrets and variables → Actions → Variables** crea:
- `REGIONE` = `LAZIO` (o la tua regione, in maiuscolo) per provare su una regione sola
- `MAX_ISTITUTI` = `200` per un test veloce

Quando funziona, cancella le due variabili e verranno scansionate tutte le scuole d'Italia.

Se il salvataggio finale fallisce con "permission denied", vai in
**Settings → Actions → General → Workflow permissions** e seleziona **Read and write**.

## Canale Telegram (facoltativo, consigliato)
1. Su Telegram scrivi a **@BotFather** → `/newbot` → ottieni il token.
2. Crea un canale pubblico e aggiungi il bot come amministratore.
3. In **Settings → Secrets and variables → Actions → Secrets** crea
   `TELEGRAM_BOT_TOKEN` (il token) e `TELEGRAM_CHAT_ID` (es. `@nomecanale`).
4. In `index.html` scrivi il link del canale in `TELEGRAM_URL`.

Ogni nuovo avviso verrà pubblicato in automatico sul canale.

## Pubblicità
Quando il sito ha traffico: in `index.html` imposta `AD_OGNI` (es. 8) e inserisci il codice
del circuito pubblicitario al posto di `<!-- spazio pubblicitario -->`.
Con la pubblicità servono anche un banner cookie e una privacy policy (obbligo GDPR).

## Limiti noti
- Lo script legge le pagine HTML. Gli albi caricati solo via JavaScript (alcune
  piattaforme esterne) per ora non vengono letti.
- Gli avvisi vengono riconosciuti dal testo, quindi possono esserci falsi positivi o mancanze.
  Le regole sono in cima a `scan.py` (`CATEGORIE`, `RE_ESCLUDI`) e si possono affinare.
