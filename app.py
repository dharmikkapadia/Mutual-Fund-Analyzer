"""
app.py — AFP NAV Explorer (Streamlit).

Minimal dark UI with TradingView-inspired charts (crosshair hover, right-side
axis, range buttons, red/green semantics) applied to fund analytics.

Run via the customtkinter launcher, or directly:
    python -m streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_js_eval import streamlit_js_eval

import holdings as H
import nav_data as D
import returns as R
import store

# --------------------------------------------------------------------------- #
# Viewing modes — each defines the full token set; native Streamlit widget
# colours are switched at runtime via st._config (note: that part is
# process-wide, so on a shared deployment the last pick wins for new loads)
# --------------------------------------------------------------------------- #
THEMES = {
    "Midnight": dict(bg="#000000", panel="#101014", grid="#34343C",
                     text="#F2F2F5", muted="#9D9DA8", accent="#2962FF",
                     up="#0ECB81", down="#F6465D", base="dark"),
    "Slate": dict(bg="#131722", panel="#1E222D", grid="#2A2E39",
                  text="#D1D4DC", muted="#787B86", accent="#2962FF",
                  up="#089981", down="#F23645", base="dark"),
    "Light": dict(bg="#FFFFFF", panel="#F4F6FA", grid="#DCE1E8",
                  text="#0E1116", muted="#5A6472", accent="#2962FF",
                  up="#0E9F6E", down="#E5484D", base="light"),
    "Sepia": dict(bg="#F4ECD8", panel="#EADFC4", grid="#CFBE9C",
                  text="#332A1B", muted="#6E5F45", accent="#205EA6",
                  up="#1E7B45", down="#B3402E", base="light"),
}
DEFAULT_THEME = "Midnight"
FONT = "-apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif"
PLOTLY_CONFIG = {
    "displayModeBar": True, "displaylogo": False, "scrollZoom": True,
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d",
                               "zoomIn2d", "zoomOut2d"],
}


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return (f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},"
            f"{int(h[4:6], 16)},{alpha})")


def on_color(hex_color: str) -> str:
    """Black or white, whichever reads better on `hex_color` (WCAG luminance)."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
           for c in (r, g, b)]
    lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return "#0E1116" if lum > 0.4 else "#FFFFFF"


def _apply_native_theme(t: dict) -> bool:
    """Point Streamlit's own widget theme at the active palette."""
    opts = {"theme.base": t["base"], "theme.backgroundColor": t["bg"],
            "theme.secondaryBackgroundColor": t["panel"],
            "theme.textColor": t["text"], "theme.primaryColor": t["accent"]}
    changed = False
    for k, v in opts.items():
        if st._config.get_option(k) != v:
            st._config.set_option(k, v)
            changed = True
    return changed


def _on_theme_change():
    st.session_state.theme = st.session_state.theme_pick
    _apply_native_theme(THEMES[st.session_state.theme])


st.set_page_config(page_title="AFP NAV Explorer", page_icon="📈",
                   layout="wide", initial_sidebar_state="expanded")

# --------------------------------------------------------------------------- #
# Session state — watchlists and theme live in the browser (localStorage) so
# they survive restarts on cloud deployments and stay per-visitor. The JSON
# file (store.py) is the desktop fallback and one-time migration source.
# AFP_NO_BROWSER_STORE=1 disables the browser sync (used by headless tests).
# --------------------------------------------------------------------------- #
LS_KEY = "afp_watchlists_v1"
LS_THEME = "afp_theme_v1"
USE_BROWSER_STORE = os.getenv("AFP_NO_BROWSER_STORE", "") != "1"

if "watchlists" not in st.session_state:
    boot, boot_theme = None, None
    if USE_BROWSER_STORE:
        raw = streamlit_js_eval(
            js_expressions=("JSON.stringify({"
                            f"wl: localStorage.getItem('{LS_KEY}'), "
                            f"th: localStorage.getItem('{LS_THEME}')}})"),
            key="ls_read")
        if raw is None:
            # Component round-trip pending; the value arriving triggers a rerun.
            st.caption("Loading saved settings…")
            st.stop()
        try:
            obj = json.loads(raw) or {}
            if obj.get("wl"):
                boot = {str(k): [int(c) for c in v]
                        for k, v in json.loads(obj["wl"]).items()}
            boot_theme = obj.get("th")
        except (ValueError, TypeError, AttributeError):
            boot = None
    st.session_state.watchlists = boot or store.load()
    st.session_state.theme = (boot_theme if boot_theme in THEMES
                              else DEFAULT_THEME)
if "active_list" not in st.session_state:
    st.session_state.active_list = next(iter(st.session_state.watchlists))
if "theme" not in st.session_state:
    st.session_state.theme = DEFAULT_THEME

T = THEMES[st.session_state.theme]
BG, PANEL, GRID = T["bg"], T["panel"], T["grid"]
TEXT, MUTED = T["text"], T["muted"]
ACCENT, UP, DOWN = T["accent"], T["up"], T["down"]
ON_ACCENT = on_color(ACCENT)
SERIES = [ACCENT, "#FF9800", UP, "#E040FB", "#26C6DA", DOWN]
SHADOW = ("0 6px 18px rgba(0,0,0,.35)" if T["base"] == "dark"
          else "0 6px 18px rgba(0,0,0,.12)")
if _apply_native_theme(T):
    # frontend picks the new widget theme up on the next run
    st.rerun()

if USE_BROWSER_STORE:
    # Write-through mirrors: re-run only when the payload changes.
    _payload = json.dumps(st.session_state.watchlists, separators=(",", ":"))
    streamlit_js_eval(
        js_expressions=f"localStorage.setItem('{LS_KEY}', "
                       f"{json.dumps(_payload)})",
        key="ls_write")
    streamlit_js_eval(
        js_expressions=f"localStorage.setItem('{LS_THEME}', "
                       f"{json.dumps(st.session_state.theme)})",
        key="ls_write_theme")

st.markdown(f"""
<style>
  [data-testid="stHeader"] {{background: transparent;}}
  /* Hide only the right-side toolbar actions — the toolbar itself hosts the
     sidebar-reopen chevron, so it must stay visible. */
  [data-testid="stToolbarActions"], [data-testid="stAppDeployButton"],
  [data-testid="stMainMenu"], footer {{display: none;}}
  [data-testid="stHeader"] button {{color: {MUTED};}}
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
  [data-testid="stCustomComponentV1"] {{display: none;}}
  [data-testid="stSidebar"] {{border-right: 1px solid {GRID};}}
  [data-testid="stSidebar"] .block-container {{padding-top: 1.2rem;}}
  hr {{border-color: {GRID};}}

  /* ---- multiselect chips: solid accent, high-contrast text ---- */
  [data-baseweb="tag"] {{background-color: {ACCENT} !important;
    border-radius: 6px;}}
  [data-baseweb="tag"], [data-baseweb="tag"] span,
  [data-baseweb="tag"] div {{color: {ON_ACCENT} !important;
    font-weight: 500;}}
  [data-baseweb="tag"] svg {{fill: {ON_ACCENT} !important;
    color: {ON_ACCENT} !important;}}
  [data-baseweb="tag"] [role="button"] {{opacity: .8;
    border-radius: 4px; transition: opacity .15s ease,
    background-color .15s ease;}}
  [data-baseweb="tag"] [role="button"]:hover {{opacity: 1;
    background-color: {rgba(ON_ACCENT, 0.25)};}}
  [data-baseweb="select"] [data-baseweb="tag"] {{margin: 2px 3px;}}

  /* ---- motion ---- */
  html {{scroll-behavior: smooth;}}
  .stApp {{transition: background-color .4s ease, color .4s ease;}}
  @media (prefers-reduced-motion: no-preference) {{
    @keyframes afpFadeUp {{
      from {{opacity: 0; transform: translateY(8px);}}
      to   {{opacity: 1; transform: none;}}
    }}
    .block-container {{animation: afpFadeUp .35s ease both;}}
    [role="tabpanel"] {{animation: afpFadeUp .35s ease both;}}
    [data-testid="stMetric"] {{transition: transform .18s ease,
      border-color .18s ease, box-shadow .18s ease;}}
    [data-testid="stMetric"]:hover {{transform: translateY(-2px);
      border-color: {ACCENT}; box-shadow: {SHADOW};}}
    .stButton button, [data-testid="stFormSubmitButton"] button
      {{transition: transform .15s ease, filter .15s ease,
        box-shadow .15s ease;}}
    .stButton button:hover {{transform: translateY(-1px);
      filter: brightness(1.08); box-shadow: {SHADOW};}}
    .stButton button:active {{transform: translateY(0) scale(.98);}}
    .stTabs [data-baseweb="tab"] {{transition: color .2s ease;}}
    [data-testid="stExpander"] {{transition: border-color .2s ease;}}
    [data-testid="stExpander"]:hover {{border-color: {ACCENT};}}
    a {{transition: color .15s ease;}}
  }}
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
                        font=dict(family=FONT, size=13, color=TEXT)),
        showlegend=legend,
        legend=dict(orientation="h", x=0, y=1.1, bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT, size=12)),
        dragmode="pan",
        modebar=dict(bgcolor="rgba(0,0,0,0)", color=MUTED, activecolor=TEXT),
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
    """st.dataframe with numbers pre-formatted and NaN as an em-dash
    (the data grid renders nulls as 'None' even with a Styler na_rep)."""
    disp = df.copy()
    for c in disp.select_dtypes("number").columns:
        disp[c] = disp[c].map(lambda v: "—" if pd.isna(v) else f"{v:,.2f}")
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


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def get_portfolio(isin: str, name: str, amfi_code: int | None = None) -> dict:
    return H.fetch_portfolio(isin, name, amfi_code)


def isin_of(univ: pd.DataFrame, code: int) -> str:
    row = univ[univ["code"] == code]
    isin = row["isin"].iloc[0] if not row.empty else ""
    return isin if isinstance(isin, str) and len(isin) >= 8 else ""


def links_md(name: str, rv_code=None) -> str:
    rv = (H.rupeevest_scheme_link(rv_code) if rv_code
          else H.rupeevest_link(name))
    return (f"[Rupeevest]({rv}) · "
            f"[Factsheet]({H.factsheet_link(name)}) · "
            f"[ValueResearch]({H.valueresearch_link(name)})")


def name_of(univ: pd.DataFrame, code: int) -> str:
    row = univ[univ["code"] == code]
    return row["name"].iloc[0] if not row.empty else f"Scheme {code}"


def pct(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.2f}%"


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

    st.divider()
    section("Appearance")
    _theme_names = list(THEMES.keys())
    st.selectbox("Viewing mode", _theme_names,
                 index=_theme_names.index(st.session_state.theme),
                 key="theme_pick", on_change=_on_theme_change,
                 label_visibility="collapsed")


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

tabs = st.tabs(["Dashboard", "Overview", "Returns", "Rolling", "SIP / XIRR",
                "Compare", "Holdings", "Peers"])

RET_COLS = ["1D %", "1Y %", "3Y % pa", "5Y % pa"]


def _ret_color(v):
    if pd.isna(v):
        return f"color: {MUTED}"
    return f"color: {UP}" if v >= 0 else f"color: {DOWN}"


def snapshot_table(rows: list[dict], highlight_code: int | None = None) -> None:
    """Render a snapshot grid: sparkline + red/green returns, sortable."""
    num_df = pd.DataFrame(rows)
    disp = num_df.copy()
    ret_cols = [c for c in RET_COLS if c in disp.columns]
    for c in ret_cols + (["Max DD %"] if "Max DD %" in disp.columns else []):
        disp[c] = num_df[c].map(lambda v: "—" if pd.isna(v) else f"{v:.2f}")
    if "NAV" in disp.columns:
        disp["NAV"] = num_df["NAV"].map(
            lambda v: "—" if pd.isna(v) else f"₹{v:,.2f}")
    styler = disp.style.apply(
        lambda col: num_df[col.name].map(_ret_color), subset=ret_cols)
    if highlight_code is not None and "Code" in disp.columns:
        styler = styler.apply(
            lambda r: [f"background-color: {GRID}"] * len(r)
            if r["Code"] == highlight_code else [""] * len(r), axis=1)
    st.dataframe(
        styler, hide_index=True, width="stretch",
        height=min(40 + 35 * len(disp), 600),
        column_config={
            "1Y trend": st.column_config.LineChartColumn("1Y trend"),
            "Code": st.column_config.NumberColumn("Code", format="%d"),
        })


# ---- Dashboard ---- #
with tabs[0]:
    section(f"Watchlist “{active}” — {len(codes)} scheme(s)")
    snap_rows, failed = [], []
    with st.spinner("Loading watchlist…"):
        for c in codes:
            try:
                s_i, _ = get_history(c)
                snap = R.snapshot(s_i)
                if not snap:
                    raise ValueError("empty history")
            except Exception:  # noqa: BLE001
                failed.append(c)
                continue
            snap_rows.append({
                "Scheme": name_of(universe, c), "Code": c,
                "NAV": snap["nav"], "1Y trend": snap["spark"],
                "1D %": snap["chg_1d"], "1Y %": snap["1Y"],
                "3Y % pa": snap["3Y"], "5Y % pa": snap["5Y"],
                "Max DD %": snap["mdd"]})
    if snap_rows:
        snapshot_table(snap_rows, highlight_code=code)
        st.caption("1Y is absolute; 3Y/5Y are CAGR. Click a column header "
                   "to sort. The selected scheme is highlighted.")
    if failed:
        st.warning(f"Could not load: {', '.join(str(c) for c in failed)}")

    section("Manage list")
    for c in codes:
        cc1, cc2 = st.columns([6, 1])
        cc1.write(f"{name_of(universe, c)}  ·  `{c}`")
        if cc2.button("Remove", key=f"rm_{c}"):
            store.remove(st.session_state.watchlists, active, c)
            st.rerun()
    st.caption("Watchlists are saved in this browser (localStorage), so "
               "they persist across sessions and cloud restarts."
               + ("" if store.BROWSER_ONLY else
                  f" A local backup is kept at {store.WATCHLIST_FILE}."))

# ---- Overview ---- #
with tabs[1]:
    left, right = st.columns([2, 1], gap="large")
    with left:
        section("NAV history")
        log_scale = st.toggle("Log scale", value=False)
        fig = go.Figure(go.Scatter(
            x=series.index, y=series.values, name="NAV",
            line=dict(color=ACCENT, width=1.5),
            fill="tozeroy", fillcolor=rgba(ACCENT, 0.07),
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
            fill="tozeroy", fillcolor=rgba(DOWN, 0.12),
            hovertemplate="%{y:.2f}%<extra></extra>"))
        tv(ddfig, 200)
        chart(ddfig)
    with right:
        section("Trailing returns")
        table(R.trailing_returns(series), hide_index=True)

        section("Risk")
        rc1, rc2 = st.columns(2)
        rf = rc1.number_input("Risk-free %", value=6.5, step=0.25,
                              help="Annual risk-free rate for Sharpe/Sortino "
                                   "and alpha (e.g. 91-day T-bill yield).") / 100
        win_lbl = rc2.selectbox("Window", ["3Y", "1Y", "5Y", "All"],
                                help="Trailing window the ratios are "
                                     "computed over.")
        win_yrs = {"1Y": 1, "3Y": 3, "5Y": 5, "All": None}[win_lbl]
        rr = R.risk_ratios(series, rf, win_yrs)
        if rr:
            k1, k2 = st.columns(2)
            k1.metric("Sharpe", "—" if np.isnan(rr["sharpe"])
                      else f"{rr['sharpe']:.2f}")
            k2.metric("Sortino", "—" if np.isnan(rr["sortino"])
                      else f"{rr['sortino']:.2f}")
            k3, k4 = st.columns(2)
            k3.metric("Std dev (ann.)", pct(rr["vol"] * 100),
                      help="Annualised standard deviation of daily returns "
                           "over the window.")
            k4.metric("CAGR", pct(rr["cagr"] * 100))
            st.caption(f"Computed over {rr['years']:.1f}y of daily NAVs "
                       f"vs a {rf*100:.2f}% risk-free rate.")
        else:
            st.caption("Not enough history for risk ratios.")

        # benchmark-relative: beta / alpha vs an index fund's NAV series
        idx_univ = universe[universe["category"]
                            .str.contains("index", case=False, na=False)]
        if idx_univ.empty:
            idx_univ = universe
        bench_opts = {f"{r['name']}  ·  [{r['code']}]": r["code"]
                      for _, r in idx_univ.iterrows()}
        bench_keys = list(bench_opts.keys())
        default_ix = next((i for i, k in enumerate(bench_keys)
                           if "nifty 50" in k.lower()
                           and "direct" in k.lower()), 0)
        bench_lbl = st.selectbox("Benchmark (index fund)", bench_keys,
                                 index=default_ix,
                                 help="Beta/alpha use this fund's NAV as the "
                                      "market proxy — pick the index fund "
                                      "that matches the scheme's mandate.")
        try:
            bench_series, _ = get_history(bench_opts[bench_lbl])
            bs = R.benchmark_stats(series, bench_series, rf, win_yrs)
        except Exception:  # noqa: BLE001
            bs = {}
        if bs:
            b1, b2, b3 = st.columns(3)
            b1.metric("Beta", f"{bs['beta']:.2f}")
            b2.metric("Alpha (ann.)", pct(bs["alpha"] * 100))
            b3.metric("R²", f"{bs['r2']:.2f}")
            st.caption(f"vs {bench_lbl.split('  ·')[0]} · "
                       f"{bs['observations']:,} common days · benchmark CAGR "
                       f"{bs['bench_cagr']*100:.2f}%")
        else:
            st.caption("Beta/alpha need ≥60 overlapping NAV days with the "
                       "benchmark.")

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

    section("Fund facts & links")
    sch_name = name_of(universe, code)
    facts, facts_src = {}, None
    try:
        port = get_portfolio(isin_of(universe, code), sch_name, code)
        facts, facts_src = port.get("facts", {}), port.get("source")
    except Exception:  # noqa: BLE001
        pass
    if facts and (facts.get("aum") or facts.get("managers")):
        f1, f2, f3 = st.columns([1, 1, 2])
        aum = facts.get("aum")
        if aum and aum > 1e6:          # absolute rupees -> crores
            aum = aum / 1e7
        f1.metric("Fund size (AUM)", f"₹{aum:,.0f} Cr" if aum else "—")
        f2.metric("Expense ratio",
                  f"{facts['expense']:.2f}%" if facts.get("expense") else "—")
        with f3:
            st.markdown(f"**Fund manager(s)**<br>"
                        f"{facts.get('managers') or '—'}",
                        unsafe_allow_html=True)
        st.caption(f"Source: {facts_src} · "
                   + links_md(sch_name, port.get("rv_schemecode")
                              if facts_src == "Rupeevest" else None))
    else:
        st.caption("Live fund facts (AUM, manager) are unavailable right "
                   f"now — use the links: {links_md(sch_name)}")

# ---- Returns (point-to-point) ---- #
with tabs[2]:
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
with tabs[3]:
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

    # rolling beta/alpha vs the benchmark chosen on the Overview tab
    try:
        bench_roll, _ = get_history(bench_opts[bench_lbl])
        rba = R.rolling_beta_alpha(series, bench_roll, rf, win)
    except Exception:  # noqa: BLE001
        rba = pd.DataFrame()
    if not rba.empty:
        section(f"{win}Y rolling alpha (%) vs "
                f"{bench_lbl.split('  ·')[0]}")
        afig = go.Figure(go.Scatter(
            x=rba.index, y=rba["alpha"] * 100, name="Alpha",
            line=dict(color=UP, width=1.2),
            hovertemplate="%{y:.2f}%<extra></extra>"))
        afig.add_hline(y=0, line_dash="dash", line_color=MUTED, line_width=1)
        tv(afig, 260)
        chart(afig)

        section(f"{win}Y rolling beta")
        bfig = go.Figure(go.Scatter(
            x=rba.index, y=rba["beta"], name="Beta",
            line=dict(color="#FF9800", width=1.2),
            hovertemplate="%{y:.2f}<extra></extra>"))
        bfig.add_hline(y=1, line_dash="dash", line_color=MUTED, line_width=1)
        tv(bfig, 260)
        chart(bfig)
        st.caption("Alpha above 0 = beating the benchmark after adjusting "
                   "for beta and the risk-free rate; beta above 1 = "
                   "amplifying benchmark moves. Benchmark and risk-free "
                   "rate are set on the Overview tab.")
    else:
        st.caption(f"Rolling beta/alpha needs more than {win}Y of history "
                   "overlapping the benchmark (set on the Overview tab).")

# ---- SIP ---- #
with tabs[4]:
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
with tabs[5]:
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

# ---- Holdings (portfolio overlap) ---- #
with tabs[6]:
    section("Portfolio holdings & overlap")
    st.caption("Holdings and fund facts come from Rupeevest's own API "
               "(matched from AMFI names to its scheme codes), with "
               "Groww/Kuvera as fallbacks. Portfolios update monthly with "
               "AMC disclosures. If a fetch fails you can upload a holdings "
               "CSV below.")
    hl_labels = st.multiselect("Schemes (from this watchlist)",
                               list(scheme_labels.keys()),
                               default=[chosen_label], key="hold_pick")
    if "holdings_data" not in st.session_state:
        st.session_state.holdings_data = {}

    if st.button("Fetch holdings", type="primary") and hl_labels:
        errs = []
        prog = st.progress(0.0, text="Fetching portfolios…")
        for i, lbl in enumerate(hl_labels):
            c = scheme_labels[lbl]
            try:
                st.session_state.holdings_data[lbl] = get_portfolio(
                    isin_of(universe, c), name_of(universe, c), c)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{lbl.split('  ·')[0]}: {e}")
            prog.progress((i + 1) / len(hl_labels))
        prog.empty()
        for msg in errs:
            st.warning(msg)

    with st.expander("Manual upload (CSV with security + weight % columns)"):
        up_lbl = st.selectbox("Scheme the file belongs to", hl_labels or
                              list(scheme_labels.keys()), key="hold_up_lbl")
        up = st.file_uploader("Holdings CSV", type="csv", key="hold_up_file")
        if up is not None and up_lbl:
            try:
                hdf = H.parse_uploaded(pd.read_csv(up))
                st.session_state.holdings_data[up_lbl] = {
                    "holdings": hdf, "facts": {}, "source": "Manual upload"}
                st.success(f"Loaded {len(hdf)} holdings for "
                           f"{up_lbl.split('  ·')[0]}.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Couldn't parse the file: {e}")

    have = {lbl.split("  ·")[0]: st.session_state.holdings_data[lbl]
            for lbl in hl_labels if lbl in st.session_state.holdings_data}
    hmap = {n: d["holdings"] for n, d in have.items()
            if d.get("holdings") is not None and not d["holdings"].empty}

    if have:
        for n, d in have.items():
            f = d.get("facts") or {}
            aum = f.get("aum")
            if aum and aum > 1e6:
                aum = aum / 1e7
            bits = [f"**{n}**",
                    f"{len(d['holdings'])} holdings"
                    if d.get("holdings") is not None and
                    not d["holdings"].empty else "no holdings",
                    f"AUM ₹{aum:,.0f} Cr" if aum else None,
                    f"manager: {f['managers']}" if f.get("managers") else None,
                    f"({d.get('source', '?')})"]
            st.markdown(" · ".join(b for b in bits if b) + "  \n"
                        + links_md(n, d.get("rv_schemecode")),
                        unsafe_allow_html=True)

    if len(hmap) >= 2:
        section("Overlap % (sum of overlapping weights)")
        mat = H.overlap_matrix(hmap)
        short = [n[:30] + "…" if len(n) > 30 else n for n in mat.index]
        ofig = go.Figure(go.Heatmap(
            z=mat.values, x=short, y=short,
            colorscale=[[0, PANEL], [1, ACCENT]], zmin=0, zmax=100,
            texttemplate="%{z:.1f}%", textfont=dict(size=12),
            showscale=False, xgap=2, ygap=2,
            hovertemplate="%{y} ∩ %{x}: %{z:.2f}%<extra></extra>"))
        tv(ofig, max(220, 60 + 48 * len(mat)), unified=False, spikes=False)
        ofig.update_yaxes(side="left", showgrid=False)
        ofig.update_xaxes(showgrid=False)
        chart(ofig)

    if hmap:
        section("Combined holdings — common rows highlighted")
        common_only = st.checkbox("Show only common holdings", value=False)
        comb = H.combined_table(hmap)
        if common_only:
            comb = comb[comb["Held by"] >= 2]
        wcols = [c for c in comb.columns if c not in ("Security", "Held by")]
        disp = comb.copy()
        for c in wcols:
            disp[c] = comb[c].map(
                lambda v: "—" if pd.isna(v) else f"{v:.2f}")
        styler = disp.style.apply(
            lambda r: [f"background-color: {rgba(UP, 0.12)}"] * len(r)
            if comb.loc[r.name, "Held by"] >= 2 else [""] * len(r), axis=1)
        st.dataframe(styler, hide_index=True, width="stretch",
                     height=min(40 + 35 * len(disp), 640))
        uniq = {n: int((comb[wcols].notna().sum(axis=1) == 1)
                       [comb[n].notna()].sum()) for n in wcols}
        if len(wcols) >= 2:
            st.caption("Unique holdings — "
                       + " · ".join(f"{n}: {u}" for n, u in uniq.items())
                       + ". Weights are % of corpus; highlighted rows are "
                         "held by 2+ schemes.")

        sect_map = {n: d for n, d in hmap.items()
                    if "sector" in d.columns and d["sector"].notna().any()}
        if sect_map:
            section("Sector allocation (%)")
            srows = []
            for n, d in sect_map.items():
                g = d.dropna(subset=["sector"]).groupby("sector")["weight"] \
                     .sum()
                srows += [{"scheme": n, "sector": sec, "weight": w}
                          for sec, w in g.items()]
            sdf = pd.DataFrame(srows)
            order = (sdf.groupby("sector")["weight"].max()
                        .sort_values().index.tolist())
            sfig = go.Figure()
            for i, n in enumerate(sect_map):
                sub = (sdf[sdf["scheme"] == n].set_index("sector")["weight"]
                       .reindex(order))
                sfig.add_trace(go.Bar(
                    y=order, x=sub.values, name=n, orientation="h",
                    marker_color=SERIES[i % len(SERIES)],
                    marker_line_width=0,
                    hovertemplate="%{x:.2f}%<extra>" + n + "</extra>"))
            tv(sfig, max(280, 34 * len(order)), legend=True,
               unified=False, spikes=False)
            sfig.update_layout(barmode="group", bargap=0.25)
            sfig.update_yaxes(side="left")
            chart(sfig)
        elif hmap:
            st.caption("No sector data in the fetched holdings — sector "
                       "comparison appears when the provider supplies it.")
    elif have:
        st.info("No holdings data parsed yet — fetch again later or use the "
                "manual upload.")

# ---- Peers (category benchmarking) ---- #
with tabs[7]:
    row = universe[universe["code"] == code]
    peer_cat = row["category"].iloc[0] if not row.empty else None
    if not peer_cat:
        st.warning("This scheme isn't in the current AMFI universe, so its "
                   "category peers can't be determined.")
    else:
        section(f"Benchmark against the category — {peer_cat}")
        scheme_name_l = name_of(universe, code).lower()
        f1, f2 = st.columns(2)
        growth_only = f1.checkbox("Growth options only", value=True,
                                  help="Excludes IDCW/dividend options so "
                                       "each fund is counted once.")
        plan = f2.radio("Plan", ["Direct", "Regular", "All"], horizontal=True,
                        index=0 if "direct" in scheme_name_l else 1)

        peers = universe[universe["category"] == peer_cat]
        if growth_only:
            peers = peers[peers["name"].str.contains("growth", case=False,
                                                     na=False)]
        if plan == "Direct":
            peers = peers[peers["name"].str.contains("direct", case=False,
                                                     na=False)]
        elif plan == "Regular":
            peers = peers[~peers["name"].str.contains("direct", case=False,
                                                      na=False)]
        # the selected scheme always stays in the set
        if code not in peers["code"].values and not row.empty:
            peers = pd.concat([peers, row])
        st.caption(f"{len(peers):,} peer scheme(s) match. Histories are "
                   "fetched once and cached for 6 hours.")

        peer_key = (code, peer_cat, growth_only, plan)
        cached = st.session_state.get("peer_result")
        if st.button(f"Load returns for {len(peers):,} peers", type="primary"):
            prog = st.progress(0.0, text="Fetching peer NAV histories…")
            rows_p = []
            for i, (_, r) in enumerate(peers.iterrows()):
                try:
                    s_p, _ = get_history(r["code"])
                    snap = R.snapshot(s_p)
                    if snap:
                        rows_p.append({
                            "Scheme": r["name"], "Code": int(r["code"]),
                            "NAV": snap["nav"], "1Y trend": snap["spark"],
                            "1D %": snap["chg_1d"], "1Y %": snap["1Y"],
                            "3Y % pa": snap["3Y"], "5Y % pa": snap["5Y"],
                            "Max DD %": snap["mdd"]})
                except Exception:  # noqa: BLE001
                    pass
                prog.progress((i + 1) / len(peers))
            prog.empty()
            st.session_state.peer_result = (peer_key, rows_p)
            cached = st.session_state.peer_result

        if cached and cached[0] == peer_key and cached[1]:
            peer_rows = cached[1]
            pdf = pd.DataFrame(peer_rows)

            # rank of the selected scheme within the category, per horizon
            section("Category rank")
            rks = st.columns(3)
            pctiles = []
            for i, col in enumerate(["1Y %", "3Y % pa", "5Y % pa"]):
                vals = pdf[col].dropna()
                mine = pdf.loc[pdf["Code"] == code, col]
                if mine.empty or pd.isna(mine.iloc[0]) or vals.empty:
                    rks[i].metric(col, "—")
                    continue
                v = mine.iloc[0]
                rank = int((vals > v).sum()) + 1
                pctile = (vals < v).mean() * 100
                pctiles.append(f"{col}: beat {pctile:.0f}% of peers")
                rks[i].metric(col, f"#{rank} of {len(vals)}")
            st.caption("Rank 1 = best in category. "
                       + (" · ".join(pctiles) if pctiles else ""))

            section("Return distribution vs peers")
            dfig = go.Figure()
            for i, col in enumerate(["1Y %", "3Y % pa", "5Y % pa"]):
                vals = pdf[col].dropna()
                if vals.empty:
                    continue
                dfig.add_trace(go.Box(
                    y=vals, name=col, boxpoints=False, showlegend=False,
                    line=dict(color=MUTED, width=1),
                    fillcolor=rgba(MUTED, 0.15)))
                mine = pdf.loc[pdf["Code"] == code, col]
                if not mine.empty and pd.notna(mine.iloc[0]):
                    dfig.add_trace(go.Scatter(
                        x=[col], y=[mine.iloc[0]], mode="markers",
                        marker=dict(color=ACCENT, size=11,
                                    line=dict(color=TEXT, width=1)),
                        name="This scheme", showlegend=(i == 0),
                        hovertemplate="%{y:.2f}%<extra>This scheme</extra>"))
            tv(dfig, 360, legend=True, unified=False, spikes=False)
            chart(dfig)

            section("Peer table")
            snapshot_table(
                sorted(peer_rows,
                       key=lambda r: (r["3Y % pa"] is None
                                      or pd.isna(r["3Y % pa"]),
                                      -(r["3Y % pa"] or 0))),
                highlight_code=code)
        elif cached and cached[0] != peer_key:
            st.info("Filters changed — reload peer returns to refresh "
                    "the comparison.")

st.divider()
st.caption("Data: AMFI NAVAll.txt (universe + latest NAV) and api.mfapi.in "
           "(history, fetched on-demand). mfapi.in is an unofficial community "
           "API. Returns are NAV-based and exclude exit loads and taxes. "
           "For information only — not investment advice.")
