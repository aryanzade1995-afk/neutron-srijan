"""Multi-factor authentication for the investigation console.

Password plus a time-based one-time code (TOTP, RFC 6238), because a fraud
console exposes victim VPAs, account ages and freeze recommendations - a single
reusable secret is not an adequate gate on that.

TOTP is implemented on the standard library rather than pulled in as a
dependency: it is an HMAC of a counter, and the whole of it is below.

State lives in the store (Redis when available):
  mt:auth:user:<username>       HASH   credentials and enrolment
  mt:auth:session:<token>       STRING username, TTL = session lifetime
  mt:auth:challenge:<token>     STRING username, short TTL, one login attempt
  mt:auth:throttle:<username>   STRING failed attempt counter, TTL

Set MULETRACE_AUTH=off to run the console open, for a rehearsal where handing a
laptop round matters more than the gate.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time

from store import STORE

AUTH_ENABLED = os.environ.get("MULETRACE_AUTH", "on").lower() not in ("off", "0", "false")

SESSION_TTL = int(os.environ.get("MULETRACE_SESSION_TTL", 60 * 60 * 8))   # one shift
CHALLENGE_TTL = 180             # time to enter the code after the password
MAX_ATTEMPTS = 5
THROTTLE_TTL = 300

SESSION_COOKIE = "mt_auth"

USER_KEY = "mt:auth:user:"
SESSION_KEY = "mt:auth:session:"
CHALLENGE_KEY = "mt:auth:challenge:"
THROTTLE_KEY = "mt:auth:throttle:"

ISSUER = "MuleTrace"


# ---------- TOTP (RFC 6238) ----------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_at(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def verify_totp(secret: str, code: str, window: int = 1, step: int = 30) -> bool:
    """Accept the neighbouring steps too, so a slightly skewed phone clock works."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    counter = int(time.time() // step)
    return any(hmac.compare_digest(totp_at(secret, counter + drift), code)
               for drift in range(-window, window + 1))


def provisioning_uri(username: str, secret: str) -> str:
    label = f"{ISSUER}:{username}"
    return (f"otpauth://totp/{label}?secret={secret}&issuer={ISSUER}"
            f"&algorithm=SHA1&digits=6&period=30")


# ---------- passwords ----------

def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return derived.hex(), salt.hex()


def check_password(password: str, stored_hash: str, salt_hex: str) -> bool:
    candidate, _ = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, stored_hash)


# ---------- users ----------

def create_user(username: str, password: str, role: str = "investigator") -> dict:
    pw_hash, salt = hash_password(password)
    secret = new_totp_secret()
    record = {
        "username": username,
        "password_hash": pw_hash,
        "salt": salt,
        "totp_secret": secret,
        "role": role,
        "mfa_enrolled": "0",
        "created_at": str(int(time.time())),
    }
    STORE.hset(USER_KEY + username, {k: v.encode() for k, v in record.items()})
    return record


def get_user(username: str) -> dict | None:
    raw = STORE.hgetall(USER_KEY + username)
    if not raw:
        return None
    return {k: v.decode() if isinstance(v, bytes) else v for k, v in raw.items()}


def mark_enrolled(username: str) -> None:
    STORE.hset(USER_KEY + username, {"mfa_enrolled": b"1"})


def ensure_demo_user() -> tuple[str, str, str] | None:
    """Seed one account on first boot so the console is reachable.

    Returns (username, password, secret) only when it actually creates the user,
    so the credentials are printed once rather than on every restart.
    """
    username = os.environ.get("MULETRACE_USER", "investigator")
    if get_user(username):
        return None
    password = os.environ.get("MULETRACE_PASSWORD") or secrets.token_urlsafe(9)
    record = create_user(username, password, role="investigator")
    return username, password, record["totp_secret"]


# ---------- throttling ----------

def _attempts(username: str) -> int:
    raw = STORE.get(THROTTLE_KEY + username)
    return int(raw) if raw else 0


def record_failure(username: str) -> int:
    count = _attempts(username) + 1
    STORE.set(THROTTLE_KEY + username, str(count).encode(), ttl=THROTTLE_TTL)
    return count


def clear_failures(username: str) -> None:
    STORE.delete(THROTTLE_KEY + username)


def is_locked(username: str) -> bool:
    return _attempts(username) >= MAX_ATTEMPTS


# ---------- challenges and sessions ----------

def start_challenge(username: str) -> str:
    token = secrets.token_urlsafe(24)
    STORE.set(CHALLENGE_KEY + token, username.encode(), ttl=CHALLENGE_TTL)
    return token


def resolve_challenge(token: str) -> str | None:
    raw = STORE.get(CHALLENGE_KEY + (token or ""))
    return raw.decode() if raw else None


def consume_challenge(token: str) -> None:
    STORE.delete(CHALLENGE_KEY + (token or ""))


def start_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    payload = json.dumps({"username": username, "issued": int(time.time())})
    STORE.set(SESSION_KEY + token, payload.encode(), ttl=SESSION_TTL)
    return token


def session_user(token: str | None) -> str | None:
    if not token:
        return None
    raw = STORE.get(SESSION_KEY + token)
    if not raw:
        return None
    try:
        return json.loads(raw)["username"]
    except (ValueError, KeyError):
        return None


def end_session(token: str | None) -> None:
    if token:
        STORE.delete(SESSION_KEY + token)
