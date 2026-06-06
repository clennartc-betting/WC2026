"""
WK 2026 Odds Tracker, Beslissingsmotor & Wedstrijd Simulator
============================================================
Hobby-project: simuleer (zonder echt geld) gokken op WK 2026 wedstrijden,
puur op WDL bij 90 minuten, op basis van odds-beweging signalen.

Strategie (samengevat):
  - Vroege beweging   (>48u voor aftrap, |Delta prob| >= drempel)
        => vermoedelijk sharp money => meebewegen (positief = bet op uitkomst)
  - Late beweging     (<6u voor aftrap, |Delta prob| >= drempel)
        => vermoedelijk publiek sentiment => tegen in (negatief = bet op uitkomst)
  - Re-bet bij |Delta prob| >= hogere drempel op zelfde (wedstrijd, uitkomst).
  - Eén wedstrijd kan max op één uitkomst inzetten (geen tegengestelde bets).
  - Bet sizing: quarter Kelly. Bankroll dubbel bijgehouden (compound én vast).

Volledig hands-off via GitHub Actions (zie .github/workflows/scrape.yml).

Verplichte env vars:
  ODDS_API_KEY    The Odds API sleutel
Optioneel:
  GH_TOKEN        GitHub token voor Issues-notificaties (auto in Actions)
  GH_REPO         '<owner>/<repo>' voor Issues (auto in Actions)
  WK2026_DB       pad naar SQLite database (default: wk2026.db)
  WK2026_SPORT    sport key (default: soccer_fifa_world_cup)
  WK2026_REGIO    regio's (default: eu,uk)

Commando's:
  daily-run     Volledige cyclus (scrape + beslis + settle + notify)
  scrape        Alleen odds ophalen, opslaan en mediaan aggregeren
  signalen      Toon huidige signalen (zonder bets te plaatsen)
  beslis        Plaats virtuele bets op basis van actuele signalen
  settle        Haal scores op en wikkel openstaande bets af
  stats         Toon ROI, win-rate, max drawdown
  bankroll      Toon huidige compound + vaste bankroll
  bets          Toon openstaande en afgewikkelde bets
  wedstrijden   Toon aankomende wedstrijden
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests


# ── Configuratie ────────────────────────────────────────────────────────────

API_KEY                  = os.environ.get("ODDS_API_KEY")
DB_PATH                  = os.environ.get("WK2026_DB", "wk2026.db")
SPORT                    = os.environ.get("WK2026_SPORT", "soccer_fifa_world_cup")
REGIO                    = os.environ.get("WK2026_REGIO", "eu,uk")
MARKTEN                  = ["h2h"]

STARTBANKROLL            = float(os.environ.get("WK2026_STARTBANKROLL", "100"))

MIN_BEWEGING_PROB        = float(os.environ.get("WK2026_THRESHOLD",       "0.06"))
MIN_BEWEGING_PROB_REBET  = float(os.environ.get("WK2026_THRESHOLD_REBET", "0.12"))
VROEG_UUR_GRENS          = int(os.environ.get("WK2026_VROEG_UUR",  "48"))
LAAT_UUR_GRENS           = int(os.environ.get("WK2026_LAAT_UUR",   "6"))

KELLY_FRACTIE            = float(os.environ.get("WK2026_KELLY_FRACTIE",   "0.25"))
MAX_BET_FRACTIE          = float(os.environ.get("WK2026_MAX_BET_FRACTIE", "0.05"))

GH_TOKEN                 = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
GH_REPO                  = os.environ.get("GH_REPO")  or os.environ.get("GITHUB_REPOSITORY")
NOTIFY_ENABLED           = bool(GH_TOKEN and GH_REPO)

# Scrape-throttling: de workflow vuurt vaak (elke 2u) omdat GitHub cron
# onbetrouwbaar is en runs overslaat. Het script bepaalt zelf of er
# daadwerkelijk een API-call gedaan wordt, op basis van tijd sinds de
# laatste scrape. Zo blijft het credit-verbruik begrensd ongeacht hoe
# vaak de workflow draait.
MIN_SCRAPE_GAP_HOURS       = float(os.environ.get("WK2026_MIN_SCRAPE_GAP_HOURS", "3.5"))
# Vlak voor een aftrap willen we dichter op elkaar scrapen (zodat het
# 'laat_publiek' signaal genoeg datapunten in de laatste uren heeft).
SCRAPE_BOOST_WINDOW_HOURS  = float(os.environ.get("WK2026_SCRAPE_BOOST_WINDOW_HOURS", "6"))
MIN_SCRAPE_GAP_BOOST_HOURS = float(os.environ.get("WK2026_MIN_SCRAPE_GAP_BOOST_HOURS", "1.5"))

# Pre-match notificaties: stuur 1 alert per wedstrijd zodra de aftrap
# binnen dit aantal uur valt. Ruim genomen zodat een overgeslagen run
# de alert niet helemaal mist.
PREMATCH_LEAD_HOURS        = float(os.environ.get("WK2026_PREMATCH_LEAD_HOURS", "3"))

# Notificatiekanaal (CallMeBot WhatsApp, met optionele e-mail fallback).
CALLMEBOT_PHONE   = os.environ.get("CALLMEBOT_PHONE")     # bv. +31612345678
CALLMEBOT_APIKEY  = os.environ.get("CALLMEBOT_APIKEY")
SMTP_USER         = os.environ.get("SMTP_USER")           # gmail adres
SMTP_PASS         = os.environ.get("SMTP_PASS")           # gmail app password
NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL")        # ontvanger
NOTIFY_DRYRUN     = os.environ.get("WK2026_NOTIFY_DRYRUN", "").lower() in ("1", "true", "yes")


# ── Klein helperblok ────────────────────────────────────────────────────────

def log(*args, **kwargs):
    """Print met UTC tijdstempel, flushed (voor GH Actions live logs)."""
    print("[" + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "]",
          *args, **kwargs, flush=True)


def _require_api_key():
    if not API_KEY:
        raise RuntimeError(
            "ODDS_API_KEY is niet gezet. Zet hem als env var of GitHub Secret."
        )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ── Database schema ─────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS wedstrijden (
    id              TEXT PRIMARY KEY,
    thuisteam       TEXT,
    uitteam         TEXT,
    aanvang         TEXT,
    voltooid        INTEGER DEFAULT 0,
    thuis_score     INTEGER,
    uit_score       INTEGER,
    winnaar         TEXT,
    afgewikkeld_op  TEXT,
    gh_issue_number INTEGER
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wedstrijd_id  TEXT,
    bookmaker     TEXT,
    uitkomst      TEXT,
    odd           REAL,
    implied_prob  REAL,
    timestamp     TEXT,
    FOREIGN KEY (wedstrijd_id) REFERENCES wedstrijden(id)
);
CREATE INDEX IF NOT EXISTS idx_odds_wed_uit_ts
    ON odds_snapshots (wedstrijd_id, uitkomst, timestamp);

CREATE TABLE IF NOT EXISTS aggregated_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wedstrijd_id  TEXT,
    uitkomst      TEXT,
    median_odd    REAL,
    mean_odd      REAL,
    n_bookmakers  INTEGER,
    implied_prob  REAL,
    timestamp     TEXT,
    FOREIGN KEY (wedstrijd_id) REFERENCES wedstrijden(id)
);
CREATE INDEX IF NOT EXISTS idx_agg_wed_uit_ts
    ON aggregated_snapshots (wedstrijd_id, uitkomst, timestamp);

CREATE TABLE IF NOT EXISTS bets (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    wedstrijd_id             TEXT,
    uitkomst                 TEXT,
    signaal_type             TEXT,
    beweging_prob            REAL,
    locked_odd               REAL,
    is_rebet                 INTEGER DEFAULT 0,
    stake_compound           REAL,
    stake_fixed              REAL,
    bankroll_compound_at_bet REAL,
    payout_compound          REAL,
    payout_fixed             REAL,
    pnl_compound             REAL,
    pnl_fixed                REAL,
    resultaat                TEXT,
    geplaatst_op             TEXT,
    afgewikkeld_op           TEXT,
    FOREIGN KEY (wedstrijd_id) REFERENCES wedstrijden(id)
);
CREATE INDEX IF NOT EXISTS idx_bets_wed ON bets (wedstrijd_id);

CREATE TABLE IF NOT EXISTS api_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT,
    endpoint    TEXT,
    credits_used INTEGER,
    credits_remaining INTEGER,
    status      TEXT
);
"""


def init_db(conn: sqlite3.Connection):
    """Maakt schema aan en past lichte migraties toe als oude DB bestaat."""
    conn.executescript(SCHEMA)

    bestaande_kolommen = {row[1] for row in conn.execute("PRAGMA table_info(bets)")}
    voor_migratie = {
        "is_rebet":                 "INTEGER DEFAULT 0",
        "stake_compound":           "REAL",
        "stake_fixed":              "REAL",
        "bankroll_compound_at_bet": "REAL",
        "payout_compound":          "REAL",
        "payout_fixed":             "REAL",
        "pnl_compound":             "REAL",
        "pnl_fixed":                "REAL",
        "geplaatst_op":             "TEXT",
        "afgewikkeld_op":           "TEXT",
        "locked_odd":               "REAL",
    }
    for col, decl in voor_migratie.items():
        if col not in bestaande_kolommen:
            try:
                conn.execute(f"ALTER TABLE bets ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass

    bestaande_wed = {row[1] for row in conn.execute("PRAGMA table_info(wedstrijden)")}
    voor_migratie_wed = {
        "voltooid":        "INTEGER DEFAULT 0",
        "thuis_score":     "INTEGER",
        "uit_score":       "INTEGER",
        "winnaar":         "TEXT",
        "afgewikkeld_op":  "TEXT",
        "gh_issue_number": "INTEGER",
        "prematch_genotificeerd": "TEXT",
    }
    for col, decl in voor_migratie_wed.items():
        if col not in bestaande_wed:
            try:
                conn.execute(f"ALTER TABLE wedstrijden ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass

    conn.commit()


# ── The Odds API client ─────────────────────────────────────────────────────

def haal_odds_op() -> list[dict]:
    """Vraag actuele odds op bij The Odds API."""
    _require_api_key()
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds/"
    params = {
        "apiKey":     API_KEY,
        "regions":    REGIO,
        "markets":    ",".join(MARKTEN),
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    log(f"[API/odds] {len(data)} wedstrijden | "
        f"quota gebruikt={resp.headers.get('x-requests-used','?')} "
        f"resterend={resp.headers.get('x-requests-remaining','?')}")
    return data


def haal_scores_op(days_from: int = 3) -> list[dict]:
    """Vraag (mogelijk) afgeronde scores op."""
    _require_api_key()
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/scores/"
    params = {"apiKey": API_KEY, "daysFrom": days_from}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    log(f"[API/scores] {len(data)} wedstrijden | "
        f"quota gebruikt={resp.headers.get('x-requests-used','?')} "
        f"resterend={resp.headers.get('x-requests-remaining','?')}")
    return data


def registreer_api_run(conn, endpoint: str, resp: Optional[requests.Response], status: str):
    used      = resp.headers.get("x-requests-used")      if resp is not None else None
    remaining = resp.headers.get("x-requests-remaining") if resp is not None else None
    conn.execute(
        "INSERT INTO api_runs (timestamp, endpoint, credits_used, credits_remaining, status) "
        "VALUES (?,?,?,?,?)",
        (_iso_now(), endpoint,
         int(used) if used and used.isdigit() else None,
         int(remaining) if remaining and remaining.isdigit() else None,
         status)
    )
    conn.commit()


# ── Opslag van ruwe odds-snapshots ──────────────────────────────────────────

def sla_odds_snapshot_op(conn: sqlite3.Connection, data: list[dict]) -> str:
    """Sla per bookmaker, per uitkomst een snapshot-rij op."""
    now = _iso_now()

    for wedstrijd in data:
        wid       = wedstrijd["id"]
        thuisteam = wedstrijd["home_team"]
        uitteam   = wedstrijd["away_team"]
        aanvang   = wedstrijd["commence_time"]

        conn.execute(
            "INSERT INTO wedstrijden (id, thuisteam, uitteam, aanvang) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "    thuisteam=excluded.thuisteam, "
            "    uitteam=excluded.uitteam, "
            "    aanvang=excluded.aanvang",
            (wid, thuisteam, uitteam, aanvang)
        )

        for bookmaker in wedstrijd.get("bookmakers", []):
            naam = bookmaker["key"]
            for markt in bookmaker.get("markets", []):
                if markt["key"] != "h2h":
                    continue
                for outcome in markt["outcomes"]:
                    label = outcome["name"]
                    odd   = outcome["price"]
                    if label == thuisteam:
                        uitkomst = "thuis"
                    elif label == uitteam:
                        uitkomst = "uit"
                    else:
                        uitkomst = "gelijkspel"

                    conn.execute(
                        "INSERT INTO odds_snapshots "
                        "(wedstrijd_id, bookmaker, uitkomst, odd, implied_prob, timestamp) "
                        "VALUES (?,?,?,?,?,?)",
                        (wid, naam, uitkomst, odd, round(1 / odd, 4), now)
                    )

    conn.commit()
    log(f"[DB] Ruwe snapshots opgeslagen om {now}")
    return now


def sla_aggregated_snapshot_op(conn: sqlite3.Connection, timestamp: str):
    """
    Bouw aggregated_snapshots op uit ruwe snapshots van deze scrape-timestamp:
    mediaan + gemiddelde odd per (wedstrijd, uitkomst).
    """
    rijen = conn.execute(
        "SELECT wedstrijd_id, uitkomst, odd FROM odds_snapshots WHERE timestamp = ?",
        (timestamp,)
    ).fetchall()

    bucket: dict[tuple[str, str], list[float]] = {}
    for wid, uitkomst, odd in rijen:
        bucket.setdefault((wid, uitkomst), []).append(odd)

    for (wid, uitkomst), odds in bucket.items():
        med  = statistics.median(odds)
        mean = statistics.fmean(odds)
        conn.execute(
            "INSERT INTO aggregated_snapshots "
            "(wedstrijd_id, uitkomst, median_odd, mean_odd, n_bookmakers, implied_prob, timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (wid, uitkomst, round(med, 4), round(mean, 4), len(odds),
             round(1 / med, 4), timestamp)
        )

    conn.commit()
    log(f"[DB] Aggregaten opgeslagen ({len(bucket)} rijen) om {timestamp}")


# ── Bewegingsanalyse op aggregaten ──────────────────────────────────────────

def bereken_beweging_aggregated(conn: sqlite3.Connection, wedstrijd_id: str) -> Optional[dict]:
    """Bereken vroege en late beweging op aggregaten voor één wedstrijd."""
    aanvang_row = conn.execute(
        "SELECT aanvang FROM wedstrijden WHERE id = ?", (wedstrijd_id,)
    ).fetchone()
    if not aanvang_row:
        return None

    aanvang_ts  = _parse_iso(aanvang_row[0]).timestamp()
    vroeg_grens = aanvang_ts - VROEG_UUR_GRENS * 3600
    laat_grens  = aanvang_ts - LAAT_UUR_GRENS  * 3600

    resultaten: dict[str, dict] = {}
    for uitkomst in ("thuis", "gelijkspel", "uit"):
        rijen = conn.execute(
            "SELECT median_odd, implied_prob, timestamp FROM aggregated_snapshots "
            "WHERE wedstrijd_id = ? AND uitkomst = ? "
            "ORDER BY timestamp ASC",
            (wedstrijd_id, uitkomst)
        ).fetchall()
        if len(rijen) < 2:
            continue

        opening_prob = rijen[0][1]
        huidige      = rijen[-1]

        vroege_rijen = [r for r in rijen if _parse_iso(r[2]).timestamp() <= vroeg_grens]
        late_rijen   = [r for r in rijen if _parse_iso(r[2]).timestamp() >= laat_grens]

        vroege_beweging = None
        if vroege_rijen:
            vroege_beweging = round(vroege_rijen[-1][1] - opening_prob, 4)

        late_beweging = None
        if len(late_rijen) >= 2:
            late_beweging = round(late_rijen[-1][1] - late_rijen[0][1], 4)

        resultaten[uitkomst] = {
            "huidige_odd":      huidige[0],
            "opening_prob":     opening_prob,
            "huidige_prob":     huidige[1],
            "vroege_beweging":  vroege_beweging,
            "late_beweging":    late_beweging,
        }

    return resultaten


def detecteer_signalen_voor_wedstrijd(conn, wedstrijd_id: str) -> list[dict]:
    """
    Genereer signaal-kandidaten voor één wedstrijd.

    Conventie:
      vroeg_sharp: bet op uitkomst waar vroege_beweging >= +drempel
                   (positieve verschuiving = sharps loaden deze kant)
      laat_publiek: bet op uitkomst waar late_beweging <= -drempel
                    (publiek heeft de andere kant gepusht; deze kant is value)
    """
    beweging = bereken_beweging_aggregated(conn, wedstrijd_id)
    if not beweging:
        return []

    signalen = []
    for uitkomst, data in beweging.items():
        vb = data["vroege_beweging"]
        lb = data["late_beweging"]

        if vb is not None and vb >= MIN_BEWEGING_PROB:
            signalen.append({
                "wedstrijd_id":  wedstrijd_id,
                "uitkomst":      uitkomst,
                "signaal_type":  "vroeg_sharp",
                "beweging":      vb,
                "huidige_odd":   data["huidige_odd"],
                "huidige_prob":  data["huidige_prob"],
            })

        if lb is not None and lb <= -MIN_BEWEGING_PROB:
            signalen.append({
                "wedstrijd_id":  wedstrijd_id,
                "uitkomst":      uitkomst,
                "signaal_type":  "laat_publiek",
                "beweging":      lb,
                "huidige_odd":   data["huidige_odd"],
                "huidige_prob":  data["huidige_prob"],
            })

    return signalen


# ── Kelly + bankroll ────────────────────────────────────────────────────────

def kelly_inzet(bankroll: float, beweging_magnitude: float, odd: float) -> float:
    """Quarter Kelly inzet op basis van bewegingsgrootte als edge-proxy."""
    if bankroll <= 0 or odd <= 1:
        return 0.0

    implied_prob = 1 / odd
    confidence   = min(beweging_magnitude / MIN_BEWEGING_PROB, 2.0)
    geschatte_p  = min(implied_prob + beweging_magnitude * confidence * 0.3, 0.95)
    q = 1 - geschatte_p
    b = odd - 1

    kelly = (geschatte_p * b - q) / b
    kelly = max(kelly, 0.0)

    bedrag     = bankroll * kelly * KELLY_FRACTIE
    max_bedrag = bankroll * MAX_BET_FRACTIE
    return round(min(bedrag, max_bedrag), 2)


def bankroll_compound_huidig(conn) -> float:
    """Compound bankroll = start + som(pnl van afgewikkelde bets)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl_compound), 0) FROM bets WHERE resultaat IS NOT NULL"
    ).fetchone()
    return round(STARTBANKROLL + (row[0] or 0.0), 2)


def bankroll_compound_beschikbaar(conn) -> float:
    """Beschikbare bankroll = compound - som(open stakes)."""
    open_stake = conn.execute(
        "SELECT COALESCE(SUM(stake_compound), 0) FROM bets WHERE resultaat IS NULL"
    ).fetchone()[0] or 0.0
    return round(bankroll_compound_huidig(conn) - open_stake, 2)


# ── Bet plaatsing ───────────────────────────────────────────────────────────

def plaats_bets_voor_scrape(conn: sqlite3.Connection) -> list[dict]:
    """
    Loop alle nog niet-gespeelde wedstrijden langs, evalueer signalen op aggregaten,
    plaats virtuele bets met de regels:
      - max 2 bets per (wedstrijd, uitkomst): 1 initial + 1 re-bet
      - geen tweede uitkomst per wedstrijd
      - re-bet vereist hogere drempel én dezelfde richting
    """
    geplaatst: list[dict] = []
    now = _iso_now()

    wedstrijden = conn.execute(
        "SELECT id, thuisteam, uitteam, aanvang FROM wedstrijden "
        "WHERE voltooid = 0 AND aanvang > ? "
        "ORDER BY aanvang",
        (now,)
    ).fetchall()

    for wid, thuis, uit, aanvang in wedstrijden:
        signalen = detecteer_signalen_voor_wedstrijd(conn, wid)
        if not signalen:
            continue

        bestaand = conn.execute(
            "SELECT uitkomst, beweging_prob, signaal_type FROM bets "
            "WHERE wedstrijd_id = ? ORDER BY id",
            (wid,)
        ).fetchall()

        if bestaand and len(bestaand) >= 2:
            continue

        if bestaand:
            locked_outcome = bestaand[0][0]
            signalen = [s for s in signalen if s["uitkomst"] == locked_outcome]
            if not signalen:
                continue

        beste = max(signalen, key=lambda s: abs(s["beweging"]))
        is_rebet = bool(bestaand)

        if is_rebet:
            laatste_richting = 1 if bestaand[-1][1] > 0 else -1
            huidige_richting = 1 if beste["beweging"] > 0 else -1
            if huidige_richting != laatste_richting:
                continue
            if abs(beste["beweging"]) < MIN_BEWEGING_PROB_REBET:
                continue
        else:
            if abs(beste["beweging"]) < MIN_BEWEGING_PROB:
                continue

        bankroll_c = bankroll_compound_beschikbaar(conn)
        stake_c    = kelly_inzet(bankroll_c, abs(beste["beweging"]), beste["huidige_odd"])
        stake_f    = kelly_inzet(STARTBANKROLL, abs(beste["beweging"]), beste["huidige_odd"])

        if stake_c <= 0 and stake_f <= 0:
            continue

        cur = conn.execute(
            "INSERT INTO bets "
            "(wedstrijd_id, uitkomst, signaal_type, beweging_prob, locked_odd, is_rebet, "
            " stake_compound, stake_fixed, bankroll_compound_at_bet, geplaatst_op) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (wid, beste["uitkomst"], beste["signaal_type"], beste["beweging"],
             beste["huidige_odd"], int(is_rebet),
             stake_c, stake_f, bankroll_c, now)
        )
        bet_id = cur.lastrowid

        geplaatst.append({
            "bet_id":           bet_id,
            "wedstrijd_id":     wid,
            "wedstrijd_naam":   f"{thuis} vs {uit}",
            "aanvang":          aanvang,
            "uitkomst":         beste["uitkomst"],
            "signaal_type":     beste["signaal_type"],
            "beweging":         beste["beweging"],
            "locked_odd":       beste["huidige_odd"],
            "stake_compound":   stake_c,
            "stake_fixed":      stake_f,
            "bankroll_at_bet":  bankroll_c,
            "is_rebet":         is_rebet,
        })

    conn.commit()
    log(f"[BET] {len(geplaatst)} nieuwe bet(s) geplaatst")
    return geplaatst


# ── Settlement ──────────────────────────────────────────────────────────────

def _bepaal_winnaar(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "thuis"
    if home_score < away_score:
        return "uit"
    return "gelijkspel"


def heeft_open_bets_op_klaar_wedstrijden(conn) -> bool:
    """True als minstens één open bet hoort bij een wedstrijd die vermoedelijk al klaar is."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM bets b JOIN wedstrijden w ON b.wedstrijd_id = w.id "
        "WHERE b.resultaat IS NULL AND w.voltooid = 0 AND w.aanvang < ? LIMIT 1",
        (cutoff,)
    ).fetchone()
    return row is not None


def settle_voltooide_wedstrijden(conn: sqlite3.Connection) -> list[dict]:
    """
    Haal scores op (als nuttig), markeer wedstrijden als voltooid en wikkel bets af.
    Retourneert lijst met info per gesettelde bet voor notificatie.
    """
    if not heeft_open_bets_op_klaar_wedstrijden(conn):
        log("[SETTLE] Geen open bets op afgelopen wedstrijden - sla /scores call over")
        return []

    scores_data = haal_scores_op(days_from=3)

    settled: list[dict] = []
    now = _iso_now()

    for ev in scores_data:
        if not ev.get("completed"):
            continue
        wid = ev["id"]

        scores_arr = ev.get("scores") or []
        if len(scores_arr) < 2:
            continue

        thuisteam = ev.get("home_team")
        uitteam   = ev.get("away_team")
        home_score = away_score = None
        for s in scores_arr:
            try:
                sc = int(s.get("score"))
            except (TypeError, ValueError):
                continue
            if s.get("name") == thuisteam:
                home_score = sc
            elif s.get("name") == uitteam:
                away_score = sc

        if home_score is None or away_score is None:
            continue

        winnaar = _bepaal_winnaar(home_score, away_score)

        conn.execute(
            "UPDATE wedstrijden SET voltooid = 1, thuis_score = ?, uit_score = ?, "
            "winnaar = ?, afgewikkeld_op = ? WHERE id = ?",
            (home_score, away_score, winnaar, now, wid)
        )

        open_bets = conn.execute(
            "SELECT id, uitkomst, locked_odd, stake_compound, stake_fixed FROM bets "
            "WHERE wedstrijd_id = ? AND resultaat IS NULL",
            (wid,)
        ).fetchall()

        for bet_id, uitkomst, odd, stake_c, stake_f in open_bets:
            if uitkomst == winnaar:
                resultaat = "gewonnen"
                payout_c = round((stake_c or 0) * (odd or 0), 2)
                payout_f = round((stake_f or 0) * (odd or 0), 2)
            else:
                resultaat = "verloren"
                payout_c = 0.0
                payout_f = 0.0
            pnl_c = round(payout_c - (stake_c or 0), 2)
            pnl_f = round(payout_f - (stake_f or 0), 2)

            conn.execute(
                "UPDATE bets SET resultaat = ?, payout_compound = ?, payout_fixed = ?, "
                "pnl_compound = ?, pnl_fixed = ?, afgewikkeld_op = ? WHERE id = ?",
                (resultaat, payout_c, payout_f, pnl_c, pnl_f, now, bet_id)
            )

            wed_row = conn.execute(
                "SELECT thuisteam, uitteam, gh_issue_number FROM wedstrijden WHERE id = ?",
                (wid,)
            ).fetchone()

            settled.append({
                "bet_id":         bet_id,
                "wedstrijd_id":   wid,
                "wedstrijd_naam": f"{wed_row[0]} vs {wed_row[1]}",
                "uitkomst":       uitkomst,
                "winnaar":        winnaar,
                "thuis_score":    home_score,
                "uit_score":      away_score,
                "resultaat":      resultaat,
                "stake_compound": stake_c,
                "stake_fixed":    stake_f,
                "payout_compound": payout_c,
                "payout_fixed":    payout_f,
                "pnl_compound":    pnl_c,
                "pnl_fixed":       pnl_f,
                "gh_issue":        wed_row[2],
            })

    conn.commit()
    log(f"[SETTLE] {len(settled)} bet(s) afgewikkeld")
    return settled


# ── GitHub Issues notificaties ──────────────────────────────────────────────

def _gh_request(method: str, path: str, json_body: Optional[dict] = None) -> Optional[dict]:
    if not NOTIFY_ENABLED:
        return None
    url = f"https://api.github.com/repos/{GH_REPO}{path}"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.request(method, url, json=json_body, headers=headers, timeout=15)
    if resp.status_code >= 400:
        log(f"[GH] {method} {path} -> {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json() if resp.content else {}


def gh_post_issue(title: str, body: str, labels: list[str]) -> Optional[int]:
    data = _gh_request("POST", "/issues", {"title": title, "body": body, "labels": labels})
    if data and "number" in data:
        return data["number"]
    return None


def gh_add_comment(issue_number: int, body: str):
    _gh_request("POST", f"/issues/{issue_number}/comments", {"body": body})


def gh_update_issue(issue_number: int, **fields):
    if fields:
        _gh_request("PATCH", f"/issues/{issue_number}", fields)


def _fmt_aanvang(iso_str: str) -> str:
    try:
        dt = _parse_iso(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_str


def notificeer_nieuwe_bets(conn, geplaatst: list[dict]):
    """Maak/update GitHub Issue per wedstrijd voor nieuwe bets."""
    if not NOTIFY_ENABLED:
        log("[GH] Notificaties uit (GH_TOKEN/GH_REPO ontbreekt)")
        return

    for b in geplaatst:
        issue_row = conn.execute(
            "SELECT gh_issue_number FROM wedstrijden WHERE id = ?", (b["wedstrijd_id"],)
        ).fetchone()
        issue_nr = issue_row[0] if issue_row else None

        prefix    = "RE-BET" if b["is_rebet"] else "BET"
        titel     = (f"{prefix}: {b['wedstrijd_naam']} - "
                     f"{b['uitkomst']} @ {b['locked_odd']} ({b['signaal_type']})")
        body = (
            f"**Wedstrijd:** {b['wedstrijd_naam']}\n"
            f"**Aanvang:** {_fmt_aanvang(b['aanvang'])}\n"
            f"**Bet op:** `{b['uitkomst']}`\n"
            f"**Signaal:** `{b['signaal_type']}` "
            f"(beweging: {b['beweging']:+.1%})\n"
            f"**Locked mediaan-odd:** {b['locked_odd']}\n"
            f"**Stake (compound):** EUR {b['stake_compound']:.2f} "
            f"(beschikbare bankroll @ bet: EUR {b['bankroll_at_bet']:.2f})\n"
            f"**Stake (vast EUR {STARTBANKROLL:.0f} referentie):** EUR {b['stake_fixed']:.2f}\n"
            f"**Bet ID:** {b['bet_id']}\n"
            f"**Re-bet:** {'ja' if b['is_rebet'] else 'nee'}\n"
        )
        labels = ["bet", f"signal:{b['signaal_type']}", "status:open"]
        if b["is_rebet"]:
            labels.append("rebet")

        if issue_nr:
            gh_add_comment(issue_nr, f"### Nieuwe {prefix}\n\n{body}")
        else:
            issue_nr = gh_post_issue(titel, body, labels)
            if issue_nr:
                conn.execute(
                    "UPDATE wedstrijden SET gh_issue_number = ? WHERE id = ?",
                    (issue_nr, b["wedstrijd_id"])
                )
                conn.commit()


def notificeer_settled_bets(conn, settled: list[dict]):
    """Plaats settle-comments en sluit issues per wedstrijd."""
    if not NOTIFY_ENABLED:
        return

    per_wedstrijd: dict[str, list[dict]] = {}
    for s in settled:
        per_wedstrijd.setdefault(s["wedstrijd_id"], []).append(s)

    for wid, lijst in per_wedstrijd.items():
        issue_nr = lijst[0]["gh_issue"]
        if not issue_nr:
            continue

        eerste = lijst[0]
        body = (
            f"### Wedstrijd afgelopen\n"
            f"**Eindstand:** {eerste['thuis_score']} - {eerste['uit_score']}\n"
            f"**Winnaar:** `{eerste['winnaar']}`\n\n"
        )
        totaal_pnl_c = 0.0
        totaal_pnl_f = 0.0
        for s in lijst:
            body += (
                f"- **Bet #{s['bet_id']}** op `{s['uitkomst']}`: "
                f"**{s['resultaat'].upper()}**\n"
                f"  - stake compound EUR {s['stake_compound']:.2f} -> payout EUR {s['payout_compound']:.2f} "
                f"(P&L {s['pnl_compound']:+.2f})\n"
                f"  - stake vast EUR {s['stake_fixed']:.2f} -> payout EUR {s['payout_fixed']:.2f} "
                f"(P&L {s['pnl_fixed']:+.2f})\n"
            )
            totaal_pnl_c += s["pnl_compound"]
            totaal_pnl_f += s["pnl_fixed"]

        body += (
            f"\n**Totaal P&L deze wedstrijd:** compound {totaal_pnl_c:+.2f} | "
            f"vast {totaal_pnl_f:+.2f}\n"
            f"**Bankroll (compound) nu:** EUR {bankroll_compound_huidig(conn):.2f}\n"
        )

        gh_add_comment(issue_nr, body)

        nieuwe_labels = ["bet"]
        if all(s["resultaat"] == "gewonnen" for s in lijst):
            nieuwe_labels += ["status:settled", "result:won"]
        elif all(s["resultaat"] == "verloren" for s in lijst):
            nieuwe_labels += ["status:settled", "result:lost"]
        else:
            nieuwe_labels += ["status:settled", "result:mixed"]
        for s in lijst:
            if s.get("is_rebet"):
                nieuwe_labels.append("rebet")

        gh_update_issue(issue_nr, state="closed", labels=nieuwe_labels)


# ── Rapportages ─────────────────────────────────────────────────────────────

def _max_drawdown(pnls: list[float]) -> float:
    cumul, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cumul += p
        if cumul > peak:
            peak = cumul
        dd = peak - cumul
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def cmd_stats(conn):
    rijen = conn.execute(
        "SELECT stake_compound, stake_fixed, payout_compound, payout_fixed, "
        "       pnl_compound, pnl_fixed, resultaat, afgewikkeld_op "
        "FROM bets ORDER BY id"
    ).fetchall()

    n_total = len(rijen)
    settled = [r for r in rijen if r[6] is not None]
    n_won   = sum(1 for r in settled if r[6] == "gewonnen")

    stake_c   = sum((r[0] or 0) for r in settled)
    stake_f   = sum((r[1] or 0) for r in settled)
    payout_c  = sum((r[2] or 0) for r in settled)
    payout_f  = sum((r[3] or 0) for r in settled)
    pnls_c    = [(r[4] or 0) for r in sorted(settled, key=lambda r: r[7] or "")]
    pnls_f    = [(r[5] or 0) for r in sorted(settled, key=lambda r: r[7] or "")]
    pnl_c_tot = sum(pnls_c)
    pnl_f_tot = sum(pnls_f)

    roi_c = (pnl_c_tot / stake_c * 100) if stake_c > 0 else 0
    roi_f = (pnl_f_tot / stake_f * 100) if stake_f > 0 else 0
    winrate = (n_won / len(settled) * 100) if settled else 0

    bankroll_c = bankroll_compound_huidig(conn)

    print("\n" + "-" * 70)
    print(f"{'STATS':^70}")
    print("-" * 70)
    print(f"Bets totaal:           {n_total}")
    print(f"Afgewikkeld:           {len(settled)}  (open: {n_total - len(settled)})")
    print(f"Win-rate:              {winrate:.1f}%  ({n_won}/{len(settled)})")
    print()
    print(f"{'':25} {'compound':>15} {'vast EUR' + str(int(STARTBANKROLL)):>15}")
    print(f"{'Totaal stake:':25} {stake_c:>15.2f} {stake_f:>15.2f}")
    print(f"{'Totaal payout:':25} {payout_c:>15.2f} {payout_f:>15.2f}")
    print(f"{'Totaal P&L:':25} {pnl_c_tot:>+15.2f} {pnl_f_tot:>+15.2f}")
    print(f"{'ROI:':25} {roi_c:>+14.2f}% {roi_f:>+14.2f}%")
    print(f"{'Max drawdown:':25} {_max_drawdown(pnls_c):>15.2f} {_max_drawdown(pnls_f):>15.2f}")
    print()
    print(f"Bankroll (compound):   EUR {bankroll_c:.2f}  (start: EUR {STARTBANKROLL:.2f})")
    print("-" * 70 + "\n")


def cmd_bankroll(conn):
    c = bankroll_compound_huidig(conn)
    b = bankroll_compound_beschikbaar(conn)
    print(f"\nBankroll (compound, totaal):       EUR {c:.2f}")
    print(f"Bankroll (compound, beschikbaar):  EUR {b:.2f}")
    print(f"Vaste referentie:                  EUR {STARTBANKROLL:.2f}\n")


def cmd_bets(conn):
    rijen = conn.execute(
        "SELECT b.id, w.thuisteam, w.uitteam, w.aanvang, b.uitkomst, b.signaal_type, "
        "       b.locked_odd, b.stake_compound, b.stake_fixed, b.resultaat, "
        "       b.pnl_compound, b.is_rebet, b.geplaatst_op "
        "FROM bets b JOIN wedstrijden w ON b.wedstrijd_id = w.id "
        "ORDER BY w.aanvang, b.id"
    ).fetchall()

    if not rijen:
        print("Nog geen bets.")
        return

    print(f"\n{'─'*100}")
    print(f"  {'ID':>4} {'WEDSTRIJD':<30} {'UITK':<10} {'SIG':<14} {'ODD':>6} "
          f"{'STK_C':>7} {'STK_F':>7} {'STATUS':<10} {'P&L_C':>8}")
    print("-" * 100)
    for (bid, thuis, uit, aanvang, uitk, sig, odd, sc, sf, res, pnl, rebet, geplaatst) in rijen:
        status = res or "open"
        rebet_marker = "*" if rebet else " "
        pnl_str = f"{pnl:+.2f}" if pnl is not None else ""
        wedstr  = f"{thuis} vs {uit}"
        if len(wedstr) > 29:
            wedstr = wedstr[:28] + "."
        print(f"  {bid:>4}{rebet_marker} {wedstr:<30} {uitk:<10} {sig:<14} {odd:>6.2f} "
              f"{sc:>7.2f} {sf:>7.2f} {status:<10} {pnl_str:>8}")
    print("-" * 100)
    print("  (* = re-bet)\n")


def cmd_wedstrijden(conn):
    now = _iso_now()
    rijen = conn.execute(
        "SELECT thuisteam, uitteam, aanvang, voltooid FROM wedstrijden "
        "WHERE aanvang > ? ORDER BY aanvang",
        (now,)
    ).fetchall()
    print(f"\n{'-'*60}")
    print(f"  {'AANKOMENDE WEDSTRIJDEN':^56}")
    print("-" * 60)
    for thuis, uit, aanvang, voltooid in rijen:
        print(f"  {thuis:20} vs {uit:20}  {_fmt_aanvang(aanvang)}")
    print("-" * 60 + "\n")


def cmd_signalen(conn):
    now = _iso_now()
    wedstrijden = conn.execute(
        "SELECT id, thuisteam, uitteam, aanvang FROM wedstrijden "
        "WHERE voltooid = 0 AND aanvang > ? ORDER BY aanvang",
        (now,)
    ).fetchall()

    total = 0
    print(f"\n{'-'*70}")
    print(f"{'SIGNALEN':^70}")
    print("-" * 70)
    for wid, thuis, uit, aanvang in wedstrijden:
        sigs = detecteer_signalen_voor_wedstrijd(conn, wid)
        if not sigs:
            continue
        print(f"\n  {thuis} vs {uit}  ({_fmt_aanvang(aanvang)})")
        for s in sigs:
            print(f"    - {s['signaal_type']:<14} op {s['uitkomst']:<10} "
                  f"beweging {s['beweging']:+.1%}  odd {s['huidige_odd']:.2f}")
            total += 1
    if total == 0:
        print("  (geen signalen)")
    print("\n" + "-" * 70 + "\n")


# ── Scrape-throttling ───────────────────────────────────────────────────────

def _laatste_scrape_tijd(conn) -> Optional[datetime]:
    """Tijd van de meest recente succesvolle odds-scrape (uit api_runs)."""
    row = conn.execute(
        "SELECT MAX(timestamp) FROM api_runs WHERE endpoint = 'odds' AND status = 'ok'"
    ).fetchone()
    if row and row[0]:
        try:
            return _parse_iso(row[0])
        except Exception:
            return None
    return None


def _uur_tot_eerstvolgende_aftrap(conn) -> Optional[float]:
    """Uren tot de eerstvolgende nog niet begonnen wedstrijd (None = geen)."""
    now = datetime.now(timezone.utc)
    rows = conn.execute("SELECT aanvang FROM wedstrijden WHERE voltooid = 0").fetchall()
    toekomstig = []
    for (aanvang,) in rows:
        try:
            dt = _parse_iso(aanvang)
        except Exception:
            continue
        if dt > now:
            toekomstig.append((dt - now).total_seconds() / 3600)
    return min(toekomstig) if toekomstig else None


def moet_scrapen(conn) -> tuple[bool, str]:
    """
    Bepaal of we nu daadwerkelijk de Odds API aanroepen. De workflow draait
    vaak (elke 2u) omdat GitHub-cron runs overslaat; deze guard begrenst het
    credit-verbruik en versnelt vlak voor een aftrap (boost-venster).
    """
    laatste = _laatste_scrape_tijd(conn)
    if laatste is None:
        return True, "eerste scrape"

    uren_sinds = (datetime.now(timezone.utc) - laatste).total_seconds() / 3600
    tot_aftrap = _uur_tot_eerstvolgende_aftrap(conn)

    if tot_aftrap is not None and tot_aftrap <= SCRAPE_BOOST_WINDOW_HOURS:
        gap, modus = MIN_SCRAPE_GAP_BOOST_HOURS, f"boost (aftrap over {tot_aftrap:.1f}u)"
    else:
        gap, modus = MIN_SCRAPE_GAP_HOURS, "normaal"

    if uren_sinds >= gap:
        return True, f"{modus}: {uren_sinds:.1f}u sinds laatste (>= {gap}u)"
    return False, f"{modus}: pas {uren_sinds:.1f}u sinds laatste (< {gap}u) - overslaan"


# ── Generieke notificaties (WhatsApp via CallMeBot / e-mail fallback) ────────

def _send_callmebot(tekst: str) -> bool:
    """Verstuur een WhatsApp-bericht via CallMeBot. False als niet ingesteld."""
    if not (CALLMEBOT_PHONE and CALLMEBOT_APIKEY):
        return False
    try:
        resp = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": CALLMEBOT_PHONE, "text": tekst, "apikey": CALLMEBOT_APIKEY},
            timeout=25,
        )
        if resp.status_code != 200:
            log(f"[WA] CallMeBot status {resp.status_code}: {resp.text[:150]}")
            return False
        return True
    except Exception as e:
        log(f"[WA] CallMeBot fout: {e}")
        return False


def _send_email(titel: str, tekst: str) -> bool:
    """Verstuur via Gmail SMTP. False als niet ingesteld."""
    if not (SMTP_USER and SMTP_PASS and NOTIFY_EMAIL):
        return False
    import smtplib
    from email.message import EmailMessage
    try:
        msg = EmailMessage()
        msg["Subject"] = titel
        msg["From"]    = SMTP_USER
        msg["To"]      = NOTIFY_EMAIL
        msg.set_content(tekst)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=25) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception as e:
        log(f"[MAIL] fout: {e}")
        return False


def stuur_bericht(titel: str, body: str) -> bool:
    """
    Stuur een notificatie via het eerst-beschikbare kanaal:
    WhatsApp (CallMeBot) -> e-mail -> log. NOTIFY_DRYRUN logt alleen.
    """
    volledig = f"*{titel}*\n{body}" if titel else body
    if NOTIFY_DRYRUN:
        log(f"[NOTIFY dry-run]\n{volledig}")
        return True
    if _send_callmebot(volledig):
        log(f"[NOTIFY] WhatsApp verstuurd: {titel}")
        return True
    if _send_email(titel, body):
        log(f"[NOTIFY] E-mail verstuurd: {titel}")
        return True
    log(f"[NOTIFY] Geen kanaal geconfigureerd; niet verstuurd: {titel}")
    return False


# ── Pre-match notificaties ──────────────────────────────────────────────────

def _odds_trajectorie(conn, wid: str):
    """
    Geef (tijden, dict[uitkomst] -> [odd_t0, odd_mid, odd_now]) terug.
    Drie ijkpunten op de aggregaat-reeks: eerste scrape, middelste, laatste.
    """
    tijden = None
    traj: dict[str, Optional[list]] = {}
    for uitkomst in ("thuis", "gelijkspel", "uit"):
        rows = conn.execute(
            "SELECT timestamp, median_odd FROM aggregated_snapshots "
            "WHERE wedstrijd_id = ? AND uitkomst = ? ORDER BY timestamp ASC",
            (wid, uitkomst)
        ).fetchall()
        if not rows:
            traj[uitkomst] = None
            continue
        n = len(rows)
        idx = (0, n // 2, n - 1)
        traj[uitkomst] = [rows[i][1] for i in idx]
        if tijden is None:
            tijden = [_parse_iso(rows[i][0]) for i in idx]
    return tijden, traj


def _ascii_odds_tabel(conn, wid: str) -> str:
    """ASCII-tabel (monospace) van het odds-verloop op t0 / midden / nu."""
    tijden, traj = _odds_trajectorie(conn, wid)
    if not tijden:
        return ""

    def rij(label, a, b, c):
        return f"{label:<3}{a:>7}{b:>7}{c:>7}"

    regels = [rij("", "t0", "mid", "now")]
    for code, uitkomst in (("1", "thuis"), ("X", "gelijkspel"), ("2", "uit")):
        vals = traj.get(uitkomst)
        if not vals:
            regels.append(rij(code, "-", "-", "-"))
        else:
            regels.append(rij(code, f"{vals[0]:.2f}", f"{vals[1]:.2f}", f"{vals[2]:.2f}"))

    def kort(dt):
        return dt.strftime("%m-%d %Hh")

    legenda = f"t0={kort(tijden[0])}  mid={kort(tijden[1])}  now={kort(tijden[2])}"
    # Triple-backticks -> WhatsApp rendert dit als monospace zodat de
    # kolommen netjes uitlijnen.
    return "```\n" + "\n".join(regels) + "\n```\n" + legenda


def bouw_prematch_samenvatting(conn, wid: str, thuis: str, uit: str,
                               aanvang_iso: str) -> str:
    """Korte data-samenvatting van een wedstrijd voor de pre-match alert."""
    aanvang = _parse_iso(aanvang_iso)
    uren = (aanvang - datetime.now(timezone.utc)).total_seconds() / 3600

    bew = bereken_beweging_aggregated(conn, wid) or {}
    n_row = conn.execute(
        "SELECT n_bookmakers FROM aggregated_snapshots WHERE wedstrijd_id = ? "
        "ORDER BY timestamp DESC LIMIT 1", (wid,)
    ).fetchone()
    n_books = n_row[0] if n_row else "?"

    labels = {"thuis": f"1 {thuis}", "gelijkspel": "X gelijk", "uit": f"2 {uit}"}
    regels = []
    for uitkomst in ("thuis", "gelijkspel", "uit"):
        d = bew.get(uitkomst)
        if not d:
            regels.append(f"  {labels[uitkomst]}: -")
            continue
        delta = d["huidige_prob"] - d["opening_prob"]
        extra = f", laat {d['late_beweging']:+.1%}" if d.get("late_beweging") is not None else ""
        regels.append(
            f"  {labels[uitkomst]}: {d['huidige_odd']:.2f} "
            f"({d['huidige_prob']:.0%}, dopen {delta:+.1%}{extra})"
        )

    bets_op_match = conn.execute(
        "SELECT uitkomst, signaal_type, locked_odd, stake_compound, is_rebet "
        "FROM bets WHERE wedstrijd_id = ? ORDER BY id", (wid,)
    ).fetchall()
    if bets_op_match:
        bet_txt = "; ".join(
            f"{'RE-BET' if rebet else 'BET'} {u} @ {odd} (EUR {stake:.2f}, {sig})"
            for u, sig, odd, stake, rebet in bets_op_match
        )
    else:
        bet_txt = "geen bet (geen signaal boven drempel)"

    blokken = [
        f"Aftrap: {_fmt_aanvang(aanvang_iso)} (over {uren:.1f}u)",
        f"Odds (mediaan, {n_books} bookmakers):",
        "\n".join(regels),
    ]
    tabel = _ascii_odds_tabel(conn, wid)
    if tabel:
        blokken.append("Verloop (mediaan-odd):\n" + tabel)
    blokken.append(f"Bot: {bet_txt}")
    return "\n".join(blokken)


def verstuur_prematch_notificaties(conn) -> list[str]:
    """Stuur 1 alert per wedstrijd waarvan de aftrap binnen LEAD-uur valt."""
    now = datetime.now(timezone.utc)
    grens = (now + timedelta(hours=PREMATCH_LEAD_HOURS)).isoformat()
    rijen = conn.execute(
        "SELECT id, thuisteam, uitteam, aanvang FROM wedstrijden "
        "WHERE voltooid = 0 AND prematch_genotificeerd IS NULL "
        "AND aanvang > ? AND aanvang <= ? ORDER BY aanvang",
        (now.isoformat(), grens)
    ).fetchall()

    verstuurd = []
    for wid, thuis, uit, aanvang in rijen:
        body  = bouw_prematch_samenvatting(conn, wid, thuis, uit, aanvang)
        if stuur_bericht(f"WK alert: {thuis} vs {uit}", body):
            conn.execute(
                "UPDATE wedstrijden SET prematch_genotificeerd = ? WHERE id = ?",
                (_iso_now(), wid)
            )
            conn.commit()
            verstuurd.append(f"{thuis} vs {uit}")
    if verstuurd:
        log(f"[PREMATCH] {len(verstuurd)} alert(s) verstuurd")
    return verstuurd


# ── Top-level commando's ────────────────────────────────────────────────────

def cmd_scrape(conn, force: bool = False) -> bool:
    """Voer een odds-scrape uit, tenzij de throttle hem overslaat."""
    if not force:
        doen, reden = moet_scrapen(conn)
        log(f"[SCRAPE] {reden}")
        if not doen:
            return False
    data = haal_odds_op()
    ts = sla_odds_snapshot_op(conn, data)
    sla_aggregated_snapshot_op(conn, ts)
    conn.execute(
        "INSERT INTO api_runs (timestamp, endpoint, status) VALUES (?,?,?)",
        (ts, "odds", "ok")
    )
    conn.commit()
    return True


def cmd_beslis(conn):
    geplaatst = plaats_bets_voor_scrape(conn)
    notificeer_nieuwe_bets(conn, geplaatst)
    if geplaatst:
        print(f"\n{len(geplaatst)} nieuwe bet(s) geplaatst:")
        for b in geplaatst:
            mark = "RE-BET" if b["is_rebet"] else "BET"
            print(f"  [{mark}] #{b['bet_id']} {b['wedstrijd_naam']} - "
                  f"{b['uitkomst']} @ {b['locked_odd']} "
                  f"(signal={b['signaal_type']}, beweging={b['beweging']:+.1%}, "
                  f"stake_c=EUR {b['stake_compound']:.2f}, "
                  f"stake_f=EUR {b['stake_fixed']:.2f})")
    else:
        print("Geen nieuwe bets.")


def cmd_settle(conn):
    settled = settle_voltooide_wedstrijden(conn)
    notificeer_settled_bets(conn, settled)
    if settled:
        print(f"\n{len(settled)} bet(s) afgewikkeld:")
        for s in settled:
            print(f"  #{s['bet_id']} {s['wedstrijd_naam']} - "
                  f"{s['uitkomst']} vs winnaar {s['winnaar']}: "
                  f"{s['resultaat']} (PnL compound EUR {s['pnl_compound']:+.2f})")
    else:
        print("Geen bets om af te wikkelen.")


def cmd_daily_run(conn):
    """Volledige cyclus voor GitHub Actions / cron."""
    log("=" * 50)
    log("[daily-run] Start cyclus")

    gescraped = False
    try:
        gescraped = cmd_scrape(conn)
    except Exception as e:
        log(f"[daily-run] FOUT in scrape: {e}")

    if gescraped:
        try:
            geplaatst = plaats_bets_voor_scrape(conn)
            notificeer_nieuwe_bets(conn, geplaatst)
        except Exception as e:
            log(f"[daily-run] FOUT in beslis: {e}")
    else:
        log("[daily-run] Scrape overgeslagen (throttle) - geen nieuwe bet-evaluatie")

    try:
        settled = settle_voltooide_wedstrijden(conn)
        notificeer_settled_bets(conn, settled)
    except Exception as e:
        log(f"[daily-run] FOUT in settle: {e}")

    # Pre-match alerts draaien ELKE run (los van de scrape-throttle) zodat de
    # timing strak blijft, ook als de odds-scrape is overgeslagen.
    try:
        verstuur_prematch_notificaties(conn)
    except Exception as e:
        log(f"[daily-run] FOUT in prematch-notificaties: {e}")

    log(f"[daily-run] Bankroll compound: EUR {bankroll_compound_huidig(conn):.2f}")
    log("[daily-run] Klaar")
    log("=" * 50)


# ── CLI entrypoint ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WK 2026 Odds Tracker")
    parser.add_argument(
        "commando",
        choices=["daily-run", "scrape", "signalen", "beslis", "settle",
                 "stats", "bankroll", "bets", "wedstrijden",
                 "prematch", "notify-test"]
    )
    parser.add_argument("--force", action="store_true",
                        help="Negeer de scrape-throttle (alleen bij 'scrape')")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)

    try:
        if args.commando == "daily-run":
            cmd_daily_run(conn)
        elif args.commando == "scrape":
            if cmd_scrape(conn, force=args.force) is False:
                print("Scrape overgeslagen door throttle (gebruik --force om te forceren).")
        elif args.commando == "prematch":
            verstuurd = verstuur_prematch_notificaties(conn)
            print(f"{len(verstuurd)} pre-match alert(s) verstuurd."
                  if verstuurd else "Geen wedstrijden binnen het alert-venster.")
        elif args.commando == "notify-test":
            ok = stuur_bericht("WK2026 testbericht",
                               "Als je dit ziet werkt je notificatiekanaal. ✅")
            print("Testbericht verstuurd." if ok else
                  "Geen kanaal geconfigureerd (zet CALLMEBOT_* of SMTP_* of WK2026_NOTIFY_DRYRUN=1).")
        elif args.commando == "signalen":
            cmd_signalen(conn)
        elif args.commando == "beslis":
            cmd_beslis(conn)
        elif args.commando == "settle":
            cmd_settle(conn)
        elif args.commando == "stats":
            cmd_stats(conn)
        elif args.commando == "bankroll":
            cmd_bankroll(conn)
        elif args.commando == "bets":
            cmd_bets(conn)
        elif args.commando == "wedstrijden":
            cmd_wedstrijden(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
