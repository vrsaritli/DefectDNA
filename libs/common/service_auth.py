import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import cast


def _encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_service_token(secret: str, service: str, ttl_seconds: int = 300) -> str:
    payload = _encode(
        json.dumps({"service": service, "exp": int(time.time()) + ttl_seconds}, separators=(",", ":")).encode()
    )
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_encode(signature)}"


def verify_service_token(token: str, secret: str) -> dict[str, object] | None:
    try:
        payload, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode(signature), expected):
            return None
        data = json.loads(_decode(payload))
        if int(data["exp"]) < int(time.time()):
            return None
        return cast(dict[str, object], data)
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
