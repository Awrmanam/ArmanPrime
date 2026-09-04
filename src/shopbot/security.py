import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


def mask_pan(pan: str) -> str:
    digits = "".join(c for c in pan if c.isdigit())
    if len(digits) < 10:
        raise ValueError("invalid PAN")
    return f"**** {digits[-4:]}"


class Vault:
    def __init__(self, keys: dict[str, bytes], active_key_id: str):
        self.keys = keys
        self.active_key_id = active_key_id

    def encrypt(self, value: str) -> str:
        token = Fernet(self.keys[self.active_key_id]).encrypt(value.encode()).decode()
        return f"{self.active_key_id}:{token}"

    def decrypt(self, envelope: str) -> str:
        key_id, token = envelope.split(":", 1)
        try:
            return Fernet(self.keys[key_id]).decrypt(token.encode()).decode()
        except (KeyError, InvalidToken) as exc:
            raise ValueError("cannot decrypt envelope") from exc

    def rotate(self, envelope: str) -> str:
        return self.encrypt(self.decrypt(envelope))


def pan_fingerprint(pan: str, key: bytes) -> str:
    normalized = "".join(c for c in pan if c.isdigit())
    return hmac.new(key, normalized.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Callback:
    action: str
    object_id: str
    version: int


class CallbackSigner:
    def __init__(self, key: bytes):
        self.key = key

    def sign(self, callback: Callback) -> str:
        raw = json.dumps(
            [callback.action, callback.object_id, callback.version], separators=(",", ":")
        ).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:16]
        return f"1.{body}.{signature}"

    def verify(self, token: str) -> Callback:
        try:
            schema, body, signature = token.split(".")
            expected = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:16]
            if schema != "1" or not hmac.compare_digest(signature, expected):
                raise ValueError
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            action, object_id, version = json.loads(raw)
            return Callback(action, object_id, int(version))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid callback") from exc
