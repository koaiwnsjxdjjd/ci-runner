#!/usr/bin/env python3
"""批量创建20个实例在acc4账号上"""
import requests
import time
import json

MANAGER = "https://ghvps2.kekeke.cc.cd"
TOKEN = "QylTKgWtBXZJrX3wdX2Ycgt89Eb-XJk1"

created = 0
failed = 0

for i in range(20):
    print(f"=== 创建实例 {i+1}/20 ===", flush=True)
    r = requests.post(f"{MANAGER}/api/instances",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json={"account": "acc4"},
        timeout=60)
    try:
        d = r.json()
        if d.get("ok"):
            inst = d.get("instance", {})
            print(f"  ✅ {inst.get('id')} → {inst.get('url')} mcp={inst.get('mcp_url')}", flush=True)
            created += 1
        else:
            print(f"  ❌ {d.get('error','')}", flush=True)
            failed += 1
    except:
        print(f"  ❌ HTTP {r.status_code}", flush=True)
        failed += 1
    # 每次创建间隔3秒（让CF隧道创建完成）
    if i < 19:
        time.sleep(3)

print(f"\n=== 完成: {created} 成功, {failed} 失败 ===")
