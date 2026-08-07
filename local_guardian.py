#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地保命脚本（Local Guardian，在本地 Termux 运行）

- 检测 GitHub Actions 状态，故障时创建 Codespaces 保命实例
- 监控保命账号配额，快用完切换下一个健康账号
- 探测 Actions 恢复后触发迁回

用法：
  python3 local_guardian.py --once    # 检测一次并处理
  python3 local_guardian.py --monitor # 持续监控（后台）

配置（环境变量）：
  SURVIVAL_ACCOUNTS='[{"name":"acc3","token":"ghp_xxx","repo":"owner/repo"},...]'
  SURVIVAL_REPO='owner/repo'
"""
import os
import sys
import time
import json
import argparse
import urllib.request
import urllib.error

# ==================== 配置（环境变量） ====================
GITHUB_COMPONENTS_URL = os.environ.get(
    "GITHUB_COMPONENTS_URL",
    "https://www.githubstatus.com/api/v2/components.json")
ACCOUNTS_POOL = json.loads(os.environ.get("SURVIVAL_ACCOUNTS", "[]"))
QUOTA_THRESHOLD = float(os.environ.get("QUOTA_SWITCH_THRESHOLD", "0.15"))
CHECK_INTERVAL = int(os.environ.get("SURVIVAL_CHECK_INTERVAL", "300"))
DEVCONTAINER = os.environ.get("SURVIVAL_DEVCONTAINER", ".devcontainer/devcontainer.json")
SURVIVAL_REPO = os.environ.get("SURVIVAL_REPO", "qqztceghrgji/demo-vps")


def gh_request(method, url, token=None, data=None, timeout=30):
    h = {"Authorization": f"token {token}" if token else "",
         "Accept": "application/vnd.github.v3+json",
         "Content-Type": "application/json",
         "User-Agent": "Mozilla/5.0 (ghbox-local-guardian)"}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def check_actions():
    """检测 GitHub Actions 状态，返回 (ok, reason)"""
    try:
        req = urllib.request.Request(GITHUB_COMPONENTS_URL,
                                     headers={"User-Agent": "Mozilla/5.0 (ghbox-guardian)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        for c in d.get("components", []):
            if "actions" in c.get("name", "").lower():
                st = c.get("status", "").lower()
                if st in ("operational", "degraded_performance"):
                    return True, st
                return False, st
        return True, "unknown"
    except Exception as e:
        return True, f"status_api_error:{e}"


def check_rate_limit(token):
    status, d = gh_request("GET", "https://api.github.com/rate_limit", token=token)
    if status != 200:
        return 0, 0
    core = d.get("resources", {}).get("core", {})
    return core.get("remaining", 0), core.get("limit", 0)


def pick_survival_account():
    """选健康度最高的账号"""
    best = None
    best_score = -1
    for acc in ACCOUNTS_POOL:
        try:
            remaining, limit = check_rate_limit(acc.get("token", ""))
            ratio = remaining / limit if limit else 0
            if ratio > QUOTA_THRESHOLD and ratio > best_score:
                best = acc
                best_score = ratio
        except Exception:
            continue
    return best


def create_codespace(account):
    """创建保命 Codespace"""
    try:
        status, repo = gh_request("GET", f"https://api.github.com/repos/{account['repo']}",
                                  token=account["token"])
        if status != 200:
            print(f"  ❌ 获取仓库失败: {status}")
            return None
        repo_id = repo.get("id")
        status, cs = gh_request("POST", "https://api.github.com/user/codespaces",
                                token=account["token"],
                                data={"repository_id": repo_id, "ref": "main",
                                      "devcontainer_path": DEVCONTAINER,
                                      "machine": "basicLinux32gb", "geo": "UsWest"})
        if status in (200, 201, 202):
            print(f"  ✅ Codespace 创建成功: {cs.get('name')}")
            return cs
        print(f"  ❌ Codespace 创建失败: {status} {cs}")
    except Exception as e:
        print(f"  ❌ Codespace 异常: {e}")
    return None


def trigger_actions_recovery():
    """触发 Actions 恢复（manager + worker）"""
    try:
        url = f"https://api.github.com/repos/{SURVIVAL_REPO}/actions/workflows/manager.yml/dispatches"
        status, _ = gh_request("POST", url,
                               token=ACCOUNTS_POOL[0].get("token") if ACCOUNTS_POOL else None,
                               data={"ref": "main"})
        print(f"  ✅ 触发 manager 恢复: {status}")
    except Exception as e:
        print(f"  ❌ 触发 manager 恢复失败: {e}")


def handle_once():
    """单次检测并处理"""
    ok, reason = check_actions()
    print(f"Actions 状态: {reason}")
    if ok:
        print("  ✅ Actions 正常，无需保命")
        return False
    print("  ⚠️ Actions 故障，进入保命")
    acc = pick_survival_account()
    if not acc:
        print("  ❌ 无健康账号（配额不足），保命失败")
        return False
    print(f"  选择账号: {acc.get('name')}")
    cs = create_codespace(acc)
    return cs is not None


def monitor():
    """持续监控"""
    print(f"=== 本地保命监控启动（每 {CHECK_INTERVAL}s 检测）===")
    survival_active = False
    while True:
        try:
            ok, reason = check_actions()
            print(f"[{time.strftime('%H:%M:%S')}] Actions: {reason}")
            if ok:
                if survival_active:
                    print("  ✅ Actions 恢复，迁回")
                    trigger_actions_recovery()
                    survival_active = False
            else:
                if not survival_active:
                    print("  ⚠️ 故障，创建保命")
                    acc = pick_survival_account()
                    if acc and create_codespace(acc):
                        survival_active = True
        except Exception as e:
            print(f"  监控异常: {e}")
        time.sleep(CHECK_INTERVAL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", action="store_true", help="持续监控")
    parser.add_argument("--once", action="store_true", help="单次检测")
    args = parser.parse_args()

    if not ACCOUNTS_POOL:
        print("⚠️ 未配置保命账号池（环境变量 SURVIVAL_ACCOUNTS）")
        print("示例: SURVIVAL_ACCOUNTS='[{\"name\":\"acc3\",\"token\":\"ghp_xxx\",\"repo\":\"owner/repo\"}]'")
        sys.exit(1)

    if args.monitor:
        monitor()
    else:
        handle_once()


if __name__ == "__main__":
    main()