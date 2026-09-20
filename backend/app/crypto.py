"""业务数据字段级加密（任务书 PRD §7「数据隐私」）。

## 为什么需要它

PRD §7 原文：「用户区域/任务描述敏感，**加密存储**；本地化部署；不用于服务以外用途」。
在此之前，`tasks` 表里的这几个字段是**明文落盘**的：

| 字段 | 实际内容 | 敏感原因 |
|---|---|---|
| `region` | 「长江三角洲」以及用户自由输入的区域描述 | 暴露用户在关注哪些地理目标 |
| `code` | 完整可读的 GEE 脚本，含 AOI 坐标 | 坐标 + 时间 + 指标 = 可还原完整的分析意图 |
| `result` | 图层元数据、图表序列、统计量 | 含统计结果与波段信息 |
| `logs` | 生成/执行/调试全过程 | 含中间代码片段与错误上下文 |
| `feedback_note` | 用户自由填写的补充说明 | 自由文本，可能含个人信息 |

2026-09-20 实测确认：从生产库里直接捞一条记录，`region` 就是明文「长江三角洲」，
`code` 是完整可读的 GEE 脚本 —— 这一条确实没过。

## 设计要点

**1. 复用 `secure_store.py` 的既有机制，而不是另起一套。**
GEE 凭据加密已经在生产里跑了一阵了（Fernet + `.key` 文件 + 算法标记头），
把同一套东西用在业务字段上，密码学上的假设是同一个，不会多出一个需要单独审计的路径。

**2. 值前缀标记法（`ENC1:`）而不是"整库加密"或"加列标记"。**

   - 不做整库加密（SQLCipher 之类）：那要求换掉整个 sqlite 驱动，且让
     `PRAGMA`/索引/迁移全部变形，改动面远大于收益。
   - 不加 `xxx_enc INTEGER` 标记列：加列要改 DDL、要维护列与列的同步，
     而**前缀标记把"这一个值是否已加密"这件事放在值自己身上**，
     迁移时一行 `SELECT` 就能筛出待处理行，也不怕漏改某个写路径。

**3. 前缀标记让迁移可以幂等。**

   这正是"全加密"方案能安全落地的关键：`encrypt_field()` 遇到已带前缀的值会原样返回，
   所以迁移脚本重复跑不会二次加密（二次加密 = 数据永久损坏，且不可逆）。
   同理 `decrypt_field()` 对没有前缀的值原样返回 —— 于是**加解密是"可重入"的**，
   迁移中途断电、半新半旧的数据也能继续读写。

**4. 密钥可被环境变量覆盖。**

   `.key` 文件模式适合本机演示；但真要部署到别的机器时，把密钥跟着代码目录走
   并不安全。所以优先读 `WB_DATA_KEY`（base64 的 Fernet key），没有才回落到文件。
   这样两种部署形态都不用改代码。

**5. 解密失败不抛异常，而是返回一个明确的占位符。**

   什么情况会解密失败：密钥文件被换/丢了、数据被外部改过。
   这时若抛异常，会让**整个任务列表接口 500** —— 一条坏数据拖垮整个页面。
   更好的做法是只让那一条显示为"(数据无法解密)"，其余照常。
   这与项目里"单条坏数据不该让列表挂掉"的一贯做法一致。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import secure_store

#: 加密值的统一前缀。带它 = 已加密；不带 = 明文（历史数据）。
#: 这个常量就是"迁移是否完成"的唯一判据，别改名。
ENC_PREFIX = "ENC1:"

#: 密钥文件位置：backend/data/credentials/.data_key
CRED_DIR = Path(__file__).resolve().parent.parent / "data" / "credentials"
DATA_KEY_PATH = CRED_DIR / ".data_key"

#: 无法解密时的占位符。**必须显式可见** —— 静默返回空串会让人以为"这个字段本来就是空的"。
UNDECRYPTABLE = "(数据无法解密)"

#: 需要加密的 tasks 字段。改这个集合就等于改加密范围。
ENCRYPTED_TASK_FIELDS = ("region", "code", "result", "logs", "feedback_note")

#: regions 表需要加密的字段。
ENCRYPTED_REGION_FIELDS = ("name", "desc")

_KEY_ENV = "WB_DATA_KEY"

_warned = False


def _key_path() -> Path:
    """密钥文件路径。测试可用 `WB_DATA_KEY_PATH` 指向临时文件，避免污染生产密钥。"""
    override = os.getenv("WB_DATA_KEY_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return DATA_KEY_PATH


def _resolve_key() -> bytes:
    """取加密密钥。优先环境变量，其次密钥文件（不存在则生成）。

    ⚠ 不能直接调 `secure_store._load_or_create_key(路径)`：那个函数内部会把路径
    改写成 `路径.parent / ".key"`（它原本是给 `gee.enc` 这种文件用的，
    约定密钥就叫同目录的 `.key`）。直接传 `DATA_KEY_PATH` 会被改写成
    `credentials/.key` —— 与 `crypto_status()` 报出来的路径**不是同一个文件**，
    于是出现"报的路径没这个文件、加解密却能用"的诡异状态。
    所以这里自己读写密钥文件，确保"用的"和"报的"永远是同一个路径。
    """
    env = os.getenv(_KEY_ENV, "").strip()
    if env:
        return env.encode("utf-8")

    kp = _key_path()
    if kp.exists():
        return kp.read_bytes().strip()

    kp.parent.mkdir(parents=True, exist_ok=True)
    try:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
    except ImportError:
        import base64

        key = base64.urlsafe_b64encode(os.urandom(32))
    kp.write_bytes(key)
    try:  # Windows 下尽量收紧权限（非 POSIX 上会静默失败，属预期）
        os.chmod(kp, 0o600)
    except OSError:
        pass
    print("[crypto] 已生成业务数据加密密钥：%s" % kp)
    return key


def is_encrypted(value) -> bool:
    """值是否已加密。用于迁移筛选与幂等判断。"""
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def _fallback_available() -> bool:
    return secure_store._fernet() is None


def encrypt_field(value: str | None) -> str | None:
    """加密单个字段值。**幂等**：已加密的原样返回（防止二次加密毁数据）。

    `None` 与空串原样返回 —— 给空值套一层密文只会让库里多出一堆无意义的行，
    而且 `NULL` 在 `feedback IS NOT NULL` 这类判断里语义不同，不该被替换掉。
    """
    if value is None or value == "":
        return value
    if is_encrypted(value):
        return value
    key = _resolve_key()
    raw = value.encode("utf-8")
    Fernet = secure_store._fernet()
    if Fernet:
        blob = Fernet(key).encrypt(raw).decode("ascii")
    else:
        import base64

        blob = base64.b64encode(secure_store._fallback_xor(raw, key)).decode("ascii")
    return ENC_PREFIX + blob


def decrypt_field(value: str | None) -> str | None:
    """解密单个字段值。**幂等**：没有前缀的（明文/历史数据）原样返回。

    解密失败返回 `UNDECRYPTABLE` 占位符而不是抛异常 —— 一条坏数据不该
    让整个列表接口 500。
    """
    global _warned
    if value is None or value == "":
        return value
    if not is_encrypted(value):
        return value  # 明文（迁移前的历史行），照常返回
    blob = value[len(ENC_PREFIX):]
    try:
        key = _resolve_key()
        Fernet = secure_store._fernet()
        if Fernet:
            raw = Fernet(key).decrypt(blob.encode("ascii"))
        else:
            import base64

            raw = secure_store._fallback_xor(base64.b64decode(blob), key)
        return raw.decode("utf-8")
    except Exception as e:  # noqa: BLE001
        if not _warned:
            _warned = True
            print(
                "[warn] 业务数据解密失败（%s）——密钥可能被更换或数据被外部修改。"
                "受影响字段显示为 %s。" % (type(e).__name__, UNDECRYPTABLE)
            )
        return UNDECRYPTABLE


#: 盲索引前缀。用于"需要按明文做等值查询"的字段（如 regions.name 是主键一部分）。
BLIND_PREFIX = "BIDX1:"


def blind_index(plaintext: str | None) -> str | None:
    """盲索引：确定性的 HMAC-SHA256，让密文字段**仍能做等值查询**。

    ## 为什么必须有这个东西

    Fernet 每次加密都会带一个新的随机 IV，同一明文两次加密**结果不同**。
    这对 `region` / `code` 这类"只按 task_id 取"的字段毫无影响，
    但 `regions.name` 是**联合主键的一部分**，`delete_region()` 要靠
    `WHERE name=?` 定位那一行 —— 拿明文重新加密后的密文与库里的密文**永不相等**，
    删除功能直接失效（2026-09-20 实测：32 项验证里正好挂在这一条）。

    ## 它安全吗

    盲索引是**确定性**的，所以它确实泄露"两行的明文是否相同"这一个 bit。
    这是所有"可搜索加密"的固有代价，业界通行做法（Django 的
    `django-cryptography`、AWS 的 DynamoDB 加密客户端都是这套）。
    但它**不泄露明文本身**：没有密钥就无法从 `BIDX1:...` 反推原文，
    也无法与任何已知明文比对（HMAC 密钥只有服务端有）。

    对本项目这个取舍是划算的：区域名是短且低熵的文本
    （"我的测试区"这种），本来也无法靠加密保护"是否叫这个名字"，
    而删除功能坏掉是**立刻可见的产品缺陷**。

    ⚠ 调用前先 `strip()`：同一区域名带不带尾空格应当算同一个，
    否则又是"看着一样但删不掉"。
    """
    if plaintext is None or plaintext == "":
        return plaintext
    import hashlib
    import hmac as _hmac

    key = _resolve_key()
    mac = _hmac.new(key, ("bidx:" + plaintext.strip()).encode("utf-8"), hashlib.sha256)
    return BLIND_PREFIX + mac.hexdigest()


def is_blind_index(value) -> bool:
    return isinstance(value, str) and value.startswith(BLIND_PREFIX)


def crypto_status() -> dict:
    """加密设施现状。给 `demo_doctor` / 健康检查用，让"到底加没加密"可被验证。"""
    kp = _key_path()
    return {
        "enabled": True,
        "prefix": ENC_PREFIX,
        "algorithm": "fernet" if secure_store._fernet() else "xor-hmac(weak)",
        "key_source": "env:" + _KEY_ENV if os.getenv(_KEY_ENV, "").strip() else "file",
        "key_path": str(kp),
        "key_exists": kp.exists(),
        "fields": list(ENCRYPTED_TASK_FIELDS),
    }
