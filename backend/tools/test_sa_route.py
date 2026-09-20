"""服务账号路线的端到端验证（不需要真实的服务账号）。

背景：`gee_auth.resolve()` 按 A(服务账号文件) → B(加密凭据库) → C(个人 OAuth) 的顺序选，
**B 的优先级高于 C**。所以在真实位置写一个假凭据会把正在工作的 OAuth 顶掉、直接搞坏服务。
本脚本一律用 `GEE_CREDENTIALS_ENC` 指向临时目录，做完就删，不碰真实位置。

验的是「管道通不通」，不是「这个服务账号在 Google 侧存不存在」：
用本地生成的真 RSA 私钥 + 伪造的 client_email 组成结构合法的服务账号 JSON，
应当能通过解析 / 加密 / 落盘 / 回读 / 脱敏展示；向 Google 换令牌则必然失败，
那条失败路径本身也要验（用户配错时看到的就是它）。

运行：.venv\\Scripts\\python.exe tools/test_sa_route.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = str(BASE / ".venv" / "Scripts" / "python.exe")
DOCTOR = str(BASE / "tools" / "gee_doctor.py")

results: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    results.append(bool(cond))
    line = ("  PASS  " if cond else "  FAIL  ") + name
    if extra:
        line += "  -> " + str(extra)
    print(line)


def run_doctor(args: list[str], enc_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["GEE_CREDENTIALS_ENC"] = str(enc_path)
    return subprocess.run(
        [PY, DOCTOR, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(BASE), timeout=180,
    )


def run_py(code: str, enc_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["GEE_CREDENTIALS_ENC"] = str(enc_path)
    return subprocess.run(
        [PY, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(BASE), timeout=180,
    )


def make_fake_sa() -> dict:
    """造一个结构合法但 Google 侧不存在的服务账号 JSON。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "type": "service_account",
        "project_id": "fake-sa-probe-000000",
        "private_key_id": "0" * 40,
        "private_key": pem,
        "client_email": "probe@fake-sa-probe-000000.iam.gserviceaccount.com",
        "client_id": "100000000000000000000",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sa_route_"))
    enc = tmp / "gee.enc"
    print(f"临时凭据路径：{enc}\n")

    sa = make_fake_sa()
    sa_file = tmp / "fake-sa.json"
    sa_file.write_text(json.dumps(sa, ensure_ascii=False), encoding="utf-8")
    marker = sa["private_key"].splitlines()[1][:28]  # PEM body 的一小段，用来查是否明文落盘

    try:
        print("=== 1. 非法输入的拦截 ===")
        r = run_doctor(["--import-sa", str(tmp / "nope.json")], enc)
        check("文件不存在 -> 非 0 退出", r.returncode != 0, r.returncode)
        check("提示「文件不存在」", "文件不存在" in r.stdout)

        bad = tmp / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        r = run_doctor(["--import-sa", str(bad)], enc)
        check("非法 JSON -> 非 0 退出", r.returncode != 0, r.returncode)
        check("提示「不是合法的 JSON」", "不是合法的 JSON" in r.stdout)

        oauth_client = tmp / "oauth.json"
        oauth_client.write_text(json.dumps({"type": "installed", "client_id": "x"}), encoding="utf-8")
        r = run_doctor(["--import-sa", str(oauth_client)], enc)
        check("OAuth 客户端 JSON 被识别并拒收", r.returncode != 0 and "service_account" in r.stdout)
        check("说明两条路线不同", "另一条路线" in r.stdout)

        incomplete = tmp / "incomplete.json"
        d = dict(sa)
        d.pop("private_key")
        incomplete.write_text(json.dumps(d), encoding="utf-8")
        r = run_doctor(["--import-sa", str(incomplete)], enc)
        check("缺 private_key -> 拒收", r.returncode != 0 and "private_key" in r.stdout)

        check("以上均未产生密文文件", not enc.exists())

        print("\n=== 2. 正常导入（结构合法的假服务账号）===")
        r = run_doctor(["--import-sa", str(sa_file)], enc)
        out = r.stdout
        check("导入退出码为 0（文件已落盘）", r.returncode == 0, r.returncode)
        check("提示已加密保存", "已加密保存" in out)
        check("算法为强加密 fernet", "fernet" in out, out.split("算法=")[-1].split("）")[0] if "算法=" in out else "?")
        check("打印了服务账号邮箱", sa["client_email"] in out)
        check("打印了项目 ID", sa["project_id"] in out)
        check("密文文件已生成", enc.exists())
        check("密钥文件已生成", (tmp / ".key").exists())

        print("\n=== 3. 安全性：密文不得泄露私钥 ===")
        blob = enc.read_bytes()
        check("密文里搜不到私钥片段", marker.encode() not in blob)
        check("密文里搜不到 client_email 明文", sa["client_email"].encode() not in blob)
        check("密文带版本头 WBGEE1:", blob.startswith(b"WBGEE1:"))
        check("密钥与密文分开存放", (tmp / ".key").read_bytes() != blob)
        check("密钥长度符合 Fernet(44 字节 base64)", len((tmp / ".key").read_bytes().strip()) == 44,
              len((tmp / ".key").read_bytes().strip()))

        print("\n=== 4. 回读：gee_auth.resolve() 能选中加密凭据库 ===")
        code = (
            "import json;"
            "from app.gee_auth import resolve;"
            "creds, info = resolve();"
            "print(json.dumps({'ok': info.ok, 'source': info.source,"
            " 'account_masked': __import__('app.secure_store', fromlist=['x']).masked(info.account),"
            " 'project': info.project}, ensure_ascii=False))"
        )
        r = run_py(code, enc)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        check("resolve() 有输出", bool(line), r.stdout[-200:] + r.stderr[-200:])
        if line:
            got = json.loads(line[-1])
            check("凭据可用", got["ok"] is True)
            check("来源为 encrypted_store（优先级高于 oauth）", got["source"] == "encrypted_store", got["source"])
            check("账户已脱敏（p***@…）", got["account_masked"].startswith("p***@"), got["account_masked"])
            check("脱敏后不含完整本地名", "probe@" not in got["account_masked"], got["account_masked"])
            check("项目 ID 读出正确", got["project"] == sa["project_id"], got["project"])

        print("\n=== 5. 向 Google 换令牌：假凭据必须失败，且提示要可读 ===")
        check("输出含「验证未通过」", "验证未通过" in out)
        check("给出了后续授权步骤", "Earth Engine" in out and "register" in out)
        check("明确点出两项 IAM 授权", "Service Usage Consumer" in out)
        check("未把失败伪装成成功", "令牌获取成功" not in out)

        print("\n=== 6. 真实位置未被污染（服务当前仍走 OAuth）===")
        real = BASE / "data" / "credentials" / "gee.enc"
        check("真实 gee.enc 未被创建", not real.exists(), str(real))
        code2 = (
            "import json;"
            "from app.gee_auth import resolve;"
            "_, info = resolve();"
            "print(json.dumps({'source': info.source, 'ok': info.ok}))"
        )
        r = subprocess.run([PY, "-c", code2], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                           cwd=str(BASE), timeout=180)
        ln = [x for x in r.stdout.splitlines() if x.startswith("{")]
        if ln:
            got = json.loads(ln[-1])
            check("真实环境来源仍为 oauth", got["source"] == "oauth", got["source"])
            check("真实环境凭据可用", got["ok"] is True)

        print("\n=== 7. 导入时必须提示「会抢占 OAuth 优先级」===")
        check("提示了当前生效的凭据来源", "当前生效的凭据" in out, )
        check("明确警告优先级高于 OAuth", "优先级" in out and "取代" in out)
        check("给出了回退命令 --drop-sa", "--drop-sa" in out)

        print("\n=== 8. 回退：--drop-sa 后应落回 OAuth ===")
        r = run_doctor(["--drop-sa"], enc)
        check("--drop-sa 退出码为 0", r.returncode == 0, r.returncode)
        check("报告已删除密文", "已删除" in r.stdout)
        check("密文已不存在", not enc.exists())
        check("密钥已不存在", not (tmp / ".key").exists())
        check("回退后报告来源为 oauth", "oauth" in r.stdout, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")

        r = run_doctor(["--drop-sa"], enc)
        check("重复执行 --drop-sa 幂等", r.returncode == 0 and "已是回退状态" in r.stdout)

        print("\n=== 9. 重新导入后再次生效（可逆性）===")
        r = run_doctor(["--import-sa", str(sa_file)], enc)
        check("重新导入成功", r.returncode == 0 and enc.exists())
        r = run_py(code, enc)
        ln = [x for x in r.stdout.splitlines() if x.startswith("{")]
        if ln:
            got = json.loads(ln[-1])
            check("再次选中 encrypted_store", got["source"] == "encrypted_store", got["source"])

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
