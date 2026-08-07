#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地数据冗余备份：从各账号 GitHub Releases 下载加密数据到本地

（双保险：即使 GitHub 出问题，本地也有密文备份，配合 DEMO_KEY 可解密）

用法：
  python3 local_backup.py [--decrypt] [--keep N]

配置（环境变量，不硬编码 secrets）：
  BACKUP_ACCOUNTS='[{"name":"acc3","token":"ghp_xxx","repo":"owner/repo"},...]'
  DEMO_KEY='hex64位'
"""
import os
import sys
import json
import time
import shutil
import urllib.request
import urllib.error

# ==================== 配置（环境变量） ====================
_ACCOUNTS_ENV = os.environ.get("BACKUP_ACCOUNTS", "[]")
try:
    ACCOUNTS = json.loads(_ACCOUNTS_ENV)
except Exception:
    ACCOUNTS = []
DEMO_KEY = os.environ.get("DEMO_KEY", "")
BACKUP_DIR = os.path.expanduser("~/backups/ghbox-data")
KEEP_BACKUPS = 5


def fetch_json(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "User-Agent": "Mozilla/5.0 (ghbox-backup)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def download_assets(repo, token, account_dir):
    """下载账号 Releases 的所有 asset（加密密文）"""
    try:
        rel = fetch_json(f"https://api.github.com/repos/{repo}/releases/tags/backup", token)
    except Exception as e:
        print(f"  ❌ {repo}: 获取 release 失败: {e}")
        return 0
    assets = rel.get("assets", [])
    count = 0
    for a in assets:
        name = a["name"]
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/releases/assets/{a['id']}",
                headers={"Authorization": f"token {token}",
                         "Accept": "application/octet-stream",
                         "User-Agent": "Mozilla/5.0 (ghbox-backup)"})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            path = os.path.join(account_dir, name)
            with open(path, "wb") as f:
                f.write(data)
            count += 1
        except Exception as e:
            print(f"  ⚠️ {name}: 下载失败 {e}")
    return count


def verify_backup(account_dir):
    """验证：尝试解密一个加密文件"""
    if not DEMO_KEY:
        return False
    from Crypto.Cipher import AES
    try:
        key = bytes.fromhex(DEMO_KEY)
        for f in os.listdir(account_dir):
            if f.endswith((".db.enc", ".json.enc")):
                blob = open(os.path.join(account_dir, f), "rb").read()
                if len(blob) > 32:
                    nonce, tag, ct = blob[:16], blob[16:32], blob[32:]
                    data = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
                    print(f"  ✅ {f}: 解密验证通过 ({len(data)} 字节)")
                    return True
    except Exception as e:
        print(f"  ⚠️ 解密验证失败: {e}")
    return False


def clean_old_backups():
    """保留最近 KEEP_BACKUPS 份备份"""
    try:
        entries = [d for d in os.listdir(BACKUP_DIR)
                   if os.path.isdir(os.path.join(BACKUP_DIR, d)) and len(d) == 8]
        entries.sort(reverse=True)
        for old in entries[KEEP_BACKUPS:]:
            shutil.rmtree(os.path.join(BACKUP_DIR, old), ignore_errors=True)
            print(f"  清理旧备份: {old}")
    except Exception:
        pass


def main():
    if not ACCOUNTS:
        print("⚠️ 未配置账号池（环境变量 BACKUP_ACCOUNTS）")
        print("示例: BACKUP_ACCOUNTS='[{\"name\":\"acc3\",\"token\":\"ghp_xxx\",\"repo\":\"owner/repo\"}]'")
        sys.exit(1)
    ts = time.strftime("%Y%m%d_%H%M")
    ts_dir = os.path.join(BACKUP_DIR, ts)
    os.makedirs(ts_dir, exist_ok=True)
    print(f"=== 本地数据备份 {ts} ===")
    total = 0
    for acc in ACCOUNTS:
        print(f"--- {acc['name']} ({acc['repo']}) ---")
        account_dir = os.path.join(ts_dir, acc["name"])
        os.makedirs(account_dir, exist_ok=True)
        n = download_assets(acc["repo"], acc["token"], account_dir)
        total += n
        print(f"  下载 {n} 个 asset")
        if "--decrypt" in sys.argv:
            verify_backup(account_dir)
    clean_old_backups()
    print(f"✅ 备份完成，共 {total} 个文件 → {ts_dir}")


if __name__ == "__main__":
    main()