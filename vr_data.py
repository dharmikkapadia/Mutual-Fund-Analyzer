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
# fund-specific portfolio JSON (confirmed live July 2026) — the fund page
# itself is only a skeleton that this endpoint fills in via AJAX
VR_API_PORTFOLIO = VR_BASE + "/api/funds/{id}/portfolio/"
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

    def fetch_portfolio_json(self, fund_id: str, referer: str | None = None):
        """Raw JSON from the fund's portfolio API, or None on any failure."""
        try:
            time.sleep(REQUEST_DELAY)
            r = self.s.get(VR_API_PORTFOLIO.format(id=fund_id),
                           timeout=TIMEOUT,
                           headers={"Accept": "application/json, */*",
                                    "Referer": referer or VR_BASE + "/",
                                    "X-Requested-With": "XMLHttpRequest"})
            if r.ok and "json" in (r.headers.get("content-type") or ""):
                return r.json()
        except (requests.RequestException, ValueError):
            pass
        return None

    def fetch_fund(self, fund_ref: str) -> dict:
        """Canonical parameter dict for a fund page URL or numeric VR id.

        The portfolio API endpoint is the primary source (the fund page is
        an AJAX-filled skeleton); the page HTML is fetched only to fill
        fields the API parse missed.
        """
        url = fund_url(fund_ref)
        fid = _fund_id(url)
        api_p = None
        if fid:
            js = self.fetch_portfolio_json(fid, referer=url)
            if js is not None:
                api_p = parse_portfolio_api(js)
        page_p = None
        if _needs_page(api_p):
            time.sleep(REQUEST_DELAY)
            r = self.get(url)
            if api_p is None and _is_login_wall(r.text):
                raise VRError("VR served the login wall for this page — the "
                              "session isn't (or is no longer) logged in.")
            page_p = parse_fund_page(r.text)
            page_p["url"] = r.url or url
        return _merge_params(api_p, page_p, url)


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
        if _to_float(label) is not None:       # value-first layouts, e.g.
            num = _to_float(label)             # '94.04% Equity'
            word = next((t for t in texts[1:]
                         if _to_float(t) is None and t), None)
            if word is not None:
                label, value = word, num
        if label and value is not None and _to_float(label) is None:
            pairs.append((label, value))
    return pairs


# -- scoped extractors for VR's current fund-page DOM (July 2026) -------- #
# Verified against the live page structure:
#   * Concentration cards:  div.content > p.top (label) + p.middle (value)
#   * Portfolio aggregates: #portfolio_tab .info > p.info-title +
#     p.info-value ('Large 76.75%'; p.info-value-name holds the category
#     figure and must be ignored)
#   * Asset allocation:     ul.portfolio-tab-list li > span.portfolio-head
#     (value) + span.portfolio-subhead (label) — value comes FIRST, and the
#     buckets include 'Real Estate', which also exists as a *sector* name —
#     so asset buckets must never leak into the sector map
#   * Sectors:              table#sector_wise-holding_table rows
#     [Sector, Fund %, Category %]; Fund % is already % of total assets
#   * As-on date:           p#top-holding-as-on-date under Top Holdings
def _scoped_stat_cards(soup, params: dict) -> None:
    for content in soup.select("div.content"):
        top = content.find("p", class_="top")
        mid = content.find("p", class_="middle")
        if top is None or mid is None:
            continue
        label = top.get_text(" ", strip=True).lower()
        val = _to_float(mid.get_text(" ", strip=True))
        if val is None:
            continue
        if "p/e" in label and params["pe"] is None:
            params["pe"] = val
        elif "p/b" in label and params["pb"] is None:
            params["pb"] = val


def _scoped_caps(soup) -> dict:
    out = {}
    for info in soup.select("#portfolio_tab .info"):
        t = info.find("p", class_="info-title")
        v = info.find("p", class_="info-value")
        if t is None or v is None:
            continue
        cap = _norm_cap_label(_norm_label(t.get_text(" ", strip=True)))
        fv = _to_float(v.get_text(" ", strip=True))
        if cap is not None and fv is not None and 0 <= fv <= 100:
            out.setdefault(cap, fv)
    return out


def _scoped_assets(soup) -> dict:
    out = {}
    for li in soup.select("ul.portfolio-tab-list li"):
        head = li.find("span", class_="portfolio-head")
        sub = li.find("span", class_="portfolio-subhead")
        if head is None or sub is None:
            continue
        v = _to_float(head.get_text(" ", strip=True))
        lab = _norm_label(sub.get_text(" ", strip=True))
        if v is not None and lab and lab not in out:
            out[lab] = v
    return out


def _scoped_sectors(soup) -> tuple[dict, dict]:
    """(mapped sectors, unmapped rows) from the sector-holdings table.

    DataTables renders sticky-header *clone* tables with the same classes
    but no data — parse every candidate and keep the fullest one.
    """
    best: tuple[dict, dict] = ({}, {})
    tables = soup.find_all("table", id="sector_wise-holding_table")
    tables += [t for t in soup.find_all("table")
               if t not in tables
               and "sector" in " ".join(t.get("class", [])).lower()]
    for table in tables:
        sectors, extra = {}, {}
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = [td.get_text(" ", strip=True)
                     for td in tr.find_all(["td", "th"])]
            if len(cells) < 2 or not cells[0]:
                continue
            val = _to_float(cells[1])
            if val is None or not 0 <= val <= 100:
                continue
            canonical = _SECTOR_LOOKUP.get(_norm_label(cells[0]))
            if canonical is not None:
                sectors[canonical] = sectors.get(canonical, 0.0) + val
            else:
                extra.setdefault(cells[0], val)
        if len(sectors) > len(best[0]):
            best = (sectors, extra)
    return best


def _scoped_as_of(soup) -> str | None:
    rx = re.compile(r"as\s+on\s+([0-9]{1,2}[-/ ][A-Za-z0-9]{2,9}[-/ ]"
                    r"[0-9]{2,4})", re.I)
    el = soup.find(id="top-holding-as-on-date")
    scopes = [el] + soup.select("section.portfolio-tab-container")
    for scope in scopes:
        if scope is None:
            continue
        m = rx.search(scope.get_text(" ", strip=True))
        if m:
            return m.group(1)
    return None


def parse_fund_page(html: str) -> dict:
    """Extract the canonical parameter dict from a VR fund page's HTML.

    Scoped extractors for VR's known DOM run first; generic label-driven
    heuristics fill anything still missing (so older/altered layouts and
    HTML fragments from data endpoints keep working). Every field degrades
    to None (or {}) when not found — the caller/UI decides whether that's
    fatal. Percentages are returned on a 0–100 scale.
    """
    if BeautifulSoup is None:
        raise VRError("beautifulsoup4 is not installed — "
                      "pip install beautifulsoup4 lxml")
    params = blank_params()
    _from_json_islands(html, params)
    soup = BeautifulSoup(html or "", "lxml")

    _scoped_stat_cards(soup, params)
    if params["pe"] is None:
        params["pe"] = _label_number(soup, (r"P/?E\s*Ratio", r"\bP/E\b"))
    if params["pb"] is None:
        params["pb"] = _label_number(soup, (r"P/?B\s*Ratio", r"\bP/B\b"))
    if params["aum_cr"] is None:
        params["aum_cr"] = _label_number(
            soup, (r"Fund\s*Size", r"\bAUM\b", r"Net\s*Assets",
                   r"\bAssets\b"))

    caps = _scoped_caps(soup)
    assets = _scoped_assets(soup)
    sectors, extra = _scoped_sectors(soup)
    equity = assets.get("equity")
    debt = assets.get("debt")
    cash = next((v for k, v in assets.items() if k.startswith("cash")), None)

    # generic label-driven sweep fills whatever the scoped passes missed
    caps_scoped = bool(caps)
    sectors_scoped = bool(sectors)
    if not caps or not sectors or equity is None:
        seen_raw = set()      # sector rows repeat (chart legend + table) —
        for label, value in _row_pairs(soup):   # count distinct labels once
            norm = _norm_label(label)
            if not norm or value < 0 or value > 100:
                continue
            cap = _norm_cap_label(norm)
            if cap is not None:
                if not caps_scoped:
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
            if sectors_scoped:    # authoritative table already parsed
                continue
            canonical = _SECTOR_LOOKUP.get(norm)
            if canonical is not None:
                if (canonical, norm) not in seen_raw:  # 'Energy'+'Utilities'
                    seen_raw.add((canonical, norm))    # sum; repeats don't
                    sectors[canonical] = sectors.get(canonical, 0.0) + value
            elif norm in ("others", "other") and label.strip() not in extra:
                extra[label.strip()] = value

    _apply_allocations(params, caps, equity, debt, cash, sectors, extra)

    params["as_of"] = _scoped_as_of(soup)
    if params["as_of"] is None:
        m = re.search(r"as\s+on\s+([0-9]{1,2}[-/ ][A-Za-z0-9]{2,9}[-/ ]"
                      r"[0-9]{2,4})", soup.get_text(" ", strip=True), re.I)
        params["as_of"] = m.group(1) if m else None
    return params


def _apply_allocations(params: dict, caps: dict, equity, debt, cash,
                       sectors: dict, extra: dict) -> None:
    """Shared finalisation for both the HTML and the JSON parse paths."""
    # 5-bucket (giant..tiny) collapses into the sheet's 3 buckets; a bucket
    # missing while others parsed means it genuinely holds 0%
    if caps:
        params["large"] = round(caps.get("giant", 0.0)
                                + caps.get("large", 0.0), 4)
        params["mid"] = caps.get("mid", 0.0)
        params["small"] = round(caps.get("small", 0.0)
                                + caps.get("tiny", 0.0), 4)
    # the review sheet's 'Debt & Cash' is everything that isn't equity
    # (VR's asset buckets include Debt, Real Estate and Cash & Cash Eq.)
    if equity is not None:
        params["debt_cash"] = round(100.0 - equity, 4)
    elif debt is not None or cash is not None:
        params["debt_cash"] = round((debt or 0.0) + (cash or 0.0), 4)
    params["sectors"] = {k: round(v, 4) for k, v in sectors.items()}
    params["extra_sectors"] = {k: round(v, 4) for k, v in extra.items()}


# --------------------------------------------------------------------------- #
# Portfolio API JSON parsing (pure — testable offline against saved JSON)
# --------------------------------------------------------------------------- #
_HOLDING_KEYS = {"company", "company_name", "compname", "stock", "stock_name",
                 "isin", "holding_name", "market_value", "no_of_shares"}
_LABEL_FIELDS = ("name", "label", "title", "sector_name", "sector",
                 "asset_type", "type", "category", "x")
_VALUE_FIELDS = ("value", "y", "percentage", "percent", "pct", "weight",
                 "allocation", "corpus_per", "holding")
_ASSET_LABELS = {"equity", "debt", "cash", "cash & cash eq", "real estate",
                 "others", "other"}


def _key_norm(k) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(k).lower()).strip("_")


def _json_lists(obj):
    """Every list-of-dicts anywhere in the tree."""
    if isinstance(obj, list):
        if obj and all(isinstance(i, dict) for i in obj):
            yield obj
        for v in obj:
            yield from _json_lists(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _json_lists(v)


def _list_pairs(lst: list) -> list[tuple[str, float]]:
    """(label, value) pairs from a list of dicts, or [] if it doesn't have
    a consistent label+value shape (or looks like a holdings list)."""
    for item in lst:
        if _HOLDING_KEYS & {_key_norm(k) for k in item}:
            return []
    lf = next((f for f in _LABEL_FIELDS
               if sum(isinstance(i.get(f), str) and i.get(f).strip() != ""
                      for i in lst) >= max(1, len(lst) - 1)), None)
    vf = next((f for f in _VALUE_FIELDS
               if sum(_to_float(i.get(f)) is not None for i in lst)
               >= max(1, len(lst) - 1)), None)
    if lf is None or vf is None:
        return []
    out = []
    for i in lst:
        v = _to_float(i.get(vf))
        if isinstance(i.get(lf), str) and v is not None and 0 <= v <= 100:
            out.append((i[lf].strip(), v))
    return out


def _is_asset_label(norm: str) -> bool:
    return norm in _ASSET_LABELS or norm.startswith("cash")


def _classify_pairs(pairs) -> tuple[str, dict, dict]:
    """('sectors'|'caps'|'assets'|'', mapped, extra) for a pair group.

    The group's kind is decided by majority vote first, then every label
    is mapped under that kind's vocabulary — so 'Real Estate' counts as a
    sector inside a sector list but as an asset bucket inside the asset
    list, and one ambiguous label can't flip a whole group.
    """
    votes = {"sectors": 0, "caps": 0, "assets": 0}
    for label, _ in pairs:
        norm = _norm_label(label)
        if _norm_cap_label(norm) is not None:
            votes["caps"] += 1
        elif _is_asset_label(norm):
            votes["assets"] += 1
        elif norm in _SECTOR_LOOKUP:
            votes["sectors"] += 1
    kind = max(votes, key=votes.get)          # ties favour 'sectors'
    if votes[kind] < 2:
        return "", {}, {}
    mapped, extra = {}, {}
    for label, value in pairs:
        norm = _norm_label(label)
        if kind == "caps":
            cap = _norm_cap_label(norm)
            if cap is not None:
                mapped.setdefault(cap, value)
            else:
                extra.setdefault(label, value)
        elif kind == "assets":
            if _is_asset_label(norm):
                mapped.setdefault(norm, value)
            else:
                extra.setdefault(label, value)
        else:
            canonical = _SECTOR_LOOKUP.get(norm)
            if canonical is not None:
                mapped[canonical] = mapped.get(canonical, 0.0) + value
            else:
                extra.setdefault(label, value)
    return kind, mapped, extra


def parse_portfolio_api(js) -> dict:
    """Canonical parameter dict from /api/funds/{id}/portfolio/ JSON.

    The exact schema isn't documented, so extraction is shape-driven:
    scalar stats come from fuzzy key matches (portfolio-prefixed keys win),
    allocations from any list-of-dicts / plain dict whose labels match the
    known cap-bucket, asset-bucket or sector vocabularies. Holdings lists
    (anything carrying company/ISIN fields) are excluded so stock-level
    figures can't pollute fund-level ones.
    """
    params = blank_params()

    # scalar stats — prefer explicitly portfolio-scoped keys
    for want_portfolio in (True, False):
        for d in _walk_dicts(js):
            for k, v in d.items():
                key = _key_norm(k)
                if want_portfolio and "portfolio" not in key:
                    continue
                fv = _to_float(v)
                if fv is not None:
                    if params["pe"] is None and re.search(
                            r"(^|_)p_?e(_ratio)?$|price_earning", key):
                        params["pe"] = fv
                    elif params["pb"] is None and re.search(
                            r"(^|_)p_?b(_ratio)?$|price_(to_)?book", key):
                        params["pb"] = fv
                    elif params["aum_cr"] is None and re.search(
                            r"(^|_)aum($|_)|fund_size|net_assets", key):
                        params["aum_cr"] = fv
                if (params["as_of"] is None and isinstance(v, str)
                        and re.search(r"(^|_)(as_?on(_date)?|portfolio_date"
                                      r"|report_date|month_end)", key)
                        and re.search(r"\d", v)):
                    params["as_of"] = v.strip()

    caps, assets, sectors, extra = {}, {}, {}, {}
    for lst in _json_lists(js):
        pairs = _list_pairs(lst)
        if not pairs:
            continue
        kind, mapped, ex = _classify_pairs(pairs)
        if kind == "caps" and len(mapped) > len(caps):
            caps = mapped
        elif kind == "assets" and len(mapped) > len(assets):
            assets = mapped
        elif kind == "sectors" and len(mapped) > len(sectors):
            sectors, extra = mapped, ex

    # dict-shaped allocations ({'large': 76.75, ...} / {'equity': 94.04})
    for d in _walk_dicts(js):
        pairs = [(str(k), _to_float(v)) for k, v in d.items()
                 if _to_float(v) is not None and 0 <= _to_float(v) <= 100]
        if len(pairs) < 2:
            continue
        kind, mapped, ex = _classify_pairs(pairs)
        if kind == "caps" and len(mapped) > len(caps):
            caps = mapped
        elif kind == "assets" and len(mapped) > len(assets):
            assets = mapped
        elif kind == "sectors" and len(mapped) > len(sectors):
            sectors, extra = mapped, ex

    equity = assets.get("equity")
    debt = assets.get("debt")
    cash = next((v for k, v in assets.items() if k.startswith("cash")), None)
    _apply_allocations(params, caps, equity, debt, cash, sectors, extra)
    return params


def _fund_id(fund_ref) -> str | None:
    s = str(fund_ref).strip()
    m = re.search(r"/funds/(\d+)", s)
    if m:
        return m.group(1)
    return s if s.isdigit() else None


def _merge_params(api_p: dict | None, page_p: dict | None,
                  url: str) -> dict:
    """API values win; the page parse fills whatever the API lacked."""
    out = dict(api_p) if api_p else blank_params()
    if page_p:
        for k in ("pe", "pb", "aum_cr", "large", "mid", "small",
                  "debt_cash", "as_of"):
            if out.get(k) is None:
                out[k] = page_p.get(k)
        if not out.get("sectors"):
            out["sectors"] = page_p.get("sectors") or {}
            out["extra_sectors"] = page_p.get("extra_sectors") or {}
    out["url"] = (page_p or {}).get("url") or url
    return out


def _needs_page(p: dict | None) -> bool:
    return (p is None or not p.get("sectors")
            or any(p.get(k) is None for k in
                   ("pe", "pb", "aum_cr", "large", "debt_cash")))


# --------------------------------------------------------------------------- #
# Endpoint hunting — find where the page's JS loads the portfolio data from
# --------------------------------------------------------------------------- #
_HUNT_WORDS = ("portfolio", "sector", "holding", "aggregate", "asset",
               "allocation", "concentration", "fund-data", "snapshot")
_MARKERS = ("p/e", "p/b", "sector", "financial", "large", "equity")


def _candidate_urls(sess: VRSession, fund_id: str, html: str) -> list[str]:
    """URL-ish strings mentioning portfolio-ish words, from the page's
    inline scripts and its same-domain JS bundles, plus educated guesses."""
    texts = [html or ""]
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html or "", "lxml")
        srcs = [s.get("src") for s in soup.find_all("script", src=True)]
        srcs = [urljoin(VR_BASE, s) for s in srcs
                if s and ("valueresearch" in s or s.startswith("/"))]
        for s in srcs[:10]:
            try:
                time.sleep(0.5)
                b = sess.s.get(s, timeout=TIMEOUT)
                if b.ok and len(b.text) < 3_000_000:
                    texts.append(b.text)
            except requests.RequestException:
                pass
    found = set()
    rx = re.compile(r"""["']((?:https?://[^"']+|/[A-Za-z0-9_\-/{}$.]+))["']""")
    for t in texts:
        for u in rx.findall(t):
            low = u.lower()
            if any(w in low for w in _HUNT_WORDS) and not low.endswith(
                    (".js", ".css", ".png", ".svg", ".jpg", ".woff2")):
                for pat in ("{id}", "{fundId}", "${id}", "${fundId}",
                            "{{id}}", "FUND_ID"):
                    u = u.replace(pat, fund_id)
                found.add(urljoin(VR_BASE, u))
    guesses = [
        f"/api/fund/{fund_id}/portfolio/", f"/api/fund/{fund_id}/",
        f"/funds/{fund_id}/portfolio-data/",
        f"/funds/portfolio-data/{fund_id}/",
        f"/funds/{fund_id}/fund-portfolio-data/",
        f"/api/funds/{fund_id}/portfolio/",
    ]
    found.update(VR_BASE + g for g in guesses)
    return sorted(found)[:30]


def hunt_endpoints(sess: VRSession, fund_ref: str, html: str) -> None:
    m = re.search(r"(\d{3,6})", str(fund_ref))
    fund_id = m.group(1) if m else str(fund_ref)
    cands = _candidate_urls(sess, fund_id, html)
    print(f"\n-- probing {len(cands)} candidate endpoints --")
    hits = []
    for u in cands:
        time.sleep(1.0)
        try:
            r = sess.s.get(u, timeout=TIMEOUT)
            body = r.text or ""
            low = body.lower()
            marks = [w for w in _MARKERS if w in low]
            line = (f"{r.status_code} {len(body):>8,}B "
                    f"{(r.headers.get('content-type') or '?')[:24]:24} {u}")
            if r.ok and marks:
                hits.append(u)
                line += f"   <-- markers: {','.join(marks)}"
            print(line)
            if r.ok and marks and "json" in (
                    r.headers.get("content-type") or ""):
                print("      preview:", body[:300].replace("\n", " "))
        except requests.RequestException as e:
            print(f"ERR  {type(e).__name__}: {u}")
    print("\npromising:" if hits else
          "\nno endpoint hit — send vr_page_dump.html for analysis")
    for u in hits:
        print(" ", u)


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
    ap.add_argument("--hunt", action="store_true",
                    help="probe likely data endpoints the page's JS calls")
    args = ap.parse_args()

    sess = VRSession()
    if args.cookie:
        sess.set_cookie_header(args.cookie)
    elif args.email:
        sess.login(args.email, args.password or "")
        print("login: OK")
    url = fund_url(args.fund)

    api_p = None
    fid = _fund_id(url)
    if fid:
        js = sess.fetch_portfolio_json(fid, referer=url)
        if js is not None:
            jdump = Path(__file__).with_name("vr_api_dump.json")
            jdump.write_text(json.dumps(js, indent=1), encoding="utf-8")
            api_p = parse_portfolio_api(js)
            print(f"portfolio API OK -> {jdump}")
            print("-- parsed from API --")
            print(json.dumps(api_p, indent=2))
        else:
            print("portfolio API returned nothing usable")

    r = sess.get(url)
    dump = Path(__file__).with_name("vr_page_dump.html")
    dump.write_text(r.text, encoding="utf-8")
    print(f"fetched {r.url} ({len(r.text):,} bytes) -> {dump}")
    if _is_login_wall(r.text):
        print("WARNING: page looks like a login wall")
    page_p = parse_fund_page(r.text)
    page_p["url"] = r.url
    print("-- parsed from page HTML --")
    print(json.dumps(page_p, indent=2))
    print("-- merged (what the app will use) --")
    print(json.dumps(_merge_params(api_p, page_p, url), indent=2))
    if args.hunt:
        hunt_endpoints(sess, args.fund, r.text)


if __name__ == "__main__":  # pragma: no cover
    _main()
