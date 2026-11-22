import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any


def _encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session_token(session_id: str, user_id: str, signing_key: str, ttl_seconds: int) -> str:
    if len(signing_key.encode("utf-8")) < 32:
        raise RuntimeError("SESSION_SIGNING_KEY must be at least 32 bytes")
    payload = _encode(
        json.dumps(
            {"sid": session_id, "sub": user_id, "exp": int(time.time()) + ttl_seconds},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signature = hmac.new(signing_key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_encode(signature)}"


def verify_session_token(token: str, signing_key: str) -> dict[str, Any] | None:
    if len(signing_key.encode("utf-8")) < 32:
        return None
    try:
        payload, signature = token.split(".", 1)
        expected = hmac.new(
            signing_key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_decode(signature), expected):
            return None
        data = json.loads(_decode(payload))
        if not isinstance(data, dict) or int(data["exp"]) < int(time.time()):
            return None
        if not isinstance(data["sid"], str) or not isinstance(data["sub"], str):
            return None
        return data
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def hash_csrf_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
