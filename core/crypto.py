# -*- coding: utf-8 -*-
"""
AES-256-GCM 加密模块（生产级）
- 加密格式：nonce(16) + tag(16) + ciphertext
- 使用 PyCryptodome，密钥为 hex 64 位（32 字节）
- 提供便捷的加密/解密字符串、JSON 对象
"""
import json
import base64

from Crypto.Cipher import AES

import config


class CryptoError(Exception):
    """加密/解密错误"""


def _get_key():
    """获取并校验密钥"""
    if not config.DEMO_KEY:
        raise CryptoError("DEMO_KEY 未配置")
    try:
        return bytes.fromhex(config.DEMO_KEY)
    except ValueError as e:
        raise CryptoError(f"DEMO_KEY 不是合法 hex: {e}") from e


def encrypt_bytes(data: bytes) -> bytes:
    """加密字节流，返回 nonce+tag+ciphertext"""
    key = _get_key()
    cipher = AES.new(key, AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(data)
    return cipher.nonce + tag + ct


def decrypt_bytes(blob: bytes) -> bytes:
    """解密字节流，校验失败抛 CryptoError"""
    if not blob or len(blob) < 32:
        raise CryptoError("密文长度非法")
    key = _get_key()
    nonce, tag, ct = blob[:16], blob[16:32], blob[32:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        return cipher.decrypt_and_verify(ct, tag)
    except ValueError as e:
        raise CryptoError(f"解密失败（密钥不匹配或数据损坏）: {e}")


def encrypt_str(text: str) -> bytes:
    """加密字符串"""
    return encrypt_bytes(text.encode("utf-8"))


def decrypt_str(blob: bytes) -> str:
    """解密为字符串"""
    return decrypt_bytes(blob).decode("utf-8")


def encrypt_json(obj) -> bytes:
    """加密 JSON 对象"""
    return encrypt_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def decrypt_json(blob: bytes):
    """解密为 JSON 对象"""
    return json.loads(decrypt_bytes(blob).decode("utf-8"))


def encrypt_b64(data: bytes) -> str:
    """加密并 base64 编码（便于传输/存储）"""
    return base64.b64encode(encrypt_bytes(data)).decode("ascii")


def decrypt_b64(b64: str) -> bytes:
    """base64 解码后解密"""
    return decrypt_bytes(base64.b64decode(b64))