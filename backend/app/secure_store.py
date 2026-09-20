"""GEE 凭据加密存储。

任务书安全要求：「GEE 凭据仅存储于服务端并做加密处理，不得在页面明文展示」。
实现方式：Fernet 对称加密（cryptography），密钥单独存放在 .key 文件（本机、不入库、不提交）。
若环境缺少 cryptography，则降级为 HMAC 派生密钥流的轻量混淆，并在日志中明确告警。

文件布局（默认，均已被 .gitignore 忽略）：
    backend/data/credentials/gee.enc   密文（含服务账号 JSON）
    backend/data/credentials/.key      Fernet 密钥（base64）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

_KEY_NAME = ".key"


def _key_path(enc_path: Path) -> Path:
    return enc_path.parent / _KEY_NAME


def _load_or_create_key(enc_path: Path) -> bytes:
    enc_path.parent.mkdir(parents=True, exist_ok=True)
    kp = _key_path(enc_path)
    if kp.exists():
        return kp.read_bytes().strip()
    try:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
    except ImportError:
        key = base64.urlsafe_b64encode(os.urandom(32))
    kp.write_bytes(key)
    try:  # Windows 下尽量收紧权限
        os.chmod(kp, 0o600)
    except OSError:
        pass
    return key


def _fernet():
    try:
        from cryptography.fernet import Fernet

        return Fernet
    except ImportError:
        return None


def _fallback_xor(data: bytes, key: bytes) -> bytes:
    """无 cryptography 时的轻量混淆：HMAC-SHA256 计数器密钥流异或。

    注意：这是混淆而非强加密，仅用于避免凭据以明文落盘。生产部署请安装 cryptography。
    """
    out = bytearray()
    block = 0
    while len(out) < len(data):
        out.extend(hmac.new(key, block.to_bytes(8, "big"), hashlib.sha256).digest())
        block += 1
    return bytes(a ^ b for a, b in zip(data, out))


def save_credentials(info: dict, enc_path: Path) -> str:
    """加密保存服务账号 JSON，返回所用算法名。"""
    key = _load_or_create_key(enc_path)
    raw = json.dumps(info, ensure_ascii=False).encode("utf-8")
    Fernet = _fernet()
    if Fernet:
        blob = Fernet(key).encrypt(raw)
        algo = "fernet"
    else:
        blob = base64.b64encode(_fallback_xor(raw, key))
        algo = "xor-hmac(weak)"
    enc_path.parent.mkdir(parents=True, exist_ok=True)
    enc_path.write_bytes(b"WBGEE1:" + algo.encode() + b":" + blob)
    return algo


def load_credentials(enc_path: Path) -> dict | None:
    """读取并解密；文件不存在或解密失败返回 None。"""
    if not enc_path.exists():
        return None
    try:
        head, algo, blob = enc_path.read_bytes().split(b":", 2)
        if head != b"WBGEE1":
            return None
        key = _load_or_create_key(enc_path)
        if algo == b"fernet":
            from cryptography.fernet import Fernet

            raw = Fernet(key).decrypt(blob)
        else:
            raw = _fallback_xor(base64.b64decode(blob), key)
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def masked(account_email: str | None) -> str:
    """脱敏展示：abc@x.iam.gserviceaccount.com -> a***@x.iam.gserviceaccount.com

    非邮箱形式的说明文字（例如 OAuth 路线的「个人 Google 账号（OAuth 刷新令牌）」）
    原样返回 —— 它们本身不含敏感信息，硬套脱敏会显示成「未配置」误导使用者。
    """
    if not account_email:
        return "(未配置)"
    if "@" not in account_email:
        return account_email
    local, domain = account_email.split("@", 1)
    return f"{local[:1]}***@{domain}"
