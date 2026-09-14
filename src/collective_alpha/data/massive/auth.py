"""Credential loading. Secrets are read from files outside the repo and never logged."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class S3Credentials:
    access_key_id: str
    secret_access_key: str


def _nonblank_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def load_api_key(path: Path) -> str:
    env = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY")
    if env:
        return env.strip()
    if not path.exists():
        raise FileNotFoundError(f"API key file not found: {path}")
    lines = _nonblank_lines(path)
    if not lines:
        raise ValueError(f"API key file is empty: {path}")
    first = lines[0]
    if "=" in first:
        first = first.split("=", 1)[1].strip()
    elif ":" in first and not first.startswith("{"):
        first = first.split(":", 1)[1].strip()
    return first.strip("\"'")


_ID_HINTS = re.compile(r"(access[_ -]?key[_ -]?id|key[_ -]?id|^id$|access)", re.I)
_SECRET_HINTS = re.compile(r"secret", re.I)
# a short line made only of words/spaces, e.g. "Access Key ID", "Secret Access Key", "S3 Endpoint"
_LABEL_ONLY = re.compile(r"^[A-Za-z][A-Za-z _-]{1,39}$")  # words only, no digits
_LABEL_HINTS = re.compile(r"(access|secret|key|endpoint|bucket|region)", re.I)


def load_s3_credentials(path: Path, fallback_secret: str | None = None) -> S3Credentials:
    """Parse an S3 credential file in any of these shapes:

    * two lines: access key id, then secret
    * ``KEY=VALUE`` / ``KEY: VALUE`` lines whose names mention *id* / *secret*
    * a JSON object with those keys
    * one line ``id:secret`` or ``id,secret``
    * one line holding only the access key id (secret falls back to the API key,
      which is how Massive/Polygon issue S3 secrets)
    """
    env_id = os.environ.get("MASSIVE_S3_ACCESS_KEY_ID")
    env_secret = os.environ.get("MASSIVE_S3_SECRET_ACCESS_KEY")
    if env_id and env_secret:
        return S3Credentials(env_id.strip(), env_secret.strip())

    if not path.exists():
        raise FileNotFoundError(f"S3 key file not found: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    lines = _nonblank_lines(path)
    if not lines:
        raise ValueError(f"S3 key file is empty: {path}")

    if raw.startswith("{"):
        obj = json.loads(raw)
        kid = next((v for k, v in obj.items() if _ID_HINTS.search(k)), None)
        sec = next((v for k, v in obj.items() if _SECRET_HINTS.search(k)), None)
        if kid and sec:
            return S3Credentials(str(kid).strip(), str(sec).strip())

    kv: dict[str, str] = {}
    plain: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_ -]*)\s*[=:]\s*(.+)$", ln)
        if m and len(m.group(1)) <= 40:
            kv[m.group(1).strip()] = m.group(2).strip().strip("\"'")
        elif _LABEL_ONLY.match(ln) and _LABEL_HINTS.search(ln) and i + 1 < len(lines):
            # dashboard copy/paste: "Access Key ID" on one line, the value on the next
            kv[ln.strip()] = lines[i + 1].strip().strip("\"'")
            i += 1
        else:
            plain.append(ln.strip("\"'"))
        i += 1

    if kv:
        kid = next((v for k, v in kv.items() if _ID_HINTS.search(k) and not _SECRET_HINTS.search(k)), None)
        sec = next((v for k, v in kv.items() if _SECRET_HINTS.search(k)), None)
        if kid and sec:
            return S3Credentials(kid, sec)
        if kid and fallback_secret:
            return S3Credentials(kid, fallback_secret)

    if len(plain) >= 2:
        return S3Credentials(plain[0], plain[1])
    if len(plain) == 1:
        for sep in (":", ","):
            if sep in plain[0]:
                a, b = plain[0].split(sep, 1)
                return S3Credentials(a.strip(), b.strip())
        if fallback_secret:
            return S3Credentials(plain[0], fallback_secret)

    raise ValueError(f"Could not parse S3 credentials from {path}. Expected two lines (access key id, secret).")


def redact(secret: str) -> str:
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}…{secret[-3:]}"
