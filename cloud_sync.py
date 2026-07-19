"""
cloud_sync.py — optional encrypted, cross-device sync of the user's data:
watchlists AND the PF-Review data (monthly snapshots, invested values and
per-scheme VR fund codes).

This app/repo is meant to be **public**, so user data is never stored in
clear text anywhere. Each user picks a *username* and a *passphrase*. The
data JSON is encrypted inside the app (Argon2id key-derivation + Fernet /
AES-128-CBC+HMAC) and only the resulting ciphertext is sent to a **private**
GitHub repo that acts as the datastore. Anyone who somehow obtains the blob
sees only random bytes; without the passphrase it cannot be read.

Payload versioning: blobs written before the PF-Review sync hold a plain
{list name: [codes]} mapping; current blobs hold
{"v": 2, "watchlists": {...}, "pf": {...}}. unpack() reads both, so existing
records in the private repo keep working and are upgraded on the next Save.

Why a private repo (not this public one):
  * the code stays clean — user data never lands in your source history;
  * defence in depth — the data is private at the GitHub level *and* encrypted,
    so a single mistake (e.g. crypto weakness, weak passphrase) is not fatal.

Storage layout in the private repo:
    watchlists/<sha256(username)>.json
The filename is a hash, so the repo never reveals *who* a user is, only that
some opaque records exist.

Security notes / limitations (be honest with users):
  * The passphrase never leaves this process and is never stored. Lose it and
    the data is unrecoverable — that is the whole point.
  * Saving to a username that already exists requires proving you can decrypt
    the existing blob (i.e. you know its passphrase). This stops one user from
    silently clobbering another's data by guessing a username.
  * A GitHub token is required only to *talk* to the private repo; it lives in
    Streamlit secrets, never in the repo. Without it, sync is simply disabled.

Configuration — .streamlit/secrets.toml (see secrets.toml.example):
    [sync]
    token  = "github_pat_..."            # PAT with Contents read/write on repo
    repo   = "owner/private-watchlists"  # a PRIVATE repo you own
    branch = "main"                      # optional, defaults to "main"
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Optional, Tuple

import requests
from argon2.low_level import Type, hash_secret_raw
from cryptography.fernet import Fernet, InvalidToken

# --------------------------------------------------------------------------- #
# Crypto
# --------------------------------------------------------------------------- #
# Argon2id parameters. These bound how expensive an offline brute-force of a
# weak passphrase is, so do not lower them casually. 64 MiB / 3 passes is a
# reasonable interactive cost.
_ARGON_TIME = 3
_ARGON_MEMORY = 64 * 1024  # KiB -> 64 MiB
_ARGON_PARALLELISM = 4
_ENVELOPE_VERSION = 1


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 32-byte Fernet key from a passphrase + salt via Argon2id."""
    raw = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON_TIME,
        memory_cost=_ARGON_MEMORY,
        parallelism=_ARGON_PARALLELISM,
        hash_len=32,
        type=Type.ID,
    )
    return base64.urlsafe_b64encode(raw)


def encrypt(payload: dict, passphrase: str) -> str:
    """Encrypt a JSON-able payload into a self-describing envelope string."""
    if not passphrase:
        raise ValueError("A passphrase is required.")
    salt = _random_salt()
    key = _derive_key(passphrase, salt)
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    token = Fernet(key).encrypt(plaintext)
    envelope = {
        "v": _ENVELOPE_VERSION,
        "kdf": "argon2id",
        "t": _ARGON_TIME,
        "m": _ARGON_MEMORY,
        "p": _ARGON_PARALLELISM,
        "salt": base64.b64encode(salt).decode("ascii"),
        "ct": token.decode("ascii"),
    }
    return json.dumps(envelope, indent=2)


def decrypt(envelope_str: str, passphrase: str) -> dict:
    """Decrypt an envelope to the raw stored payload (legacy or v2 shape).
    Raises InvalidToken on a wrong passphrase."""
    env = json.loads(envelope_str)
    salt = base64.b64decode(env["salt"])
    # Honour the parameters stored in the envelope so old blobs stay readable
    # even if the defaults above are tuned up later.
    raw = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=int(env.get("t", _ARGON_TIME)),
        memory_cost=int(env.get("m", _ARGON_MEMORY)),
        parallelism=int(env.get("p", _ARGON_PARALLELISM)),
        hash_len=32,
        type=Type.ID,
    )
    key = base64.urlsafe_b64encode(raw)
    plaintext = Fernet(key).decrypt(env["ct"].encode("ascii"))
    return json.loads(plaintext)


# --------------------------------------------------------------------------- #
# Payload (what lives inside the encrypted envelope)
# --------------------------------------------------------------------------- #
_PAYLOAD_VERSION = 2


def _norm_watchlists(d: dict) -> dict:
    """Normalise to {name: [int, ...]} like store.load() does."""
    return {str(k): [int(c) for c in v] for k, v in (d or {}).items()}


def pack(watchlists: dict, pf: Optional[dict] = None) -> dict:
    """Combine watchlists + PF-Review data into the stored payload."""
    return {"v": _PAYLOAD_VERSION, "watchlists": watchlists, "pf": pf or {}}


def unpack(data: dict) -> Tuple[dict, Optional[dict]]:
    """(watchlists, pf-or-None) from a decrypted payload.

    Reads both shapes: the v2 {"watchlists": ..., "pf": ...} envelope and
    legacy blobs that are a plain {list name: [codes]} mapping (which can't
    collide with v2 — a legacy value is a list, never a dict).
    """
    if isinstance(data, dict) and isinstance(data.get("watchlists"), dict):
        pf = data.get("pf")
        return (_norm_watchlists(data["watchlists"]),
                dict(pf) if isinstance(pf, dict) and pf else None)
    return _norm_watchlists(data), None


def _random_salt() -> bytes:
    import os
    return os.urandom(16)


# --------------------------------------------------------------------------- #
# GitHub backend
# --------------------------------------------------------------------------- #
_API = "https://api.github.com"
_TIMEOUT = 15


def _cfg() -> Optional[Tuple[str, str, str]]:
    """Return (token, repo, branch) from Streamlit secrets, or None."""
    try:
        import streamlit as st
        sync = st.secrets["sync"]  # type: ignore[index]
        token = sync["token"]
        repo = sync["repo"]
    except Exception:  # noqa: BLE001 — any missing/secret error => disabled
        return None
    if not token or not repo:
        return None
    branch = "main"
    try:
        branch = sync.get("branch", "main") or "main"
    except Exception:  # noqa: BLE001
        pass
    return token, repo, branch


def is_configured() -> bool:
    """True when a GitHub backend is configured (token + repo present)."""
    return _cfg() is not None


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _path(username: str) -> str:
    norm = username.strip().lower().encode("utf-8")
    return f"watchlists/{hashlib.sha256(norm).hexdigest()}.json"


def _get_remote(username: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (envelope_str, sha) for a user's blob, or (None, None)."""
    cfg = _cfg()
    if cfg is None:
        raise RuntimeError("Cloud sync is not configured.")
    token, repo, branch = cfg
    url = f"{_API}/repos/{repo}/contents/{_path(username)}"
    resp = requests.get(url, headers=_headers(token),
                        params={"ref": branch}, timeout=_TIMEOUT)
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    body = resp.json()
    content = base64.b64decode(body["content"]).decode("utf-8")
    return content, body["sha"]


def pull(username: str, passphrase: str) -> Optional[Tuple[dict, Optional[dict]]]:
    """Fetch + decrypt a user's record -> (watchlists, pf-or-None).
    None if no record exists.

    Raises InvalidToken if the passphrase is wrong.
    """
    if not username.strip():
        raise ValueError("A username is required.")
    envelope, _sha = _get_remote(username)
    if envelope is None:
        return None
    return unpack(decrypt(envelope, passphrase))


def push(username: str, passphrase: str, watchlists: dict,
         pf: Optional[dict] = None) -> None:
    """Encrypt + upload a user's watchlists + PF-Review data to the
    private repo.

    If a record already exists for this username, the passphrase must decrypt
    it first — otherwise we refuse, so nobody can clobber another user's data.
    """
    cfg = _cfg()
    if cfg is None:
        raise RuntimeError("Cloud sync is not configured.")
    if not username.strip():
        raise ValueError("A username is required.")
    token, repo, branch = cfg

    existing, sha = _get_remote(username)
    if existing is not None:
        try:
            decrypt(existing, passphrase)
        except InvalidToken as exc:
            raise PermissionError(
                "That username already exists and is protected by a different "
                "passphrase. Pick another username, or use the correct "
                "passphrase."
            ) from exc

    envelope = encrypt(pack(watchlists, pf), passphrase)
    payload = {
        "message": f"watchlist sync ({_path(username)[:24]}…)",
        "content": base64.b64encode(envelope.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    url = f"{_API}/repos/{repo}/contents/{_path(username)}"
    resp = requests.put(url, headers=_headers(token),
                        json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
