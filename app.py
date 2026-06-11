"""
app.py — AFP NAV Explorer (Streamlit).

Run via the customtkinter launcher, or directly:
    python -m streamlit run app.py
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

import nav_data as D
import returns as R
import store

# AFP brand palette
NAVY, ORANGE, BLUE, GREEN = "#141413", "#D97757", "#6A9BCC", "#4A7C59"
PALETTE = [ORANGE, BLUE, GREEN, "#E8702A", "#788C5D", NAVY]

st.set_page_config(page_title="AFP NAV Explorer", page_icon="📈",
                   layout="wide", initial_sidebar_state="expanded")

st.markdown(f"""
<style>
.block-container {{padding-top: 2rem;}}
h1, h2, h3 {{color: {NAVY};}}
[data-testid="stMetricValue"] {{color: {NAVY};}}
.afp-tag {{color:{ORANGE}; font-weight:600; letter-spacing:.05em;}}
</style>""", unsafe_allow_html=True)


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
    st.markdown(f"<span class='afp-tag'>ARTHASHASTRA FINSEC</span>",
                unsafe_allow_html=True)
    st.title("NAV Explorer")
    st.caption(f"{len(universe):,} live schemes · NAV as of "
               f"{universe['date'].max().date()}")

    st.subheader("Watchlists")
    lists = list(st.session_state.watchlists.keys())
    st.session_state.active_list = st.selectbox(
        "Active list", lists,
        index=lists.index(st.session_state.active_list)
        if st.session_state.active_list in lists else 0)

    c1, c2 = st.columns(2)
    with c1:
        new_name = st.text_input("New list", key="new_list", label_visibility="collapsed",
                                 placeholder="New list name")
        if st.button("Create", use_container_width=True) and new_name:
            store.create_list(st.session_state.watchlists, new_name)
            st.session_state.active_list = new_name
            st.rerun()
    with c2:
        if st.button("Delete list", use_container_width=True):
            store.delete_list(st.session_state.watchlists,
                              st.session_state.active_list)
            st.session_state.active_list = next(iter(st.session_state.watchlists))
            st.rerun()

    st.divider()
    st.subheader("Add schemes")
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
        if st.button("➕ Add to watchlist", type="primary",
                     use_container_width=True):
            for label in pick:
                store.add(st.session_state.watchlists,
                          st.session_state.active_list, opts[label])
            st.rerun()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
active = st.session_state.active_list
codes = st.session_state.watchlists.get(active, [])

st.title("📈 AFP NAV Explorer")
st.markdown(f"**Watchlist:** {active}  ·  {len(codes)} scheme(s)")

if not codes:
    st.info("Your watchlist is empty. Use the sidebar to search the AMFI "
            "universe and add schemes.")
    st.stop()

scheme_labels = {f"{name_of(universe, c)}  ·  [{c}]": c for c in codes}
chosen_label = st.selectbox("Select a scheme", list(scheme_labels.keys()))
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

# header metrics
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Latest NAV", f"₹{series.iloc[-1]:,.2f}",
          f"as of {series.index.max().date()}")
m2.metric("Inception", f"{series.index.min().date()}")
tr = R.trailing_returns(series).set_index("Period")
m3.metric("1Y", pct(tr.loc["1Y", "Absolute %"]) if "1Y" in tr.index else "—")
m4.metric("3Y CAGR", pct(tr.loc["3Y", "Annualised %"]) if "3Y" in tr.index else "—")
m5.metric("Max Drawdown", pct(R.max_drawdown(series) * 100))
if meta:
    st.caption(f"{meta.get('fund_house','')} · {meta.get('scheme_category','')}"
               f" · {meta.get('scheme_name','')}")

tabs = st.tabs(["Overview", "Point-to-Point", "Rolling Returns",
                "SIP / XIRR", "Compare", "Manage"])

# ---- Overview ---- #
with tabs[0]:
    left, right = st.columns([2, 1])
    with left:
        fig = go.Figure(go.Scatter(x=series.index, y=series.values,
                                   line=dict(color=ORANGE, width=1.6),
                                   name="NAV"))
        fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0),
                          title="NAV history")
        st.plotly_chart(fig, use_container_width=True)

        dd = R.drawdown(series)
        ddfig = go.Figure(go.Scatter(x=dd.index, y=dd.values * 100,
                                     fill="tozeroy",
                                     line=dict(color=BLUE, width=1)))
        ddfig.update_layout(height=220, margin=dict(l=0, r=0, t=30, b=0),
                            title="Drawdown (%)")
        st.plotly_chart(ddfig, use_container_width=True)
    with right:
        st.markdown("**Trailing returns**")
        st.dataframe(R.trailing_returns(series).round(2),
                     hide_index=True, use_container_width=True)
        st.metric("Annualised volatility",
                  pct(R.annualised_vol(series) * 100))

    st.markdown("**Calendar-year returns**")
    cal = R.calendar_year_returns(series)
    if not cal.empty:
        calfig = px.bar(cal, x="Year", y="Return %",
                        color="Return %", color_continuous_scale=["#C0392B", "#EEE", GREEN],
                        color_continuous_midpoint=0)
        calfig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                             coloraxis_showscale=False)
        st.plotly_chart(calfig, use_container_width=True)

    mat = R.monthly_matrix(series)
    if not mat.empty:
        st.markdown("**Month-on-month heatmap (%)**")
        hm = px.imshow(mat, text_auto=".1f", aspect="auto",
                       color_continuous_scale=["#C0392B", "#FFFFFF", GREEN],
                       color_continuous_midpoint=0)
        hm.update_layout(height=max(220, 26 * len(mat)),
                         margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(hm, use_container_width=True)

# ---- Point-to-Point ---- #
with tabs[1]:
    st.subheader("Custom date range")
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

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Financial-year returns**")
        st.dataframe(R.fy_returns(series).round(2), hide_index=True,
                     use_container_width=True)
    with c2:
        st.markdown("**Month-on-month returns**")
        st.dataframe(R.monthly_returns(series).round(2).iloc[::-1],
                     hide_index=True, use_container_width=True, height=380)

# ---- Rolling ---- #
with tabs[2]:
    win = st.radio("Rolling window", [1, 3, 5], horizontal=True,
                   format_func=lambda x: f"{x}Y")
    hurdle = st.number_input("Hurdle for ‘% above’ (annualised %)",
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

        rfig = go.Figure(go.Scatter(x=roll.index, y=roll.values * 100,
                                    line=dict(color=ORANGE, width=1)))
        rfig.add_hline(y=hurdle * 100, line_dash="dash", line_color=GREEN)
        rfig.update_layout(height=320, margin=dict(l=0, r=0, t=30, b=0),
                           title=f"{win}Y rolling annualised return (%)")
        st.plotly_chart(rfig, use_container_width=True)

        hist = px.histogram(roll * 100, nbins=50)
        hist.update_traces(marker_color=BLUE)
        hist.update_layout(height=260, showlegend=False,
                           margin=dict(l=0, r=0, t=30, b=0),
                           title="Distribution of rolling returns (%)")
        st.plotly_chart(hist, use_container_width=True)

# ---- SIP ---- #
with tabs[3]:
    st.subheader("SIP return (XIRR)")
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
                   "Point-to-Point tab.")

# ---- Compare ---- #
with tabs[4]:
    st.subheader("Compare schemes")
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
                cfig.add_trace(go.Scatter(x=reb.index, y=reb[col], name=col,
                               line=dict(color=PALETTE[i % len(PALETTE)],
                                         width=1.4)))
            cfig.update_layout(height=420, margin=dict(l=0, r=0, t=30, b=0),
                               title="Growth of ₹100 (common start, no scaling rules)",
                               legend=dict(orientation="h", y=-0.2))
            st.plotly_chart(cfig, use_container_width=True)
            st.markdown("**Trailing-return comparison** "
                        "(annualised where >1Y, else absolute)")
            st.dataframe(R.compare_trailing(named).round(2),
                         use_container_width=True)

# ---- Manage ---- #
with tabs[5]:
    st.subheader(f"Manage ‘{active}’")
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
