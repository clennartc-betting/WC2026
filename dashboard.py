"""
WK 2026 Bet-Simulatie Dashboard
================================
Interactief Streamlit-dashboard dat `wk2026.db` uitleest en alle relevante
informatie toont: odds-beweging over tijd, signaal-nabijheid, geplaatste
bets, bankroll, wedstrijdschema en data/API-gezondheid.

De signaal- en Kelly-logica hieronder spiegelt exact die in
`wk2026_tracker.py`, zodat het dashboard laat zien wat de bot zou doen.

Starten:
    pip install streamlit plotly pandas
    streamlit run dashboard.py

Het dashboard leest standaard `wk2026.db` in deze map. Overschrijf met:
    WK2026_DB=pad/naar/wk2026.db streamlit run dashboard.py
"""

import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── Config (spiegelt wk2026_tracker.py) ─────────────────────────────────────

DB_PATH                 = os.environ.get("WK2026_DB", "wk2026.db")
STARTBANKROLL           = float(os.environ.get("WK2026_STARTBANKROLL", "100"))
MIN_BEWEGING_PROB       = float(os.environ.get("WK2026_THRESHOLD",       "0.06"))
MIN_BEWEGING_PROB_REBET = float(os.environ.get("WK2026_THRESHOLD_REBET", "0.12"))
VROEG_UUR_GRENS         = int(os.environ.get("WK2026_VROEG_UUR", "48"))
LAAT_UUR_GRENS          = int(os.environ.get("WK2026_LAAT_UUR",  "6"))
KELLY_FRACTIE           = float(os.environ.get("WK2026_KELLY_FRACTIE",   "0.25"))
MAX_BET_FRACTIE         = float(os.environ.get("WK2026_MAX_BET_FRACTIE", "0.05"))

UITKOMST_LABEL = {"thuis": "Thuis (1)", "gelijkspel": "Gelijk (X)", "uit": "Uit (2)"}
UITKOMST_KLEUR = {"thuis": "#2563eb", "gelijkspel": "#9333ea", "uit": "#dc2626"}

st.set_page_config(page_title="WK 2026 Bet Dashboard", page_icon="⚽", layout="wide")


# ── Datatoegang (met cache op DB-mtime zodat refresh werkt) ──────────────────

def _db_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False)
def laad_tabellen(path: str, _mtime: float) -> dict[str, pd.DataFrame]:
    """Lees alle tabellen in als DataFrames. `_mtime` invalideert de cache."""
    conn = sqlite3.connect(path)
    try:
        tabellen = {}
        for naam in ("wedstrijden", "aggregated_snapshots", "odds_snapshots",
                     "bets", "api_runs"):
            try:
                tabellen[naam] = pd.read_sql_query(f"SELECT * FROM {naam}", conn)
            except Exception:
                tabellen[naam] = pd.DataFrame()
    finally:
        conn.close()

    # Tijdkolommen parsen
    w = tabellen["wedstrijden"]
    if not w.empty:
        w["aanvang_dt"] = pd.to_datetime(w["aanvang"], utc=True, errors="coerce")
    a = tabellen["aggregated_snapshots"]
    if not a.empty:
        a["ts_dt"] = pd.to_datetime(a["timestamp"], utc=True, errors="coerce")
    r = tabellen["api_runs"]
    if not r.empty:
        r["ts_dt"] = pd.to_datetime(r["timestamp"], utc=True, errors="coerce")
    return tabellen


# ── Signaal-/Kelly-logica (spiegelt de tracker) ─────────────────────────────

def bereken_beweging(agg_match: pd.DataFrame, aanvang: pd.Timestamp) -> dict:
    """Bereken vroege en late beweging per uitkomst voor één wedstrijd."""
    if aanvang is None or pd.isna(aanvang):
        return {}
    ko = aanvang.timestamp()
    vroeg_grens = ko - VROEG_UUR_GRENS * 3600
    laat_grens  = ko - LAAT_UUR_GRENS  * 3600

    out = {}
    for uitkomst in ("thuis", "gelijkspel", "uit"):
        sub = agg_match[agg_match["uitkomst"] == uitkomst].sort_values("ts_dt")
        if len(sub) < 2:
            continue
        ts = sub["ts_dt"].map(lambda d: d.timestamp())
        probs = sub["implied_prob"].to_numpy()
        opening = probs[0]

        vroeg_mask = ts <= vroeg_grens
        laat_mask  = ts >= laat_grens

        vroege_beweging = None
        if vroeg_mask.any():
            vroege_beweging = round(float(probs[vroeg_mask.to_numpy()][-1] - opening), 4)

        late_beweging = None
        if laat_mask.sum() >= 2:
            late_vals = probs[laat_mask.to_numpy()]
            late_beweging = round(float(late_vals[-1] - late_vals[0]), 4)

        out[uitkomst] = {
            "opening_prob":    round(float(opening), 4),
            "huidige_prob":    round(float(probs[-1]), 4),
            "huidige_odd":     float(sub["median_odd"].to_numpy()[-1]),
            "vroege_beweging": vroege_beweging,
            "late_beweging":   late_beweging,
            "n_punten":        len(sub),
        }
    return out


def kelly_inzet(bankroll: float, beweging_magnitude: float, odd: float) -> float:
    """Quarter Kelly inzet (identiek aan tracker)."""
    if bankroll <= 0 or odd <= 1:
        return 0.0
    implied = 1 / odd
    confidence = min(beweging_magnitude / MIN_BEWEGING_PROB, 2.0)
    p_hat = min(implied + beweging_magnitude * confidence * 0.3, 0.95)
    q = 1 - p_hat
    b = odd - 1
    kelly = max((p_hat * b - q) / b, 0.0)
    bedrag = bankroll * kelly * KELLY_FRACTIE
    return round(min(bedrag, bankroll * MAX_BET_FRACTIE), 2)


def verzamel_signalen(tab: dict) -> pd.DataFrame:
    """Bouw een tabel met alle (bijna-)signalen over alle aankomende matches."""
    w = tab["wedstrijden"]
    a = tab["aggregated_snapshots"]
    if w.empty or a.empty:
        return pd.DataFrame()

    now = pd.Timestamp.now(tz="UTC")
    rijen = []
    for _, m in w.iterrows():
        if m.get("voltooid", 0) == 1 or pd.isna(m["aanvang_dt"]) or m["aanvang_dt"] <= now:
            continue
        agg_match = a[a["wedstrijd_id"] == m["id"]]
        bew = bereken_beweging(agg_match, m["aanvang_dt"])
        for uitkomst, d in bew.items():
            vb, lb = d["vroege_beweging"], d["late_beweging"]
            # Vroeg-sharp: positieve beweging
            if vb is not None:
                afstand = MIN_BEWEGING_PROB - vb  # >0 = nog niet getriggerd
                rijen.append({
                    "wedstrijd": f"{m['thuisteam']} vs {m['uitteam']}",
                    "aanvang":   m["aanvang_dt"],
                    "uitkomst":  uitkomst,
                    "type":      "vroeg_sharp",
                    "beweging":  vb,
                    "drempel":   MIN_BEWEGING_PROB,
                    "afstand":   round(afstand, 4),
                    "getriggerd": vb >= MIN_BEWEGING_PROB,
                    "odd":       d["huidige_odd"],
                })
            # Laat-publiek: negatieve beweging in laatste 6u
            if lb is not None:
                afstand = MIN_BEWEGING_PROB - (-lb)
                rijen.append({
                    "wedstrijd": f"{m['thuisteam']} vs {m['uitteam']}",
                    "aanvang":   m["aanvang_dt"],
                    "uitkomst":  uitkomst,
                    "type":      "laat_publiek",
                    "beweging":  lb,
                    "drempel":   MIN_BEWEGING_PROB,
                    "afstand":   round(afstand, 4),
                    "getriggerd": lb <= -MIN_BEWEGING_PROB,
                    "odd":       d["huidige_odd"],
                })
    df = pd.DataFrame(rijen)
    if not df.empty:
        df = df.sort_values("afstand")  # dichtst bij triggeren bovenaan
    return df


# ── Helpers ──────────────────────────────────────────────────────────────────

def bankroll_compound(bets: pd.DataFrame) -> float:
    if bets.empty or "pnl_compound" not in bets:
        return STARTBANKROLL
    settled = bets[bets["resultaat"].notna()]
    return round(STARTBANKROLL + settled["pnl_compound"].fillna(0).sum(), 2)


def fmt_dt(d) -> str:
    if d is None or pd.isna(d):
        return "—"
    return pd.Timestamp(d).strftime("%Y-%m-%d %H:%M UTC")


# ── Laden ────────────────────────────────────────────────────────────────────

if not os.path.exists(DB_PATH):
    st.error(f"Database niet gevonden: `{DB_PATH}`. "
             f"Zet hem in deze map of stel WK2026_DB in.")
    st.stop()

tab = laad_tabellen(DB_PATH, _db_mtime(DB_PATH))
w   = tab["wedstrijden"]
agg = tab["aggregated_snapshots"]
odds = tab["odds_snapshots"]
bets = tab["bets"]
runs = tab["api_runs"]

now = pd.Timestamp.now(tz="UTC")


# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚽ WK 2026")
    st.caption("Bet-simulatie dashboard")

    laatste_scrape = agg["ts_dt"].max() if not agg.empty else None
    if laatste_scrape is not None and not pd.isna(laatste_scrape):
        uur_geleden = (now - laatste_scrape).total_seconds() / 3600
        kleur = "🟢" if uur_geleden < 8 else ("🟡" if uur_geleden < 24 else "🔴")
        st.metric("Laatste scrape", f"{uur_geleden:.1f}u geleden")
        st.caption(f"{kleur} {fmt_dt(laatste_scrape)}")

    if st.button("🔄 Ververs data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("Strategie-config")
    st.write(f"- Startbankroll: €{STARTBANKROLL:.0f}")
    st.write(f"- Signaaldrempel: {MIN_BEWEGING_PROB:.0%}")
    st.write(f"- Re-bet drempel: {MIN_BEWEGING_PROB_REBET:.0%}")
    st.write(f"- Vroeg-venster: >{VROEG_UUR_GRENS}u voor KO")
    st.write(f"- Laat-venster: <{LAAT_UUR_GRENS}u voor KO")
    st.write(f"- Kelly: {KELLY_FRACTIE:.2f} (max {MAX_BET_FRACTIE:.0%}/bet)")
    st.divider()
    st.caption(f"DB: `{DB_PATH}`")
    st.caption(f"Grootte: {os.path.getsize(DB_PATH)/1e6:.1f} MB")


# ── Header KPIs ──────────────────────────────────────────────────────────────

st.title("WK 2026 — Bet-simulatie dashboard")

n_matches   = len(w)
n_voltooid  = int((w["voltooid"] == 1).sum()) if not w.empty else 0
n_upcoming  = int(((w["voltooid"] == 0) & (w["aanvang_dt"] > now)).sum()) if not w.empty else 0
n_bets      = len(bets)
settled     = bets[bets["resultaat"].notna()] if not bets.empty else pd.DataFrame()
n_settled   = len(settled)
n_open      = n_bets - n_settled
n_won       = int((settled["resultaat"] == "gewonnen").sum()) if not settled.empty else 0
br_compound = bankroll_compound(bets)

if not settled.empty:
    tot_stake   = settled["stake_compound"].fillna(0).sum()
    tot_pnl     = settled["pnl_compound"].fillna(0).sum()
    roi         = (tot_pnl / tot_stake * 100) if tot_stake > 0 else 0.0
    winrate     = (n_won / n_settled * 100) if n_settled else 0.0
else:
    tot_stake = tot_pnl = roi = winrate = 0.0

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Bankroll (compound)", f"€{br_compound:.2f}",
          delta=f"{br_compound - STARTBANKROLL:+.2f}")
c2.metric("Bets totaal", n_bets, delta=f"{n_open} open" if n_open else None)
c3.metric("Win-rate", f"{winrate:.0f}%", delta=f"{n_won}/{n_settled}" if n_settled else "n.v.t.")
c4.metric("ROI", f"{roi:+.1f}%")
c5.metric("Wedstrijden", n_matches, delta=f"{n_upcoming} aankomend")
c6.metric("Scrapes", agg["ts_dt"].nunique() if not agg.empty else 0)

# Pre-tournament info banner
if n_bets == 0:
    eerstvolgende = w[w["aanvang_dt"] > now]["aanvang_dt"].min() if not w.empty else None
    dagen = (eerstvolgende - now).total_seconds() / 86400 if eerstvolgende is not None and not pd.isna(eerstvolgende) else None
    msg = "ℹ️ **Nog geen bets geplaatst.** "
    if dagen is not None:
        msg += f"Eerste wedstrijd over **{dagen:.1f} dagen** ({fmt_dt(eerstvolgende)}). "
    msg += ("De bot wacht tot een odds-beweging de drempel van "
            f"{MIN_BEWEGING_PROB:.0%} overschrijdt. Bekijk hieronder welke "
            "wedstrijden daar het dichtst bij zitten.")
    st.info(msg)

st.divider()


# ── Tabs ─────────────────────────────────────────────────────────────────────

t_signalen, t_beweging, t_bets, t_schema, t_data = st.tabs(
    ["🎯 Signalen", "📈 Odds-beweging", "🎲 Bets & bankroll",
     "📅 Schema", "🩺 Data & API"]
)


# ── TAB: Signalen ────────────────────────────────────────────────────────────

with t_signalen:
    st.subheader("Signaal-nabijheid")
    st.caption("Hoe dicht zit elke wedstrijd/uitkomst bij het triggeren van een bet? "
               "`afstand` = drempel − beweging; ≤ 0 betekent getriggerd.")
    sig = verzamel_signalen(tab)
    if sig.empty:
        st.info("Nog onvoldoende data om beweging te berekenen "
                "(minstens 2 snapshots per wedstrijd nodig).")
    else:
        getriggerd = sig[sig["getriggerd"]]
        if not getriggerd.empty:
            st.success(f"🔥 {len(getriggerd)} actief signaal/signalen die de drempel halen!")
            st.dataframe(
                getriggerd.assign(
                    uitkomst=getriggerd["uitkomst"].map(UITKOMST_LABEL),
                    beweging=(getriggerd["beweging"] * 100).round(1),
                    aanvang=getriggerd["aanvang"].map(fmt_dt),
                )[["wedstrijd", "aanvang", "uitkomst", "type", "beweging", "odd"]]
                .rename(columns={"beweging": "beweging %"}),
                use_container_width=True, hide_index=True,
            )
        else:
            st.write("**Top 15 wedstrijden die het dichtst bij een signaal zitten:**")

        top = sig.head(15).copy()
        top["beweging %"]  = (top["beweging"] * 100).round(1)
        top["drempel %"]   = (top["drempel"] * 100).round(0)
        top["afstand %"]   = (top["afstand"] * 100).round(1)
        top["uitkomst"]    = top["uitkomst"].map(UITKOMST_LABEL)
        top["aanvang"]     = top["aanvang"].map(fmt_dt)
        st.dataframe(
            top[["wedstrijd", "aanvang", "uitkomst", "type",
                 "beweging %", "drempel %", "afstand %", "odd"]],
            use_container_width=True, hide_index=True,
        )

        # Bar chart van grootste bewegingen
        st.subheader("Grootste bewegingen (absoluut)")
        plotdf = sig.reindex(sig["beweging"].abs().sort_values(ascending=False).index).head(15)
        labels = plotdf["wedstrijd"] + " · " + plotdf["uitkomst"].map(UITKOMST_LABEL)
        fig = go.Figure(go.Bar(
            x=plotdf["beweging"] * 100, y=labels, orientation="h",
            marker_color=["#16a34a" if v > 0 else "#dc2626" for v in plotdf["beweging"]],
            text=[f"{v*100:+.1f}%" for v in plotdf["beweging"]], textposition="outside",
        ))
        fig.add_vline(x=MIN_BEWEGING_PROB * 100, line_dash="dash", line_color="gray",
                      annotation_text=f"+drempel {MIN_BEWEGING_PROB:.0%}")
        fig.add_vline(x=-MIN_BEWEGING_PROB * 100, line_dash="dash", line_color="gray",
                      annotation_text=f"−drempel")
        fig.update_layout(height=500, yaxis={"autorange": "reversed"},
                          xaxis_title="Beweging in implied probability (%-punt)",
                          margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, use_container_width=True)


# ── TAB: Odds-beweging ───────────────────────────────────────────────────────

with t_beweging:
    st.subheader("Odds-beweging over tijd")
    if w.empty or agg.empty:
        st.info("Geen aggregated snapshots beschikbaar.")
    else:
        w_sorted = w.sort_values("aanvang_dt")
        match_namen = {
            f"{r['thuisteam']} vs {r['uitteam']}  ({fmt_dt(r['aanvang_dt'])})": r["id"]
            for _, r in w_sorted.iterrows()
        }
        col_a, col_b = st.columns([3, 1])
        keuze = col_a.selectbox("Kies wedstrijd", list(match_namen.keys()))
        metric = col_b.radio("Toon", ["Implied probability", "Decimal odd"])
        wid = match_namen[keuze]

        match_row = w[w["id"] == wid].iloc[0]
        agg_match = agg[agg["wedstrijd_id"] == wid].sort_values("ts_dt")
        ycol = "implied_prob" if metric.startswith("Implied") else "median_odd"

        fig = go.Figure()
        for uitkomst in ("thuis", "gelijkspel", "uit"):
            sub = agg_match[agg_match["uitkomst"] == uitkomst]
            if sub.empty:
                continue
            naam = UITKOMST_LABEL[uitkomst]
            if uitkomst == "thuis":
                naam = f"{match_row['thuisteam']} (1)"
            elif uitkomst == "uit":
                naam = f"{match_row['uitteam']} (2)"
            fig.add_trace(go.Scatter(
                x=sub["ts_dt"], y=sub[ycol], mode="lines+markers", name=naam,
                line=dict(color=UITKOMST_KLEUR[uitkomst], width=2),
                hovertemplate="%{y:.3f}<br>%{x|%Y-%m-%d %H:%M}<extra>" + naam + "</extra>",
            ))

        ko = match_row["aanvang_dt"]
        if ko is not None and not pd.isna(ko):
            for delta_u, lbl, kl in [(0, "Aftrap", "#111827"),
                                     (VROEG_UUR_GRENS, f"−{VROEG_UUR_GRENS}u (vroeg-grens)", "#6b7280"),
                                     (LAAT_UUR_GRENS, f"−{LAAT_UUR_GRENS}u (laat-grens)", "#6b7280")]:
                x = ko - pd.Timedelta(hours=delta_u)
                fig.add_vline(x=x.timestamp() * 1000, line_dash="dot", line_color=kl,
                              annotation_text=lbl, annotation_position="top")

        fig.update_layout(height=480, xaxis_title="Tijd (UTC)",
                          yaxis_title=metric, hovermode="x unified",
                          legend=dict(orientation="h", y=1.12),
                          margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

        # Bewegingsoverzicht onder de grafiek
        bew = bereken_beweging(agg_match, ko)
        if bew:
            cols = st.columns(len(bew))
            for col, (uitkomst, d) in zip(cols, bew.items()):
                naam = UITKOMST_LABEL[uitkomst]
                delta = d["huidige_prob"] - d["opening_prob"]
                col.metric(
                    naam,
                    f"{d['huidige_prob']:.1%}  (odd {d['huidige_odd']:.2f})",
                    delta=f"{delta:+.1%} sinds open",
                )
                vb = d["vroege_beweging"]
                lb = d["late_beweging"]
                col.caption(
                    f"vroeg: {vb:+.1%}" if vb is not None else "vroeg: —"
                )
                col.caption(
                    f"laat: {lb:+.1%}" if lb is not None else "laat: — (nog >6u tot KO)"
                )
        st.caption(f"Gebaseerd op mediaan over {int(agg_match['n_bookmakers'].mean())} "
                   f"bookmakers (gemiddeld), {agg_match['ts_dt'].nunique()} snapshots.")


# ── TAB: Bets & bankroll ─────────────────────────────────────────────────────

with t_bets:
    st.subheader("Geplaatste bets")
    if bets.empty:
        st.info("Nog geen bets in de database. Zodra een signaal de drempel haalt, "
                "verschijnen ze hier — met stake, locked odd en (na afloop) resultaat.")
    else:
        toon = bets.merge(
            w[["id", "thuisteam", "uitteam", "aanvang_dt"]],
            left_on="wedstrijd_id", right_on="id", how="left", suffixes=("", "_w")
        )
        toon["wedstrijd"] = toon["thuisteam"] + " vs " + toon["uitteam"]
        toon["uitkomst_lbl"] = toon["uitkomst"].map(UITKOMST_LABEL)
        toon["status"] = toon["resultaat"].fillna("open")
        kol = ["id", "wedstrijd", "uitkomst_lbl", "signaal_type", "beweging_prob",
               "locked_odd", "is_rebet", "stake_compound", "stake_fixed",
               "resultaat", "pnl_compound", "geplaatst_op"]
        kol = [k for k in kol if k in toon.columns]
        st.dataframe(toon[kol].sort_values("geplaatst_op", ascending=False)
                     if "geplaatst_op" in toon else toon[kol],
                     use_container_width=True, hide_index=True)

        # Equity curve
        if not settled.empty and "afgewikkeld_op" in settled:
            eq = settled.sort_values("afgewikkeld_op").copy()
            eq["cum_pnl"] = eq["pnl_compound"].fillna(0).cumsum()
            eq["bankroll"] = STARTBANKROLL + eq["cum_pnl"]
            fig = go.Figure(go.Scatter(
                x=pd.to_datetime(eq["afgewikkeld_op"], utc=True, errors="coerce"),
                y=eq["bankroll"], mode="lines+markers", line=dict(color="#16a34a", width=2),
            ))
            fig.add_hline(y=STARTBANKROLL, line_dash="dash", line_color="gray",
                          annotation_text=f"start €{STARTBANKROLL:.0f}")
            fig.update_layout(height=360, title="Bankroll-curve (compound)",
                              xaxis_title="Tijd", yaxis_title="€",
                              margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, use_container_width=True)

    st.divider()
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Totaal ingezet (settled)", f"€{tot_stake:.2f}")
    cc2.metric("Totaal P&L", f"€{tot_pnl:+.2f}")
    cc3.metric("Bankroll nu", f"€{br_compound:.2f}", delta=f"{br_compound - STARTBANKROLL:+.2f}")


# ── TAB: Schema ──────────────────────────────────────────────────────────────

with t_schema:
    st.subheader("Wedstrijdschema")
    if w.empty:
        st.info("Geen wedstrijden in de database.")
    else:
        sch = w.sort_values("aanvang_dt").copy()
        sch["status"] = sch.apply(
            lambda r: ("✅ voltooid" if r["voltooid"] == 1
                       else ("🔴 live/voorbij" if r["aanvang_dt"] <= now else "⏳ aankomend")),
            axis=1)
        sch["aanvang"] = sch["aanvang_dt"].map(fmt_dt)
        sch["uitslag"] = sch.apply(
            lambda r: (f"{int(r['thuis_score'])}–{int(r['uit_score'])}"
                       if pd.notna(r.get("thuis_score")) else "—"),
            axis=1)
        kol = ["thuisteam", "uitteam", "aanvang", "status", "uitslag", "winnaar"]
        kol = [k for k in kol if k in sch.columns]
        st.dataframe(sch[kol], use_container_width=True, hide_index=True)
        st.caption(f"{n_upcoming} aankomend · {n_voltooid} voltooid · {n_matches} totaal")


# ── TAB: Data & API ──────────────────────────────────────────────────────────

with t_data:
    st.subheader("Scrape-cadans")
    if agg.empty:
        st.info("Geen snapshots.")
    else:
        ideaal = "elke 6u (4/dag)"
        per_dag = agg.groupby(agg["ts_dt"].dt.date)["ts_dt"].nunique()

        fig = go.Figure(go.Bar(x=[str(d) for d in per_dag.index], y=per_dag.values,
                               marker_color="#2563eb"))
        fig.add_hline(y=4, line_dash="dash", line_color="green",
                      annotation_text="doel: 4/dag")
        fig.update_layout(height=300, title=f"Scrapes per dag (doel: {ideaal})",
                          xaxis_title="Datum", yaxis_title="aantal scrapes",
                          margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

        gem = per_dag.mean()
        if gem < 3.5:
            st.warning(f"⚠️ Gemiddeld {gem:.1f} scrapes/dag — minder dan de beoogde 4. "
                       "GitHub Actions cron kan vertraagd/overgeslagen zijn bij hoge load. "
                       "Niet kritiek, maar minder datapunten = trager signaal.")
        else:
            st.success(f"✅ Gemiddeld {gem:.1f} scrapes/dag.")

    c1, c2, c3 = st.columns(3)
    c1.metric("Ruwe odds-rijen", f"{len(odds):,}")
    c2.metric("Aggregaat-rijen", f"{len(agg):,}")
    c3.metric("Gem. bookmakers/match",
              f"{agg['n_bookmakers'].mean():.0f}" if not agg.empty else "—")

    st.subheader("API-runs")
    if runs.empty:
        st.info("Geen API-runs geregistreerd.")
    else:
        toon = runs.sort_values("ts_dt", ascending=False).copy()
        toon["tijd"] = toon["ts_dt"].map(fmt_dt)
        kol = [k for k in ["tijd", "endpoint", "credits_used", "credits_remaining", "status"]
               if k in toon.columns]
        st.dataframe(toon[kol], use_container_width=True, hide_index=True)
        rem = runs["credits_remaining"].dropna()
        if not rem.empty:
            st.metric("Laatst bekende resterende credits", int(rem.iloc[-1]))
        else:
            st.caption("Geen credit-headers geregistreerd in deze runs.")
