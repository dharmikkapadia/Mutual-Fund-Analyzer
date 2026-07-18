# 📈 AFP NAV Explorer

A mutual-fund NAV viewer and analytics workbench covering the **entire AMFI
universe** (~10,000+ live Indian mutual-fund schemes). A TradingView-inspired
Streamlit app for watchlists, deep return & risk analytics, portfolio holdings
& overlap, and category peer benchmarking — deployable to the cloud or run as a
desktop app via the bundled customtkinter launcher.

> **Live app:** https://mutual-fund-analyzer-nxp7chbxbewtxwue44pr2x.streamlit.app
>
> For information only. Returns are NAV-based and exclude exit loads and taxes —
> not investment advice. AFP is not SEBI-registered.

## UI
Minimal TradingView-inspired design with four **viewing modes** — Midnight
(true black, default), Slate, Light and Sepia — switchable from the sidebar's
Appearance section and remembered per browser. Each mode retokens the whole app:
custom CSS, native Streamlit widgets (via runtime theme config) and every chart.
High-contrast text, crosshair hover, right-side axes, 1M/6M/YTD/1Y/3Y/5Y/All
range buttons, and red/green gain-loss semantics. Charts are fully interactive:
scroll to zoom, a minimal modebar (zoom/pan/reset/fullscreen/PNG), and hover
tooltips that show both the rebased value and the cumulative % gain. The NAV
history chart adds a Google-Finance-style **drag-to-select**: sweep across any
date range to read back its absolute return, annualised CAGR and the NAV move,
with the period shaded and its endpoints marked.
Subtle motion (fade-up transitions, hover lifts) respects
`prefers-reduced-motion`. Chart styling is centralised in the `tv()` helper in
`app.py`; `.streamlit/config.toml` holds the default (Midnight) widget theme.

## Data sources
| Source | Role |
|---|---|
| AMFI `NAVAll.txt` | Full scheme universe (code, ISIN, name, category, fund house) + latest NAV. Powers search, the watchlist picker and the daily NAV. Dead schemes (stale dates) are filtered out. |
| `api.mfapi.in/mf/{code}` | Full daily NAV history per scheme — the basis of every returns calculation. Fetched on-demand and cached in-session. |
| `rupeevest.com` (`get_holding_asset`) | Portfolio holdings, sector splits and fund facts (AUM, managers). Matched to scheme codes via the bundled `rupeevest_codes.csv`. Groww/Kuvera are automatic fallbacks; manual CSV upload is the last resort. |
| `valueresearchonline.com` (login required) | Fund-level portfolio parameters for the **PF Review** tab: P/B, P/E, AUM, large/mid/small-cap split, debt & cash and sector allocation. Primary source is the fund's portfolio JSON endpoint (`/api/funds/{id}/portfolio/` — the fund page itself is an AJAX-filled skeleton), with page-HTML parsing as fallback. Fetched with the *user's own* VR account (credentials held in session memory only); every fetched number lands in an editable grid, so the tab also works fully manually. See `vr_data.py`. |

## Features
- **Watchlist dashboard** — sortable grid of every scheme in the active list: 1Y NAV sparkline, latest NAV, 1D/1Y/3Y/5Y returns and max drawdown, red/green coded.
- **Overview** — trailing returns (1M–since inception), a NAV-history chart with **drag-to-select return measurement** (sweep any window for its absolute/annualised return), calendar-year bars, month-on-month heatmap, drawdown curve, and the deepest-drawdowns table with recovery time.
- **Risk ratios** — Sharpe, Sortino and annualised standard deviation over a selectable 1Y/3Y/5Y/All window with a configurable risk-free rate, plus **beta, Jensen's alpha, R² and up/down capture ratios** against a user-pickable index fund.
- **Plan comparison** — Direct vs Regular CAGR gap for the same scheme (the expense-ratio drag), when both plans are in the universe.
- **Point-to-point returns** — custom dates, financial-year (Apr–Mar) table, month-on-month table. Absolute always; CAGR when the span exceeds 1Y.
- **Rolling returns** — 1/3/5Y daily-rolling, annualised, with distribution stats and a histogram, plus **rolling beta/alpha** vs the chosen benchmark.
- **SIP / XIRR & goal planner** — money-weighted SIP return vs lumpsum CAGR, and the required monthly SIP to hit a target corpus from the fund's own rolling-return distribution (pessimistic/median/optimistic) with a projected-corpus fan chart.
- **Compare** — any number of schemes overlaid as growth-of-₹100 from a common start, plus a trailing-return comparison table.
- **Holdings & overlap** — portfolio holdings, sectors and fund facts from Rupeevest's API; select multiple schemes for an **overlap matrix** (sum of min weights), a combined holdings table with common positions highlighted and unique counts, and a grouped **sector-allocation** comparison. See `holdings.py`.
- **Category peers** — benchmark a scheme against its full AMFI category (e.g. small-cap vs every small-cap): rank & percentile per horizon, a box-plot distribution, a growth-of-₹100 chart overlaying chosen peers and an equal-weight **category-average** line, a **risk-vs-return scatter** (volatility vs 3Y CAGR with median crosshairs), a **3Y rolling-return consistency band** (the fund vs the peer 25–75% range), a sortable peer table, and one-click adding of same-category peers to the watchlist.
- **Portfolio** — treat the watchlist as one weighted portfolio: blended growth-of-₹100, blended CAGR/volatility/max-drawdown/Sharpe, and a weekly-return correlation matrix for diversification.
- **PF Review** — the app version of a monthly "MF Portfolio Review" spreadsheet: enter each watchlist scheme's invested ₹ value, pull its month-end parameters (P/B, P/E, AUM, large/mid/small-cap split, debt & cash, 19 sector weights) from its Value Research page, and read the **investment-value-weighted** portfolio aggregates (blended P/E, cap mix, sector mix). Everything lands in an editable grid (manual entry works without VR), monthly **snapshots** persist in the browser for month-over-month comparison, and one click downloads the review as an Excel workbook with live `SUMPRODUCT` formulas — the same layout as the manual sheet. See `pf_review.py` / `vr_data.py`.
- **Watchlists** — multiple named lists, persisted in the **browser** (localStorage) so they survive cloud restarts and stay per-visitor; `~/.afp_nav_explorer/watchlists.json` is the desktop fallback / migration source.

## Run
```
python -m pip install -r requirements.txt
```
Desktop launcher (recommended):
```
python launcher.py
```
Or run Streamlit directly:
```
python -m streamlit run app.py
```

## Deploy (Streamlit Community Cloud)
Point [share.streamlit.io](https://share.streamlit.io) at this repo with
`app.py` as the main file — no other config needed. On the cloud, watchlists are
stored in each visitor's browser via localStorage (the ephemeral, shared server
filesystem is auto-disabled; `AFP_BROWSER_ONLY=1` forces this on other hosts,
`AFP_NO_BROWSER_STORE=1` disables the browser sync for headless tests).

## Project layout
- `app.py` — Streamlit UI (themes, charts, all tabs).
- `returns.py` — pure pandas/numpy analytics engine (returns, risk ratios, capture, drawdowns, SIP/goal, blend, correlation) — no network or UI, fully unit-testable.
- `nav_data.py` — AMFI parsing + mfapi history fetch.
- `holdings.py` — Rupeevest/Groww/Kuvera holdings fetch + overlap analytics.
- `vr_data.py` — Value Research login + fund-portfolio page parsing (label-driven, defensive). Runnable as a CLI to diagnose parsing against the live site: `python vr_data.py 16026 --email … --password …`.
- `pf_review.py` — pure PF-Review engine: value-weighted parameter maths, monthly snapshot (de)serialisation and the Excel export with live formulas — no network or UI, unit-testable offline.
- `store.py` — watchlist + PF-Review persistence (browser localStorage primary, JSON file fallback).
- `cloud_sync.py` — optional encrypted, cross-device watchlist sync (see below).
- `launcher.py` — customtkinter desktop launcher.
- `rupeevest_codes.csv` — bundled AMFI → Rupeevest scheme-code map.
- `.streamlit/config.toml` — default (Midnight) widget theme.
- `.claude/hooks/session-start.sh` — installs dependencies for Claude Code on the web sessions.

## Cross-device watchlist sync (optional, encrypted)
This repo is safe to make **public**. User watchlists are never stored in clear
text. When enabled, the app encrypts each user's watchlist *in the browser/app*
with their passphrase (Argon2id key derivation + Fernet/AES) and stores only the
ciphertext in a **separate private** GitHub repo that acts as the datastore.

How it works:
- A user picks a **username** (locates their record) and a **passphrase**
  (decrypts it). The blob is stored at `watchlists/<sha256(username)>.json`, so
  the store never reveals who a user is.
- Reading needs the GitHub token only because the store repo is private; the
  passphrase never leaves the app and is **not recoverable** if lost.
- Saving to an existing username requires proving you know its passphrase, so
  one user cannot overwrite another's data.

Setup:
1. Create a **private** GitHub repo (e.g. `you/mfa-watchlists-private`).
2. Create a fine-grained PAT scoped to that repo with **Contents: read & write**.
3. Copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` (gitignored)
   and fill in `token`, `repo`, `branch`. On Streamlit Cloud, paste the same
   under **App → Settings → Secrets**.

Without secrets configured, the sync UI is hidden and the app behaves exactly as
before (per-browser localStorage only).

## Roadmap
- Branded **Excel / PDF export** of a scheme or watchlist.
- **Alerts** — NAV move %, 52-week high/low, drawdown breach.
- A benchmark **TRI series** for true index-relative metrics.

## Notes
Returns are NAV-based and exclude exit loads and taxes. For information only —
not investment advice. AFP is not SEBI-registered.
