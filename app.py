"""
app.py — AFP NAV Explorer (Streamlit).

Minimal dark UI with TradingView-inspired charts (crosshair hover, right-side
axis, range buttons, red/green semantics) applied to fund analytics.

Run via the customtkinter launcher, or directly:
    python -m streamlit run app.py
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import nav_data as D
import returns as R
import store

# --------------------------------------------------------------------------- #
# Design tokens — TradingView-inspired dark palette
# --------------------------------------------------------------------------- #
BG, PANEL, GRID = "#131722", "#1E222D", "#2A2E39"
TEXT, MUTED = "#D1D4DC", "#787B86"
ACCENT, UP, DOWN = "#2962FF", "#089981", "#F23645"
SERIES = [ACCENT, "#FF9800", UP, "#E040FB", "#26C6DA", DOWN]
FONT = "-apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif"
PLOTLY_CONFIG = {"displayModeBar": False}

st.set_page_config(page_title="AFP NAV Explorer", page_icon="📈",
                   layout="wide", initial_sidebar_state="expanded")

st.markdown(f"""
<style>
  [data-testid="stHeader"] {{background: transparent;}}
  [data-testid="stToolbar"], .stAppDeployButton, #MainMenu, footer
    {{display: none;}}
  .block-container {{padding-top: 1.4rem; padding-bottom: 2rem;
    max-width: 1240px;}}

  html, body, p, span, label {{color: {TEXT};}}
  h1, h2, h3, h4 {{color: {TEXT}; font-weight: 600;}}
  small, .stCaption, [data-testid="stCaptionContainer"] p {{color: {MUTED};}}

  .brand {{color: {MUTED}; font-size: .65rem; font-weight: 600;
    letter-spacing: .2em; text-transform: uppercase;}}
  .scheme-title {{color: {TEXT}; font-size: 1.45rem; font-weight: 600;
    line-height: 1.3; margin: 0;}}
  .scheme-sub {{color: {MUTED}; font-size: .82rem; margin: .2rem 0 0;}}
  .sec {{color: {MUTED}; font-size: .7rem; font-weight: 600;
    letter-spacing: .12em; text-transform: uppercase;
    margin: 1.4rem 0 .5rem; padding-bottom: .35rem;
    border-bottom: 1px solid {GRID};}}
  .pos {{color: {UP};}} .neg {{color: {DOWN};}}

  [data-testid="stMetric"] {{background: {PANEL}; border: 1px solid {GRID};
    border-radius: 8px; padding: .8rem 1rem;}}
  [data-testid="stMetricLabel"] p {{color: {MUTED} !important;
    font-size: .68rem !important; font-weight: 600;
    letter-spacing: .08em; text-transform: uppercase;}}
  [data-testid="stMetricValue"] {{color: {TEXT}; font-size: 1.3rem;
    font-variant-numeric: tabular-nums;}}
  [data-testid="stMetricDelta"] {{font-size: .8rem;}}

  .stTabs [data-baseweb="tab-list"] {{gap: .25rem;
    border-bottom: 1px solid {GRID};}}
  .stTabs [data-baseweb="tab"] {{color: {MUTED}; font-size: .85rem;
    padding: .4rem .9rem;}}
  .stTabs [aria-selected="true"] {{color: {TEXT};}}

  [data-testid="stDataFrame"] {{font-variant-numeric: tabular-nums;}}
  [data-testid="stSidebar"] {{border-right: 1px solid {GRID};}}
  [data-testid="stSidebar"] .block-container {{padding-top: 1.2rem;}}
  hr {{border-color: {GRID};}}
</style>""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Chart helpers — one consistent TradingView-like template
# --------------------------------------------------------------------------- #
def tv(fig: go.Figure, height: int = 320, *, legend: bool = False,
       unified: bool = True, spikes: bool = True) -> go.Figure:
    """Apply the shared minimal chart style: transparent panes, faint grid,
    crosshair spikes, right-side y-axis, dark hover card."""
    fig.update_layout(
        height=height, margin=dict(l=0, r=4, t=8, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=12, color=MUTED),
        hovermode="x unified" if unified else "closest",
        hoverlabel=dict(bgcolor=PANEL, bordercolor=GRID,
                        font=dict(family=FONT, size=12, color=TEXT)),
        showlegend=legend,
        legend=dict(orientation="h", x=0, y=1.1, bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT, size=12)),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, showline=False, ticks="",
                     showspikes=spikes, spikemode="across", spikesnap="cursor",
                     spikedash="dot", spikethickness=1, spikecolor=MUTED)
    fig.update_yaxes(side="right", gridcolor=GRID, zeroline=False,
                     showline=False, ticks="")
    return fig


def range_buttons(fig: go.Figure) -> go.Figure:
    """TradingView-style period buttons on a time-series chart."""
    fig.update_xaxes(rangeselector=dict(
        buttons=[
            dict(count=1, label="1M", step="month", stepmode="backward"),
            dict(count=6, label="6M", step="month", stepmode="backward"),
            dict(label="YTD", step="year", stepmode="todate"),
            dict(count=1, label="1Y", step="year", stepmode="backward"),
            dict(count=3, label="3Y", step="year", stepmode="backward"),
            dict(count=5, label="5Y", step="year", stepmode="backward"),
            dict(label="All", step="all"),
        ],
        x=0, y=1.12, bgcolor=PANEL, activecolor=ACCENT,
        bordercolor=GRID, borderwidth=1, font=dict(color=TEXT, size=11)))
    return fig


def chart(fig: go.Figure) -> None:
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)


def section(label: str) -> None:
    st.markdown(f"<div class='sec'>{label}</div>", unsafe_allow_html=True)


def table(df: pd.DataFrame, **kwargs) -> None:
    """st.dataframe with NaN shown as an em-dash instead of 'None'."""
    disp = df.round(2)
    disp = disp.astype(object).where(disp.notna(), "—")
    st.dataframe(disp, width="stretch", **kwargs)


# --------------------------------------------------------------------------- #
# Cached data access (in-session cache => polite to mfapi)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=60 * 60 * 6, show_spinner="Loading AMFI universe…")
def get_universe() -> pd.DataFrame:
    return D.live_universe(D.parse_amfi(D.fetch_amfi_raw()))


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def get_history(code: int):
    payload = D.fetch_scheme(int(code))
    return D.history_to_series(payload), D.scheme_meta(payload)


def name_of(univ: pd.DataFrame, code: int) -> str:
    row = univ[univ["code"] == code]
    return row["name"].iloc[0] if not row.empty else f"Scheme {code}"


def pct(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.2f}%"


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
if "watchlists" not in st.session_state:
    st.session_state.watchlists = store.load()
if "active_list" not in st.session_state:
    st.session_state.active_list = next(iter(st.session_state.watchlists))

try:
    universe = get_universe()
except Exception as e:  # noqa: BLE001
    st.error(f"Could not load the AMFI universe: {e}")
    st.stop()


# --------------------------------------------------------------------------- #
# Sidebar — universe search + watchlist management
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("<div class='brand'>Arthashastra Finsec</div>",
                unsafe_allow_html=True)
    st.markdown("<p class='scheme-title'>NAV Explorer</p>",
                unsafe_allow_html=True)
    st.caption(f"{len(universe):,} live schemes · NAV as of "
               f"{universe['date'].max().date()}")

    section("Watchlist")
    lists = list(st.session_state.watchlists.keys())
    st.session_state.active_list = st.selectbox(
        "Active list", lists,
        index=lists.index(st.session_state.active_list)
        if st.session_state.active_list in lists else 0,
        label_visibility="collapsed")

    with st.expander("Edit lists"):
        new_name = st.text_input("New list", key="new_list",
                                 label_visibility="collapsed",
                                 placeholder="New list name")
        c1, c2 = st.columns(2)
        if c1.button("Create", width="stretch") and new_name:
            store.create_list(st.session_state.watchlists, new_name)
            st.session_state.active_list = new_name
            st.rerun()
        if c2.button("Delete current", width="stretch"):
            store.delete_list(st.session_state.watchlists,
                              st.session_state.active_list)
            st.session_state.active_list = next(iter(st.session_state.watchlists))
            st.rerun()

    section("Add schemes")
    fh = st.selectbox("Fund house", [""] + sorted(universe["fund_house"]
                                                  .dropna().unique().tolist()))
    cat = st.selectbox("Category", [""] + sorted(universe["category"]
                                                 .dropna().unique().tolist()))
    q = st.text_input("Search name", placeholder="e.g. contra, small cap")
    results = D.search_universe(universe, query=q, fund_house=fh, category=cat)
    st.caption(f"{len(results):,} matches")
    if not results.empty:
        opts = {f"{r['name']}  ·  [{r['code']}]": r["code"]
                for _, r in results.head(300).iterrows()}
        pick = st.multiselect("Results", list(opts.keys()),
                              label_visibility="collapsed")
        if st.button("Add to watchlist", type="primary",
                     width="stretch"):
            for label in pick:
                store.add(st.session_state.watchlists,
                          st.session_state.active_list, opts[label])
            st.rerun()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
active = st.session_state.active_list
codes = st.session_state.watchlists.get(active, [])

if not codes:
    st.markdown("<p class='scheme-title'>Your watchlist is empty</p>",
                unsafe_allow_html=True)
    st.caption("Use the sidebar to search the AMFI universe and add schemes.")
    st.stop()

scheme_labels = {f"{name_of(universe, c)}  ·  [{c}]": c for c in codes}
chosen_label = st.selectbox("Scheme", list(scheme_labels.keys()),
                            label_visibility="collapsed")
code = scheme_labels[chosen_label]

with st.spinner("Fetching NAV history from mfapi.in…"):
    try:
        series, meta = get_history(code)
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not fetch history for {code}: {e}")
        st.stop()

if series.empty:
    st.warning("No NAV history returned for this scheme.")
    st.stop()

# ---- Header: scheme identity + key metrics ---- #
st.markdown(
    f"<p class='scheme-title'>{meta.get('scheme_name', chosen_label.split('  ·')[0])}</p>"
    f"<p class='scheme-sub'>{meta.get('fund_house', '')} · "
    f"{meta.get('scheme_category', '')} · since {series.index.min().date()} · "
    f"code {code} · list “{active}”</p>",
    unsafe_allow_html=True)
st.write("")

tr = R.trailing_returns(series).set_index("Period")
chg_1d = (series.iloc[-1] / series.iloc[-2] - 1.0) * 100 if len(series) > 1 else None

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Latest NAV", f"₹{series.iloc[-1]:,.2f}",
          f"{chg_1d:+.2f}%" if chg_1d is not None else None,
          help=f"As of {series.index.max().date()}; delta vs previous NAV day.")
m2.metric("1Y return", pct(tr.loc["1Y", "Absolute %"]) if "1Y" in tr.index else "—")
m3.metric("3Y CAGR", pct(tr.loc["3Y", "Annualised %"]) if "3Y" in tr.index else "—")
m4.metric("5Y CAGR", pct(tr.loc["5Y", "Annualised %"]) if "5Y" in tr.index else "—")
m5.metric("Max drawdown", pct(R.max_drawdown(series) * 100))

tabs = st.tabs(["Overview", "Returns", "Rolling", "SIP / XIRR",
                "Compare", "Watchlist"])

# ---- Overview ---- #
with tabs[0]:
    left, right = st.columns([2, 1], gap="large")
    with left:
        section("NAV history")
        log_scale = st.toggle("Log scale", value=False)
        fig = go.Figure(go.Scatter(
            x=series.index, y=series.values, name="NAV",
            line=dict(color=ACCENT, width=1.5),
            fill="tozeroy", fillcolor="rgba(41,98,255,0.07)",
            hovertemplate="₹%{y:,.2f}<extra></extra>"))
        tv(fig, 380)
        range_buttons(fig)
        if log_scale:
            fig.update_yaxes(type="log")
        chart(fig)

        section("Drawdown")
        dd = R.drawdown(series)
        ddfig = go.Figure(go.Scatter(
            x=dd.index, y=dd.values * 100, name="Drawdown",
            line=dict(color=DOWN, width=1),
            fill="tozeroy", fillcolor="rgba(242,54,69,0.12)",
            hovertemplate="%{y:.2f}%<extra></extra>"))
        tv(ddfig, 200)
        chart(ddfig)
    with right:
        section("Trailing returns")
        table(R.trailing_returns(series), hide_index=True)
        st.metric("Annualised volatility", pct(R.annualised_vol(series) * 100),
                  help="Stdev of daily returns, annualised over 252 trading days.")

    section("Calendar-year returns")
    cal = R.calendar_year_returns(series)
    if not cal.empty:
        calfig = go.Figure(go.Bar(
            x=cal["Year"], y=cal["Return %"],
            marker_color=[UP if v >= 0 else DOWN for v in cal["Return %"]],
            marker_line_width=0,
            hovertemplate="%{y:.1f}%<extra></extra>"))
        tv(calfig, 280, spikes=False)
        calfig.update_layout(bargap=0.45)
        chart(calfig)

    mat = R.monthly_matrix(series)
    if not mat.empty:
        section("Month-on-month heatmap (%)")
        hm = go.Figure(go.Heatmap(
            z=mat.values, x=mat.columns, y=mat.index.astype(str),
            colorscale=[[0, DOWN], [0.5, PANEL], [1, UP]], zmid=0,
            texttemplate="%{z:.1f}", textfont=dict(size=10),
            showscale=False, hoverongaps=False,
            hovertemplate="%{x} %{y}: %{z:.2f}%<extra></extra>",
            xgap=2, ygap=2))
        tv(hm, max(220, 26 * len(mat)), unified=False, spikes=False)
        hm.update_yaxes(autorange="reversed", side="left")
        hm.update_xaxes(showgrid=False)
        hm.update_yaxes(showgrid=False)
        chart(hm)

# ---- Returns (point-to-point) ---- #
with tabs[1]:
    section("Custom date range")
    lo, hi = series.index.min().date(), series.index.max().date()
    c1, c2 = st.columns(2)
    d1 = c1.date_input("From", value=max(lo, hi - dt.timedelta(days=365)),
                       min_value=lo, max_value=hi)
    d2 = c2.date_input("To", value=hi, min_value=lo, max_value=hi)
    if d1 < d2:
        p = R.point_to_point(series, d1, d2)
        x1, x2, x3, x4 = st.columns(4)
        x1.metric("NAV (from)", f"₹{p['nav1']:,.2f}")
        x2.metric("NAV (to)", f"₹{p['nav2']:,.2f}")
        x3.metric("Absolute", pct(p["abs"] * 100))
        x4.metric(f"CAGR ({p['years']:.1f}y)",
                  pct(p["cagr"] * 100) if p["cagr"] is not None else "—")
    else:
        st.warning("‘From’ must be before ‘To’.")

    c1, c2 = st.columns(2, gap="large")
    with c1:
        section("Financial-year returns")
        st.dataframe(R.fy_returns(series).round(2), hide_index=True,
                     width="stretch")
    with c2:
        section("Month-on-month returns")
        st.dataframe(R.monthly_returns(series).round(2).iloc[::-1],
                     hide_index=True, width="stretch", height=380)

# ---- Rolling ---- #
with tabs[2]:
    c1, c2 = st.columns([1, 1])
    win = c1.radio("Rolling window", [1, 3, 5], horizontal=True,
                   format_func=lambda x: f"{x}Y")
    hurdle = c2.number_input("Hurdle for ‘% above’ (annualised %)",
                             value=8.0, step=0.5) / 100
    roll = R.rolling_returns(series, win)
    if roll.empty:
        st.warning(f"Not enough history for a {win}Y rolling window.")
    else:
        stats = R.rolling_stats(roll, hurdle)
        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("Observations", f"{stats['observations']:,}")
        s2.metric("Average", pct(stats["mean"] * 100))
        s3.metric("Median", pct(stats["median"] * 100))
        s4.metric("Min / Max",
                  f"{stats['min']*100:.1f} / {stats['max']*100:.1f}%")
        s5.metric("% negative", pct(stats["pct_negative"]))
        st.caption(f"{pct(stats['pct_above_hurdle'])} of windows beat the "
                   f"{hurdle*100:.1f}% hurdle · IQR "
                   f"{stats['p25']*100:.1f}% – {stats['p75']*100:.1f}%")

        section(f"{win}Y rolling annualised return (%)")
        rfig = go.Figure(go.Scatter(
            x=roll.index, y=roll.values * 100, name=f"{win}Y rolling",
            line=dict(color=ACCENT, width=1),
            hovertemplate="%{y:.2f}%<extra></extra>"))
        rfig.add_hline(y=hurdle * 100, line_dash="dash", line_color=MUTED,
                       line_width=1)
        tv(rfig, 320)
        chart(rfig)

        section("Distribution of rolling returns (%)")
        hist = go.Figure(go.Histogram(
            x=roll.values * 100, nbinsx=60, marker_color=ACCENT,
            marker_line_width=0,
            hovertemplate="%{x}: %{y}<extra></extra>"))
        tv(hist, 240, unified=False, spikes=False)
        hist.update_layout(bargap=0.08)
        chart(hist)

# ---- SIP ---- #
with tabs[3]:
    section("SIP return (XIRR)")
    c1, c2, c3, c4 = st.columns(4)
    amt = c1.number_input("Monthly amount (₹)", value=10000, step=1000)
    sip_lo = c2.date_input("Start", value=max(series.index.min().date(),
                           series.index.max().date() - dt.timedelta(days=3 * 365)),
                           min_value=series.index.min().date(),
                           max_value=series.index.max().date(), key="sip_lo")
    sip_hi = c3.date_input("End", value=series.index.max().date(),
                           min_value=series.index.min().date(),
                           max_value=series.index.max().date(), key="sip_hi")
    dom = c4.number_input("SIP day", value=1, min_value=1, max_value=28)
    if sip_lo < sip_hi:
        sip = R.sip_xirr(series, amt, sip_lo, sip_hi, int(dom))
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Invested", f"₹{sip['invested']:,.0f}")
        r2.metric("Current value", f"₹{sip['final_value']:,.0f}")
        r3.metric("Absolute gain", pct(sip["abs_gain_pct"]))
        r4.metric("XIRR", pct(sip["xirr"] * 100))
        st.caption("XIRR is the money-weighted annualised return on the SIP "
                   "cashflows. Lumpsum CAGR for the same window is on the "
                   "Returns tab.")

# ---- Compare ---- #
with tabs[4]:
    section("Compare schemes")
    cmp_labels = st.multiselect("Schemes to compare (from this watchlist)",
                                list(scheme_labels.keys()),
                                default=[chosen_label])
    if len(cmp_labels) >= 1:
        named = {}
        for lbl in cmp_labels:
            try:
                s_i, _ = get_history(scheme_labels[lbl])
                named[lbl.split("  ·")[0]] = s_i
            except Exception:  # noqa: BLE001
                st.warning(f"Skipped {lbl} (fetch failed).")
        if named:
            reb = R.compare_rebased(named)
            cfig = go.Figure()
            for i, col in enumerate(reb.columns):
                cfig.add_trace(go.Scatter(
                    x=reb.index, y=reb[col], name=col,
                    line=dict(color=SERIES[i % len(SERIES)], width=1.4),
                    hovertemplate="%{y:,.1f}<extra>" + col + "</extra>"))
            tv(cfig, 420, legend=True)
            range_buttons(cfig)
            st.caption("Growth of ₹100 from a common start date.")
            chart(cfig)
            section("Trailing-return comparison (annualised where >1Y, else absolute)")
            table(R.compare_trailing(named))

# ---- Watchlist ---- #
with tabs[5]:
    section(f"Manage “{active}”")
    for c in codes:
        cc1, cc2 = st.columns([6, 1])
        cc1.write(f"{name_of(universe, c)}  ·  `{c}`")
        if cc2.button("Remove", key=f"rm_{c}"):
            store.remove(st.session_state.watchlists, active, c)
            st.rerun()
    st.caption(f"Watchlists are saved at: {store.WATCHLIST_FILE}")

st.divider()
st.caption("Data: AMFI NAVAll.txt (universe + latest NAV) and api.mfapi.in "
           "(history, fetched on-demand). mfapi.in is an unofficial community "
           "API. Returns are NAV-based and exclude exit loads and taxes. "
           "For information only — not investment advice.")
