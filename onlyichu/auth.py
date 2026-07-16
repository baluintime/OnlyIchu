"""Upstox OAuth login flow and token storage.

Upstox access tokens are valid for one trading day (expire ~3:30 AM IST the
next day), so `python -m onlyichu login` needs to be run each morning unless
UPSTOX_ACCESS_TOKEN is provided by other means.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

import requests

BASE_URL = "https://api.upstox.com"
TOKEN_FILE = Path(os.environ.get("ONLYICHU_HOME", str(Path.home() / ".onlyichu"))) / "credentials.json"


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def get_app_credentials() -> tuple[str, str, str]:
    _load_dotenv()
    api_key = os.environ.get("UPSTOX_API_KEY", "")
    secret = os.environ.get("UPSTOX_API_SECRET", "")
    redirect = os.environ.get("UPSTOX_REDIRECT_URI", "")
    if not api_key or not secret or not redirect:
        raise SystemExit(
            "Missing Upstox app credentials. Set UPSTOX_API_KEY, UPSTOX_API_SECRET "
            "and UPSTOX_REDIRECT_URI in the environment or a .env file (see .env.example)."
        )
    return api_key, secret, redirect


def build_login_url(api_key: str, redirect_uri: str) -> str:
    params = urllib.parse.urlencode(
        {"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri}
    )
    return f"{BASE_URL}/v2/login/authorization/dialog?{params}"


def exchange_code(code: str, api_key: str, secret: str, redirect_uri: str) -> str:
    resp = requests.post(
        f"{BASE_URL}/v2/login/authorization/token",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "code": code.strip(),
            "client_id": api_key,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise SystemExit(f"Token exchange failed: {resp.text}")
    return token


def save_token(token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"access_token": token}), encoding="utf-8")
    TOKEN_FILE.chmod(0o600)


def load_token() -> str:
    """Access token from env (UPSTOX_ACCESS_TOKEN) or the saved credentials file."""
    _load_dotenv()
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if token:
        return token
    if TOKEN_FILE.exists():
        token = json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("access_token")
        if token:
            return token
    raise SystemExit(
        "No Upstox access token found. Run `python -m onlyichu login` first "
        "(or set UPSTOX_ACCESS_TOKEN)."
    )


def interactive_login() -> None:
    api_key, secret, redirect = get_app_credentials()
    url = build_login_url(api_key, redirect)
    print("Open this URL in a browser, log in to Upstox and approve access:\n")
    print(f"  {url}\n")
    print(f"You will be redirected to {redirect}?code=XXXX — paste the `code` value below.")
    code = input("Authorization code: ").strip()
    token = exchange_code(code, api_key, secret, redirect)
    save_token(token)
    print(f"Access token saved to {TOKEN_FILE} (valid for today's session).")
