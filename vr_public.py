"""
vr_public.py — login-free Value Research fetcher for the PF Review tab.

Pulls the per-fund parameters the monthly review needs from VR's PUBLIC
endpoints, keyed by the numeric fund code (the number in the fund page URL):

    * /api/funds/port-aggregates-chart/{code}   -> Large / Mid / Small split
    * /api/funds/asset-allocation-chart/{code}  -> Debt & Cash (= 100 - Equity)
    * /api/funds/sector-chart/{code}            -> sector allocation, as-on
                                                   date and the fund's name
    * /fund-details/{code}/?tab=fund-portfolio  -> portfolio P/B and P/E
    * /fund-details/{code}/?tab=overview        -> AUM (₹ cr)

No VR account, login or cookie is involved — these are the same endpoints the
public fund page loads its own charts from, which is why this path works
where scraping the (login-walled) portfolio tab did not. This is the exact
approach proven in the standalone "MF Portfolio Review" desktop fetcher.

Percentages are returned on the 0-100 scale used across the app, and sector
names are mapped onto the canonical PF-Review columns via vr_data's
vocabulary, so the returned record (vr_data.blank_params() shape) feeds
pf_review.params_to_row() unchanged.

cloudscraper (optional) transparently clears Cloudflare's basic JS challenge;
without it the module falls back to plain requests.

Diagnostics (prints every parsed field for one fund):
    python vr_public.py 16026
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import re
import time

import requests

try:  # bs4 is required for the P/B, P/E and AUM fragments
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

# cloudscraper clears Cloudflare's basic JS challenge if it ever appears.
# Pure-Python and optional: without it the module uses plain requests.
try:
    import cloudscraper  # noqa: F401
    _HAS_CLOUDSCRAPER = True
except Exception:  # pragma: no cover  # noqa: BLE001
    _HAS_CLOUDSCRAPER = False

from vr_data import VRError, _SECTOR_LOOKUP, _norm_label, blank_params

VR_BASE = "https://www.valueresearchonline.com"
TIMEOUT = 30
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def create_session():
    """A requests-compatible session — cloudscraper when available."""
    if _HAS_CLOUDSCRAPER:
        sess = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows",
                     "mobile": False})
    else:
        sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT,
                         "Accept-Language": "en-US,en;q=0.9"})
    return sess


def session_kind() -> str:
    return "cloudscraper" if _HAS_CLOUDSCRAPER else "requests"


def fund_code(ref) -> str | None:
    """Numeric VR fund code from a fund page URL or a plain code string."""
    s = str(ref or "").strip()
    m = re.search(r"/funds/(\d+)", s)
    if m:
        return m.group(1)
    return s if s.isdigit() else None


def _looks_like_challenge(text: str) -> bool:
    t = (text or "")[:4000].lower()
    return ("just a moment" in t or "cf-browser-verification" in t
            or "challenge-platform" in t or "attention required" in t)


def _get(sess, url: str, code: str, as_json: bool, xhr: bool = False):
    """GET with a small retry loop. Returns parsed JSON or text."""
    headers = {
        "Accept": ("application/json, text/plain, */*" if as_json else
                   "text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "*/*;q=0.8"),
        "Referer": f"{VR_BASE}/funds/{code}/",
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
    last: Exception = VRError("no attempt made")
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=TIMEOUT, headers=headers)
            if r.status_code in (403, 429, 500, 502, 503, 504):
                last = VRError(f"HTTP {r.status_code}"
                               + (" (bot protection?)"
                                  if r.status_code in (403, 503) else ""))
                time.sleep(1.2 * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.text
            if _looks_like_challenge(text):
                raise VRError("VR returned a bot-check page")
            return r.json() if as_json else text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise last


# --------------------------------------------------------------------------- #
# Pure parsers (no network — testable offline)
# --------------------------------------------------------------------------- #
def _num(v) -> float:
    """Null-safe float for JSON values: None / missing / '' -> 0.0."""
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def parse_port_aggregates(data: dict) -> tuple[float, float, float]:
    """port-aggregates-chart JSON -> (large, mid, small) as % of equity.

    A bucket the fund doesn't hold comes back null/missing -> 0.
    """
    def pct(key):
        node = data.get(key) or {}
        return round(_num(node.get("fund_value")), 4)
    return (pct("large_percentage"), pct("mid_percentage"),
            pct("small_percentage"))


def parse_asset_allocation(data: dict) -> float | None:
    """asset-allocation-chart JSON -> Debt & Cash % (= 100 - equity)."""
    items = data.get("data") or []
    if not items:
        return None
    equity = 0.0
    for item in items:
        code = str(item.get("code", "")).lower()
        name = str(item.get("name", "")).strip().lower()
        if code == "eq" or name == "equity":
            equity += _num(item.get("y", 0))
    return round(100.0 - equity, 4)


def parse_sector_chart(data: dict) -> tuple[dict, dict, str, str]:
    """sector-chart JSON -> (canonical sectors %, unmapped rows %,
    as-on ISO date, fund name)."""
    cats = data.get("categories") or []
    series = data.get("series") or []
    fund_series = (series[0].get("data") or []) if series else []
    sectors: dict[str, float] = {}
    extra: dict[str, float] = {}
    for name, val in zip(cats, fund_series):
        pct = round(_num(val), 4)
        canonical = _SECTOR_LOOKUP.get(
            _norm_label(str(name).replace("&amp;", "&")))
        if canonical is not None:
            sectors[canonical] = round(sectors.get(canonical, 0.0) + pct, 4)
        elif pct > 0:
            extra[str(name)] = pct
    fund_name = ""
    try:
        fund_name = str(series[0]["name"]).split(" - ")[0].strip()
    except (IndexError, KeyError, TypeError):
        pass
    return sectors, extra, str(data.get("date", ""))[:10], fund_name


def _unwrap_fragment(text: str) -> str:
    """fund-details returns JSON {"tab_html": "..."} — return the inner
    HTML fragment (or the text unchanged if it's already HTML)."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("tab_html"):
            return obj["tab_html"]
    except ValueError:
        pass
    m = re.search(r'"tab_html"\s*:\s*"(.*?)","selected_tab"', text, re.S)
    return m.group(1) if m else text


def _clean_fragment(text: str) -> str:
    """Escaped fragment -> parseable HTML: decode \\uXXXX and entities,
    drop escaping backslashes, repair mixed attribute quotes."""
    frag = _unwrap_fragment(text)
    frag = re.sub(r"\\u([0-9a-fA-F]{4})",
                  lambda m: chr(int(m.group(1), 16)), frag)
    frag = frag.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")
    frag = frag.replace("\\", "")
    frag = html_lib.unescape(frag)
    frag = frag.replace("='\"", '="').replace("\"'", '"')
    return frag


def _first_number(s: str | None) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", s or "")
    return float(m.group()) if m else None


def extract_pb_pe(text: str) -> tuple[float | None, float | None]:
    """HTML/JSON fragment -> (pb, pe). Value lives in <p class="middle">
    right after the <p class="top">…Ratio label; falls back to a
    visible-text search."""
    soup = BeautifulSoup(_clean_fragment(text), "html.parser")

    def by_class(label):
        for p in soup.find_all(class_="top"):
            if label.lower() in p.get_text(" ", strip=True).lower():
                mid = p.find_next(class_="middle")
                if mid:
                    v = _first_number(mid.get_text(" ", strip=True))
                    if v is not None:
                        return v
        return None

    txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    def by_text(labels):
        # the value sits just before its "Category" comparator
        for lab in labels:
            j = txt.find(lab)
            if j >= 0:
                m = re.search(r"(\d+(?:\.\d+)?)\s*Category", txt[j:j + 400])
                if m:
                    return float(m.group(1))
        return None

    pb = by_class("P/B Ratio")
    if pb is None:
        pb = by_text(["Portfolio P/B Ratio", "P/B Ratio", "Price to Book"])
    pe = by_class("P/E Ratio")
    if pe is None:
        pe = by_text(["Portfolio P/E Ratio", "P/E Ratio",
                      "Price to Earnings"])
    return pb, pe


def extract_aum(text: str) -> float | None:
    """HTML/JSON fragment -> AUM in ₹ cr. Anchors on an Assets/Fund Size/
    AUM/Net Assets label near a ₹..Cr value; skips Avg Mkt Cap."""
    tx = BeautifulSoup(_clean_fragment(text),
                       "html.parser").get_text(" ", strip=True)
    tx = re.sub(r"\s+", " ", tx)
    labels = r"(?:Net\s+Assets|Fund\s+Size|Assets|AUM)"
    val = r"(?:₹|Rs\.?)?\s*([\d,]+(?:\.\d+)?)\s*Cr"
    for pat in (labels + r"\D{0,120}?" + val, val + r"\D{0,60}?" + labels):
        for m in re.finditer(pat, tx, re.S | re.I):
            window = tx[max(0, m.start() - 40):m.end()].lower()
            if "avg" in window or "average" in window or "category" in window:
                continue
            num = float(m.group(1).replace(",", ""))
            return num
    return None


def pretty_date(iso) -> str:
    """'2026-06-30' -> '30-Jun-2026' (VR's 'As on' style)."""
    try:
        return dt.datetime.strptime(str(iso)[:10],
                                    "%Y-%m-%d").strftime("%d-%b-%Y")
    except ValueError:
        return str(iso) if iso else ""


# --------------------------------------------------------------------------- #
# Per-fund fetch
# --------------------------------------------------------------------------- #
def fetch_fund(sess, code) -> dict:
    """Canonical parameter dict (vr_data.blank_params shape, 0-100 scale)
    for one numeric VR fund code, plus 'warnings' and 'fund_name'.

    Each endpoint degrades independently into a warning; VRError is raised
    only when *every* endpoint failed (network down / bot wall), so the
    caller can tell "this host is blocked" from "one field is missing".
    """
    if BeautifulSoup is None:
        raise VRError("beautifulsoup4 is not installed — "
                      "pip install beautifulsoup4")
    code = fund_code(code)
    if not code:
        raise VRError("not a VR fund code or fund page URL")
    p = blank_params()
    p.update({"warnings": [], "fund_name": "",
              "url": f"{VR_BASE}/funds/{code}/"})
    got_any = False

    # --- market-cap split ---
    try:
        data = _get(sess, f"{VR_BASE}/api/funds/port-aggregates-chart/{code}",
                    code, True)
        p["large"], p["mid"], p["small"] = parse_port_aggregates(data)
        got_any = True
    except Exception as e:  # noqa: BLE001
        p["warnings"].append(f"market-cap split failed: {e}")

    # --- debt & cash (100 - equity) ---
    try:
        data = _get(sess,
                    f"{VR_BASE}/api/funds/asset-allocation-chart/{code}",
                    code, True)
        p["debt_cash"] = parse_asset_allocation(data)
        got_any = got_any or p["debt_cash"] is not None
        if p["as_of"] is None and data.get("date"):
            p["as_of"] = pretty_date(data.get("date"))
    except Exception as e:  # noqa: BLE001
        p["warnings"].append(f"asset allocation failed: {e}")

    # --- sectors + as-on date + fund name ---
    try:
        data = _get(sess, f"{VR_BASE}/api/funds/sector-chart/{code}",
                    code, True)
        sectors, extra, date, name = parse_sector_chart(data)
        p["sectors"], p["extra_sectors"] = sectors, extra
        p["fund_name"] = name
        if date:
            p["as_of"] = pretty_date(date)
        got_any = got_any or bool(sectors)
    except Exception as e:  # noqa: BLE001
        p["warnings"].append(f"sectors failed: {e}")

    # --- P/B & P/E (fund-portfolio fragment), AUM (overview fragment).
    #     Extractors run per response so the tab_html unwrap works. ---
    frags = {}
    for tag, url in (("portfolio",
                      f"{VR_BASE}/fund-details/{code}/?tab=fund-portfolio"),
                     ("overview",
                      f"{VR_BASE}/fund-details/{code}/?tab=overview")):
        try:
            frags[tag] = _get(sess, url, code, False, xhr=True)
            got_any = True
        except Exception as e:  # noqa: BLE001
            p["warnings"].append(f"{tag} page failed: {e}")
    for page in frags.values():
        if p["pb"] is None or p["pe"] is None:
            pb, pe = extract_pb_pe(page)
            p["pb"] = p["pb"] if p["pb"] is not None else pb
            p["pe"] = p["pe"] if p["pe"] is not None else pe
        if p["aum_cr"] is None:
            p["aum_cr"] = extract_aum(page)
    if frags:
        for field, key in (("P/B", "pb"), ("P/E", "pe"), ("AUM", "aum_cr")):
            if p[key] is None:
                p["warnings"].append(f"{field} not found in the VR page")

    if not got_any:
        raise VRError(
            "every VR endpoint failed for this fund — "
            + (p["warnings"][0] if p["warnings"] else "no response"))

    # sanity: mapped sectors + debt & cash should land near 100%
    if p["debt_cash"] is not None and p["sectors"]:
        total = p["debt_cash"] + sum(p["sectors"].values())
        if abs(total - 100.0) > 2.0:
            p["warnings"].append(
                f"sectors + debt & cash = {total:.2f}% (expected ~100% — "
                "a sector may be unmapped)")
    return p


def read_codes_csv(data: bytes) -> list[tuple[str, str | None]]:
    """(code, name-or-None) pairs from a fund-codes CSV.

    Accepts the desktop fetcher's template (a column whose header contains
    'code', optional column containing 'name'/'scheme') or a plain list of
    codes with no header. Tolerates stray columns and blank rows.
    """
    import csv
    import io

    rows = list(csv.reader(io.StringIO(
        data.decode("utf-8-sig", errors="replace"))))
    specs: list[tuple[str, str | None]] = []
    if not rows:
        return specs

    def norm(h):
        return re.sub(r"[^a-z]", "", str(h).lower())

    header = [norm(h) for h in rows[0]]
    code_idx = name_idx = None
    for i, h in enumerate(header):
        if code_idx is None and ("code" in h or h in ("id", "fundid")):
            code_idx = i
        if name_idx is None and ("name" in h or "scheme" in h):
            name_idx = i
    start = 1 if code_idx is not None else 0
    if code_idx is None:
        code_idx = 0                       # no header: first column is the code

    for row in rows[start:]:
        if not row:
            continue
        code = str(row[code_idx]).strip() if len(row) > code_idx else ""
        if not code.isdigit():             # tolerate stray columns
            nums = [str(c).strip() for c in row if str(c).strip().isdigit()]
            if not nums:
                continue
            code = nums[0]
        name = None
        if name_idx is not None and len(row) > name_idx:
            nm = str(row[name_idx]).strip()
            if nm and not nm.isdigit():
                name = nm
        if code not in (c for c, _ in specs):
            specs.append((code, name))
    return specs


def peek_fund(sess, code) -> tuple[str, str]:
    """One cheap JSON call -> (fund name, as-on date) for a code."""
    code = fund_code(code)
    if not code:
        raise VRError("not a VR fund code or fund page URL")
    data = _get(sess, f"{VR_BASE}/api/funds/sector-chart/{code}", code, True)
    _, _, date, name = parse_sector_chart(data)
    return name, pretty_date(date)


# --------------------------------------------------------------------------- #
# Diagnostics CLI
# --------------------------------------------------------------------------- #
def _main() -> None:  # pragma: no cover
    import sys

    ref = sys.argv[1] if len(sys.argv) > 1 else "16026"
    print(f"HTTP client: {session_kind()}")
    sess = create_session()
    p = fetch_fund(sess, ref)
    print(json.dumps(p, indent=2, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    _main()
