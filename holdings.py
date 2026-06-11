"""
holdings.py — portfolio holdings, fund facts and overlap analytics.

Primary source is Rupeevest's internal JSON API (the same endpoints the
website itself calls):
  * GET /home/get_search_data                      -> name -> schemecode map
  * GET /home/get_mf_portfolio_tracker?schemecode= -> stock_data (rows with
    fincode/invdate/percent_aum), stock_mapping {fincode: stock name} and
    fund_info [{s_name, aumdate, aumtotal}]
Schemes are matched from AMFI names by token similarity with hard gates on
Direct/Regular plan and Growth vs IDCW, so a flexi-cap fund can never match
an ETF. Groww and Kuvera remain as fallbacks with the same strict matching.

Provider responses change shape without notice, so parsing is defensive:
field lookups try candidate keys and structures are discovered by walking
the JSON. Any failure raises HoldingsError with a readable message — the UI
degrades to manual CSV upload plus external links.

The overlap maths at the bottom is pure pandas and unit-testable offline.
Pairwise overlap % = sum of min(weight_a, weight_b) over common holdings —
the standard "portfolio overlap" definition.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote_plus

import pandas as pd
import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.rupeevest.com/",
}
TIMEOUT = 25

RV_HOME = "https://www.rupeevest.com/"
RV_SEARCH = "https://www.rupeevest.com/home/get_search_data"
RV_PORTFOLIO = ("https://www.rupeevest.com/home/get_mf_portfolio_tracker"
                "?schemecode={code}")
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
# HTTP helpers
# --------------------------------------------------------------------------- #
_SESS: requests.Session | None = None


def _session() -> requests.Session:
    """Shared session, warmed on rupeevest.com so PHP session cookies exist."""
    global _SESS
    if _SESS is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(RV_HOME, timeout=TIMEOUT)
        except requests.RequestException:
            pass
        _SESS = s
    return _SESS


def _get_json(url: str, params: dict | None = None):
    r = _session().get(url, params=params, timeout=TIMEOUT)
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


# --------------------------------------------------------------------------- #
# Scheme-name matching (AMFI name -> provider's scheme)
# --------------------------------------------------------------------------- #
_STOP = {"plan", "option", "fund", "scheme", "the", "of", "and", "an"}


def _tokens(name: str) -> set[str]:
    s = str(name).lower()
    s = (s.replace("(g)", " growth ").replace("(d)", " idcw ")
          .replace("(idcw)", " idcw ").replace("(div)", " idcw "))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    t = set(s.split()) - _STOP
    if "dir" in t:
        t.discard("dir")
        t.add("direct")
    if "reg" in t:
        t.discard("reg")
        t.add("regular")
    if t & {"dividend", "div", "payout", "reinvestment"}:
        t.add("idcw")
    return t


def _name_score(target: set[str], candidate: str) -> float:
    """Jaccard similarity with hard plan/option gates (0 on mismatch)."""
    c = _tokens(candidate)
    if not c:
        return 0.0
    if ("direct" in target) != ("direct" in c):
        return 0.0
    if "growth" in target and "idcw" in c:
        return 0.0
    if "idcw" in target and "idcw" not in c:
        return 0.0
    return len(target & c) / max(1, len(target | c))


_MIN_SCORE = 0.45


# --------------------------------------------------------------------------- #
# Holdings normalisation
# --------------------------------------------------------------------------- #
_NAME_KEYS = ("company_name", "stock_name", "security_name", "name",
              "companyName", "stockName", "instrument_name", "holding_name",
              "compname", "s_name")
_WEIGHT_KEYS = ("percent_aum", "corpus_per", "percentage", "weight",
                "weighting", "corpus_percentage", "portfolio_percentage",
                "percent", "net_assets", "holding_percentage")
_SECTOR_KEYS = ("sector_name", "sector", "industry_name", "industry",
                "macro_sector", "sectorName", "sect_name")


def _rows_to_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if not df.empty:
        df = (df.groupby("security", as_index=False)
                .agg(weight=("weight", "sum"), sector=("sector", "first"))
                .sort_values("weight", ascending=False)
                .reset_index(drop=True))
    return df


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
    return _rows_to_df(rows)


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
        "aum": _to_float(_first(d, ("aumtotal", "aum", "fund_size",
                                    "fundSize", "net_assets",
                                    "aum_in_crores"))),
        "expense": _to_float(_first(d, ("expense_ratio", "expenseRatio",
                                        "expense_ratio_percentage"))),
        "managers": _parse_managers(_first(d, ("fund_manager", "fund_managers",
                                               "fundManagers", "managers"),
                                           "")),
        "as_of": _first(d, ("aumdate", "portfolio_date", "holdings_date",
                            "as_of", "portfolioDate")),
    }


# --------------------------------------------------------------------------- #
# Provider: Rupeevest (primary)
# --------------------------------------------------------------------------- #
_RV_CACHE: dict = {"t": 0.0, "data": None}


def _rv_search_data() -> list[dict]:
    if _RV_CACHE["data"] is None or time.time() - _RV_CACHE["t"] > 6 * 3600:
        js = _get_json(RV_SEARCH)
        recs = js.get("search_data") if isinstance(js, dict) else None
        if not recs:
            recs = _dicts_with_key(js, "schemecode")
        if not recs:
            raise HoldingsError("unexpected get_search_data payload")
        _RV_CACHE.update(t=time.time(), data=recs)
    return _RV_CACHE["data"]


def _rupeevest(isin: str, name: str) -> dict:
    target = _tokens(name)
    best, best_score = None, 0.0
    for rec in _rv_search_data():
        for key in ("s_name1", "s_name"):
            sc = _name_score(target, rec.get(key) or "")
            if sc > best_score:
                best, best_score = rec, sc
    if best is None or best_score < _MIN_SCORE:
        raise HoldingsError(f"no confident name match for '{name}' "
                            f"(best score {best_score:.2f})")
    code = best.get("schemecode")
    js = _get_json(RV_PORTFOLIO.format(code=code))
    if not isinstance(js, dict):
        raise HoldingsError("unexpected portfolio_tracker payload")

    # fincode -> stock name / sector maps
    stock_map, sect_map = {}, {}
    for holder in _dicts_with_key(js, "stock_mapping"):
        if isinstance(holder.get("stock_mapping"), dict):
            stock_map = {str(k): str(v)
                         for k, v in holder["stock_mapping"].items()}
            break
    for k, v in js.items():
        if "sect" in str(k).lower() and isinstance(v, dict):
            sect_map = {str(kk): str(vv) for kk, vv in v.items()}
            break

    # the tracker returns positions across months — keep the latest month
    raw = js.get("stock_data") or []
    flat = []
    for it in raw:
        if isinstance(it, list):
            flat.extend(x for x in it if isinstance(x, dict))
        elif isinstance(it, dict):
            flat.append(it)
    dates = pd.to_datetime([str(it.get("invdate", "")) for it in flat],
                           errors="coerce")
    if len(flat) and pd.notna(dates.max()):
        flat = [it for it, d in zip(flat, dates) if d == dates.max()]
    seen, rows = set(), []
    for it in flat:
        fin = str(it.get("fincode", ""))
        if fin and fin in seen:
            continue
        seen.add(fin)
        nm = stock_map.get(fin) or _first(it, _NAME_KEYS)
        w = _to_float(_first(it, _WEIGHT_KEYS))
        sec = sect_map.get(fin) or _first(it, _SECTOR_KEYS)
        if nm and w is not None and 0 < w <= 100:
            rows.append({"security": str(nm).strip(), "weight": w,
                         "sector": str(sec).strip() if sec else None})
    holdings = _rows_to_df(rows)

    fi = js.get("fund_info") or []
    facts = _parse_facts(fi[0] if isinstance(fi, list) and fi else {})
    if holdings.empty and not facts.get("aum"):
        raise HoldingsError(f"schemecode {code} returned no holdings")
    return {"holdings": holdings, "facts": facts,
            "rv_schemecode": code}


# --------------------------------------------------------------------------- #
# Provider: Groww (fallback)
# --------------------------------------------------------------------------- #
def _groww(isin: str, name: str) -> dict:
    js = _get_json(GROWW_SEARCH, params={"query": name, "size": 20})
    target = _tokens(name)
    best, best_score = None, 0.0
    for r in _dicts_with_key(js, "search_id"):
        etype = str(_first(r, ("entity_type", "entityType"), "")).lower()
        if etype and "scheme" not in etype:
            continue
        cand = _first(r, ("title", "s_name", "name", "scheme_name")) or ""
        sc = _name_score(target, cand)
        if isin and isin in " ".join(str(v) for v in r.values()):
            sc = max(sc, 0.95)
        if sc > best_score:
            best, best_score = r, sc
    if best is None or best_score < _MIN_SCORE:
        raise HoldingsError(f"no confident search match for '{name}' "
                            f"(best score {best_score:.2f})")
    sid = best["search_id"]

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


# --------------------------------------------------------------------------- #
# Provider: Kuvera (fallback)
# --------------------------------------------------------------------------- #
def _kuvera_parse(d: dict) -> dict:
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


def _kuvera(isin: str, name: str) -> dict:
    if isin and len(isin) >= 8:
        try:  # some deployments accept the ISIN directly as the code
            js = _get_json(KUVERA_FUND.format(code=isin))
            d = js[0] if isinstance(js, list) and js else js
            if isinstance(d, dict) and d:
                return _kuvera_parse(d)
        except Exception:  # noqa: BLE001
            pass
    target = _tokens(name)
    best, best_score = None, 0.0
    for e in _get_json(KUVERA_LIST):
        if not isinstance(e, dict):
            continue
        if isin and isin in " ".join(str(v) for v in e.values()):
            best, best_score = e, 1.0
            break
        sc = _name_score(target, _first(e, ("n", "name", "scheme_name")) or "")
        if sc > best_score:
            best, best_score = e, sc
    if best is None or best_score < _MIN_SCORE:
        raise HoldingsError(f"no confident match in scheme list "
                            f"(best score {best_score:.2f})")
    code = _first(best, ("c", "code", "scheme_code"))
    js = _get_json(KUVERA_FUND.format(code=code))
    d = js[0] if isinstance(js, list) and js else js
    if not isinstance(d, dict):
        raise HoldingsError("unexpected fund payload")
    return _kuvera_parse(d)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def fetch_portfolio(isin: str, name: str) -> dict:
    """Holdings + fund facts for a scheme, Rupeevest first.

    Returns {"holdings": DataFrame[security, weight%, sector], "facts": dict,
    "source": provider name}. Raises HoldingsError when every provider
    fails (the UI falls back to manual upload + external links).
    """
    errors = []
    for fn, src in ((_rupeevest, "Rupeevest"), (_groww, "Groww"),
                    (_kuvera, "Kuvera")):
        try:
            out = fn(isin, name)
            out["source"] = src
            return out
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src}: {type(e).__name__}: {e}")
    raise HoldingsError(" | ".join(errors))


def parse_uploaded(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a user-uploaded holdings table to [security, weight, sector]."""
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
    return _rows_to_df(out.to_dict("records"))


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


def rupeevest_scheme_link(schemecode) -> str:
    return f"https://www.rupeevest.com/Mutual-Funds-India/{schemecode}"


def rupeevest_link(name: str) -> str:
    return ("https://www.google.com/search?q="
            + quote_plus(f"site:rupeevest.com {name}"))


def factsheet_link(name: str) -> str:
    return ("https://www.google.com/search?q="
            + quote_plus(f"{name} latest factsheet pdf"))


def valueresearch_link(name: str) -> str:
    return ("https://www.google.com/search?q="
            + quote_plus(f"site:valueresearchonline.com {name}"))
