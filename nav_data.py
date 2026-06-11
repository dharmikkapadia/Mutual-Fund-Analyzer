"""
nav_data.py — data access for the AFP NAV Explorer.

Two sources, joined on AMFI scheme code:
  * AMFI NAVAll.txt  -> full scheme universe (code, name, ISIN, category,
    fund house) + latest NAV. Source of the searchable list and daily NAV.
  * api.mfapi.in     -> full daily NAV history per scheme. Source for every
    returns calculation (pulled on-demand, cached in-session).

mfapi.in is an unofficial community API with no SLA, so fetches are wrapped
with timeouts and the caller (app.py) caches results via st.cache_data.
"""

from __future__ import annotations

import io
import requests
import pandas as pd

AMFI_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
MFAPI_URL = "https://api.mfapi.in/mf/{code}"
HEADERS = {"User-Agent": "AFP-NAV-Explorer/1.0"}
TIMEOUT = 30


# --------------------------------------------------------------------------- #
# AMFI universe
# --------------------------------------------------------------------------- #
def fetch_amfi_raw() -> str:
    r = requests.get(AMFI_URL, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def parse_amfi(text: str) -> pd.DataFrame:
    """Parse NAVAll.txt into a tidy DataFrame.

    The file interleaves three kinds of lines: section headers like
    'Open Ended Schemes(Equity Scheme - Contra Fund)', bare fund-house names,
    and 6-field ';'-delimited data rows. We carry the current category and
    fund house down onto each data row.
    """
    category, fund_house, rows = None, None, []
    for line in io.StringIO(text):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("Scheme Code;"):
            continue
        if ";" in line:
            parts = line.split(";")
            if len(parts) < 6 or not parts[0].strip().isdigit():
                continue
            rows.append({
                "code": int(parts[0].strip()),
                "isin": parts[1].strip(),
                "name": parts[3].strip(),
                "nav": pd.to_numeric(parts[4].strip(), errors="coerce"),
                "date": pd.to_datetime(parts[5].strip(),
                                       format="%d-%b-%Y", errors="coerce"),
                "category": category,
                "fund_house": fund_house,
            })
        elif "Scheme" in line and "(" in line:
            category = line.strip()
        else:
            fund_house = line.strip()
    return pd.DataFrame(rows)


def live_universe(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only schemes priced on the latest available date (drops dead ones)."""
    if df.empty:
        return df
    latest = df["date"].max()
    return df[df["date"] >= latest - pd.Timedelta(days=4)].reset_index(drop=True)


def search_universe(df: pd.DataFrame, query: str = "", fund_house: str = "",
                    category: str = "") -> pd.DataFrame:
    out = df
    if query:
        out = out[out["name"].str.contains(query, case=False, na=False)]
    if fund_house:
        out = out[out["fund_house"] == fund_house]
    if category:
        out = out[out["category"] == category]
    return out


# --------------------------------------------------------------------------- #
# mfapi.in history
# --------------------------------------------------------------------------- #
def fetch_scheme(code: int) -> dict:
    r = requests.get(MFAPI_URL.format(code=code), headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def history_to_series(payload: dict) -> pd.Series:
    """Convert an mfapi payload's `data` block to an ascending float Series."""
    data = payload.get("data", [])
    if not data:
        return pd.Series(dtype=float)
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y", errors="coerce")
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    s = df.dropna(subset=["date"]).set_index("date")["nav"].sort_index()
    return s[s > 0]


def scheme_meta(payload: dict) -> dict:
    return payload.get("meta", {})
