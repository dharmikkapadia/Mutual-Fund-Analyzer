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
import random
import re
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_js_eval import streamlit_js_eval

import cloud_sync
import holdings as H
import nav_data as D
import pf_review as P
import returns as R
import store
import vr_data as V
import vr_public as VP

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
# The NAV chart keeps the box-select tool so users can re-arm drag-to-measure
# after switching to pan (see the Overview tab).
NAV_CONFIG = {**PLOTLY_CONFIG,
              "modeBarButtonsToRemove":
                  [b for b in PLOTLY_CONFIG["modeBarButtonsToRemove"]
                   if b != "select2d"]}


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


def mix(c1: str, c2: str, t: float) -> str:
    """Linear blend of two hex colours; t=0 -> c1, t=1 -> c2."""
    a, b = c1.lstrip("#"), c2.lstrip("#")
    ch = [round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t)
          for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(ch)


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
    # Native widgets and st.dataframe read Streamlit's server-side theme
    # config, which only repaints on a fresh page load — so a theme change
    # persists the choice and reloads (handled near the top of the script).
    st.session_state.theme = st.session_state.theme_pick
    st.session_state._theme_changed = True


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
LS_PF = "afp_pf_review_v1"
USE_BROWSER_STORE = os.getenv("AFP_NO_BROWSER_STORE", "") != "1"

if "watchlists" not in st.session_state:
    boot, boot_theme, boot_pf = None, None, None
    if USE_BROWSER_STORE:
        raw = streamlit_js_eval(
            js_expressions=("JSON.stringify({"
                            f"wl: localStorage.getItem('{LS_KEY}'), "
                            f"th: localStorage.getItem('{LS_THEME}'), "
                            f"pf: localStorage.getItem('{LS_PF}')}})"),
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
            if obj.get("pf"):
                pf = json.loads(obj["pf"])
                if isinstance(pf, dict):
                    boot_pf = {k: dict(pf.get(k) or {})
                               for k in store.PF_DEFAULT}
        except (ValueError, TypeError, AttributeError):
            boot = None
    st.session_state.watchlists = boot or store.load()
    st.session_state.theme = (boot_theme if boot_theme in THEMES
                              else DEFAULT_THEME)
    st.session_state.pf_data = boot_pf or store.load_pf()
if "pf_data" not in st.session_state:
    st.session_state.pf_data = store.load_pf()
if "active_list" not in st.session_state:
    st.session_state.active_list = next(iter(st.session_state.watchlists))
if "theme" not in st.session_state:
    st.session_state.theme = DEFAULT_THEME

T = THEMES[st.session_state.theme]
BG, PANEL, GRID = T["bg"], T["panel"], T["grid"]
TEXT, MUTED = T["text"], T["muted"]
ACCENT, UP, DOWN = T["accent"], T["up"], T["down"]
ON_ACCENT = on_color(ACCENT)
IS_DARK = T["base"] == "dark"
SERIES = [ACCENT, "#FF9800", UP, "#E040FB", "#26C6DA", DOWN]
SHADOW = ("0 6px 18px rgba(0,0,0,.35)" if IS_DARK
          else "0 6px 18px rgba(0,0,0,.12)")
# active range-selector button: solid accent reads with light text on dark
# themes; on light themes use a pale tint so the dark body text stays legible
RANGE_ACTIVE = ACCENT if IS_DARK else mix(ACCENT, BG, 0.72)
# heatmaps don't follow the native theme, so colour their cell text explicitly;
# the overlap scale tops out at a mid tint on light themes so TEXT stays read.
OV_SCALE = ([[0, PANEL], [1, ACCENT]] if IS_DARK
            else [[0, BG], [1, mix(ACCENT, BG, 0.5)]])
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
    if st.session_state.pop("_theme_changed", False):
        # persist the new theme then hard-reload so the native widget theme
        # (and st.dataframe) repaint cleanly from the bootstrap
        streamlit_js_eval(
            js_expressions=(f"localStorage.setItem('{LS_THEME}', "
                            f"{json.dumps(st.session_state.theme)}); "
                            f"parent.window.location.reload();"),
            key="theme_reload")
        st.stop()
    streamlit_js_eval(
        js_expressions=f"localStorage.setItem('{LS_THEME}', "
                       f"{json.dumps(st.session_state.theme)})",
        key="ls_write_theme")
    _pf_payload = json.dumps(st.session_state.pf_data, separators=(",", ":"))
    streamlit_js_eval(
        js_expressions=f"localStorage.setItem('{LS_PF}', "
                       f"{json.dumps(_pf_payload)})",
        key="ls_write_pf")

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

  /* ---- surfaces: paint immediately so theme switches never flash a
     stale background before the native-theme reload settles ---- */
  .stApp, [data-testid="stAppViewContainer"],
  [data-testid="stMain"] {{background-color: {BG};}}
  [data-testid="stSidebar"] {{background-color: {PANEL};}}

  /* ---- inputs: consistent panel fill + readable text ---- */
  [data-baseweb="select"] > div, [data-baseweb="input"],
  [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
  [data-testid="stDateInput"] input, textarea {{
    background-color: {PANEL} !important; color: {TEXT} !important;
    border-color: {GRID} !important;}}
  [data-baseweb="select"] svg {{fill: {MUTED} !important;}}

  /* ---- dropdown menus: panel surface, accent highlight (same contrast
     rule as the chips) ---- */
  [data-baseweb="popover"] ul, [data-baseweb="menu"] ul,
  [data-baseweb="popover"] [role="listbox"] {{
    background-color: {PANEL} !important; border: 1px solid {GRID};}}
  [role="option"] {{color: {TEXT} !important;}}
  [role="option"]:hover, [role="option"][aria-selected="true"] {{
    background-color: {ACCENT} !important; color: {ON_ACCENT} !important;}}

  /* ---- buttons: primary on accent (high contrast), secondary on panel ---- */
  [data-testid="stBaseButton-primary"],
  [data-testid="stBaseButton-primary"] * {{
    background-color: {ACCENT} !important; color: {ON_ACCENT} !important;
    border-color: {ACCENT} !important;}}
  [data-testid="stBaseButton-secondary"] {{
    background-color: {PANEL} !important; color: {TEXT} !important;
    border-color: {GRID} !important;}}

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
        x=0, y=1.12, bgcolor=PANEL, activecolor=RANGE_ACTIVE,
        bordercolor=GRID, borderwidth=1, font=dict(color=TEXT, size=11)))
    return fig


def chart(fig: go.Figure) -> None:
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)


# --------------------------------------------------------------------------- #
# Drag-to-select a return window (Google-Finance style) on a NAV chart
# --------------------------------------------------------------------------- #
def selected_span(series: pd.Series, sel) -> tuple | None:
    """Two real NAV dates bounding a Plotly box selection, or None.

    `sel` is what ``st.plotly_chart(..., on_select="rerun", key=…)`` stores in
    session_state; ``selection.box[0].x`` holds the dragged [start, end] range.
    The bounds are clamped to the loaded history and snapped to actual NAV
    dates, so the endpoint markers land exactly on the line."""
    try:
        xs = sel["selection"]["box"][0]["x"]
        a, b = pd.Timestamp(xs[0]), pd.Timestamp(xs[1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    lo, hi = (a, b) if a <= b else (b, a)
    lo = lo.tz_localize(None) if lo.tzinfo else lo
    hi = hi.tz_localize(None) if hi.tzinfo else hi
    lo, hi = max(lo, series.index.min()), min(hi, series.index.max())
    if lo >= hi:
        return None
    seg = series.loc[lo:hi]
    return (seg.index[0], seg.index[-1]) if len(seg) >= 2 else None


def mark_span(fig: go.Figure, series: pd.Series, span: tuple) -> None:
    """Shade the selected period, drop endpoint dots on the line and badge the
    absolute return — the visual half of the drag-to-select feature."""
    d0, d1 = span
    n0, n1 = float(series.loc[d0]), float(series.loc[d1])
    col = UP if n1 >= n0 else DOWN
    fig.add_vrect(x0=d0, x1=d1, line_width=0, layer="below",
                  fillcolor=rgba(col, 0.10))
    fig.add_trace(go.Scatter(
        x=[d0, d1], y=[n0, n1], mode="markers", showlegend=False,
        marker=dict(color=col, size=9, line=dict(color=BG, width=1.5)),
        hoverinfo="skip"))
    fig.add_annotation(
        x=d1, y=1, xref="x", yref="paper", xanchor="right", yanchor="top",
        text=f"<b>{(n1 / n0 - 1.0) * 100:+.2f}%</b>", showarrow=False,
        font=dict(color=on_color(col), size=12), bgcolor=col, borderpad=4)


def span_readout(series: pd.Series, span: tuple) -> None:
    """Metric strip describing the drag-selected window (period, NAV move,
    absolute return and — for spans over a year — annualised CAGR)."""
    d0, d1 = span
    p = R.point_to_point(series, d0, d1)
    days = (d1 - d0).days
    c1, c2, c3 = st.columns(3)
    c1.metric("Selected period", f"{days:,} days",
              help=f"{d0.date()} → {d1.date()}")
    c2.metric("NAV", f"₹{p['nav2']:,.2f}", f"{p['abs'] * 100:+.2f}%",
              help=f"₹{p['nav1']:,.2f} on {d0.date()} → "
                   f"₹{p['nav2']:,.2f} on {d1.date()}")
    cagr = p["cagr"]
    c3.metric("Annualised (CAGR)",
              pct(cagr * 100) if cagr is not None else "—",
              help="Compound annual growth — shown only for spans over 1 year.")


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


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def peer_rolling_band(codes: tuple, win: int) -> pd.DataFrame:
    """median / p25 / p75 of the `win`-year rolling annualised return across
    a set of peer schemes, aligned on a common daily index. Cached on the
    sorted code tuple so it only recomputes when the peer set changes."""
    cols = {}
    for c in codes:
        try:
            s, _ = get_history(c)
            r = R.rolling_returns(s, win)
            if not r.empty:
                cols[c] = r
        except Exception:  # noqa: BLE001
            pass
    if len(cols) < 3:
        return pd.DataFrame()
    df = pd.DataFrame(cols).dropna(how="all")
    if df.empty:
        return pd.DataFrame()
    return pd.DataFrame({"median": df.median(axis=1),
                         "p25": df.quantile(0.25, axis=1),
                         "p75": df.quantile(0.75, axis=1)}).dropna()


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def category_index(codes: tuple) -> pd.Series:
    """Equal-weight growth-of-100 index across a peer set, cached on the
    sorted code tuple."""
    named = {}
    for c in codes:
        try:
            s, _ = get_history(c)
            if not s.empty:
                named[c] = s
        except Exception:  # noqa: BLE001
            pass
    return R.equal_weight_index(named)


def isin_of(univ: pd.DataFrame, code: int) -> str:
    row = univ[univ["code"] == code]
    isin = row["isin"].iloc[0] if not row.empty else ""
    return isin if isinstance(isin, str) and len(isin) >= 8 else ""


_PLAN_WORDS = {"direct", "regular", "growth", "idcw", "dividend", "payout",
               "reinvestment", "plan", "option", "reg", "dir", "bonus"}


def _core_words(name: str) -> frozenset:
    toks = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).split()
    return frozenset(t for t in toks if t not in _PLAN_WORDS)


def find_sibling_plan(univ: pd.DataFrame, code: int):
    """The same scheme's opposite plan (Direct↔Regular) from the universe,
    matched on the plan-stripped name within the same category. None if
    no confident single match."""
    row = univ[univ["code"] == code]
    if row.empty:
        return None
    name = str(row["name"].iloc[0])
    core, cat = _core_words(name), row["category"].iloc[0]
    is_direct = "direct" in name.lower()
    pool = univ[(univ["category"] == cat) & (univ["code"] != code)]
    if "growth" in name.lower():
        pool = pool[pool["name"].str.contains("growth", case=False, na=False)]
    best = None
    for _, r in pool.iterrows():
        nm = str(r["name"])
        if ("direct" in nm.lower()) == is_direct:        # need the other plan
            continue
        if _core_words(nm) == core:
            if best is not None:
                return None                               # ambiguous
            best = r
    return best


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

    if cloud_sync.is_configured():
        with st.expander("☁️ Cloud sync (access anywhere)"):
            st.caption(
                "Encrypt your watchlists **and PF-Review data** (monthly "
                "snapshots, invested values, VR fund codes) with a "
                "passphrase and sync them to a private store, so you can "
                "load them on any device. The passphrase never leaves this "
                "app and is **not recoverable** — if you lose it, the data "
                "is gone."
            )
            cu = st.text_input("Username", key="sync_user",
                               placeholder="e.g. ravi.k")
            cp = st.text_input("Passphrase", key="sync_pass", type="password",
                               placeholder="a long, memorable phrase")
            sc1, sc2 = st.columns(2)
            if sc1.button("Load", width="stretch", key="sync_load"):
                if not cu or not cp:
                    st.warning("Enter a username and passphrase.")
                else:
                    try:
                        res = cloud_sync.pull(cu, cp)
                        if res is None:
                            st.info("No saved record found for that "
                                    "username yet — use Save to create one.")
                        else:
                            wl, pf_synced = res
                            st.session_state.watchlists = wl
                            st.session_state.active_list = next(iter(wl))
                            store.save(wl)
                            if pf_synced is not None:
                                st.session_state.pf_data = {
                                    k: dict(pf_synced.get(k) or {})
                                    for k in store.PF_DEFAULT}
                                store.save_pf(st.session_state.pf_data)
                            st.success(
                                "Watchlists loaded."
                                if pf_synced is None else
                                "Watchlists + PF Review data loaded.")
                            st.rerun()
                    except cloud_sync.InvalidToken:
                        st.error("Wrong passphrase for that username.")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Could not load: {e}")
            if sc2.button("Save", width="stretch", key="sync_save",
                          type="primary"):
                if not cu or not cp:
                    st.warning("Enter a username and passphrase.")
                else:
                    try:
                        cloud_sync.push(cu, cp, st.session_state.watchlists,
                                        st.session_state.pf_data)
                        st.success("Watchlists + PF Review data saved "
                                   "to the cloud.")
                    except PermissionError as e:
                        st.error(str(e))
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Could not save: {e}")

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
                "Compare", "Holdings", "Peers", "Portfolio", "PF Review"])

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

        # Drag-selection is stored under the widget key; read it before drawing
        # so the highlight + return render on the same pass (on_select reruns).
        span = selected_span(series, st.session_state.get("nav_sel"))

        fig = go.Figure(go.Scatter(
            x=series.index, y=series.values, name="NAV",
            line=dict(color=ACCENT, width=1.5),
            fill="tozeroy", fillcolor=rgba(ACCENT, 0.07),
            hovertemplate="₹%{y:,.2f}<extra></extra>"))
        tv(fig, 380)
        range_buttons(fig)
        if log_scale:
            fig.update_yaxes(type="log")
        # Press-and-drag horizontally to sweep out a return window (like Google
        # Finance) rather than pan; the box is locked to the time axis. A dotted
        # guide follows the cursor while dragging; once released, Plotly's own
        # selection rectangle is hidden (activeselection transparent) so only
        # the shaded band, endpoint dots and % badge drawn by mark_span show.
        fig.update_layout(
            dragmode="select", selectdirection="h",
            newselection=dict(line=dict(color=ACCENT, width=1, dash="dot")),
            activeselection=dict(fillcolor="rgba(0,0,0,0)", opacity=0))
        if span:
            mark_span(fig, series, span)
        st.plotly_chart(fig, width="stretch", key="nav_sel",
                        on_select="rerun", selection_mode="box",
                        config=NAV_CONFIG)
        if span:
            span_readout(series, span)
            st.caption("Drag across the chart to measure the return over any "
                       "period. Single- or double-click to clear.")
        else:
            st.caption("Tip: drag across the chart to select a date range and "
                       "see its return — Google-Finance style.")

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

        # up/down capture vs the same benchmark
        try:
            cap = R.capture_ratios(series, bench_series, win_yrs)
        except Exception:  # noqa: BLE001
            cap = {}
        if cap.get("up_capture") is not None or cap.get("down_capture"):
            cc1, cc2 = st.columns(2)
            cc1.metric("Up capture",
                       f"{cap['up_capture']:.0f}%" if cap.get("up_capture")
                       is not None else "—",
                       help="Share of the benchmark's gains captured in "
                            "up months. Higher is better.")
            cc2.metric("Down capture",
                       f"{cap['down_capture']:.0f}%" if cap.get("down_capture")
                       is not None else "—",
                       help="Share of the benchmark's losses taken in down "
                            "months. Lower is better.",
                       delta_color="inverse")
            st.caption(f"Monthly capture over {cap.get('months', 0)} common "
                       f"months ({cap.get('up_months', 0)} up / "
                       f"{cap.get('down_months', 0)} down).")

    section("Worst drawdowns & recovery")
    ddt = R.drawdown_table(series, top=5)
    if ddt.empty:
        st.caption("No drawdown episodes found.")
    else:
        disp = ddt.copy()
        disp["Depth %"] = disp["Depth %"].map(lambda v: f"{v:.2f}")
        disp["Recovery"] = disp["Recovery"].map(
            lambda v: "ongoing" if pd.isna(v) else str(v))
        disp["Recovery days"] = disp["Recovery days"].map(
            lambda v: "—" if pd.isna(v) else f"{int(v):,}")
        st.dataframe(disp, hide_index=True, width="stretch")
        st.caption("Deepest peak-to-trough falls, with calendar days to the "
                   "trough and to full recovery. “ongoing” = not yet recovered.")

    # direct vs regular plan gap (purely NAV-based; both must be in the universe)
    sib = find_sibling_plan(universe, code)
    if sib is not None:
        try:
            sib_series, _ = get_history(int(sib["code"]))
            this_si = R.point_to_point(series, series.index.min(),
                                       series.index.max())
            both_start = max(series.index.min(), sib_series.index.min())
            a = R.point_to_point(series, both_start, series.index.max())
            b = R.point_to_point(sib_series, both_start, sib_series.index.max())
            ra = a["cagr"] if a["cagr"] is not None else a["abs"]
            rb = b["cagr"] if b["cagr"] is not None else b["abs"]
            if ra is not None and rb is not None:
                section(f"Plan comparison — this vs its "
                        f"{'Regular' if 'direct' in name_of(universe, code).lower() else 'Direct'} plan")
                g1, g2, g3 = st.columns(3)
                g1.metric("This plan CAGR", pct(ra * 100))
                g2.metric("Other plan CAGR", pct(rb * 100))
                g3.metric("Annual gap", pct((ra - rb) * 100),
                          help="Expense-ratio drag shows up as a persistent "
                               "CAGR gap between Direct and Regular plans of "
                               "the same scheme.")
                st.caption(f"Other plan: {sib['name']} · since common start "
                           f"{both_start.date()}.")
        except Exception:  # noqa: BLE001
            pass

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
            texttemplate="%{z:.1f}", textfont=dict(size=10, color=TEXT),
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

    # ---- goal planner ---- #
    section("Goal planner")
    st.caption("How much to invest monthly to reach a target — using this "
               "fund's own historical rolling-return distribution as the "
               "optimistic / median / pessimistic range.")
    g1, g2, g3 = st.columns(3)
    target = g1.number_input("Target corpus (₹)", value=10_000_000,
                             step=500_000, min_value=10_000)
    horizon = g2.number_input("Years to goal", value=10, min_value=1,
                              max_value=40)
    win_avail = max(1, min(5, int((series.index.max()
                                   - series.index.min()).days / 365.25)))
    roll_win = g3.selectbox("Return basis (rolling window)",
                            [w for w in (1, 3, 5) if w <= win_avail] or [1],
                            index=0,
                            help="Required SIP is computed from the spread of "
                                 "this fund's historical N-year rolling "
                                 "annualised returns.")
    roll = R.rolling_returns(series, roll_win)
    stt = R.rolling_stats(roll)
    if stt:
        scen = {"Pessimistic (25th pct)": stt["p25"],
                "Median": stt["median"],
                "Optimistic (75th pct)": stt["p75"]}
        sc = st.columns(3)
        for (lbl, rate), col in zip(scen.items(), sc):
            need = R.required_sip(target, horizon, rate)
            col.metric(lbl.split(" (")[0],
                       f"₹{need:,.0f}/mo" if need == need else "—",
                       f"{rate*100:.1f}% pa", delta_color="off")
        st.caption(f"Based on {stt['observations']:,} historical {roll_win}Y "
                   "rolling windows. Past returns don't guarantee future "
                   "results — treat these as a planning range, not a forecast.")

        section("Projected corpus")
        pfig = go.Figure()
        colors = {"Pessimistic (25th pct)": DOWN, "Median": ACCENT,
                  "Optimistic (75th pct)": UP}
        med_sip = R.required_sip(target, horizon, scen["Median"])
        for lbl, rate in scen.items():
            months, corpus, invested = R.sip_projection(med_sip, horizon, rate)
            pfig.add_trace(go.Scatter(
                x=months / 12, y=corpus, name=lbl,
                line=dict(color=colors[lbl],
                          width=1.6 if lbl == "Median" else 1.2,
                          dash="solid" if lbl == "Median" else "dot"),
                hovertemplate="yr %{x:.1f}: ₹%{y:,.0f}<extra>"
                              + lbl + "</extra>"))
        _, _, invested = R.sip_projection(med_sip, horizon, scen["Median"])
        pfig.add_trace(go.Scatter(
            x=months / 12, y=invested, name="Invested",
            line=dict(color=MUTED, width=1, dash="dash"),
            hovertemplate="yr %{x:.1f}: ₹%{y:,.0f}<extra>Invested</extra>"))
        pfig.add_hline(y=target, line_dash="dot", line_color=MUTED,
                       line_width=1)
        tv(pfig, 340, legend=True)
        chart(pfig)
        st.caption(f"Corpus path for a ₹{med_sip:,.0f}/mo SIP (the median-rate "
                   f"requirement) under each return scenario. Dotted line = "
                   f"₹{target:,.0f} target.")
    else:
        st.caption("Not enough history for a rolling-return based plan.")

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
                    customdata=np.array(
                        [f"{v:+.1f}" for v in reb[col].to_numpy() - 100.0]
                    ).reshape(-1, 1),
                    line=dict(color=SERIES[i % len(SERIES)], width=1.4),
                    hovertemplate="₹%{y:,.1f}  (%{customdata[0]}%)"
                                  "<extra>" + col + "</extra>"))
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
            colorscale=OV_SCALE, zmin=0, zmax=100,
            texttemplate="%{z:.1f}%", textfont=dict(size=12, color=TEXT),
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
                            "Max DD %": snap["mdd"], "Vol %": snap["vol"]})
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

            # growth-of-₹100 line chart: selected peers + the category
            # average, rebased to a common start (mirrors the Compare tab)
            section("Performance vs peers")
            peer_code_of = {f"{r['Scheme']}  ·  [{r['Code']}]": int(r["Code"])
                            for r in peer_rows}
            my_label = next((l for l, c in peer_code_of.items() if c == code),
                            None)
            top_peers = [f"{r['Scheme']}  ·  [{r['Code']}]" for r in sorted(
                peer_rows, key=lambda r: -(r["3Y % pa"]
                                           if r["3Y % pa"] is not None
                                           and not pd.isna(r["3Y % pa"])
                                           else -1e9))
                if int(r["Code"]) != code][:2]
            plot_labels = st.multiselect(
                "Schemes to plot (peers from this category)",
                list(peer_code_of.keys()),
                default=([my_label] if my_label else []) + top_peers,
                key="peer_plot")
            inc_avg = st.checkbox("Show category average", value=True,
                                  key="peer_plot_avg",
                                  help="Equal-weight average of every scheme "
                                       "in this category.")
            named_p = {}
            for lbl in plot_labels:
                try:
                    s_i, _ = get_history(peer_code_of[lbl])
                    nm = lbl.split("  ·")[0]
                    named_p[(nm + " ★") if peer_code_of[lbl] == code
                            else nm] = s_i
                except Exception:  # noqa: BLE001
                    st.warning(f"Skipped {lbl.split('  ·')[0]} (fetch failed).")
            avg_name = "▬ Category average"
            if inc_avg:
                cat_idx = category_index(
                    tuple(sorted(r["Code"] for r in peer_rows)))
                if not cat_idx.empty:
                    named_p[avg_name] = cat_idx
            if named_p:
                reb = R.compare_rebased(named_p)
                pfig = go.Figure()
                for i, col in enumerate(reb.columns):
                    is_avg = col == avg_name
                    pfig.add_trace(go.Scatter(
                        x=reb.index, y=reb[col], name=col,
                        customdata=np.array(
                            [f"{v:+.1f}" for v in reb[col].to_numpy() - 100.0]
                        ).reshape(-1, 1),
                        line=dict(color=MUTED if is_avg
                                  else SERIES[i % len(SERIES)],
                                  width=2.2 if is_avg else 1.4,
                                  dash="dash" if is_avg else "solid"),
                        hovertemplate="₹%{y:,.1f}  (%{customdata[0]}%)"
                                      "<extra>" + col + "</extra>"))
                tv(pfig, 420, legend=True)
                range_buttons(pfig)
                st.caption("Growth of ₹100 from a common start date. "
                           "★ = selected scheme · ▬ dashed = category average "
                           "(equal-weight). Hover for values; drag to pan, "
                           "scroll to zoom.")
                chart(pfig)

            # risk-vs-return scatter: every peer as a dot (x=vol, y=3Y CAGR)
            risk_pts = [r for r in peer_rows
                        if r.get("Vol %") is not None
                        and not pd.isna(r.get("Vol %"))
                        and r.get("3Y % pa") is not None
                        and not pd.isna(r.get("3Y % pa"))]
            if len(risk_pts) >= 3:
                section("Risk vs return (3Y)")
                xs = [r["Vol %"] for r in risk_pts]
                ys = [r["3Y % pa"] for r in risk_pts]
                names = [r["Scheme"] for r in risk_pts]
                is_me = [r["Code"] == code for r in risk_pts]
                scat = go.Figure()
                scat.add_trace(go.Scatter(
                    x=[v for v, m in zip(xs, is_me) if not m],
                    y=[v for v, m in zip(ys, is_me) if not m],
                    text=[n for n, m in zip(names, is_me) if not m],
                    mode="markers", name="Peers",
                    marker=dict(color=MUTED, size=8, opacity=0.55),
                    hovertemplate="%{text}<br>vol %{x:.1f}%· "
                                  "3Y %{y:.1f}%<extra></extra>"))
                # median cross-hairs
                scat.add_vline(x=float(np.median(xs)), line_dash="dot",
                               line_color=MUTED, line_width=1)
                scat.add_hline(y=float(np.median(ys)), line_dash="dot",
                               line_color=MUTED, line_width=1)
                if any(is_me):
                    scat.add_trace(go.Scatter(
                        x=[v for v, m in zip(xs, is_me) if m],
                        y=[v for v, m in zip(ys, is_me) if m],
                        text=[n for n, m in zip(names, is_me) if m],
                        mode="markers", name="This scheme",
                        marker=dict(color=ACCENT, size=14,
                                    line=dict(color=TEXT, width=1.5)),
                        hovertemplate="%{text}<br>vol %{x:.1f}%· "
                                      "3Y %{y:.1f}%<extra></extra>"))
                tv(scat, 380, legend=True, unified=False, spikes=False)
                scat.update_xaxes(title="Annualised volatility %")
                scat.update_yaxes(title="3Y CAGR %", side="left")
                chart(scat)
                st.caption("Up and to the left is better (more return per unit "
                           "of risk). Dotted lines mark the category median; "
                           "the top-left quadrant beats the median on both.")

            # rolling-return consistency vs the peer median
            rband = peer_rolling_band(
                tuple(sorted(r["Code"] for r in peer_rows)), 3)
            if rband is not None and not rband.empty:
                section("3Y rolling return — this scheme vs peer median")
                rfig = go.Figure()
                rfig.add_trace(go.Scatter(
                    x=rband.index, y=rband["p75"] * 100, name="Peer 75th pct",
                    line=dict(width=0), showlegend=False,
                    hoverinfo="skip"))
                rfig.add_trace(go.Scatter(
                    x=rband.index, y=rband["p25"] * 100, name="Peer 25–75%",
                    fill="tonexty", fillcolor=rgba(MUTED, 0.18),
                    line=dict(width=0),
                    hovertemplate="%{y:.1f}%<extra>Peer 25th pct</extra>"))
                rfig.add_trace(go.Scatter(
                    x=rband.index, y=rband["median"] * 100, name="Peer median",
                    line=dict(color=MUTED, width=1.2, dash="dash"),
                    hovertemplate="%{y:.1f}%<extra>Peer median</extra>"))
                try:
                    my_roll = R.rolling_returns(series, 3)
                    rfig.add_trace(go.Scatter(
                        x=my_roll.index, y=my_roll.values * 100,
                        name="This scheme",
                        line=dict(color=ACCENT, width=1.6),
                        hovertemplate="%{y:.1f}%<extra>This scheme</extra>"))
                except Exception:  # noqa: BLE001
                    pass
                tv(rfig, 360, legend=True)
                range_buttons(rfig)
                st.caption("How the fund's 3Y rolling return has tracked the "
                           "shaded peer 25–75% band over time — consistency, "
                           "not just a single end-point ranking.")

            section("Peer table")
            snapshot_table(
                sorted(peer_rows,
                       key=lambda r: (r["3Y % pa"] is None
                                      or pd.isna(r["3Y % pa"]),
                                      -(r["3Y % pa"] or 0))),
                highlight_code=code)

            # grow the watchlist with same-category peers (flexi with flexi,
            # small cap with small cap, …)
            section(f"Add these peers to “{active}”")
            addable = {f"{r['Scheme']}  ·  [{r['Code']}]": int(r["Code"])
                       for r in sorted(
                           peer_rows,
                           key=lambda r: -(r["3Y % pa"]
                                           if r["3Y % pa"] is not None
                                           and not pd.isna(r["3Y % pa"])
                                           else -1e9))
                       if int(r["Code"]) not in codes}
            if addable:
                to_add = st.multiselect(
                    f"Same-category schemes not yet in this watchlist "
                    f"({peer_cat})", list(addable.keys()), key="peer_add")
                if st.button("Add selected to watchlist", key="peer_add_btn",
                             type="primary") and to_add:
                    for lbl in to_add:
                        store.add(st.session_state.watchlists, active,
                                  addable[lbl])
                    st.toast(f"Added {len(to_add)} scheme(s) to “{active}”.")
                    st.rerun()
            else:
                st.caption("Every matching peer is already in this watchlist.")
        elif cached and cached[0] != peer_key:
            st.info("Filters changed — reload peer returns to refresh "
                    "the comparison.")

# ---- Portfolio (weighted watchlist blend) ---- #
with tabs[8]:
    section("Blended portfolio")
    st.caption("Treat the watchlist as one portfolio: set weights, then see "
               "the blended growth, risk metrics and how correlated the "
               "holdings are. Analysis runs over the schemes' common history.")
    if len(codes) < 2:
        st.info("Add at least two schemes to the watchlist to build a "
                "portfolio.")
    else:
        port_pick = st.multiselect(
            "Schemes in the portfolio", list(scheme_labels.keys()),
            default=list(scheme_labels.keys()), key="port_pick")
        weights, named_pf = {}, {}
        if port_pick:
            st.caption("Weights (need not sum to 100 — they're normalised).")
            wcols = st.columns(min(4, len(port_pick)))
            for i, lbl in enumerate(port_pick):
                nm = lbl.split("  ·")[0]
                weights[nm] = wcols[i % len(wcols)].number_input(
                    nm[:18], min_value=0.0, value=round(100 / len(port_pick), 1),
                    step=5.0, key=f"w_{scheme_labels[lbl]}")
                try:
                    named_pf[nm], _ = get_history(scheme_labels[lbl])
                except Exception:  # noqa: BLE001
                    st.warning(f"Skipped {nm} (fetch failed).")
        port = R.blend(named_pf, weights) if named_pf else pd.Series(dtype=float)
        if not port.empty:
            tot = sum(max(0.0, w) for w in weights.values()) or 1
            m1, m2, m3, m4 = st.columns(4)
            p_si = R.point_to_point(port, port.index.min(), port.index.max())
            m1.metric("Blended CAGR",
                      pct((p_si["cagr"] if p_si["cagr"] is not None
                           else p_si["abs"]) * 100))
            m2.metric("Volatility", pct(R.annualised_vol(port) * 100))
            m3.metric("Max drawdown", pct(R.max_drawdown(port) * 100))
            rr_p = R.risk_ratios(port, rf)
            m4.metric("Sharpe", f"{rr_p['sharpe']:.2f}"
                      if rr_p and not np.isnan(rr_p["sharpe"]) else "—")

            section("Portfolio growth (₹100)")
            gfig = go.Figure(go.Scatter(
                x=port.index, y=port.values, name="Portfolio",
                customdata=np.array(
                    [f"{v:+.1f}" for v in port.values - 100.0]).reshape(-1, 1),
                line=dict(color=ACCENT, width=1.6),
                fill="tozeroy", fillcolor=rgba(ACCENT, 0.07),
                hovertemplate="₹%{y:,.1f}  (%{customdata[0]}%)"
                              "<extra>Portfolio</extra>"))
            tv(gfig, 380)
            range_buttons(gfig)
            st.caption(f"Weighted blend over the common history of "
                       f"{len(named_pf)} schemes since {port.index.min().date()}.")
            chart(gfig)

            cm = R.correlation_matrix(named_pf)
            if not cm.empty:
                section("Correlation of weekly returns")
                short = [n[:24] + "…" if len(n) > 24 else n for n in cm.index]
                cfig = go.Figure(go.Heatmap(
                    z=cm.values, x=short, y=short, zmin=-1, zmax=1,
                    colorscale=[[0, DOWN], [0.5, PANEL], [1, UP]], zmid=0,
                    texttemplate="%{z:.2f}", textfont=dict(size=11, color=TEXT),
                    showscale=False, xgap=2, ygap=2,
                    hovertemplate="%{y} ↔ %{x}: %{z:.2f}<extra></extra>"))
                tv(cfig, max(240, 60 + 46 * len(cm)), unified=False,
                   spikes=False)
                cfig.update_yaxes(side="left", showgrid=False)
                cfig.update_xaxes(showgrid=False)
                chart(cfig)
                st.caption("Lower correlations (toward 0 or red) mean better "
                           "diversification — the funds move less in lockstep.")
        elif port_pick:
            st.info("Not enough overlapping history across the selected "
                    "schemes to blend.")

# ---- PF Review (monthly Value Research parameters, value-weighted) ---- #
def _pf_save() -> None:
    store.save_pf(st.session_state.pf_data)


def _pf_grid_df(pf_codes: list[int], values: dict, snaps: dict) -> pd.DataFrame:
    """Editor seed: fetched params, else the latest snapshot, else blanks."""
    latest = snaps.get(max(snaps)) if snaps else None
    by_code = ({str(r.get("code")): r for r in P.snapshot_rows(latest)}
               if latest else {})
    fetched = st.session_state.get("pf_fetched", {})
    recs = []
    for c in pf_codes:
        rec = {"Code": str(c), "Scheme Name": name_of(universe, c),
               "Value": float(values.get(str(c)) or 0.0)}
        src = fetched.get(str(c), {}).get("row") or {
            k: by_code.get(str(c), {}).get(k) for k in P.PARAM_COLS}
        for k in P.PARAM_COLS:
            v = src.get(k)
            rec[k] = np.nan if v is None else v
        recs.append(rec)
    return pd.DataFrame(recs).set_index("Code")


with tabs[9]:
    section("Monthly portfolio review")
    st.caption(
        "The watchlist as your real portfolio: enter each scheme's invested "
        "value, pull its month-end parameters (P/B, P/E, AUM, market-cap "
        "split, sectors) from Value Research's **public** data endpoints — "
        "no VR account or login involved — and read the **value-weighted** "
        "portfolio aggregates: the app version of the monthly MF Portfolio "
        "Review spreadsheet. Every cell stays editable, so the tab also "
        "works fully manually.")

    pf = st.session_state.pf_data
    vr_urls: dict = pf.setdefault("vr_urls", {})
    pf_values: dict = pf.setdefault("values", {})
    pf_snaps: dict = pf.setdefault("snapshots", {})

    # -- scheme rows: invested value lives in the grid; VR code per scheme -- #
    section("Fund pages on Value Research")
    st.caption("One VR fund page URL **or just the numeric fund code** per "
               "scheme (e.g. `16026` or `…/funds/16026/hdfc-flexi-cap-fund"
               "-direct-plan/`). Remembered across sessions. **Auto-find** "
               "fills the blanks via VR search.")
    url_changed = False
    _unonce = st.session_state.get("pf_url_nonce", 0)
    for c in codes:
        u = st.text_input(
            name_of(universe, c), key=f"pfu_{_unonce}_{c}",
            value=vr_urls.get(str(c), ""),
            placeholder="fund code (e.g. 16026) or "
                        "https://www.valueresearchonline.com/funds/…")
        if u.strip() != vr_urls.get(str(c), ""):
            vr_urls[str(c)] = u.strip()
            url_changed = True
    if url_changed:
        _pf_save()

    ac1, ac2 = st.columns([1, 1])
    if ac1.button("Auto-find VR pages",
                  help="Search VR for schemes whose URL/code is blank."):
        misses = []
        finder = V.VRSession()          # unauthenticated — search is public
        with st.spinner("Searching Value Research…"):
            for c in codes:
                if vr_urls.get(str(c)):
                    continue
                nm = name_of(universe, c)
                try:
                    cands = finder.search_funds(nm)
                except Exception as e:  # noqa: BLE001
                    misses.append(f"{nm}: search failed ({e})")
                    continue
                target = H._tokens(nm)
                best, score = None, 0.0
                for cn, cu in cands:
                    sc = H._name_score(target, cn)
                    if sc > score:
                        best, score = cu, sc
                if best and score >= 0.45:
                    vr_urls[str(c)] = best
                else:
                    misses.append(f"{nm}: no confident match "
                                  f"(best {score:.2f})")
        _pf_save()
        # fresh widget keys so the URL fields pick up the found values
        st.session_state.pf_url_nonce = _unonce + 1
        if misses:
            st.session_state.pf_msgs = [
                ("warning", "Not auto-matched — paste these URLs manually:"
                 "\n\n- " + "\n- ".join(misses))]
        st.rerun()

    if ac2.button("Fetch from Value Research", type="primary",
                  help="Pull P/B, P/E, AUM, cap split and sectors from VR's "
                       "public endpoints for every scheme that has a fund "
                       "code or page URL set."):
        fetched = st.session_state.setdefault("pf_fetched", {})
        errs, notes = [], []
        for c in codes:
            ref = vr_urls.get(str(c), "").strip()
            if ref and VP.fund_code(ref) is None:
                errs.append(f"{name_of(universe, c)}: no numeric fund code "
                            f"in '{ref}' — paste the VR fund page URL or "
                            "the code itself.")
        todo = [(c, VP.fund_code(vr_urls.get(str(c), ""))) for c in codes]
        todo = [(c, fid) for c, fid in todo if fid]
        vp_sess = VP.create_session()
        prog = st.progress(0.0, text="Fetching from Value Research…")
        dead_streak = 0
        for i, (c, fid) in enumerate(todo):
            nm = name_of(universe, c)
            prog.progress((i + 1) / max(1, len(todo)), text=f"VR: {nm}")
            try:
                prm = VP.fetch_fund(vp_sess, fid)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{nm}: {e}")
                # every endpoint failing for one fund means VR is refusing
                # this host — stop instead of hammering a blocked server
                dead_streak += 1
                if dead_streak >= 2:
                    errs.append(
                        "Stopped: VR is refusing every request from this "
                        "host (bot protection). Try again in a few "
                        "minutes; if it keeps failing on a cloud "
                        "deployment, run the fetch from a locally-run "
                        "app — manual entry, snapshots and the Excel "
                        "export all still work here.")
                    break
                continue
            dead_streak = 0
            fetched[str(c)] = {"row": P.params_to_row(prm),
                               "as_of": prm.get("as_of"),
                               "vr_name": prm.get("fund_name")}
            if prm.get("extra_sectors"):
                ex = ", ".join(f"{k} {v:.2f}%" for k, v
                               in prm["extra_sectors"].items())
                notes.append(f"{nm}: unmapped sector rows — {ex}")
            for w in prm.get("warnings", []):
                notes.append(f"{nm}: {w}")
            if i < len(todo) - 1:
                time.sleep(random.uniform(0.8, 1.6))   # be a polite guest
        prog.empty()
        msgs = []
        skipped = len(codes) - len(todo)
        if skipped:
            msgs.append(("info", f"{skipped} scheme(s) skipped — no VR "
                                 "fund code or page URL set."))
        if notes:
            msgs.append(("warning", "Fetched with caveats (edit the grid "
                                    "manually if needed):\n\n- "
                                    + "\n- ".join(notes)))
        if errs:
            msgs.append(("error", "Fetch failures:\n\n- "
                                  + "\n- ".join(errs)))
        st.session_state.pf_msgs = msgs
        if fetched:
            st.session_state.pf_nonce = st.session_state.get("pf_nonce",
                                                             0) + 1
        st.rerun()

    for _kind, _msg in st.session_state.pop("pf_msgs", []):
        getattr(st, _kind)(_msg)

    fetched_meta = st.session_state.get("pf_fetched", {})
    if fetched_meta:
        dates = {m.get("as_of") for m in fetched_meta.values()
                 if m.get("as_of")}
        st.caption(f"Fetched {len(fetched_meta)} scheme(s)"
                   + (f" · portfolio as on {', '.join(sorted(dates))}"
                      if dates else ""))
        named = [(name_of(universe, c),
                  fetched_meta[str(c)].get("vr_name") or "?")
                 for c in codes if str(c) in fetched_meta]
        if any(v != "?" for _, v in named):
            with st.expander("Cross-check: what VR calls each fetched code"):
                table(pd.DataFrame(named, columns=["Watchlist scheme",
                                                   "Fund name as per VR"]))

    # -- the review grid (every cell editable, like the spreadsheet) -- #
    section("Review grid")
    st.caption("₹ **Value** = your invested amount per scheme (drives the "
               "weights). Percentages are 0–100. Edit any cell — fetched "
               "numbers are just prefills.")
    seed = _pf_grid_df(codes, pf_values, pf_snaps)
    npct = st.column_config.NumberColumn(format="%.2f", min_value=0.0)
    edited = st.data_editor(
        seed, key=f"pf_editor_{st.session_state.get('pf_nonce', 0)}",
        hide_index=True, width="stretch", num_rows="fixed",
        column_config={
            "Scheme Name": st.column_config.TextColumn(disabled=True,
                                                       width="medium"),
            "Value": st.column_config.NumberColumn(format="localized",
                                                   min_value=0.0),
            "P/B": npct, "P/E": npct,
            "Aum in cr": st.column_config.NumberColumn(format="localized",
                                                       min_value=0.0),
            **{c: npct for c in [*P.CAP_COLS, "Debt & Cash",
                                 *P.SECTOR_COLS]},
        })

    val_changed = False
    for c_idx, r in edited.iterrows():
        v = 0.0 if pd.isna(r["Value"]) else float(r["Value"])
        if pf_values.get(str(c_idx)) != v:
            pf_values[str(c_idx)] = v
            val_changed = True
    if val_changed:
        _pf_save()

    rows = []
    for c_idx, r in edited.iterrows():
        rows.append({"code": str(c_idx), "name": r["Scheme Name"],
                     "value": r["Value"], "vr_url": vr_urls.get(str(c_idx)),
                     **{k: r[k] for k in P.PARAM_COLS}})
    review = P.review_frame(rows)
    summary = P.weighted_summary(review)
    have_values = summary["Value"] > 0

    if not have_values:
        st.info("Enter invested values in the grid to see the weighted "
                "portfolio aggregates.")
    else:
        section("Weighted portfolio aggregates")
        weights = review["Value"] / summary["Value"] * 100.0
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total value", f"₹{summary['Value']:,.0f}")
        m2.metric("Weighted P/E", "—" if pd.isna(summary["P/E"])
                  else f"{summary['P/E']:.2f}")
        m3.metric("Weighted P/B", "—" if pd.isna(summary["P/B"])
                  else f"{summary['P/B']:.2f}")
        m4.metric("Weighted AUM", "—" if pd.isna(summary["Aum in cr"])
                  else f"₹{summary['Aum in cr']:,.0f} cr")
        tot = summary[P.TOTAL_COL]
        m5.metric("Sectors + cash", "—" if pd.isna(tot) else f"{tot:.1f}%",
                  help="Debt & Cash + all sector weights — should land "
                       "near 100% if the data is complete.")

        cap_vals = [summary[c] for c in [*P.CAP_COLS, "Debt & Cash"]]
        if not all(pd.isna(v) for v in cap_vals):
            cfig = go.Figure()
            for lbl, v, col in zip(
                    ["Large", "Mid", "Small", "Debt & Cash"], cap_vals,
                    [ACCENT, SERIES[1], SERIES[3], MUTED]):
                cfig.add_trace(go.Bar(
                    x=[0.0 if pd.isna(v) else v], y=["Mix"], name=lbl,
                    orientation="h", marker_color=col,
                    hovertemplate=f"{lbl}: %{{x:.2f}}%<extra></extra>"))
            cfig.update_layout(barmode="stack", showlegend=True, height=110,
                               margin=dict(l=0, r=0, t=0, b=0))
            tv(cfig, 110, unified=False, spikes=False)
            cfig.update_xaxes(visible=False)
            cfig.update_yaxes(visible=False)
            chart(cfig)
            st.caption("Market-cap mix of the whole portfolio (weighted "
                       "Large / Mid / Small % of equity, plus Debt & Cash).")

        sect = summary[P.SECTOR_COLS].dropna()
        sect = sect[sect > 0].sort_values(ascending=True)
        if not sect.empty:
            sfig = go.Figure(go.Bar(
                x=sect.values, y=sect.index, orientation="h",
                marker_color=ACCENT,
                hovertemplate="%{y}: %{x:.2f}%<extra></extra>"))
            tv(sfig, max(220, 40 + 22 * len(sect)), unified=False,
               spikes=False)
            sfig.update_xaxes(ticksuffix="%")
            chart(sfig)
            st.caption("Weighted sector allocation (% of portfolio, "
                       "incl. the debt & cash drag).")

        full = review.copy()
        full.insert(2, "Weight %", weights.round(2))
        srow = {"Scheme Name": "◆ PORTFOLIO (weighted)", "Weight %": 100.0,
                **{k: summary[k] for k in [P.VALUE_COL, *P.PARAM_COLS,
                                           P.TOTAL_COL]}}
        full = pd.concat([full, pd.DataFrame([srow])], ignore_index=True)
        with st.expander("Full review table (as it will export)"):
            table(full)

    # -- monthly snapshots + Excel export -- #
    section("Monthly snapshots & export")
    sc1, sc2, sc3 = st.columns([1.2, 1, 1.8])
    as_on = sc1.date_input("As on", value=dt.date.today(),
                           key="pf_as_on", format="DD/MM/YYYY")
    mkey = as_on.strftime("%Y-%m")
    if sc2.button(f"Save snapshot · {mkey}", disabled=not have_values):
        pf_snaps[mkey] = P.snapshot_pack(rows, as_on.strftime("%d/%m/%Y"))
        _pf_save()
        st.success(f"Saved {mkey} ({len(rows)} schemes). Snapshots persist "
                   "in this browser"
                   + (" — use ☁️ Cloud sync (sidebar) to back them up "
                      "across devices." if cloud_sync.is_configured()
                      else "."))
    if have_values:
        sc3.download_button(
            "⬇ Download Excel (PF Review)",
            data=P.to_excel_bytes(review, as_on.strftime("%d/%m/%Y")),
            file_name=f"MF_Portfolio_Review_as_on_"
                      f"{as_on.strftime('%d%m%Y')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.sheet",
            help="Same layout and live formulas as the manual workbook.")

    with st.expander("Import a review workbook (.xlsx) as a snapshot"):
        st.caption(
            "Upload a manually maintained **MF Portfolio Review** workbook "
            "(one row per scheme: value, P/B, P/E, AUM, cap split, Debt & "
            "Cash, sector columns). It's stored as that month's snapshot, "
            "so months that predate the app join the comparison history.")
        up = st.file_uploader("Workbook", type=["xlsx"], key="pf_upload",
                              label_visibility="collapsed")
        if up is not None:
            imp = None
            try:
                imp = P.parse_workbook(up.getvalue())
            except Exception as e:  # noqa: BLE001
                st.error(f"Couldn't read that workbook: {e}")
            if imp and not imp["rows"]:
                st.warning("No scheme rows found under the header row.")
            elif imp:
                matched = 0
                cands = [(name_of(universe, c), str(c)) for c in codes]
                for row in imp["rows"]:
                    code_m, score = H._best_core_match(
                        H._core_tokens(row["name"]), cands)
                    if code_m is not None and score >= 0.5:
                        row["code"] = code_m
                        matched += 1
                tot_val = sum(r.get("value") or 0 for r in imp["rows"])
                st.caption(
                    f"{len(imp['rows'])} scheme(s) · total "
                    f"₹{tot_val:,.0f}"
                    + (f" · as on {imp['as_on']}" if imp["as_on"] else "")
                    + f" · {matched} matched to watchlist schemes"
                    + ("" if matched == len(imp["rows"]) else
                       " (unmatched rows still count in snapshot "
                       "comparisons, but won't load into the grid)"))
                key_guess = (P.month_key_from_as_on(imp["as_on"])
                             or dt.date.today().strftime("%Y-%m"))
                ic1, ic2 = st.columns([1, 1.4])
                imp_key = ic1.text_input("Save under month (YYYY-MM)",
                                         value=key_guess, key="pf_imp_key")
                if ic2.button("Save uploaded snapshot", type="primary",
                              key="pf_imp_save"):
                    if not re.fullmatch(r"\d{4}-\d{2}", imp_key.strip()):
                        st.error("Month must look like 2026-03.")
                    else:
                        k = imp_key.strip()
                        pf_snaps[k] = P.snapshot_pack(
                            imp["rows"], imp["as_on"] or k)
                        _pf_save()
                        st.session_state.pf_msgs = [(
                            "success",
                            f"Imported {up.name} as snapshot {k} "
                            f"({len(imp['rows'])} schemes).")]
                        st.rerun()

    if pf_snaps:
        months = sorted(pf_snaps, reverse=True)
        pick = st.multiselect(
            "Compare saved months (weighted aggregates)", months,
            default=months[:2], key="pf_cmp")
        if pick:
            comp = {}
            for mk in sorted(pick):
                srows = P.snapshot_rows(pf_snaps[mk])
                if srows:
                    comp[mk] = P.weighted_summary(P.review_frame(srows))
            if comp:
                cdf = pd.DataFrame(comp).T
                if len(comp) == 2:
                    a, b = cdf.index[0], cdf.index[1]
                    cdf.loc[f"Δ {b} vs {a}"] = cdf.loc[b] - cdf.loc[a]
                table(cdf.reset_index(names="Month"))
        hist = P.summary_history(pf_snaps)
        if len(hist) >= 2:
            section("Trends across months")
            hx = list(hist.index)

            # valuation & size — one measure per chart, never a dual axis
            t1, t2, t3 = st.columns(3)
            for col_st, title, series, fmt in (
                    (t1, "Weighted P/E", hist["P/E"], ".2f"),
                    (t2, "Weighted P/B", hist["P/B"], ".2f"),
                    (t3, "Total value (₹)", hist["Value"], ",.0f")):
                with col_st:
                    st.caption(title)
                    lfig = go.Figure(go.Scatter(
                        x=hx, y=series.values, mode="lines+markers",
                        line=dict(color=ACCENT, width=2),
                        marker=dict(size=8, color=ACCENT),
                        hovertemplate="%{x}: %{y:" + fmt + "}"
                                      "<extra></extra>"))
                    tv(lfig, 190, unified=False)
                    lfig.update_xaxes(type="category")
                    chart(lfig)

            # cap-mix drift — same colour-per-bucket as the mix bar above
            mfig = go.Figure()
            for lbl, coln, colr in (("Large", "Large Stocks", ACCENT),
                                    ("Mid", "Mid cap Stocks", SERIES[1]),
                                    ("Small", "Small cap Stocks",
                                     SERIES[3]),
                                    ("Debt & Cash", "Debt & Cash", MUTED)):
                mfig.add_trace(go.Bar(
                    x=hx, y=hist[coln].values, name=lbl, marker_color=colr,
                    hovertemplate=lbl + " · %{x}: %{y:.2f}%"
                                  "<extra></extra>"))
            mfig.update_layout(barmode="stack")
            tv(mfig, 280, legend=True, unified=False, spikes=False)
            mfig.update_xaxes(type="category")
            mfig.update_yaxes(ticksuffix="%")
            chart(mfig)
            st.caption("Market-cap mix over time — the style-drift view: "
                       "watch Small grow or Debt & Cash build up.")

            # sector rotation — magnitude job, single-hue heatmap
            sect_hist = hist[P.SECTOR_COLS].astype(float).fillna(0.0)
            keep = [c for c in P.SECTOR_COLS
                    if sect_hist[c].abs().max() > 0.005]
            keep.sort(key=lambda c: sect_hist[c].iloc[-1], reverse=True)
            if keep:
                hfig = go.Figure(go.Heatmap(
                    z=[sect_hist[c].values for c in reversed(keep)],
                    x=hx, y=list(reversed(keep)),
                    colorscale=OV_SCALE, showscale=False,
                    texttemplate="%{z:.1f}",
                    textfont=dict(size=11, color=TEXT), xgap=2, ygap=2,
                    hovertemplate="%{y} · %{x}: %{z:.2f}%<extra></extra>"))
                tv(hfig, max(240, 60 + 24 * len(keep)), unified=False,
                   spikes=False)
                hfig.update_xaxes(type="category", showgrid=False)
                hfig.update_yaxes(side="left", showgrid=False)
                chart(hfig)
                st.caption("Sector weights by month (% of portfolio, "
                           "sorted by the latest month) — darker = "
                           "heavier; read along a row to see rotation.")

                dc1, dc2 = st.columns([1.4, 2.6])
                base_pick = dc1.selectbox(
                    f"Sector change: {hx[-1]} vs", hx[:-1],
                    index=len(hx) - 2, key="pf_delta_base")
                delta = (sect_hist.iloc[-1]
                         - sect_hist.loc[base_pick])[keep].round(2)
                delta = delta[delta.abs() >= 0.01].sort_values()
                if delta.empty:
                    dc2.caption("No sector moved by 0.01% or more "
                                "between those months.")
                else:
                    dfig = go.Figure(go.Bar(
                        x=delta.values, y=delta.index, orientation="h",
                        marker_color=[UP if v > 0 else DOWN
                                      for v in delta.values],
                        hovertemplate="%{y}: %{x:+.2f} pp"
                                      "<extra></extra>"))
                    tv(dfig, max(200, 40 + 24 * len(delta)),
                       unified=False, spikes=False)
                    dfig.update_xaxes(ticksuffix=" pp", zeroline=True,
                                      zerolinecolor=GRID)
                    dfig.add_vline(x=0, line_color=GRID)
                    chart(dfig)
                    st.caption(f"Where the portfolio rotated between "
                               f"{base_pick} and {hx[-1]} — green gained "
                               "weight, red gave it up (percentage "
                               "points).")

        mc1, mc2 = st.columns([1.2, 1])
        load_pick = mc1.selectbox("Load a snapshot into the grid", months,
                                  key="pf_load_pick")
        if mc2.button("Load / Delete…", key="pf_load_menu",
                      help="Load fills the grid from the chosen month; "
                           "delete removes it permanently."):
            st.session_state.pf_manage = True
        if st.session_state.get("pf_manage"):
            b1, b2, b3 = st.columns(3)
            if b1.button(f"Load {load_pick}", key="pf_do_load"):
                by = {str(r.get("code")): r
                      for r in P.snapshot_rows(pf_snaps[load_pick])}
                st.session_state.pf_fetched = {
                    k: {"row": {c: v.get(c) for c in P.PARAM_COLS},
                        "as_of": pf_snaps[load_pick].get("as_on")}
                    for k, v in by.items()}
                for k, v in by.items():
                    if v.get("value") is not None:
                        pf_values[k] = v["value"]
                st.session_state.pf_nonce = st.session_state.get(
                    "pf_nonce", 0) + 1
                st.session_state.pf_manage = False
                _pf_save()
                st.rerun()
            if b2.button(f"Delete {load_pick}", key="pf_do_del"):
                pf_snaps.pop(load_pick, None)
                st.session_state.pf_manage = False
                _pf_save()
                st.rerun()
            if b3.button("Cancel", key="pf_cancel"):
                st.session_state.pf_manage = False
                st.rerun()

    st.caption("Fund parameters © Value Research — fetched with your own "
               "account, for your personal review. Numbers land in an "
               "editable grid, so the tab works without VR too (type the "
               "values, exactly like the spreadsheet).")

st.divider()
st.caption("Data: AMFI NAVAll.txt (universe + latest NAV) and api.mfapi.in "
           "(history, fetched on-demand). mfapi.in is an unofficial community "
           "API. Returns are NAV-based and exclude exit loads and taxes. "
           "For information only — not investment advice.")
