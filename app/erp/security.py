# -*- coding: utf-8 -*-
"""密码散列与会话安全"""
import hashlib
import hmac
import os
import binascii

ITERATIONS = 120_000
SALT_BYTES = 16


def hash_password(password: str, salt: str = None):
    """返回 (password_hash_hex, salt_hex)"""
    if salt is None:
        salt = binascii.hexlify(os.urandom(SALT_BYTES)).decode()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             binascii.unhexlify(salt.encode()), ITERATIONS)
    return binascii.hexlify(dk).decode(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    try:
        calc, _ = hash_password(password, salt)
    except Exception:
        return False
    return hmac.compare_digest(calc, password_hash or "")


class Session:
    """当前登录会话"""
    def __init__(self, user: dict):
        self.user = user or {}

    @property
    def is_login(self) -> bool:
        return bool(self.user.get("id"))

    def get(self, key, default=None):
        return self.user.get(key, default)

    def to_dict(self) -> dict:
        return dict(self.user)
