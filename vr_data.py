"""
vr_data.py — Value Research Online fund-portfolio client.

Fetches the per-fund parameters the monthly "PF Review" tab needs —
portfolio P/E and P/B, AUM, the large/mid/small-cap split, debt & cash and
the sector allocation — from a fund's page on valueresearchonline.com
(e.g. /funds/16026/hdfc-flexi-cap-fund-direct-plan/).

Access notes:
  * The portfolio section of VR fund pages sits behind a VR login, so all
    fetching goes through VRSession, authenticated with the *user's own*
    credentials. Credentials live in memory for the session only — they are
    never written to disk.
  * VR's markup changes without notice and the site runs bot protection.
    Parsing is therefore label-driven and defensive (same philosophy as
    holdings.py): fields are located by visible label text, tried against
    embedded JSON first and then the HTML, and a missing field degrades to
    None instead of failing the whole fetch.
  * If scripted login is refused (captcha / bot wall), the user can paste
    the Cookie header from their logged-in browser instead — see
    VRSession.set_cookie_header.

Diagnostics (run from a machine that can reach VR):
    python vr_data.py 16026 --email you@example.com --password '...'
    python vr_data.py <fund url> --cookie 'sessionid=...; csrftoken=...'
prints every parsed field and saves the raw HTML next to the script so
selector fixes can be made against the real markup.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote_plus, urljoin

import requests

try:  # bs4 is required for parsing; import error surfaces at fetch time
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

VR_BASE = "https://www.valueresearchonline.com"
VR_LOGIN_PATHS = ("/login/", "/accounts/login/", "/membership/login/")
# autocomplete endpoints seen on VR over time; tried in order
VR_SEARCH_PATHS = (
    "/api/search-suggestions/?q={q}",
    "/api/search/?q={q}",
    "/search/?q={q}",
)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "application/json;q=0.8,*/*;q=0.7"),
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": VR_BASE + "/",
}
TIMEOUT = 30
REQUEST_DELAY = 1.5          # polite delay between VR page fetches


class VRError(RuntimeError):
    """Raised when Value Research can't be reached, logged into or parsed."""


# --------------------------------------------------------------------------- #
# Canonical schema — mirrors the PF-Review Excel columns exactly
# --------------------------------------------------------------------------- #
SECTOR_COLS = ["Financial", "Cap Goods", "Automo", "Tech", "Healthcare",
               "Real Estate", "Energy & Utilities", "Con Staples",
               "Materials", "Services", "Construction", "Chemicals",
               "Con Discretionary", "Industrial", "Communication",
               "Metals & Mining", "Insurance", "Textiles", "Diversified"]

# VR sector label (lowercased, punctuation collapsed) -> canonical column.
# VR has renamed sectors over the years; all known variants are listed.
_SECTOR_VARIANTS = {
    "Financial": ("financial", "financials", "financial services", "banks"),
    "Cap Goods": ("capital goods", "cap goods"),
    "Automo": ("automobile", "automobiles", "auto",
               "automobile & ancillaries"),
    "Tech": ("technology", "information technology", "it"),
    "Healthcare": ("healthcare", "health care", "pharmaceuticals", "pharma"),
    "Real Estate": ("realty", "real estate"),
    "Energy & Utilities": ("energy & utilities", "energy and utilities",
                           "energy", "utilities", "oil & gas", "power"),
    "Con Staples": ("consumer staples", "fmcg"),
    "Materials": ("materials", "construction materials", "cement"),
    "Services": ("services",),
    "Construction": ("construction", "infrastructure"),
    "Chemicals": ("chemicals",),
    "Con Discretionary": ("consumer discretionary", "cons discretionary",
                          "consumer durables"),
    "Industrial": ("industrial", "industrials"),
    "Communication": ("communication", "communication services", "telecom"),
    "Metals & Mining": ("metals & mining", "metals and mining", "metals"),
    "Insurance": ("insurance",),
    "Textiles": ("textiles", "textile"),
    "Diversified": ("diversified",),
}
_SECTOR_LOOKUP = {v: k for k, vs in _SECTOR_VARIANTS.items() for v in vs}

_CAP_LABELS = {"giant": "giant", "large": "large", "largecap": "large",
               "mid": "mid", "midcap": "mid", "small": "small",
               "smallcap": "small", "tiny": "tiny", "micro": "tiny"}


def _norm_label(s: str) -> str:
    """Lowercase, drop parentheticals/'%', collapse punctuation."""
    s = re.sub(r"\(.*?\)", " ", str(s).lower()).replace("%", " ")
    s = re.sub(r"[^a-z&]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_cap_label(norm: str) -> str | None:
    """'large cap'/'large cap stocks'/'giant' -> canonical cap bucket."""
    words = [w for w in norm.split() if w not in ("stocks", "stock")]
    if 1 <= len(words) <= 2 and words[-1:] == ["cap"]:
        words = words[:-1]
    if len(words) == 1:
        return _CAP_LABELS.get(words[0])
    return None


def _to_float(x):
    """'36.30%', '₹1,06,496 Cr', '1,668' -> float. None when not numeric."""
    if x is None:
        return None
    s = re.sub(r"[₹,\s]", "", str(x)).replace("%", "")
    s = re.sub(r"(?i)(cr|crore|cr\.)$", "", s)
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def blank_params() -> dict:
    """The canonical per-fund record, all fields None/empty."""
    return {"pe": None, "pb": None, "aum_cr": None,
            "large": None, "mid": None, "small": None, "debt_cash": None,
            "sectors": {}, "extra_sectors": {}, "as_of": None, "url": None}


# --------------------------------------------------------------------------- #
# Session (login / cookie handling)
# --------------------------------------------------------------------------- #
class VRSession:
    """Authenticated requests session against valueresearchonline.com."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._logged_in = False

    # -- auth ------------------------------------------------------------- #
    def login(self, email: str, password: str) -> None:
        """Form login with the user's own VR credentials.

        Django-style flow: GET the login page for the CSRF token, POST the
        form, then verify by probing for logged-in markers. Raises VRError
        with a readable message on any failure (incl. bot walls).
        """
        if not email or not password:
            raise VRError("Value Research email and password are required.")
        last = None
        for path in VR_LOGIN_PATHS:
            url = VR_BASE + path
            try:
                r = self.s.get(url, timeout=TIMEOUT)
                if r.status_code in (403, 503):
                    last = f"{path}: HTTP {r.status_code} (bot protection?)"
                    continue
                if r.status_code == 404:
                    last = f"{path}: HTTP 404"
                    continue
                token = self._csrf_token(r.text)
                form = {"username": email, "email": email,
                        "password": password}
                if token:
                    form["csrfmiddlewaretoken"] = token
                time.sleep(0.5)
                p = self.s.post(url, data=form, timeout=TIMEOUT,
                                headers={"Referer": url},
                                allow_redirects=True)
                if p.status_code in (403, 503):
                    last = f"{path}: POST HTTP {p.status_code}"
                    continue
                if self._looks_logged_in(p.text) or self.check_login():
                    self._logged_in = True
                    return
                last = f"{path}: credentials not accepted"
            except requests.RequestException as e:
                last = f"{path}: {type(e).__name__}: {e}"
        raise VRError(
            f"Value Research login failed ({last}). If your credentials are "
            "correct, VR may be blocking scripted logins — log in from your "
            "browser and paste its Cookie header instead.")

    def set_cookie_header(self, cookie_header: str) -> None:
        """Use cookies pasted from a logged-in browser ('a=1; b=2 ...')."""
        n = 0
        for part in str(cookie_header).split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    self.s.cookies.set(k.strip(), v.strip(),
                                       domain=".valueresearchonline.com")
                    n += 1
        if n == 0:
            raise VRError("No cookies found in the pasted text — copy the "
                          "full Cookie header from your browser's dev tools.")
        self._logged_in = True   # verified lazily on first fetch

    def check_login(self) -> bool:
        """Probe a member page for logged-in markers."""
        try:
            r = self.s.get(VR_BASE + "/", timeout=TIMEOUT)
            return self._looks_logged_in(r.text)
        except requests.RequestException:
            return False

    @staticmethod
    def _csrf_token(html: str) -> str | None:
        m = re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']'
                      r'([^"\']+)', html)
        return m.group(1) if m else None

    @staticmethod
    def _looks_logged_in(html: str) -> bool:
        low = (html or "").lower()
        return any(k in low for k in ("logout", "log out", "my account",
                                      "my-account", "sign out"))

    # -- fetching --------------------------------------------------------- #
    def get(self, url: str) -> requests.Response:
        r = self.s.get(url, timeout=TIMEOUT)
        if r.status_code in (403, 503):
            raise VRError(f"VR refused the request (HTTP {r.status_code}) — "
                          "likely bot protection. Try the cookie-paste "
                          "option, or fetch less frequently.")
        r.raise_for_status()
        return r

    def search_funds(self, query: str) -> list[tuple[str, str]]:
        """[(fund name, fund page url)] candidates for a scheme name."""
        q = quote_plus(str(query).strip())
        for path in VR_SEARCH_PATHS:
            try:
                r = self.get(VR_BASE + path.format(q=q))
            except (VRError, requests.RequestException):
                continue
            out = _parse_search_results(r.text, r.headers.get("content-type"))
            if out:
                return out
        return []

    def fetch_fund(self, fund_ref: str) -> dict:
        """Canonical parameter dict for a fund page URL or numeric VR id."""
        url = fund_url(fund_ref)
        time.sleep(REQUEST_DELAY)
        r = self.get(url)
        html = r.text
        if _is_login_wall(html):
            raise VRError("VR served the login wall for this page — the "
                          "session isn't (or is no longer) logged in.")
        params = parse_fund_page(html)
        params["url"] = r.url or url
        return params


def fund_url(fund_ref: str) -> str:
    """Accept a full VR fund URL, a '/funds/...' path, or a numeric id."""
    s = str(fund_ref).strip()
    if s.startswith("http"):
        return s
    if s.startswith("/"):
        return urljoin(VR_BASE, s)
    if s.isdigit():
        return f"{VR_BASE}/funds/{s}/"
    raise VRError(f"'{fund_ref}' is neither a VR fund URL nor a fund id.")


def _is_login_wall(html: str) -> bool:
    low = (html or "").lower()
    has_login = ("csrfmiddlewaretoken" in low
                 and ('type="password"' in low or "type='password'" in low))
    return has_login and "sector" not in low


def _parse_search_results(body: str, content_type: str | None
                          ) -> list[tuple[str, str]]:
    """Fund candidates from a search/autocomplete response (JSON or HTML)."""
    out, seen = [], set()

    def _add(name, url):
        name, url = str(name).strip(), str(url).strip()
        if name and "/funds/" in url and url not in seen:
            seen.add(url)
            out.append((name, urljoin(VR_BASE, url)))

    if "json" in (content_type or "") or (body or "").lstrip()[:1] in "[{":
        try:
            js = json.loads(body)
        except ValueError:
            js = None
        if js is not None:
            for d in _walk_dicts(js):
                url = next((d[k] for k in ("url", "link", "href", "slug")
                            if isinstance(d.get(k), str)), "")
                name = next((d[k] for k in ("name", "title", "label", "text",
                                            "fund_name", "value")
                             if isinstance(d.get(k), str)), "")
                if url and name:
                    _add(name, url)
            return out
    if BeautifulSoup is not None and body:
        soup = BeautifulSoup(body, "lxml")
        for a in soup.find_all("a", href=True):
            if "/funds/" in a["href"]:
                _add(a.get_text(" ", strip=True), a["href"])
    return out


# --------------------------------------------------------------------------- #
# Page parsing (pure — testable offline against saved HTML)
# --------------------------------------------------------------------------- #
def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def _json_islands(html: str) -> list:
    """Embedded JSON blobs (__NEXT_DATA__, ld+json, big inline objects)."""
    out = []
    for m in re.finditer(
            r'<script[^>]*(?:id="__NEXT_DATA__"|type="application/(?:ld\+)?'
            r'json)"[^>]*>(.*?)</script>', html or "", re.S):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            pass
    return out


def _from_json_islands(html: str, params: dict) -> None:
    """Best-effort fill from embedded JSON; HTML parsing still runs after."""
    for island in _json_islands(html):
        for d in _walk_dicts(island):
            if params["pe"] is None:
                params["pe"] = _to_float(
                    next((d[k] for k in ("pe", "pe_ratio", "peRatio",
                                         "portfolio_pe") if k in d), None))
            if params["pb"] is None:
                params["pb"] = _to_float(
                    next((d[k] for k in ("pb", "pb_ratio", "pbRatio",
                                         "portfolio_pb") if k in d), None))
            if params["aum_cr"] is None:
                params["aum_cr"] = _to_float(
                    next((d[k] for k in ("aum", "fund_size", "fundSize",
                                         "net_assets") if k in d), None))


def _label_number(soup, patterns: tuple[str, ...]):
    """The first number appearing shortly *after* a matching label.

    Only text following the label (within a short window) is considered, so
    two stats sharing one container ('P/E Ratio 23.45 P/B Ratio 3.35') don't
    contaminate each other. Covers <tr><td>label</td><td>value</td></tr>,
    <li>label <span>value</span></li> and dt/dd layouts alike.
    """
    rx = re.compile("|".join(patterns), re.I)

    def _after(text: str):
        m = rx.search(text or "")
        return _to_float(text[m.end():m.end() + 40]) if m else None

    for el in soup.find_all(string=rx):
        v = _after(str(el))
        if v is not None:
            return v
        parent = el.find_parent(["tr", "li", "dt", "p", "th", "td", "div"])
        for container in (parent, parent.parent if parent is not None
                          else None):
            if container is not None:
                v = _after(container.get_text(" ", strip=True))
                if v is not None:
                    return v
        # dt/dd and label-above-value layouts: value in the next sibling
        if parent is not None:
            sib = parent.find_next_sibling()
            if sib is not None:
                v = _to_float(sib.get_text(" ", strip=True))
                if v is not None:
                    return v
    return None


def _row_pairs(soup):
    """(label, value%) pairs from every table row / list item on the page."""
    pairs = []
    for row in soup.find_all(["tr", "li"]):
        cells = row.find_all(["td", "th", "span", "div"], recursive=True)
        texts = [c.get_text(" ", strip=True) for c in cells]
        texts = [t for t in texts if t]
        if len(texts) < 2:
            text = row.get_text(" ", strip=True)
            m = re.match(r"(.+?)\s+(-?\d+(?:\.\d+)?)\s*%?$", text)
            if m:
                texts = [m.group(1), m.group(2)]
            else:
                continue
        label, value = texts[0], _to_float(texts[1])
        if value is None:                      # label ... value at the end
            value = _to_float(texts[-1])
        if label and value is not None:
            pairs.append((label, value))
    return pairs


def parse_fund_page(html: str) -> dict:
    """Extract the canonical parameter dict from a VR fund page's HTML.

    Every field degrades to None (or {}) when not found — the caller/UI
    decides whether that's fatal. Percentages are returned on a 0–100 scale.
    """
    if BeautifulSoup is None:
        raise VRError("beautifulsoup4 is not installed — "
                      "pip install beautifulsoup4 lxml")
    params = blank_params()
    _from_json_islands(html, params)
    soup = BeautifulSoup(html or "", "lxml")

    if params["pe"] is None:
        params["pe"] = _label_number(soup, (r"P/?E\s*Ratio", r"\bP/E\b"))
    if params["pb"] is None:
        params["pb"] = _label_number(soup, (r"P/?B\s*Ratio", r"\bP/B\b"))
    if params["aum_cr"] is None:
        params["aum_cr"] = _label_number(
            soup, (r"Fund\s*Size", r"\bAUM\b", r"Net\s*Assets"))

    caps, sectors, extra = {}, {}, {}
    seen_raw = set()          # sector rows repeat (chart legend + table) —
    equity = debt = cash = None    # count each *distinct label* once
    for label, value in _row_pairs(soup):
        norm = _norm_label(label)
        if not norm or value < 0 or value > 100:
            continue
        cap = _norm_cap_label(norm)
        if cap is not None:
            caps.setdefault(cap, value)
            continue
        if norm == "equity" and equity is None:
            equity = value
            continue
        if norm == "debt" and debt is None:
            debt = value
            continue
        if norm.startswith("cash") and cash is None:
            cash = value
            continue
        canonical = _SECTOR_LOOKUP.get(norm)
        if canonical is not None:
            if (canonical, norm) not in seen_raw:   # 'Energy' + 'Utilities'
                seen_raw.add((canonical, norm))     # sum; repeats don't
                sectors[canonical] = sectors.get(canonical, 0.0) + value
        elif norm in ("others", "other") and label.strip() not in extra:
            extra[label.strip()] = value

    # 5-bucket (giant..tiny) collapses into the sheet's 3 buckets; a bucket
    # missing while others parsed means it genuinely holds 0%
    if caps:
        params["large"] = round(caps.get("giant", 0.0)
                                + caps.get("large", 0.0), 4)
        params["mid"] = caps.get("mid", 0.0)
        params["small"] = round(caps.get("small", 0.0)
                                + caps.get("tiny", 0.0), 4)
    if debt is not None or cash is not None:
        params["debt_cash"] = round((debt or 0.0) + (cash or 0.0), 4)
    elif equity is not None:
        params["debt_cash"] = round(100.0 - equity, 4)

    params["sectors"] = {k: round(v, 4) for k, v in sectors.items()}
    params["extra_sectors"] = {k: round(v, 4) for k, v in extra.items()}

    m = re.search(r"as\s+on\s+([0-9]{1,2}[-/ ][A-Za-z0-9]{2,9}[-/ ]"
                  r"[0-9]{2,4})", soup.get_text(" ", strip=True), re.I)
    params["as_of"] = m.group(1) if m else None
    return params


# --------------------------------------------------------------------------- #
# Diagnostics CLI — run on a machine that can reach VR
# --------------------------------------------------------------------------- #
def _main() -> None:  # pragma: no cover
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(
        description="Probe a Value Research fund page and print what parses.")
    ap.add_argument("fund", help="VR fund URL or numeric fund id (e.g. 16026)")
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--cookie", help="Cookie header from a logged-in browser")
    args = ap.parse_args()

    sess = VRSession()
    if args.cookie:
        sess.set_cookie_header(args.cookie)
    elif args.email:
        sess.login(args.email, args.password or "")
        print("login: OK")
    url = fund_url(args.fund)
    r = sess.get(url)
    dump = Path(__file__).with_name("vr_page_dump.html")
    dump.write_text(r.text, encoding="utf-8")
    print(f"fetched {r.url} ({len(r.text):,} bytes) -> {dump}")
    if _is_login_wall(r.text):
        print("WARNING: page looks like a login wall")
    out = parse_fund_page(r.text)
    out["url"] = r.url
    print(json.dumps(out, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _main()
