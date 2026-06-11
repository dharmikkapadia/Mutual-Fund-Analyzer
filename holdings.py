"""
holdings.py — portfolio holdings, fund facts and overlap analytics.

Holdings and fund facts come from public, unauthenticated JSON endpoints
(Groww first, Kuvera as fallback), matched by ISIN from the AMFI universe.
Rupeevest has no public API and actively blocks non-browser clients, so it
is linked from the UI rather than scraped.

Provider responses change shape without notice, so all parsing is defensive:
field lookups try a list of candidate keys and structures are discovered by
walking the JSON. Any failure raises HoldingsError with a readable message —
the UI degrades to manual CSV upload plus external links.

The overlap maths at the bottom is pure pandas and unit-testable offline.
Pairwise overlap %% = sum of min(weight_a, weight_b) over common holdings —
the standard "portfolio overlap" definition.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

import pandas as pd
import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}
TIMEOUT = 20

GROWW_SEARCH = "https://groww.in/v1/api/search/v3/query/global/st_query"
GROWW_SCHEME = (
    "https://groww.in/v1/api/data/mf/web/v4/scheme/{sid}",
    "https://groww.in/v1/api/data/mf/web/v3/scheme/{sid}",
)
KUVERA_LIST = "https://api.kuvera.in/mf/api/v4/fund_schemes/list.json"
KUVERA_FUND = "https://api.kuvera.in/mf/api/v5/fund_schemes/{code}.json"


class HoldingsError(RuntimeError):
    """Raised when no provider could return usable portfolio data."""


# --------------------------------------------------------------------------- #
# Generic JSON helpers
# --------------------------------------------------------------------------- #
def _get_json(url: str, params: dict | None = None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _first(d: dict, keys, default=None):
    """First non-empty value among candidate keys."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", [], {}):
            return d[k]
    return default


def _dicts_with_key(obj, key: str) -> list[dict]:
    """All dicts anywhere in a JSON tree that contain `key`."""
    out = []
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj)
        for v in obj.values():
            out.extend(_dicts_with_key(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_dicts_with_key(v, key))
    return out


def _to_float(x):
    try:
        return float(str(x).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


_NAME_KEYS = ("company_name", "stock_name", "security_name", "name",
              "companyName", "stockName", "instrument_name", "holding_name")
_WEIGHT_KEYS = ("corpus_per", "percentage", "weight", "weighting",
                "corpus_percentage", "portfolio_percentage", "percent",
                "net_assets", "holding_percentage")
_SECTOR_KEYS = ("sector_name", "sector", "industry_name", "industry",
                "macro_sector", "sectorName")


def _parse_holdings_list(items: list) -> pd.DataFrame:
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = _first(it, _NAME_KEYS)
        weight = _to_float(_first(it, _WEIGHT_KEYS))
        sector = _first(it, _SECTOR_KEYS)
        if name and weight is not None and 0 < weight <= 100:
            rows.append({"security": str(name).strip(), "weight": weight,
                         "sector": str(sector).strip() if sector else None})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = (df.groupby("security", as_index=False)
                .agg(weight=("weight", "sum"), sector=("sector", "first"))
                .sort_values("weight", ascending=False)
                .reset_index(drop=True))
    return df


def _parse_managers(val) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return str(_first(val, ("name", "fund_manager")) or "")
    if isinstance(val, list):
        names = [_parse_managers(v) for v in val]
        return ", ".join(n for n in names if n)
    return ""


def _parse_facts(d: dict) -> dict:
    return {
        "aum": _to_float(_first(d, ("aum", "fund_size", "fundSize",
                                    "net_assets", "aum_in_crores"))),
        "expense": _to_float(_first(d, ("expense_ratio", "expenseRatio",
                                        "expense_ratio_percentage"))),
        "managers": _parse_managers(_first(d, ("fund_manager", "fund_managers",
                                               "fundManagers", "managers"),
                                           "")),
        "as_of": _first(d, ("portfolio_date", "holdings_date", "as_of",
                            "portfolioDate")),
    }


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
def _groww(isin: str, name: str) -> dict:
    sid = None
    for query in (isin, name):
        if not query or len(str(query)) < 4:
            continue
        js = _get_json(GROWW_SEARCH, params={"query": query, "size": 10})
        recs = _dicts_with_key(js, "search_id")
        # prefer an exact ISIN match, else the first scheme-looking record
        for r in recs:
            if isin and isin in str(r.values()):
                sid = r["search_id"]
                break
        if sid is None and recs:
            sid = recs[0]["search_id"]
        if sid:
            break
    if not sid:
        raise HoldingsError("scheme not found in search")

    detail, last_err = None, None
    for tmpl in GROWW_SCHEME:
        try:
            detail = _get_json(tmpl.format(sid=sid))
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
    if detail is None:
        raise HoldingsError(f"scheme detail fetch failed ({last_err})")

    holdings = pd.DataFrame()
    for holder in _dicts_with_key(detail, "holdings"):
        if isinstance(holder.get("holdings"), list) and holder["holdings"]:
            holdings = _parse_holdings_list(holder["holdings"])
            if not holdings.empty:
                break
    facts = {}
    for cand in _dicts_with_key(detail, "fund_manager") + [detail]:
        facts = _parse_facts(cand)
        if facts.get("managers") or facts.get("aum"):
            break
    if holdings.empty and not facts.get("aum"):
        raise HoldingsError("response contained no holdings or facts")
    return {"holdings": holdings, "facts": facts}


def _kuvera(isin: str, name: str) -> dict:
    if not isin or len(isin) < 8:
        raise HoldingsError("no ISIN to match against Kuvera")
    codes = []
    for e in _get_json(KUVERA_LIST):
        if isinstance(e, dict) and isin in [str(v) for v in e.values()]:
            code = _first(e, ("c", "code", "scheme_code"))
            if code:
                codes.append(code)
    if not codes:
        raise HoldingsError("ISIN not found in Kuvera scheme list")
    js = _get_json(KUVERA_FUND.format(code=codes[0]))
    d = js[0] if isinstance(js, list) and js else js
    if not isinstance(d, dict):
        raise HoldingsError("unexpected fund payload")
    holdings = pd.DataFrame()
    for holder in _dicts_with_key(d, "holdings"):
        if isinstance(holder.get("holdings"), list) and holder["holdings"]:
            holdings = _parse_holdings_list(holder["holdings"])
            if not holdings.empty:
                break
    facts = _parse_facts(d)
    if holdings.empty and not facts.get("aum"):
        raise HoldingsError("response contained no holdings or facts")
    return {"holdings": holdings, "facts": facts}


def fetch_portfolio(isin: str, name: str) -> dict:
    """Holdings + fund facts for a scheme.

    Returns {"holdings": DataFrame[security, weight%], "facts": dict,
    "source": provider name}. Raises HoldingsError when every provider
    fails (the UI falls back to manual upload + external links).
    """
    errors = []
    for fn, src in ((_groww, "Groww"), (_kuvera, "Kuvera")):
        try:
            out = fn(isin, name)
            out["source"] = src
            return out
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src}: {type(e).__name__}: {e}")
    raise HoldingsError(" | ".join(errors))


def parse_uploaded(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a user-uploaded holdings table to [security, weight]."""
    cols = {str(c).strip().lower(): c for c in df.columns}
    name_col = next((cols[k] for k in cols
                     if any(t in k for t in ("security", "stock", "company",
                                             "instrument", "name", "holding"))),
                    None)
    w_col = next((cols[k] for k in cols
                  if any(t in k for t in ("weight", "%", "percent", "corpus",
                                          "asset"))), None)
    if name_col is None or w_col is None:
        raise HoldingsError(
            "couldn't find a security-name and weight column — expected "
            "headers like 'Security' and 'Weight %'")
    sec_col = next((cols[k] for k in cols
                    if any(t in k for t in ("sector", "industry"))), None)
    out = pd.DataFrame({
        "security": df[name_col].astype(str).str.strip(),
        "weight": df[w_col].map(_to_float),
        "sector": (df[sec_col].astype(str).str.strip()
                   if sec_col is not None else None)})
    out = out.dropna(subset=["security", "weight"])
    out = out[(out["weight"] > 0) & (out["weight"] <= 100)]
    return (out.groupby("security", as_index=False)
               .agg(weight=("weight", "sum"), sector=("sector", "first"))
               .sort_values("weight", ascending=False).reset_index(drop=True))


# --------------------------------------------------------------------------- #
# Overlap analytics (pure — no network)
# --------------------------------------------------------------------------- #
_SUFFIX = re.compile(
    r"\b(ltd|limited|pvt|private|co|corp|corporation|inc)\b\.?", re.I)


def canon(security: str) -> str:
    """Canonical security name so 'HDFC Bank Ltd.' == 'HDFC BANK LIMITED'."""
    s = _SUFFIX.sub("", str(security))
    return re.sub(r"[^A-Z0-9]+", " ", s.upper()).strip()


def _weight_map(df: pd.DataFrame) -> pd.Series:
    m = df.assign(key=df["security"].map(canon)).groupby("key")["weight"].sum()
    return m[m > 0]


def overlap_pct(a: pd.DataFrame, b: pd.DataFrame) -> float:
    """Portfolio overlap %: sum of min weights over common holdings."""
    wa, wb = _weight_map(a), _weight_map(b)
    common = wa.index.intersection(wb.index)
    if common.empty:
        return 0.0
    return float(pd.concat([wa[common], wb[common]], axis=1).min(axis=1).sum())


def overlap_matrix(named: dict[str, pd.DataFrame]) -> pd.DataFrame:
    names = list(named)
    mat = pd.DataFrame(100.0, index=names, columns=names)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            v = overlap_pct(named[a], named[b])
            mat.loc[a, b] = mat.loc[b, a] = v
    return mat


def combined_table(named: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Security x scheme weight table with a 'Held by' count column.

    Securities held by >= 2 schemes sort first (these are the overlaps the
    UI highlights); display names come from the first scheme listing them.
    """
    frames, display = [], {}
    for scheme, df in named.items():
        if df is None or df.empty:
            continue
        d = df.assign(key=df["security"].map(canon))
        for _, r in d.iterrows():
            display.setdefault(r["key"], r["security"])
        frames.append(d.groupby("key")["weight"].sum().rename(scheme))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1)
    out.insert(0, "Held by", out.notna().sum(axis=1))
    out.insert(0, "Security", [display[k] for k in out.index])
    out["_max"] = out[list(named)].max(axis=1)
    out = (out.sort_values(["Held by", "_max"], ascending=False)
              .drop(columns="_max").reset_index(drop=True))
    return out


def rupeevest_link(name: str) -> str:
    return ("https://www.google.com/search?q="
            + quote_plus(f"site:rupeevest.com {name}"))


def factsheet_link(name: str) -> str:
    return ("https://www.google.com/search?q="
            + quote_plus(f"{name} latest factsheet pdf"))


def valueresearch_link(name: str) -> str:
    return ("https://www.google.com/search?q="
            + quote_plus(f"site:valueresearchonline.com {name}"))
