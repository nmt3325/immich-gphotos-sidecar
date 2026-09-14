"""Turn a Google "Embedded Setup" oauth_token into gpmc auth data.

Capturing auth data from the Google Photos Android app normally needs ReVanced +
GmsCore or a rooted device. The gotohp GUI avoids that by replaying what Play
Services does when an account is added to a brand new device:

1. sign in on https://accounts.google.com/EmbeddedSetup in any browser and copy
   the ``oauth_token`` cookie (a short lived ``oauth2_4/0A...`` value),
2. POST it to https://android.clients.google.com/auth with ``service=ac2dm`` and
   a freshly generated android id, which returns the account e-mail plus a
   master token (``aas_et/...``),
3. assemble the Google Photos flavoured credential string that gpmc calls
   "auth data".

This module is a small port of that exchange (gotohp ``core/googleauth.go``).
The result is exactly what ``GPMC_AUTH_DATA`` expects, so no capture tooling,
root access or patched APK is required.

The master token grants full access to the Google account: never log it, and
keep the generated file private (it is written with mode 0600).
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union
from urllib.parse import parse_qsl, urlencode

import requests

from .log import get_logger

log = get_logger("auth")

# Play Services sign-in endpoint used when a device adds a Google account.
AUTH_ENDPOINT = "https://android.clients.google.com/auth"
# The real e-mail is unknown before the exchange; Google resolves it from the
# token and echoes it back, so a placeholder is all that can be sent.
EMAIL_HINT = "oauth-token@example.com"
PLAY_SERVICES_SIG = "38918a453d07199354f8b19af05ec6562ced5788"
PLAY_SERVICES_VERSION = "240913000"
PHOTOS_PACKAGE = "com.google.android.apps.photos"
PHOTOS_SIG = "24bb24c05e47e0aefa68a58a766179d9b613a600"
PHOTOS_SERVICE = (
    "oauth2:openid https://www.googleapis.com/auth/mobileapps.native"
    " https://www.googleapis.com/auth/photos.native"
)
USER_AGENT = "GoogleAuth/1.4"

MIN_TOKEN_LEN = 16
MAX_TOKEN_LEN = 8192

ERROR_HINTS = {
    "BadAuthentication": (
        "google rejected the oauth_token; it is single use and short lived, "
        "so sign in on https://accounts.google.com/EmbeddedSetup again and "
        "copy a fresh cookie"
    ),
    "NeedsBrowser": "google wants a fresh Embedded Setup sign-in for this account",
    "MissingDroidguard": "google rejected the device verification data",
    "DeviceManagementRequiredOrSyncDisabled": (
        "the account requires device management (Workspace policy); use a "
        "personal account or capture auth data from a managed device"
    ),
}

AUTH_DATA_FILE_HEADER = (
    "# gpmc auth data written by `immich-gphotos-sidecar creds add`.\n"
    "# Anyone holding this string can act as the Google account: keep it private.\n"
)


class AuthError(RuntimeError):
    """An oauth_token could not be turned into usable auth data."""


def normalize_oauth_token(value: str) -> str:
    """Accept the raw cookie value, or a pasted ``oauth_token=...`` pair."""
    token = (value or "").strip()
    if token.startswith("oauth_token="):
        token = token[len("oauth_token=") :].strip()
    if "\n" in token or "\r" in token:
        raise AuthError("the oauth_token must be a single line")
    if "Token=" in token and "&" in token:
        raise AuthError(
            "this already looks like gpmc auth data; set it as GPMC_AUTH_DATA "
            "instead of exchanging it"
        )
    if not MIN_TOKEN_LEN <= len(token) <= MAX_TOKEN_LEN:
        raise AuthError(
            "this does not look like an Embedded Setup oauth_token (expect the "
            "cookie value, e.g. oauth2_4/0A...)"
        )
    return token


def generate_android_id() -> str:
    """Random 64 bit device id, like the one Play Services would report."""
    return secrets.token_hex(8)


def parse_auth_response(body: str) -> Dict[str, str]:
    """Google answers with ``key=value`` lines, not JSON."""
    values: Dict[str, str] = {}
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def build_auth_data(email: str, master_token: str, android_id: str) -> str:
    """Credential string for the Google Photos app (gpmc's ``auth_data``)."""
    return urlencode(
        {
            "androidId": android_id,
            "app": PHOTOS_PACKAGE,
            "callerPkg": PHOTOS_PACKAGE,
            "callerSig": PHOTOS_SIG,
            "client_sig": PHOTOS_SIG,
            "device_country": "us",
            "Email": email,
            "google_play_services_version": PLAY_SERVICES_VERSION,
            "lang": "en_US",
            "oauth2_foreground": "1",
            "operatorCountry": "us",
            "sdk_version": "33",
            "service": PHOTOS_SERVICE,
            "source": "android",
            "Token": master_token,
        }
    )


def exchange_oauth_token(
    oauth_token: str,
    *,
    android_id: Optional[str] = None,
    proxy: str = "",
    timeout: int = 30,
    endpoint: str = AUTH_ENDPOINT,
    session: Optional[Any] = None,
) -> Dict[str, str]:
    """Trade the Embedded Setup cookie for an account e-mail and master token."""
    token = normalize_oauth_token(oauth_token)
    device_id = android_id or generate_android_id()
    form = {
        "accountType": "HOSTED_OR_GOOGLE",
        "Email": EMAIL_HINT,
        "has_permission": "1",
        "add_account": "1",
        "ACCESS_TOKEN": "1",
        "Token": token,
        "service": "ac2dm",
        "source": "android",
        "androidId": device_id,
        "device_country": "us",
        "operatorCountry": "us",
        "lang": "en",
        "sdk_version": "17",
        "google_play_services_version": PLAY_SERVICES_VERSION,
        "client_sig": PLAY_SERVICES_SIG,
        "callerSig": PLAY_SERVICES_SIG,
        "droidguard_results": "dummy123",
    }
    http = session or requests.Session()
    log.info("exchanging the Embedded Setup token for android id %s", device_id)
    try:
        response = http.post(
            endpoint,
            data=form,
            headers={
                "Accept-Encoding": "identity",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=max(5, int(timeout or 30)),
            # never hand a token-bearing request to a redirect target
            allow_redirects=False,
        )
        if response.status_code >= 300:
            raise AuthError(
                f"google authentication returned HTTP {response.status_code}"
            )
        values = parse_auth_response(response.text)
    except requests.RequestException as exc:
        raise AuthError(f"could not reach google authentication: {exc}") from exc
    finally:
        if session is None:
            http.close()

    failure = values.get("Error", "").strip()
    if failure:
        raise AuthError(ERROR_HINTS.get(failure, f"google authentication failed with {failure}"))
    master_token = values.get("Token", "").strip()
    email = values.get("Email", "").strip()
    if not master_token:
        raise AuthError("google did not return a master token")
    if "@" not in email:
        raise AuthError("google did not return the account e-mail")
    return {"email": email, "masterToken": master_token, "androidId": device_id}


def create_auth_data(
    oauth_token: str,
    *,
    android_id: Optional[str] = None,
    proxy: str = "",
    timeout: int = 30,
    endpoint: str = AUTH_ENDPOINT,
    session: Optional[Any] = None,
) -> Dict[str, str]:
    """Full oauth_token -> auth data flow. Returns e-mail, android id, auth data."""
    exchange = exchange_oauth_token(
        oauth_token,
        android_id=android_id,
        proxy=proxy,
        timeout=timeout,
        endpoint=endpoint,
        session=session,
    )
    return {
        "email": exchange["email"],
        "androidId": exchange["androidId"],
        "authData": build_auth_data(
            exchange["email"], exchange["masterToken"], exchange["androidId"]
        ),
    }


def mask_auth_data(auth_data: str) -> str:
    """Auth data with the master token redacted, safe to print or log."""
    if not auth_data:
        return ""
    parts = []
    for key, value in parse_qsl(auth_data, keep_blank_values=True):
        if key.lower() == "token" and value:
            value = f"{value[:10]}...({len(value)} chars)"
        parts.append(f"{key}={value}")
    return "&".join(parts) or "(unparsable auth data)"


def write_auth_data_file(path: Union[str, Path], auth_data: str) -> Path:
    """Store auth data on the persistent volume, readable by the owner only."""
    target = Path(path)
    payload = (auth_data or "").strip()
    if not payload:
        raise AuthError("refusing to write empty auth data")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=".auth-data-",
            delete=False,
        )
        try:
            with handle:
                handle.write(AUTH_DATA_FILE_HEADER)
                handle.write(payload + "\n")
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, target)
        except OSError:
            try:
                os.unlink(handle.name)
            except OSError:  # pragma: no cover - cleanup only
                pass
            raise
    except OSError as exc:
        raise AuthError(f"could not write {target}: {exc}") from exc
    return target


def read_auth_data_file(path: Union[str, Path]) -> str:
    """First non-comment line of an auth data file (empty when unreadable)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""
