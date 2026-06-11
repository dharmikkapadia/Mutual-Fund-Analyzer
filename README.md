# AFP NAV Explorer

A mutual-fund NAV viewer and returns analyser for the full AMFI universe.
Streamlit core (the analytics + UI) with a customtkinter launcher so it opens
like a desktop app inside the AFP suite.

## UI
Minimal dark theme inspired by TradingView (slate background `#131722`,
high-contrast text, crosshair hover, right-side axes, 1M/6M/YTD/1Y/3Y/5Y/All
range buttons, red/green gain-loss semantics). The Streamlit theme lives in
`.streamlit/config.toml`; chart styling is centralised in the `tv()` helper
in `app.py` so every chart shares one template.

## Data sources
| Source | Role |
|---|---|
| AMFI `NAVAll.txt` | Full scheme universe (code, ISIN, name, category, fund house) + latest NAV. Powers search, the watchlist picker, and the daily NAV. Dead schemes (stale dates) are filtered out. |
| `api.mfapi.in/mf/{code}` | Full daily NAV history per scheme. Powers every returns calculation. Fetched **on-demand** and cached in-session (mfapi is an unofficial community API, so this keeps requests polite). |

The two join on AMFI's scheme code.

## Setup (Windows, Microsoft Store Python 3.14)
```
python -m pip install -r requirements.txt
```

## Run
Desktop launcher (recommended):
```
python launcher.py
```
Or run Streamlit directly:
```
python -m streamlit run app.py
```
The launcher starts Streamlit headless via `python -m streamlit` (the reliable
invocation when the Scripts dir isn't on PATH) and opens the browser.

## Features (v1)
- **Watchlist dashboard** — sortable grid of every scheme in the active list: 1Y NAV sparkline, latest NAV, 1D/1Y/3Y/5Y returns and max drawdown, red/green coded.
- **Risk ratios** — Sharpe and Sortino over a selectable 1Y/3Y/5Y/All window with a configurable risk-free rate, beside volatility and CAGR on the Overview.
- **Category peers** — benchmark a scheme against its full AMFI category universe (e.g. a small-cap fund vs every other small-cap fund): rank and percentile per horizon, box-plot distribution with the scheme marked, and a sortable peer table. Growth-only and Direct/Regular plan filters keep the peer set honest; histories are fetched once on demand and cached.
- **Watchlists** — multiple named lists, persisted at `~/.afp_nav_explorer/watchlists.json`.
- **Point-to-point returns** — custom dates, financial-year (Apr–Mar) table, month-on-month table. Absolute always; CAGR shown when the span exceeds 1Y.
- **Rolling returns** — 1/3/5Y daily-rolling, annualised, with distribution stats (min/max/median/avg, % negative, % above a hurdle, IQR) plus a histogram.
- **Comparison** — any number of schemes overlaid as growth-of-₹100 from a common start (no min/max scaling rules), plus a trailing-return comparison table.
- **Overview** — trailing block (1M–since inception), calendar-year bar, MoM heatmap, drawdown curve, max drawdown, annualised volatility.
- **SIP / XIRR** — monthly SIP money-weighted return alongside lumpsum CAGR.

## File map
- `returns.py` — pure pandas/numpy analytics engine (unit-tested in `test_engine.py`).
- `nav_data.py` — AMFI parsing + mfapi history fetch.
- `store.py` — watchlist persistence.
- `app.py` — Streamlit UI.
- `launcher.py` — customtkinter desktop launcher.
- `.streamlit/config.toml` — dark theme tokens for native Streamlit widgets.

## Roadmap (phase 2, scoped but not built)
- Benchmark-relative metrics vs an index: alpha/beta, up/down capture (needs an index TRI series).
- AUM and expense ratio (AMFI monthly disclosure / other source).
- Correlation matrix and a weighted blended-portfolio simulator.
- Alerts (NAV move %, 52-wk high/low, drawdown breach) and AFP-branded Excel/PDF export.

## Notes
Returns are NAV-based and exclude exit loads and taxes. For information only —
not investment advice. AFP is not SEBI-registered.
