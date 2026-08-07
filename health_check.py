#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghbox 健康体检：token 有效性 / Actions 触发测试 / Codespaces 现状 / 备份资产

配置（环境变量，不硬编码）：
  HEALTH_ACCOUNTS='[{"name":"main","token":"ghp_xxx","repo":"owner/repo"},...]'
"""
import os
import json
import urllib.request
import urllib.error

ACCOUNTS = json.loads(os.environ.get("HEALTH_ACCOUNTS", "[]"))


def req(method, url, token=None, data=None, timeout=30):
    h = {"Authorization": f"token {token}" if token else "",
         "Accept": "application/vnd.github.v3+json",
         "User-Agent": "Mozilla/5.0 (ghbox-health)"}
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:500]
    except Exception as e:
        return 0, str(e)


def main():
    if not ACCOUNTS:
        print("⚠️ 未配置账号（环境变量 HEALTH_ACCOUNTS）")
        print("示例: HEALTH_ACCOUNTS='[{\"name\":\"main\",\"token\":\"ghp_xxx\",\"repo\":\"owner/repo\"}]'")
        return

    print("=" * 60)
    print("1) 各账号 token 有效性 + rate limit")
    print("=" * 60)
    for a in ACCOUNTS:
        st, d = req("GET", "https://api.github.com/rate_limit", token=a["token"])
        if st == 200:
            core = d["resources"]["core"]
            print(f"  [{a['name']}] OK   remaining={core['remaining']}/{core['limit']}")
        else:
            print(f"  [{a['name']}] FAIL status={st} {d if isinstance(d, str) else json.dumps(d)[:200]}")

    print()
    print("=" * 60)
    print("2) 各账号下现有 Actions runs（最近3条）")
    print("=" * 60)
    for a in ACCOUNTS:
        st, d = req("GET", f"https://api.github.com/repos/{a['repo']}/actions/runs?per_page=3",
                    token=a["token"])
        if st == 200:
            runs = d.get("workflow_runs", [])
            for r_ in runs:
                print(f"  [{a['name']}] #{r_['id']} {r_['name']} status={r_['status']} "
                      f"conclusion={r_['conclusion']} created={r_['created_at']}")
            if not runs:
                print(f"  [{a['name']}] 无运行记录")
        else:
            print(f"  [{a['name']}] FAIL status={st}")

    print()
    print("=" * 60)
    print("3) 触发 manager workflow 测试（确认 Actions 是否可用）")
    print("=" * 60)
    main_acc = next((a for a in ACCOUNTS if a.get("name") == "main"), ACCOUNTS[0])
    st, d = req("POST",
                f"https://api.github.com/repos/{main_acc['repo']}/actions/workflows/manager.yml/dispatches",
                token=main_acc["token"], data={"ref": "main"})
    if st == 204:
        print("  ✅ dispatch 已接受（204）。Actions 可用或已排队")
    else:
        print(f"  ❌ dispatch 失败 status={st} {d if isinstance(d, str) else json.dumps(d)[:300]}")

    print()
    print("=" * 60)
    print("4) 各账号现有 Codespaces")
    print("=" * 60)
    for a in ACCOUNTS:
        st, d = req("GET", "https://api.github.com/user/codespaces?per_page=100", token=a["token"])
        if st == 200:
            css = d.get("codespaces", [])
            if not css:
                print(f"  [{a['name']}] 无 Codespace")
            for cs in css:
                print(f"  [{a['name']}] {cs.get('name')} state={cs.get('state')} "
                      f"machine={cs.get('machine', {}).get('name')} "
                      f"repo={cs.get('repository', {}).get('full_name')}")
        else:
            print(f"  [{a['name']}] FAIL status={st}")

    print()
    print("=" * 60)
    print("5) 各账号 Releases 备份资产（确认数据完好）")
    print("=" * 60)
    for a in ACCOUNTS:
        st, d = req("GET", f"https://api.github.com/repos/{a['repo']}/releases/tags/backup",
                    token=a["token"])
        if st == 200:
            assets = [x["name"] for x in d.get("assets", [])]
            print(f"  [{a['name']}] release_id={d.get('id')} assets={len(assets)}个: {assets[:12]}")
        else:
            print(f"  [{a['name']}] 无 backup release (status={st})")


if __name__ == "__main__":
    main()