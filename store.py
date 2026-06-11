"""
store.py — local persistence for watchlists.

The NAV history is pulled on-demand, but watchlists must survive between
sessions, so they live in a small JSON file under the user's home directory:
    ~/.afp_nav_explorer/watchlists.json
Shape: { "<list name>": [scheme_code, ...], ... }
"""

from __future__ import annotations

import json
from pathlib import Path

APP_DIR = Path.home() / ".afp_nav_explorer"
WATCHLIST_FILE = APP_DIR / "watchlists.json"


def _ensure() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not WATCHLIST_FILE.exists():
        WATCHLIST_FILE.write_text(json.dumps({"My Watchlist": []}, indent=2))


def load() -> dict:
    _ensure()
    try:
        data = json.loads(WATCHLIST_FILE.read_text())
        return {k: [int(c) for c in v] for k, v in data.items()}
    except (json.JSONDecodeError, ValueError):
        return {"My Watchlist": []}


def save(watchlists: dict) -> None:
    _ensure()
    WATCHLIST_FILE.write_text(json.dumps(watchlists, indent=2))


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
