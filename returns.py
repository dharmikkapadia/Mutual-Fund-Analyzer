"""
returns.py — analytical core for the AFP NAV Explorer.

Pure pandas/numpy. No network, no Streamlit, so every function here is
unit-testable in isolation. All NAV inputs are a pandas Series indexed by a
DatetimeIndex (ascending), float NAV, with non-trading days simply absent.

Return conventions (per build spec):
  - Absolute return is always available.
  - Annualised (CAGR) is computed for horizons > 1 year. For <= 1Y the
    annualised figure is returned as None so the UI can show "—".
  - Rolling returns for windows >= 1Y are expressed annualised (CAGR), which
    is what makes 1Y / 3Y / 5Y windows comparable.
Indian financial year = 1-Apr to 31-Mar (FYxx labelled by the ending year).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365.25
TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(series: pd.Series) -> pd.Series:
    """Sort ascending, coerce to float, drop zero/NaN NAVs."""
    s = pd.Series(series).astype(float).sort_index()
    s = s[s > 0]
    return s[~s.index.duplicated(keep="last")]


def nav_asof(series: pd.Series, date) -> float:
    """NAV on `date`, rolling back to the nearest prior trading day (holidays)."""
    s = _clean(series)
    val = s.asof(pd.Timestamp(date))
    return float(val) if pd.notna(val) else np.nan


def annualise(absolute_return: float, years: float):
    """CAGR from an absolute return over `years`; None if <= 1Y or invalid."""
    if years is None or years <= 1.0 or absolute_return <= -1.0:
        return None
    return (1.0 + absolute_return) ** (1.0 / years) - 1.0


# --------------------------------------------------------------------------- #
# Point-to-point
# --------------------------------------------------------------------------- #
def point_to_point(series: pd.Series, d1, d2) -> dict:
    """Return between two dates. Annualised only when span > 1Y."""
    s = _clean(series)
    d1, d2 = pd.Timestamp(d1), pd.Timestamp(d2)
    nav1, nav2 = nav_asof(s, d1), nav_asof(s, d2)
    if np.isnan(nav1) or np.isnan(nav2):
        return {"nav1": nav1, "nav2": nav2, "abs": np.nan,
                "cagr": None, "years": np.nan}
    years = (d2 - d1).days / DAYS_PER_YEAR
    absr = nav2 / nav1 - 1.0
    return {"nav1": nav1, "nav2": nav2, "abs": absr,
            "cagr": annualise(absr, years), "years": years}


def fy_returns(series: pd.Series) -> pd.DataFrame:
    """Financial-year (Apr–Mar) returns for every FY with a valid start NAV."""
    s = _clean(series)
    if s.empty:
        return pd.DataFrame(columns=["FY", "Start", "End", "Return %"])
    rows = []
    first_year = s.index.min().year - 1
    last_year = s.index.max().year + 1
    for end_yr in range(first_year + 1, last_year + 1):
        start = pd.Timestamp(year=end_yr - 1, month=3, day=31)
        end = pd.Timestamp(year=end_yr, month=3, day=31)
        n0, n1 = nav_asof(s, start), nav_asof(s, end)
        if (np.isnan(n0) or np.isnan(n1) or start < s.index.min()
                or end > s.index.max()):
            continue
        rows.append({"FY": f"FY{str(end_yr)[-2:]}", "Start": start.date(),
                     "End": end.date(), "Return %": (n1 / n0 - 1.0) * 100})
    return pd.DataFrame(rows)


def calendar_year_returns(series: pd.Series) -> pd.DataFrame:
    """Calendar-year returns plus a YTD row for the current year."""
    s = _clean(series)
    if s.empty:
        return pd.DataFrame(columns=["Year", "Return %"])
    ye = s.resample("YE").last()
    chg = ye.pct_change() * 100
    rows = [{"Year": str(idx.year), "Return %": val}
            for idx, val in chg.items() if pd.notna(val)]
    # YTD from last full year-end to latest NAV
    last_dec = s[s.index <= pd.Timestamp(year=s.index.max().year - 1,
                                         month=12, day=31)]
    if not last_dec.empty:
        ytd = (s.iloc[-1] / last_dec.iloc[-1] - 1.0) * 100
        rows.append({"Year": f"{s.index.max().year} (YTD)", "Return %": ytd})
    return pd.DataFrame(rows)


def monthly_returns(series: pd.Series) -> pd.DataFrame:
    """Month-on-month returns (month-end NAV)."""
    s = _clean(series)
    me = s.resample("ME").last()
    chg = me.pct_change() * 100
    return pd.DataFrame({"Month": [d.strftime("%b-%Y") for d in chg.index],
                         "Return %": chg.values}).dropna()


def monthly_matrix(series: pd.Series) -> pd.DataFrame:
    """Year x Month matrix of MoM returns (for a heatmap)."""
    m = monthly_returns(series)
    if m.empty:
        return pd.DataFrame()
    m["dt"] = pd.to_datetime(m["Month"], format="%b-%Y")
    m["Year"] = m["dt"].dt.year
    m["Mon"] = m["dt"].dt.strftime("%b")
    order = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    piv = m.pivot_table(index="Year", columns="Mon", values="Return %")
    return piv.reindex(columns=[c for c in order if c in piv.columns])


# --------------------------------------------------------------------------- #
# Trailing returns (factsheet block)
# --------------------------------------------------------------------------- #
_TRAILING = {
    "1M": pd.DateOffset(months=1), "3M": pd.DateOffset(months=3),
    "6M": pd.DateOffset(months=6), "1Y": pd.DateOffset(years=1),
    "3Y": pd.DateOffset(years=3), "5Y": pd.DateOffset(years=5),
}


def trailing_returns(series: pd.Series, asof=None) -> pd.DataFrame:
    """Standard trailing block + since-inception, abs and annualised."""
    s = _clean(series)
    if s.empty:
        return pd.DataFrame(columns=["Period", "Absolute %", "Annualised %"])
    end = pd.Timestamp(asof) if asof else s.index.max()
    nav_now = nav_asof(s, end)
    rows = []
    for label, off in _TRAILING.items():
        start = end - off
        if start < s.index.min():
            continue
        p = point_to_point(s, start, end)
        rows.append({"Period": label, "Absolute %": p["abs"] * 100,
                     "Annualised %": (p["cagr"] * 100) if p["cagr"] is not None
                     else np.nan})
    si = point_to_point(s, s.index.min(), end)
    rows.append({"Period": "Since Incep.", "Absolute %": si["abs"] * 100,
                 "Annualised %": (si["cagr"] * 100) if si["cagr"] is not None
                 else np.nan})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Rolling returns
# --------------------------------------------------------------------------- #
def _daily(series: pd.Series) -> pd.Series:
    s = _clean(series)
    idx = pd.date_range(s.index.min(), s.index.max(), freq="D")
    return s.reindex(idx).ffill()


def rolling_returns(series: pd.Series, years: float) -> pd.Series:
    """Daily-rolling annualised (CAGR) returns over a `years`-long window."""
    d = _daily(series)
    if d.empty:
        return pd.Series(dtype=float)
    yrs = int(years)
    # value `yrs` years before each date t (exact calendar lookup on a daily,
    # forward-filled series; dates before inception resolve to NaN).
    targets = d.index - pd.DateOffset(years=yrs)
    past = pd.Series(d.reindex(targets).to_numpy(), index=d.index)
    roll = (d / past) ** (1.0 / yrs) - 1.0
    valid_from = d.index.min() + pd.DateOffset(years=yrs)
    return roll[roll.index >= valid_from].dropna()


def rolling_stats(roll: pd.Series, hurdle: float = 0.0) -> dict:
    """Distribution stats for a rolling-return series (decimals in/out)."""
    if roll is None or roll.empty:
        return {}
    return {
        "observations": int(roll.size),
        "min": float(roll.min()), "max": float(roll.max()),
        "mean": float(roll.mean()), "median": float(roll.median()),
        "std": float(roll.std()),
        "pct_negative": float((roll < 0).mean() * 100),
        "pct_above_hurdle": float((roll >= hurdle).mean() * 100),
        "p25": float(roll.quantile(0.25)), "p75": float(roll.quantile(0.75)),
    }


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #
def drawdown(series: pd.Series) -> pd.Series:
    s = _clean(series)
    return s / s.cummax() - 1.0


def max_drawdown(series: pd.Series) -> float:
    dd = drawdown(series)
    return float(dd.min()) if not dd.empty else np.nan


def annualised_vol(series: pd.Series) -> float:
    """Annualised stdev of daily (trading-day) returns."""
    s = _clean(series)
    rets = s.pct_change().dropna()
    return float(rets.std() * np.sqrt(TRADING_DAYS)) if not rets.empty else np.nan


# --------------------------------------------------------------------------- #
# SIP / XIRR
# --------------------------------------------------------------------------- #
def xirr(cashflows: list[tuple], guess: float = 0.1) -> float:
    """XIRR for [(date, amount), ...]; outflows negative, inflows positive."""
    if len(cashflows) < 2:
        return np.nan
    cf = sorted(cashflows, key=lambda x: pd.Timestamp(x[0]))
    t0 = pd.Timestamp(cf[0][0])
    times = np.array([(pd.Timestamp(d) - t0).days / DAYS_PER_YEAR for d, _ in cf])
    amts = np.array([a for _, a in cf], dtype=float)

    def npv(r):
        return np.sum(amts / (1.0 + r) ** times)

    # Newton, then bisection fallback for robustness.
    r = guess
    for _ in range(100):
        f = npv(r)
        df = np.sum(-times * amts / (1.0 + r) ** (times + 1.0))
        if df == 0 or not np.isfinite(df):
            break
        step = f / df
        r -= step
        if abs(step) < 1e-8:
            return float(r)
    lo, hi = -0.9999, 100.0
    flo, fhi = npv(lo), npv(hi)
    if np.sign(flo) == np.sign(fhi):
        return np.nan
    for _ in range(200):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if abs(fm) < 1e-7:
            return float(mid)
        if np.sign(fm) == np.sign(flo):
            lo, flo = mid, fm
        else:
            hi = mid
    return float((lo + hi) / 2.0)


def sip_xirr(series: pd.Series, amount: float, start, end,
             day_of_month: int = 1) -> dict:
    """Monthly SIP of `amount` from start to end. Returns XIRR + summary."""
    s = _clean(series)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    flows, units, invested = [], 0.0, 0.0
    dt = pd.Timestamp(year=start.year, month=start.month,
                      day=min(day_of_month, 28))
    k = 0
    while dt <= end:
        nav = nav_asof(s, dt)
        if not np.isnan(nav):
            units += amount / nav
            invested += amount
            flows.append((dt, -amount))
        k += 1
        dt = pd.Timestamp(year=start.year, month=start.month,
                          day=min(day_of_month, 28)) + pd.DateOffset(months=k)
    nav_end = nav_asof(s, end)
    final_value = units * nav_end if not np.isnan(nav_end) else np.nan
    flows.append((end, final_value))
    return {"invested": invested, "final_value": final_value,
            "units": units, "xirr": xirr(flows),
            "abs_gain_pct": (final_value / invested - 1.0) * 100
            if invested else np.nan}


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def rebase_to_100(series: pd.Series, base_date=None) -> pd.Series:
    s = _clean(series)
    if s.empty:
        return s
    base = nav_asof(s, base_date) if base_date else s.iloc[0]
    return s / base * 100.0


def compare_rebased(named_series: dict, base_date=None) -> pd.DataFrame:
    """Align several schemes from a common start date, rebased to 100."""
    if base_date is None:
        starts = [_clean(s).index.min() for s in named_series.values()
                  if not _clean(s).empty]
        base_date = max(starts) if starts else None
    cols = {}
    for name, s in named_series.items():
        r = rebase_to_100(s, base_date)
        cols[name] = r[r.index >= pd.Timestamp(base_date)] if base_date is not None else r
    return pd.DataFrame(cols).ffill()


def compare_trailing(named_series: dict) -> pd.DataFrame:
    """Trailing-return comparison table across schemes (annualised where >1Y)."""
    frames = {}
    for name, s in named_series.items():
        t = trailing_returns(s).set_index("Period")
        # prefer annualised where present, else absolute
        frames[name] = t["Annualised %"].fillna(t["Absolute %"])
    return pd.DataFrame(frames)
