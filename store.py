"""
store.py — local persistence for watchlists.

Watchlists primarily live in the *browser* (localStorage, synced by app.py),
so they survive restarts on cloud deployments and stay per-visitor. This
module is the desktop fallback / migration source: a small JSON file under
the user's home directory:
    ~/.afp_nav_explorer/watchlists.json
Shape: { "<list name>": [scheme_code, ...], ... }

On Streamlit Community Cloud the filesystem is ephemeral and shared by every
visitor, so file I/O is disabled there (BROWSER_ONLY) and the browser copy is
the only store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path.home() / ".afp_nav_explorer"
WATCHLIST_FILE = APP_DIR / "watchlists.json"

# Streamlit Community Cloud sets HOSTNAME=streamlit; AFP_BROWSER_ONLY=1
# forces browser-only storage on any other host.
BROWSER_ONLY = (os.getenv("HOSTNAME") == "streamlit"
                or os.getenv("AFP_BROWSER_ONLY", "") == "1")

DEFAULT = {"My Watchlist": []}


def _ensure() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not WATCHLIST_FILE.exists():
        WATCHLIST_FILE.write_text(json.dumps(DEFAULT, indent=2))


def load() -> dict:
    if BROWSER_ONLY:
        return {k: list(v) for k, v in DEFAULT.items()}
    _ensure()
    try:
        data = json.loads(WATCHLIST_FILE.read_text())
        return {k: [int(c) for c in v] for k, v in data.items()}
    except (json.JSONDecodeError, ValueError):
        return {k: list(v) for k, v in DEFAULT.items()}


def save(watchlists: dict) -> None:
    if BROWSER_ONLY:
        return
    _ensure()
    WATCHLIST_FILE.write_text(json.dumps(watchlists, indent=2))


# ---- PF Review persistence (same browser-first strategy as watchlists) ---- #
PF_FILE = APP_DIR / "pf_review.json"
PF_DEFAULT: dict = {"snapshots": {}, "vr_urls": {}, "values": {}}


def load_pf() -> dict:
    """Monthly-review data: snapshots, VR page urls and invested values."""
    if not BROWSER_ONLY and PF_FILE.exists():
        try:
            data = json.loads(PF_FILE.read_text())
            if isinstance(data, dict):
                return {k: dict(data.get(k) or {}) for k in PF_DEFAULT}
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    return {k: dict(v) for k, v in PF_DEFAULT.items()}


def save_pf(data: dict) -> None:
    if BROWSER_ONLY:
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    PF_FILE.write_text(json.dumps(data, indent=2))


def add(watchlists: dict, list_name: str, code: int) -> dict:
    watchlists.setdefault(list_name, [])
    if int(code) not in watchlists[list_name]:
        watchlists[list_name].append(int(code))
    save(watchlists)
    return watchlists


def remove(watchlists: dict, list_name: str, code: int) -> dict:
    if list_name in watchlists and int(code) in watchlists[list_name]:
        watchlists[list_name].remove(int(code))
        save(watchlists)
    return watchlists


def create_list(watchlists: dict, list_name: str) -> dict:
    watchlists.setdefault(list_name, [])
    save(watchlists)
    return watchlists


def delete_list(watchlists: dict, list_name: str) -> dict:
    watchlists.pop(list_name, None)
    if not watchlists:
        watchlists["My Watchlist"] = []
    save(watchlists)
    return watchlists
