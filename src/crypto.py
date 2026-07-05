"""Shared AES-256-GCM helpers for the small encrypted JSON token blobs ASTRA stores
(Robinhood read tokens + Autotrader agentic OAuth tokens). One implementation over the
`cryptography` library, keyed by a base64 32-byte key."""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encrypt_json(data: dict, key_b64: str) -> str:
    """Encrypt a dict → JSON string ({iv, tag, ciphertext}, all base64)."""
    key = base64.b64decode(key_b64)
    iv = os.urandom(12)
    ct_tag = AESGCM(key).encrypt(iv, json.dumps(data).encode(), None)
    return json.dumps({
        "iv":         base64.b64encode(iv).decode(),
        "tag":        base64.b64encode(ct_tag[-16:]).decode(),
        "ciphertext": base64.b64encode(ct_tag[:-16]).decode(),
    })


def decrypt_json(blob: str, key_b64: str) -> dict:
    """Inverse of encrypt_json — decrypt the {iv, tag, ciphertext} JSON blob → dict."""
    enc = json.loads(blob)
    key = base64.b64decode(key_b64)
    iv = base64.b64decode(enc["iv"])
    tag = base64.b64decode(enc["tag"])
    ct = base64.b64decode(enc["ciphertext"])
    return json.loads(AESGCM(key).decrypt(iv, ct + tag, None))
