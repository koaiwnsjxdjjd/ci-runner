#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghbox 一键无缝部署脚本 v2（彻底解决部署慢 + 隧道冲突）

流程：
  1. 推送代码到 master + 所有 fork
  2. 取消所有旧 manager/worker run（避免并行抢隧道 + 防止 run 堆积排队）
  3. 触发新 manager
  4. 触发所有 running worker（错峰 3s）
  5. 轮询等待 manager + 所有 worker 就绪
  6. 输出部署报告

用法：
  python3 deploy.py [--skip-push] [--only manager|worker|all]

环境变量：
  GH_TOKEN        主账号 token（必填）
  DEPLOY_ACCOUNTS 账号配置 JSON（可选，默认用内置账号）
"""
import os
import sys
import time
import json
import subprocess
import urllib.request
import urllib.error

# ==================== 配置 ====================
MAIN_REPO = "qqztceghrgji/demo-vps"
MAIN_HOST = "ghvps2.kekeke.cc.cd"
# 默认账号（token 从环境变量取）
ACCOUNTS = [
    {"name": "acc3", "repo": "wddrjdjhfurj/demo-vps", "token_env": "ACC3_TOKEN"},
    {"name": "bb2", "repo": "wwqqtybydvyc/demo-vps", "token_env": "BB2_TOKEN"},
]
# 实例归属：inst_id -> 账号名
INSTANCE_ACCOUNT = {
    "inst12": "acc3", "inst14": "acc3", "inst16": "acc3",
    "inst13": "bb2", "inst15": "bb2",
}


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def api(method, url, token, data=None, timeout=30):
    """GitHub API 请求（dict 用 JSON 发送）"""
    h = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    body = None
    if data is not None:
        h["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = r.read()
            try:
                return r.status, json.loads(content.decode() or "null")
            except Exception:
                return r.status, content.decode()
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content.decode() or "null")
        except Exception:
            return e.code, content.decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def get_token(acc):
    """获取账号 token（环境变量或主 token）"""
    return os.environ.get(acc.get("token_env", ""), "") or os.environ.get("GH_TOKEN", "")


# ==================== 1. 推送代码 ====================
def push_code():
    print("=" * 60)
    print("[1/6] 推送代码到所有仓库...")
    remotes = {
        "master": f"https://ghp_{os.environ.get('GH_TOKEN','')}@github.com/{MAIN_REPO}.git" if False else None,
    }
    # 用本地 git remote 推送（已有配置）
    cmds = [
        "cd " + os.path.expanduser("~/ghbox") + " && git add -A && git -c user.email=ghbox@local -c user.name=ghbox commit -m 'deploy update' --allow-empty 2>&1 | tail -1",
        "cd " + os.path.expanduser("~/ghbox") + " && git push newfork HEAD:main 2>&1 | tail -1",
        "cd " + os.path.expanduser("~/ghbox") + " && git push acc3fork HEAD:main 2>&1 | tail -1",
        "cd " + os.path.expanduser("~/ghbox") + " && git push bb2fork HEAD:main 2>&1 | tail -1",
    ]
    for c in cmds:
        code, out, err = run(c)
        print(f"  {out.strip() or err.strip()}")
        time.sleep(1)
    print("✅ 代码已推送")


# ==================== 2. 取消所有旧 run ====================
def cancel_all_runs():
    print("[2/6] 清理所有旧 run（防并行抢隧道 + 防堆积）...")
    repos = [(MAIN_REPO, os.environ.get("GH_TOKEN", ""))]
    for acc in ACCOUNTS:
        repos.append((acc["repo"], get_token(acc)))
    for repo, token in repos:
        if not token:
            continue
        try:
            status, d = api("GET", f"https://api.github.com/repos/{repo}/actions/runs?status=in_progress&per_page=100", token)
            if status == 200:
                runs = [r for r in d.get("workflow_runs", []) if r.get("path") in ("manager.yml", "worker.yml")]
                for r in runs:
                    api("POST", f"https://api.github.com/repos/{repo}/actions/runs/{r['id']}/cancel", token)
                    print(f"  ✗ cancel {repo} #{r['id']} ({r['path']})")
        except Exception as e:
            print(f"  ⚠️ {repo} 清理失败: {e}")
    print("✅ 旧 run 已清理")


# ==================== 3. 触发新 manager ====================
def dispatch_manager(token=None):
    token = token or os.environ.get("GH_TOKEN", "")
    print("[3/6] 触发新 manager...")
    url = f"https://api.github.com/repos/{MAIN_REPO}/actions/workflows/manager.yml/dispatches"
    status, d = api("POST", url, token, {"ref": "main"})
    if status in (200, 204):
        print("✅ manager 已触发")
        return True
    print(f"❌ manager 触发失败: {status} {d}")
    return False


# ==================== 4. 触发所有 worker ====================
def dispatch_workers():
    print("[4/6] 触发所有 worker（错峰 3s）...")
    for inst_id, acc_name in INSTANCE_ACCOUNT.items():
        acc = next((a for a in ACCOUNTS if a["name"] == acc_name), None)
        if not acc:
            continue
        token = get_token(acc)
        if not token:
            continue
        url = f"https://api.github.com/repos/{acc['repo']}/actions/workflows/worker.yml/dispatches"
        status, d = api("POST", url, token, {"ref": "main", "inputs": {"INSTANCE_ID": inst_id}})
        print(f"  {inst_id} -> {'✅' if status in (200,204) else f'❌ {status}'}")
        time.sleep(3)
    print("✅ worker 已全部触发")


# ==================== 5. 等待就绪 ====================
def wait_ready(timeout=300):
    print(f"[5/6] 等待服务就绪（最长 {timeout}s）...")
    # manager
    mgr_ready = False
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(f"https://{MAIN_HOST}/api/health",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    mgr_ready = True
                    break
        except Exception:
            pass
        time.sleep(5)
    print(f"  Manager: {'✅ 就绪' if mgr_ready else '⚠️ 超时'}")
    # workers
    for inst_id in INSTANCE_ACCOUNT:
        ok = False
        start = time.time()
        while time.time() - start < timeout:
            try:
                req = urllib.request.Request(f"https://{inst_id}.kekeke.cc.cd/api/health",
                                             headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:
                pass
            time.sleep(5)
        print(f"  {inst_id}: {'✅ 就绪' if ok else '⚠️ 超时'}")
    return mgr_ready


# ==================== 主流程 ====================
def main():
    skip_push = "--skip-push" in sys.argv
    only = None
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        if len(sys.argv) > idx + 1:
            only = sys.argv[idx + 1]

    if not os.environ.get("GH_TOKEN"):
        print("❌ 需要 GH_TOKEN 环境变量")
        sys.exit(1)

    print("\n🚀 ghbox 一键部署开始")
    if not skip_push:
        push_code()
    else:
        print("[1/6] 跳过推送")
    cancel_all_runs()
    time.sleep(3)
    if only in (None, "manager"):
        dispatch_manager()
    if only in (None, "worker"):
        time.sleep(3)
        dispatch_workers()
    print("[6/6] 部署完成！等待验证...")
    time.sleep(5)
    wait_ready()


if __name__ == "__main__":
    main()