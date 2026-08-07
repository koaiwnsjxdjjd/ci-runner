#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
帝国自动更新脚本：主仓库 push 后自动执行
① 同步所有账号 fork 仓库到最新
② 触发所有 running worker 滚动重启（无缝）
③ 触发新 manager
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from core import storage
from core import ghapi


def main():
    print("=== 帝国自动更新启动 ===", flush=True)
    accounts = storage.load_json_enc(config.ASSET_ACCOUNTS, default=[])
    instances = storage.load_json_enc(config.ASSET_INSTANCES, default=[])
    print(f"账号数: {len(accounts)} | 实例数: {len(instances)}", flush=True)

    # 1. 同步所有 fork
    print("--- 同步所有 fork ---", flush=True)
    for acc in accounts:
        repo = acc.get("repo") or config.REPO
        if repo == config.REPO:
            continue
        url = f"{ghapi.API_BASE}/repos/{repo}/merge-upstream"
        status, _ = ghapi.gh_request("POST", url, acc.get("token"), {"branch": "main"})
        print(f"  {repo}: HTTP {status}", flush=True)
        time.sleep(2)

    # 2. 滚动重启所有 running worker
    print("--- 滚动重启 worker ---", flush=True)
    running = [i for i in instances if i.get("status") == "running" and not i.get("closed")]
    for inst in running:
        acc = next((a for a in accounts if a["name"] == inst.get("account")), None)
        if not acc:
            continue
        repo = acc.get("repo") or config.REPO
        url = f"{ghapi.API_BASE}/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
        status, _ = ghapi.gh_request("POST", url, acc.get("token"),
                                     {"ref": "main", "inputs": {"INSTANCE_ID": inst["id"]}})
        print(f"  {inst['id']} 触发重启: HTTP {status}", flush=True)
        time.sleep(3)

    # 3. 触发新 manager
    print("--- 更新 manager ---", flush=True)
    url = f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
    status, _ = ghapi.gh_request("POST", url, config.GH_TOKEN, {"ref": "main"})
    print(f"  manager 触发: HTTP {status}", flush=True)

    print("=== 帝国自动更新完成 ===", flush=True)


if __name__ == "__main__":
    main()