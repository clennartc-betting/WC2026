# Prompt voor Claude Code

## Context

Ik bouw een hobby-project om te gokken op het WK 2026 (puur WDL bij 90 minuten).
De strategie is gebaseerd op odds-beweging + timing als signaal:

- **Vroege beweging** (>48u voor aftrap, >6% implied probability shift) → mogelijk sharp money → meebewegen
- **Late beweging** (<6u voor aftrap, >6% implied probability shift) → mogelijk publiek sentiment → tegen de beweging ingaan
- **Bet sizing** via quarter Kelly op basis van bewegingsgrootte als proxy voor confidence

Het startscript `wk2026_tracker.py` bevat al:
- SQLite database setup (wedstrijden, odds_snapshots, bets tabellen)
- The Odds API client
- APScheduler (elke 6 uur scrapen)
- Signaal detectie logica
- Kelly bet sizing

## Wat ik wil dat jij uitbouwt

### 1. Meerdere bookmakers aggregeren
Het script gebruikt nu één bookmaker (standaard: pinnacle). Pas `bereken_beweging()` aan
zodat het de **mediaan odd over alle beschikbare bookmakers** gebruikt per snapshot,
in plaats van één specifieke bookmaker. Dit maakt het signaal robuuster.

### 2. Backtesting module
Voeg een `backtest.py` toe dat:
- Historische odds-snapshots uit de DB inlaadt
- De signaallogica simuleert alsof we in het verleden zaten
- Per bet bijhoudt: ingezet bedrag, uitkomst, winst/verlies
- Eindresultaat toont: ROI, winrate, max drawdown, Sharpe ratio

De wedstrijduitkomsten (wie heeft gewonnen) moet ik handmatig kunnen invoeren via CLI:
```
python backtest.py resultaat <wedstrijd_id> <thuis|gelijkspel|uit>
```

### 3. Bet tracker CLI
Voeg aan het hoofdscript toe:
```
python wk2026_tracker.py bet-log <wedstrijd_id> <uitkomst> <ingezet_bedrag>
python wk2026_tracker.py bet-resultaat <bet_id> <winst|verlies|gelijkspel>
python wk2026_tracker.py stats
```
Waarbij `stats` toont: totaal ingezet, totaal terug, ROI, huidige bankroll.

### 4. Scheduling verbeteren
Het schedule commando draait nu blocking (terminal blijft bezet).
Voeg een optie toe om het als achtergrondproces te draaien met logging naar `tracker.log`.
Tip: gebruik `apscheduler` met `BackgroundScheduler` + een simpele `while True: sleep(60)` loop,
of genereer een systemd service file voor Linux / launchd plist voor macOS.

### 5. Notificaties bij signaal
Wanneer een nieuw signaal wordt gedetecteerd tijdens een scrape-run, stuur een notificatie.
Implementeer dit via een simpele **email via smtplib** (gmail app password).
Config via omgevingsvariabelen: `SMTP_USER`, `SMTP_PASS`, `NOTIFY_EMAIL`.

## Technische randvoorwaarden
- Python 3.10+
- Geen zware dependencies — `requests`, `apscheduler`, standaardlib is voldoende
- SQLite blijft de database (geen Postgres etc.)
- Alles in één project-map, geen Docker
- Type hints toevoegen waar het de leesbaarheid verbetert
- Elke functie een korte docstring

## Volgorde van aanpak
Werk de punten af in volgorde 1 → 5. Laat me na elk punt weten wat je hebt gedaan
en of er keuzes zijn die ik moet maken voordat je doorgaat.
